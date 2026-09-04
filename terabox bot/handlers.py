import asyncio
import base64
import json
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

import cv2
import numpy as np

from config import (
    DEFAULT_CONCURRENCY,
    MAX_CONCURRENCY,
    PROXIES,
    REQUEST_TIMEOUT,
    account_store,
)
from tasks import PLATFORMS, Progress, Task, handler, PLATFORM_URLS

# Desktop user-agents (TeraBox uses desktop UA for captcha solving)
DESKTOP_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
]

# Mobile user-agents for the actual view visit
MOBILE_USER_AGENTS = [
    "Mozilla/5.0 (Linux; Android 14; Pixel 8 Pro) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 14; SM-S928B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Mobile/15E148 Safari/604.1",
]

DESKTOP_VIEWPORTS = [
    {"width": 1920, "height": 1080},
    {"width": 1366, "height": 768},
    {"width": 1536, "height": 864},
]

REFERERS = [
    "https://twitter.com/",
    "https://x.com/",
    "https://www.facebook.com/",
    "https://www.instagram.com/",
    "https://www.google.com/",
    "https://t.me/",
    "",
]


def get_proxy():
    return random.choice(PROXIES) if PROXIES else None


_SURL_RE = re.compile(r"/s/([^/?#]+)")


def _extract_surl(url: str) -> Optional[str]:
    m = _SURL_RE.search(url)
    return m.group(1) if m else None


# ── CAPTCHA solver (OpenCV-based) ─────────────────────────────────────────

def _find_notch(bg_b64: str, piece_b64: str):
    """Find the notch position in the background image using edge-based matching.

    Tries multiple Canny thresholds and inverse-grayscale matching, picks the
    highest-scoring result.  Returns (notch_x, notch_y, piece_w, piece_h, score)
    in natural image coords.
    """
    bg_data = base64.b64decode(bg_b64)
    piece_data = base64.b64decode(piece_b64)

    bg_img = cv2.imdecode(np.frombuffer(bg_data, np.uint8), cv2.IMREAD_COLOR)
    piece_rgba = cv2.imdecode(np.frombuffer(piece_data, np.uint8), cv2.IMREAD_UNCHANGED)

    piece_bgr = piece_rgba[:, :, :3]
    alpha = piece_rgba[:, :, 3]
    mask = (alpha > 127).astype(np.uint8) * 255

    bg_gray = cv2.cvtColor(bg_img, cv2.COLOR_BGR2GRAY)
    piece_gray = cv2.cvtColor(piece_bgr, cv2.COLOR_BGR2GRAY)
    piece_h, piece_w = piece_bgr.shape[:2]

    # 1. Edge-based template matching at multiple Canny thresholds
    piece_edges = cv2.Canny(piece_gray, 50, 150)
    piece_edges = cv2.bitwise_and(piece_edges, mask)

    best_edge_score = -1.0
    best_edge_loc = None
    for low, high in [(30, 100), (50, 150), (70, 200)]:
        bg_edges = cv2.Canny(bg_gray, low, high)
        result = cv2.matchTemplate(bg_edges, piece_edges, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(result)
        if max_val > best_edge_score:
            best_edge_score = max_val
            best_edge_loc = max_loc

    # 2. Inverse-grayscale masked matching (notch is darker than piece content)
    bg_blur = cv2.GaussianBlur(bg_gray, (5, 5), 0)
    piece_inv = 255 - piece_gray
    piece_inv_masked = cv2.bitwise_and(piece_inv, piece_inv, mask=mask)
    result_inv = cv2.matchTemplate(bg_blur, piece_inv_masked, cv2.TM_CCOEFF_NORMED, mask=mask)
    inv_score, inv_loc = cv2.minMaxLoc(result_inv)[1], cv2.minMaxLoc(result_inv)[3]

    # Pick the better of the two (inverse-grayscale wins when it's clearly better)
    if inv_score > best_edge_score + 0.05:
        return inv_loc[0], inv_loc[1], piece_w, piece_h, inv_score
    return best_edge_loc[0], best_edge_loc[1], piece_w, piece_h, best_edge_score


def _solve_captcha(page, max_attempts: int = 8) -> bool:
    """Solve the TeraBox drag-and-drop CAPTCHA. Returns True on success."""
    for _att in range(max_attempts):
        data = page.evaluate("""() => {
            const iframe = document.querySelector('.captcha-verify-iframe');
            if (!iframe || !iframe.contentDocument) return null;
            const doc = iframe.contentDocument;
            const imgs = doc.querySelectorAll('img');
            const result = {images: []};
            for (const img of imgs) {
                result.images.push({
                    src: img.src, cls: img.className,
                    rect: img.getBoundingClientRect(),
                    naturalW: img.naturalWidth, naturalH: img.naturalHeight
                });
            }
            result.iframeRect = iframe.getBoundingClientRect();
            return result;
        }""")
        if not data:
            return False

        bg_b64 = piece_b64 = piece_rect = bg_natural = None
        for img in data["images"]:
            if img["src"].startswith("data:image/jpeg") and img["rect"]["width"] > 100:
                bg_b64 = img["src"].split(",")[1]
                bg_natural = (img["naturalW"], img["naturalH"])
            elif "tile-piece" in img["cls"]:
                piece_b64 = img["src"].split(",")[1]
                piece_rect = img["rect"]

        if not all([bg_b64, piece_b64, piece_rect, bg_natural]):
            return False

        iframe_rect = data["iframeRect"]
        notch_x, notch_y, pw, ph, score = _find_notch(bg_b64, piece_b64)
        if score < 0.2:
            time.sleep(2)
            continue

        img_disp = page.evaluate("""() => {
            const iframe = document.querySelector('.captcha-verify-iframe');
            const doc = iframe.contentDocument;
            const bgImg = doc.querySelector('img:not(.tile-piece)');
            const rect = bgImg.getBoundingClientRect();
            return {x: rect.x, y: rect.y, w: rect.width, h: rect.height};
        }""")

        sx = img_disp["w"] / bg_natural[0]
        sy = img_disp["h"] / bg_natural[1]

        target_x = img_disp["x"] + (notch_x + pw / 2) * sx
        target_y = img_disp["y"] + (notch_y + ph / 2) * sy
        current_x = piece_rect["x"] + piece_rect["width"] / 2
        current_y = piece_rect["y"] + piece_rect["height"] / 2

        start_x = iframe_rect["x"] + current_x
        start_y = iframe_rect["y"] + current_y
        end_x = iframe_rect["x"] + target_x
        end_y = iframe_rect["y"] + target_y

        # Human-like drag
        page.mouse.move(start_x, start_y)
        time.sleep(random.uniform(0.2, 0.4))
        page.mouse.down()
        time.sleep(random.uniform(0.08, 0.15))

        dx = end_x - start_x
        dy = end_y - start_y
        steps = random.randint(30, 50)
        for i in range(1, steps + 1):
            t = i / steps
            eased = t * t * (3 - 2 * t)
            x = start_x + dx * eased + random.gauss(0, 0.5)
            y = start_y + dy * eased + random.gauss(0, 0.5)
            page.mouse.move(x, y)
            time.sleep(random.uniform(0.005, 0.02))

        page.mouse.move(end_x, end_y)
        time.sleep(random.uniform(0.05, 0.1))
        page.mouse.up()
        time.sleep(8)

        # Check login API instead of overlay visibility (overlay can
        # briefly disappear during refresh, giving false positives).
        try:
            login_check = page.evaluate("""async () => {
                const r = await fetch(
                    "/api/check/login?app_id=250528&web=1&channel=dubox&clienttype=0",
                    {credentials: "include"}
                );
                return await r.json();
            }""")
            if login_check.get("errno") == 0:
                return True
        except Exception:
            pass

        # If the overlay is gone but login hasn't succeeded yet, wait
        # a bit for a redirect or state change before next attempt.
        captcha_visible = page.evaluate("""() => {
            const iframe = document.querySelector('.captcha-verify-iframe');
            return iframe && iframe.offsetWidth > 100 && iframe.offsetHeight > 100;
        }""")
        if not captcha_visible:
            time.sleep(3)

    return False


# ── TeraBox login via share page ──────────────────────────────────────────

def _terabox_login(page, email: str, password: str, share_url: str) -> bool:
    """Login to TeraBox via the share page login modal.

    Flow: visit share page → click Login → click email icon → fill form →
    click submit → solve drag CAPTCHA → verify login.

    Returns True if login succeeded.
    """
    try:
        page.goto(share_url, wait_until="domcontentloaded", timeout=20000)
        time.sleep(5)

        # Click header Login button
        try:
            page.locator('.btn.primary:has-text("Login")').first.click()
        except Exception:
            pass
        time.sleep(2)

        # Wait for the login modal to fully render, then click the email icon
        # (second icon in the other-item row). The modal is JS-injected and
        # may not be in DOM immediately after clicking Login.
        for _wait in range(10):
            logos = page.query_selector_all(".other-item .logo")
            if len(logos) > 1:
                break
            time.sleep(1)
        if len(logos) < 2:
            print("  -> Could not find email login icon")
            return False
        logos[1].click()
        time.sleep(2)

        # Fill email character by character (triggers JS enable)
        email_input = page.locator('input[placeholder="Enter your email"]')
        email_input.click()
        for ch in email:
            email_input.type(ch, delay=random.randint(25, 55))
        time.sleep(0.3)

        # Fill password
        pw_input = page.locator('input[placeholder="Enter your new password."]')
        pw_input.click()
        for ch in password:
            pw_input.type(ch, delay=random.randint(25, 55))
        time.sleep(random.uniform(0.5, 1.0))

        # Click the login button inside the form
        page.locator(".btn-class-login").click()
        time.sleep(5)

        # Check if captcha appeared (overlay uses position:fixed so offsetParent
        # is null even when visible — check the iframe dimensions instead)
        captcha_present = page.evaluate("""() => {
            const iframe = document.querySelector('.captcha-verify-iframe');
            return iframe && iframe.offsetWidth > 100 && iframe.offsetHeight > 100;
        }""")

        if captcha_present:
            print("  -> Solving CAPTCHA...")
            solved = _solve_captcha(page)
            if not solved:
                print("  -> CAPTCHA failed after max attempts")
                return False
            print("  -> CAPTCHA solved!")

        # After CAPTCHA is solved, the login API fires automatically.
        # Poll for login completion (up to 20s).
        for _i in range(20):
            time.sleep(1)
            login_check = page.evaluate("""async () => {
                try {
                    const r = await fetch('/api/check/login?app_id=250528&web=1&channel=dubox&clienttype=0',
                        {credentials: 'include'});
                    return await r.json();
                } catch(e) { return {errno: -1}; }
            }""")
            if login_check.get("errno") == 0:
                print(f"  -> Login verified (uk={login_check.get('uk')})")
                return True

        print(f"  -> Login not confirmed after 20s: {login_check}")
        return False

    except Exception as e:
        print(f"  -> Login error: {e}")
        return False


# ── Cookie persistence ─────────────────────────────────────────────────────

_COOKIES_FILE = None


def _save_cookies(email: str, cookies: list[dict]) -> None:
    """Save browser cookies for an account so we can skip login next time."""
    global _COOKIES_FILE
    from config import BASE_DIR
    if _COOKIES_FILE is None:
        _COOKIES_FILE = BASE_DIR / "cookies.json"

    data = {}
    if _COOKIES_FILE.exists():
        try:
            data = json.loads(_COOKIES_FILE.read_text())
        except Exception:
            data = {}

    data[email] = cookies
    tmp = _COOKIES_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(_COOKIES_FILE)


def _load_cookies(email: str) -> Optional[list[dict]]:
    """Load saved cookies for an account, or None if not available."""
    global _COOKIES_FILE
    from config import BASE_DIR
    if _COOKIES_FILE is None:
        _COOKIES_FILE = BASE_DIR / "cookies.json"

    if not _COOKIES_FILE.exists():
        return None

    try:
        data = json.loads(_COOKIES_FILE.read_text())
        return data.get(email)
    except Exception:
        return None


def _is_logged_in(page) -> bool:
    """Check if the current page session is logged in."""
    try:
        result = page.evaluate("""async () => {
            try {
                const r = await fetch('/api/check/login?app_id=250528&web=1&channel=dubox&clienttype=0',
                    {credentials: 'include'});
                const d = await r.json();
                return d.errno === 0;
            } catch(e) { return false; }
        }""")
        return result
    except Exception:
        return False


# ── View sending ───────────────────────────────────────────────────────────

def send_view_browser(terabox_url: str, proxy: str = None) -> tuple[int, bool]:
    """Login to a TeraBox account and visit a share link.

    TeraBox only counts views from logged-in users.
    Returns (status_code, success_bool).
    """
    from playwright.sync_api import sync_playwright

    account = account_store.pick()
    if not account:
        print("  -> All accounts on cooldown, waiting...")
        return -1, False

    email = account["email"]
    password = account["password"]
    ua = random.choice(DESKTOP_USER_AGENTS)
    vp = random.choice(DESKTOP_VIEWPORTS)

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-gpu",
                ],
            )

            context = browser.new_context(
                user_agent=ua,
                viewport=vp,
                locale=random.choice(["en-US", "en-GB"]),
                timezone_id="Asia/Kolkata",
                ignore_https_errors=True,
            )

            context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', { get: () => false });
                Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
                window.chrome = { runtime: {} };
            """)

            page = context.new_page()

            # Try loading saved cookies first
            saved_cookies = _load_cookies(email)
            cookies_valid = False

            if saved_cookies:
                print(f"  -> Loading saved cookies for {email[:8]}...")
                try:
                    context.add_cookies(saved_cookies)
                    page.goto(terabox_url, wait_until="domcontentloaded", timeout=20000)
                    time.sleep(3)
                    cookies_valid = _is_logged_in(page)
                    if cookies_valid:
                        print(f"  -> Cookies still valid for {email[:8]}!")
                except Exception:
                    cookies_valid = False

            if not cookies_valid:
                # Need to do full login
                print(f"  -> Logging in as {email[:8]}...")
                # Use any share URL for the login page
                login_url = terabox_url if "1024tera" in terabox_url or "terabox" in terabox_url else "https://1024terabox.com/s/11i27XSNmIFW2rO9oL199Ug"
                login_ok = _terabox_login(page, email, password, login_url)
                if not login_ok:
                    print(f"  -> Login failed for {email[:8]}")
                    browser.close()
                    return -1, False

                # Save cookies for next time
                new_cookies = context.cookies()
                _save_cookies(email, new_cookies)
                print(f"  -> Cookies saved for {email[:8]}")

                # Navigate to the actual share link
                resp = page.goto(terabox_url, wait_until="domcontentloaded", timeout=20000)
                time.sleep(3)

            # We're logged in and on the share page — wait for view counting
            try:
                page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass

            time.sleep(random.uniform(2.0, 4.0))
            page.evaluate("window.scrollTo(0, document.body.scrollHeight / 3)")
            time.sleep(0.5)

            status = 200  # We got this far
            account_store.mark_used(email)
            browser.close()
            return status, True

    except Exception as e:
        print(f"  -> Browser ERROR: {e}")
        return -1, False


def send_view(headers: dict, terabox_url: str, proxy: str = None) -> tuple[int, bool]:
    if account_store.count() == 0:
        print("  -> No accounts loaded! Add accounts to accounts.json")
        return -1, False
    return send_view_browser(terabox_url)


# --- The job -------------------------------------------------------------

RETRY_PAUSE = 0.5
MAX_RETRY_PAUSE = 5.0
ATTEMPT_MULTIPLIER = 3
MAX_CONSECUTIVE_FAILURES = 40
DEAD_LINK_STATUSES = {404, 410}
PROGRESS_EVERY_SECONDS = 3.0


def pick_referer(task: Task) -> str:
    if task.platform == "random":
        return random.choice(list(PLATFORM_URLS.values()))
    if task.mode in ("random", "variety"):
        return random.choice(REFERERS)
    return PLATFORM_URLS.get(task.platform, random.choice(REFERERS))


async def demo_job(task: Task, progress: Progress) -> str:
    total = task.count
    delay = task.delay
    lanes = max(1, min(task.concurrency or DEFAULT_CONCURRENCY, MAX_CONCURRENCY))
    lanes = min(lanes, total)

    ok = 0
    failed = 0
    remaining = total
    attempts_left = total * ATTEMPT_MULTIPLIER + 20
    streak = 0
    stopped: Optional[str] = None
    started = time.monotonic()
    last_report = 0.0

    await progress(f"0/{total} on {task.platform} — {lanes} at a time")

    async def report(force: bool = False) -> None:
        nonlocal last_report
        now = time.monotonic()
        if not force and now - last_report < PROGRESS_EVERY_SECONDS:
            return
        last_report = now
        pct = int(ok / total * 100)
        rate = ok / max(now - started, 0.001)
        line = f"{ok}/{total} ({pct}%) on {task.platform} — {rate:.1f}/s, {lanes} at a time"
        if failed:
            line += f", {failed} retried"
        await progress(line)

    loop = asyncio.get_running_loop()
    pool = ThreadPoolExecutor(max_workers=lanes, thread_name_prefix=f"view{task.id}")

    async def lane() -> None:
        nonlocal ok, failed, remaining, attempts_left, streak, stopped
        while stopped is None and ok < total and attempts_left > 0:
            if task.stopping:
                stopped = "stopped on request"
                return
            if remaining <= 0:
                await asyncio.sleep(0.1)
                continue
            remaining -= 1
            attempts_left -= 1

            status, success = await loop.run_in_executor(
                pool, send_view, None, task.link, get_proxy()
            )

            if success:
                ok += 1
                streak = 0
                await report()
                if delay > 0:
                    await asyncio.sleep(delay)
                continue

            failed += 1
            remaining += 1
            streak += 1
            if status in DEAD_LINK_STATUSES:
                stopped = f"the link returned {status} — there is nothing to retry"
                return
            if streak >= MAX_CONSECUTIVE_FAILURES:
                stopped = (
                    f"{streak} failures in a row — the proxy or the link looks down "
                    f"(last status: {status})"
                )
                return
            await asyncio.sleep(min(RETRY_PAUSE * streak, MAX_RETRY_PAUSE))

    try:
        await asyncio.gather(*(lane() for _ in range(lanes)))
    finally:
        pool.shutdown(wait=False)

    await report(force=True)
    elapsed = time.monotonic() - started
    rate = ok / max(elapsed, 0.001)

    if task.stopping:
        return (
            f"stopped on request at {ok}/{total} views — {elapsed:.0f}s, "
            f"{failed} retried"
        )
    if stopped:
        raise RuntimeError(f"stopped at {ok}/{total} views — {stopped}")
    if ok < total:
        raise RuntimeError(
            f"only {ok}/{total} views landed after {total * ATTEMPT_MULTIPLIER + 20} "
            f"attempts ({failed} failed) — the proxy is dropping most requests"
        )
    return (
        f"{total} views delivered for {task.link} ({task.platform}) in "
        f"{elapsed:.0f}s — {rate:.1f}/s, {lanes} at a time, {failed} retried"
    )


for _platform in set(PLATFORMS.values()):
    handler(_platform)(demo_job)

handler("random")(demo_job)

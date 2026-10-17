import asyncio
import contextlib
import random
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin

from config import (
    DEFAULT_CONCURRENCY,
    FOLLOW_REDIRECTS,
    MAX_CONCURRENCY,
    PROXIES,
    REQUEST_TIMEOUT,
)
from tasks import PLATFORMS, Progress, Task, handler, PLATFORM_URLS, PLATFORM_URL_POOLS, SITE_URLS

import requests
from requests.adapters import HTTPAdapter

REFERERS = [
    "https://twitter.com/",
    "https://x.com/",
    "https://t.co/",
    "https://www.facebook.com/",
    "https://www.instagram.com/",
    "https://www.google.com/",
    "https://www.linkedin.com/",
    "https://news.ycombinator.com/",
    "https://t.me/",
    "https://discord.com/",
    "https://kick.com/",
    "https://www.twitch.tv/",
    "https://web.whatsapp.com/",
    "",
]

USER_AGENTS = [
    # Chrome on Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    
    # Chrome on macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    
    # Safari on macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_6) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Safari/605.1.15",
    
    # Firefox on Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:115.0) Gecko/20100101 Firefox/115.0",
    
    # Firefox on macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:128.0) Gecko/20100101 Firefox/128.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:127.0) Gecko/20100101 Firefox/127.0",
    
    # Edge on Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36 Edg/127.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0",
    
    # Mobile Chrome Android
    "Mozilla/5.0 (Linux; Android 14; Pixel 8 Pro) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 14; SM-S928B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 13; SM-G998B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36",
    
    # Mobile Safari iOS
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_3 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPad; CPU iPad OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
    
    # Samsung Internet
    "Mozilla/5.0 (Linux; Android 14; SM-S928B) AppleWebKit/537.36 (KHTML, like Gecko) SamsungBrowser/24.0 Chrome/127.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 13; SM-G998B) AppleWebKit/537.36 (KHTML, like Gecko) SamsungBrowser/23.0 Chrome/126.0.0.0 Mobile Safari/537.36",
    
    # Firefox Mobile
    "Mozilla/5.0 (Android 14; Mobile; rv:128.0) Gecko/128.0 Firefox/128.0",
    "Mozilla/5.0 (Android 13; Mobile; rv:127.0) Gecko/127.0 Firefox/127.0",
    
    # Opera
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36 OPR/113.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 OPR/112.0.0.0",
]

ACCEPT_LANGS = [
    "en-US,en;q=0.9",
    "en-GB,en;q=0.8",
    "en-US,en;q=0.9,es;q=0.8",
    "en-US,en;q=0.9,fr;q=0.8",
    "en-US,en;q=0.9,de;q=0.8",
    "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
    "en-US,en;q=0.9,ja;q=0.8",
    "en-US,en;q=0.9,ko;q=0.8",
    "en-US,en;q=0.9,pt;q=0.8,pt-BR;q=0.7",
    "en-US,en;q=0.9,ru;q=0.8",
    "en-US,en;q=0.9,ar;q=0.8",
    "en-US,en;q=0.9,hi;q=0.8",
    "en-GB,en;q=0.9,en-US;q=0.8",
    "de-DE,de;q=0.9,en;q=0.8",
    "fr-FR,fr;q=0.9,en;q=0.8",
    "es-ES,es;q=0.9,en;q=0.8",
    "it-IT,it;q=0.9,en;q=0.8",
    "pt-BR,pt;q=0.9,en;q=0.8",
    "ja-JP,ja;q=0.9,en;q=0.8",
    "ko-KR,ko;q=0.9,en;q=0.8",
    "zh-CN,zh;q=0.9,en;q=0.8",
    "zh-TW,zh;q=0.9,en;q=0.8",
    "ru-RU,ru;q=0.9,en;q=0.8",
    "ar-SA,ar;q=0.9,en;q=0.8",
    "hi-IN,hi;q=0.9,en;q=0.8",
    "nl-NL,nl;q=0.9,en;q=0.8",
    "pl-PL,pl;q=0.9,en;q=0.8",
    "tr-TR,tr;q=0.9,en;q=0.8",
    "sv-SE,sv;q=0.9,en;q=0.8",
    "da-DK,da;q=0.9,en;q=0.8",
    "fi-FI,fi;q=0.9,en;q=0.8",
    "no-NO,no;q=0.9,en;q=0.8",
    "cs-CZ,cs;q=0.9,en;q=0.8",
    "hu-HU,hu;q=0.9,en;q=0.8",
    "ro-RO,ro;q=0.9,en;q=0.8",
    "sk-SK,sk;q=0.9,en;q=0.8",
    "bg-BG,bg;q=0.9,en;q=0.8",
    "hr-HR,hr;q=0.9,en;q=0.8",
    "sr-RS,sr;q=0.9,en;q=0.8",
    "sl-SI,sl;q=0.9,en;q=0.8",
    "et-EE,et;q=0.9,en;q=0.8",
    "lv-LV,lv;q=0.9,en;q=0.8",
    "lt-LT,lt;q=0.9,en;q=0.8",
    "el-GR,el;q=0.9,en;q=0.8",
    "he-IL,he;q=0.9,en;q=0.8",
    "th-TH,th;q=0.9,en;q=0.8",
    "vi-VN,vi;q=0.9,en;q=0.8",
    "id-ID,id;q=0.9,en;q=0.8",
    "ms-MY,ms;q=0.9,en;q=0.8",
]

ACCEPTS = [
    "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
]

ACCEPT_ENCODINGS = [
    "gzip, deflate, br, zstd",
    "gzip, deflate, br",
    "gzip, deflate",
    "br, gzip, deflate",
]

CACHE_CONTROLS = [
    "max-age=0",
    "no-cache",
    "no-store",
    "max-age=0, must-revalidate",
]

SEC_CH_UA = [
    '"Google Chrome";v="127", "Chromium";v="127", "Not=A?Brand";v="24"',
    '"Google Chrome";v="126", "Chromium";v="126", "Not/A?Brand";v="24"',
    '"Google Chrome";v="125", "Chromium";v="125", "Not.A/Brand";v="24"',
    '"Microsoft Edge";v="127", "Chromium";v="127", "Not=A?Brand";v="24"',
    '"Not=A?Brand";v="24", "Chromium";v="127"',
]


def realistic_headers(referer) -> dict:
    ua = random.choice(USER_AGENTS)
    is_chrome = "Chrome" in ua and "Edg" not in ua and "OPR" not in ua and "SamsungBrowser" not in ua
    is_edge = "Edg" in ua
    is_firefox = "Firefox" in ua
    is_safari = "Safari" in ua and "Chrome" not in ua
    is_mobile = "Mobile" in ua or "Android" in ua or "iPhone" in ua or "iPad" in ua
    
    headers = {
        "Referer": referer,
        "User-Agent": ua,
        "Accept-Language": random.choice(ACCEPT_LANGS),
        "Accept": random.choice(ACCEPTS),
        "Accept-Encoding": random.choice(ACCEPT_ENCODINGS),
        "Cache-Control": random.choice(CACHE_CONTROLS),
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "cross-site" if referer else "none",
        "Sec-Fetch-User": "?1",
    }
    # No X-Forwarded-For. A real browser never sends one, so a random value is
    # a plain automation tell -- and bit.ly may attribute the click to that
    # made-up address and bin it. The exit IP is the proxy's job, not a header's.
    
    # Chrome/Edge specific headers
    if is_chrome or is_edge:
        headers["sec-ch-ua"] = random.choice(SEC_CH_UA)
        headers["sec-ch-ua-mobile"] = "?1" if is_mobile else "?0"
        headers["sec-ch-ua-platform"] = '"Android"' if "Android" in ua else ('"iOS"' if "iPhone" in ua or "iPad" in ua else ('"macOS"' if "Mac" in ua else '"Windows"'))
    
    # Firefox doesn't send sec-ch-ua
    if is_firefox:
        headers.pop("sec-ch-ua", None)
        headers.pop("sec-ch-ua-mobile", None)
        headers.pop("sec-ch-ua-platform", None)
    
    # Safari doesn't send sec-ch-ua either
    if is_safari:
        headers.pop("sec-ch-ua", None)
        headers.pop("sec-ch-ua-mobile", None)
        headers.pop("sec-ch-ua-platform", None)
    
    return headers


# --- One request ---------------------------------------------------------
# Every click gets its OWN connection, which is then closed. That is
# deliberate and it is what makes the click count.
#
# The proxy is a ROTATING residential gateway: it hands out a new exit IP per
# new connection, never per request. Lanes used to keep a thread-local session
# alive to save the proxy tunnel and TLS handshake, which pinned each lane to a
# single IP for its whole run -- measured live 2026-10-12: 8 clicks on a
# reused session came from 1 IP, 8 clicks on fresh sessions came from 8. So a
# 2,500-click job reached bit.ly from ~20 addresses, all with the same
# fingerprint, and bit.ly discarded them as repeat traffic: the bot reported
# thousands of clicks and the dashboard moved by a handful.
#
# A new tunnel costs ~1-3s, so paying it SERIALLY per click put it on every
# lane's critical path. Each lane now keeps one click AND one tunnel-open in
# flight at once: while click N runs on its already-open tunnel, the tunnel
# for click N+1 is being opened next to it, so the lane cycles at request
# speed instead of request + tunnel. The exit IP is bound at CONNECT time, so
# a pre-opened tunnel is still this click's own private IP -- the property the
# 2026-10-12 measurement demanded is untouched. Concurrent CONNECTs stay at or
# below `lanes` (one per lane), the same ceiling the gateway was measured
# against; the warm never sends any HTTP, it just opens the connection.
#
# The gateway refuses new tunnels (503 after CONNECT) past about 5 at once,
# so CONCURRENCY must stay small -- see the measurements in config.py. A
# refused warm is not a lost click: the click opens its own tunnel cold, and
# that attempt is what the retry logic sees.


def _session() -> requests.Session:
    session = requests.Session()
    adapter = HTTPAdapter(pool_connections=1, pool_maxsize=1, max_retries=0)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def _proxies(proxy: str | None) -> dict | None:
    return {"http": proxy, "https": proxy} if proxy else None


def _pool_for(session: requests.Session, url: str, proxies: dict | None):
    """The urllib3 connection pool `session.get` will use for this URL."""
    adapter = session.get_adapter(url)
    try:
        prepared = requests.Request("GET", url).prepare()
        return adapter.get_connection_with_tls_context(prepared, False, proxies)
    except AttributeError:  # requests < 2.32
        return adapter.get_connection(url, proxies)


def _warm(session: requests.Session, url: str, proxies: dict | None) -> bool:
    """Open the tunnel (proxy CONNECT) and TLS to `url` now, and park the
    live connection in the session's pool, so the next `session.get` reuses
    it instead of paying the handshake. Sends no HTTP. Returns False (and
    leaves the session usable cold) if the gateway refuses the CONNECT."""
    try:
        pool = _pool_for(session, url, proxies)
        conn = pool._get_conn()  # noqa: SLF001 -- no public way to pre-open
    except Exception:
        return False
    try:
        conn.timeout = REQUEST_TIMEOUT  # bounds a hung CONNECT/TLS
        conn.connect()
        pool._put_conn(conn)  # parked; the next get() pops this exact conn
        return True
    except Exception:
        with contextlib.suppress(Exception):
            conn.close()
        return False


WARM_ATTEMPTS = 3  # the gateway 503s a CONNECT at the ceiling; it answers fast
WARM_RETRY_PAUSE = 0.25


def warm_click_session(BITLY_URL: str, proxy: str = None) -> requests.Session | None:
    """Pre-open the NEXT click's tunnel + TLS. Returns a session whose next
    GET rides that connection, or None if the gateway refused -- the click then
    opens its own tunnel cold, exactly the old behaviour."""
    proxies = _proxies(proxy)
    session = _session()
    for attempt in range(1, WARM_ATTEMPTS + 1):
        if _warm(session, BITLY_URL, proxies):
            return session
        if attempt < WARM_ATTEMPTS:
            time.sleep(WARM_RETRY_PAUSE * attempt)
    session.close()
    return None


# With FOLLOW_REDIRECTS on, the destination is fetched as its OWN request and
# retried on its own. Retrying the whole click instead would ask bit.ly for a
# second redirect, and bit.ly counts every one it serves -- so a flaky
# destination would quietly inflate the click total above what was asked for.
DESTINATION_ATTEMPTS = 3
DESTINATION_RETRY_PAUSE = 0.5


def send_clicks(
    headers: dict, BITLY_URL: str, proxy: str = None, session: requests.Session | None = None
) -> tuple[int, str, bool]:
    """Fire one click. Returns (status code, where it pointed / the error,
    whether the destination visit failed).

    A status of -1 means the request never reached the server at all. The
    status is the bit.ly hop only -- that is what decides whether the click
    counted. `session`, if given, is a tunnel pre-opened by
    warm_click_session(); it is used and closed either way.
    """
    s = session if session is not None else _session()
    proxies = _proxies(proxy)
    try:
        return _one_click(s, headers, BITLY_URL, proxies)
    finally:
        # Drops the tunnel, so the next click is handed a different exit IP.
        s.close()


def _one_click(session, headers, BITLY_URL, proxies) -> tuple[int, str, bool]:
    try:
        r = session.get(
            BITLY_URL,
            headers=headers,
            allow_redirects=False,
            timeout=REQUEST_TIMEOUT,
            proxies=proxies,
        )
    except requests.RequestException as e:
        print(f"  -> ERROR: {e}")
        return -1, str(e), False

    location = r.headers.get("Location")
    target = location or r.url
    print(f"  -> {r.status_code} -> {target}")

    if not FOLLOW_REDIRECTS or not location or not 300 <= r.status_code < 400:
        return r.status_code, target, False

    # The click is already counted. Now deliver the visit to the destination
    # so it sees the traffic (and its btag). Same headers the redirect would
    # have been followed with.
    destination = urljoin(BITLY_URL, location)
    for attempt in range(1, DESTINATION_ATTEMPTS + 1):
        try:
            d = session.get(
                destination,
                headers=headers,
                allow_redirects=True,
                timeout=REQUEST_TIMEOUT,
                proxies=proxies,
                stream=True,
            )
            print(f"     -> site {d.status_code} -> {d.url}")
            # The visit has landed by the time the response resolves: any
            # redirect chain (the btag's /setcookie hop included) was followed
            # while getting here. The body is dead weight -- ~440 KB down a
            # residential tunnel was most of a click's wall time -- so it is
            # dropped instead of downloaded.
            d.close()
            return r.status_code, target, False
        except requests.RequestException as e:
            print(f"     -> site ERROR (try {attempt}/{DESTINATION_ATTEMPTS}): {e}")
            if attempt < DESTINATION_ATTEMPTS:
                time.sleep(DESTINATION_RETRY_PAUSE * attempt)
    return r.status_code, target, True


def get_proxy():
    return random.choice(PROXIES) if PROXIES else None


# --- The job -------------------------------------------------------------

# A click only counts when the server actually answered. Anything else is
# handed back to the queue and tried again, so `count` means `count` real
# clicks, not `count` attempts.
RETRY_PAUSE = 0.5  # grows with each failure in a row
MAX_RETRY_PAUSE = 5.0
ATTEMPT_MULTIPLIER = 3  # total attempts allowed = count x this, then give up
MAX_CONSECUTIVE_FAILURES = 40  # everything failing = proxy or link is down
DEAD_LINK_STATUSES = {404, 410}  # never going to work, stop immediately
PROGRESS_EVERY_SECONDS = 3.0  # Telegram rate-limits message edits


def pick_referer(task: Task) -> str:
    if task.platform == "random":
        return random.choice(list(PLATFORM_URLS.values()))
    if task.platform in SITE_URLS:
        return SITE_URLS[task.platform]
    if task.platform in PLATFORM_URL_POOLS:
        return random.choice(PLATFORM_URL_POOLS[task.platform])
    if task.mode in ("random", "variety"):
        return random.choice(REFERERS)
    return PLATFORM_URLS[task.platform]


async def demo_job(task: Task, progress: Progress) -> str:
    total = task.count
    delay = task.delay
    lanes = max(1, min(task.concurrency or DEFAULT_CONCURRENCY, MAX_CONCURRENCY))
    lanes = min(lanes, total)

    ok = 0  # clicks the server actually answered
    failed = 0  # attempts that errored and were retried
    missed_site = 0  # clicks that counted, but never reached the destination
    cold = 0  # attempts that had to open their own tunnel (no warm one waiting)
    remaining = total  # clicks not yet claimed by a lane
    attempts_left = total * ATTEMPT_MULTIPLIER + 20
    streak = 0  # failures in a row, across all lanes
    stopped: str | None = None
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
        if missed_site:
            line += f", {missed_site} missed the site"
        await progress(line)

    loop = asyncio.get_running_loop()
    # Each lane keeps one click AND one tunnel-open in flight at once, so the
    # pool needs two workers per lane or the warm would queue behind clicks
    # and the overlap would collapse back to serial.
    pool = ThreadPoolExecutor(max_workers=2 * lanes, thread_name_prefix=f"click{task.id}")

    async def lane() -> requests.Session | None:
        """One lane. Returns its leftover pre-warmed session for cleanup."""
        nonlocal ok, failed, missed_site, cold, remaining, attempts_left, streak, stopped
        warm = None  # this lane's next click, tunnel already open
        while stopped is None and ok < total and attempts_left > 0:
            if task.stopping:  # /stopschedule
                stopped = "stopped on request"
                return warm
            if remaining <= 0:
                # Other lanes are still in flight; one may hand work back.
                await asyncio.sleep(0.1)
                continue
            remaining -= 1
            attempts_left -= 1
            if warm is None:
                cold += 1  # first click in the lane, or the gateway refused the warm

            headers = realistic_headers(pick_referer(task))
            # Click N rides the tunnel opened while click N-1 was running;
            # the tunnel for click N+1 opens while this click is in flight.
            click_fut = loop.run_in_executor(
                pool, send_clicks, headers, task.link, get_proxy(), warm
            )
            warm_fut = loop.run_in_executor(pool, warm_click_session, task.link, get_proxy())
            warm = None
            status, detail, site_missed = await click_fut
            warm = await warm_fut

            if 200 <= status < 400:
                ok += 1
                streak = 0
                if site_missed:
                    missed_site += 1
                await report()
                if delay > 0:
                    await asyncio.sleep(delay)
                continue

            # Not a click. Put it back so the total is still reached.
            failed += 1
            remaining += 1
            streak += 1
            if status in DEAD_LINK_STATUSES:
                stopped = f"the link returned {status} — there is nothing to retry"
                return warm
            if streak >= MAX_CONSECUTIVE_FAILURES:
                stopped = (
                    f"{streak} failures in a row — the proxy or the link looks down "
                    f"(last: {detail[:120]})"
                )
                return warm
            await asyncio.sleep(min(RETRY_PAUSE * streak, MAX_RETRY_PAUSE))
        return warm

    leftovers: list[requests.Session | None] = []
    try:
        leftovers = await asyncio.gather(*(lane() for _ in range(lanes)))
    finally:
        pool.shutdown(wait=False)
        for session in leftovers:
            if session is not None:
                session.close()

    await report(force=True)
    elapsed = time.monotonic() - started
    rate = ok / max(elapsed, 0.001)

    if task.stopping:
        # Asked for, not a failure -- report it as a normal outcome.
        note = f" — {missed_site} never reached the site" if missed_site else ""
        return (
            f"stopped on request at {ok}/{total} clicks — {elapsed:.0f}s, "
            f"{failed} retried{note}"
        )
    if stopped:
        raise RuntimeError(f"stopped at {ok}/{total} clicks — {stopped}")
    if ok < total:
        raise RuntimeError(
            f"only {ok}/{total} clicks landed after {total * ATTEMPT_MULTIPLIER + 20} "
            f"attempts ({failed} failed) — the proxy is dropping most requests"
        )
    summary = (
        f"{total} clicks delivered for {task.link} ({task.platform}) in "
        f"{elapsed:.0f}s — {rate:.1f}/s, {lanes} at a time, {failed} retried"
    )
    if cold:
        summary += f", {cold} cold (tunnel not pre-opened in time)"
    if missed_site:
        summary += f" — but {missed_site} never reached the destination site"
    return summary


for _platform in set(PLATFORMS.values()):
    handler(_platform)(demo_job)

# "random" uses the same handler - it picks random platform per request
handler("random")(demo_job)

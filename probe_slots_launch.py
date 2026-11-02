#!/usr/bin/env python3
"""How does a Laravel white-label (cricmatch/khelofun/starexch/playcric365)
launch a SLOTS game, as opposed to a live-casino table?

Read-only. Logs in, opens the slots lobby, and reports:
  * the site's own goToCasinoLive() source -- which is the whole question.
    Logged OUT every site in this family stubs it to `$(".cls_loginbtn")
    .click()`, so it CANNOT be read anonymously and the real route has to be
    taken from a real session.
  * the tile for the wanted game: its attributes and which element actually
    carries the handler.
  * with --open, clicks that tile and says where it landed (new tab or same
    tab, which host, whether a Spribe client rendered).

Places no bet and types no credential: --from-pairs reads an account out of
the bot's own pairs file, the same way probe_mines.py does.

    .venv/bin/python probe_slots_launch.py \
        --from-pairs pairs.playcric365.gameplay.json \
        --url https://playcric365.com/ --name Mines --lobby "/slots/?p=spribe"
"""
import argparse
import json
import time
from urllib.parse import urlsplit

import main as m
from playwright.sync_api import sync_playwright

parser = argparse.ArgumentParser()
parser.add_argument("username", nargs="?", default=None)
parser.add_argument("password", nargs="?", default=None)
parser.add_argument("--from-pairs", default=None, metavar="FILE",
                    help="read credentials from a bot pairs file instead of "
                         "putting a live password on the command line")
parser.add_argument("--pair", default=None)
parser.add_argument("--side", default="banker", choices=["banker", "player"])
parser.add_argument("--url", default=None)
parser.add_argument("--proxy", default=None)
parser.add_argument("--name", default="Mines", help="the tile to look for")
parser.add_argument("--lobby", default=None,
                    help="lobby path to open (default: the profile's "
                         "casino_lobby_path)")
parser.add_argument("--open", action="store_true",
                    help="actually click the tile and report where it lands")
parser.add_argument("--secs", type=int, default=40,
                    help="how long to watch the launched tab")
parser.add_argument("--ua", default=None,
                    help="force a user agent for the whole context. Spribe's "
                         "CDN flat-403s headless Chromium's default UA, and a "
                         "403 carries no CORS header, so the game client dies "
                         "with 'Retry limit of client config exceeded!' -- "
                         "which reads as a broken launch, not as a UA problem.")
parser.add_argument("--headed", action="store_true")
args = parser.parse_args()

if args.from_pairs:
    with open(args.from_pairs) as fh:
        pairs = json.load(fh).get("pairs", {})
    if not pairs:
        raise SystemExit(f"{args.from_pairs} holds no pairs")
    pid = args.pair or sorted(pairs, key=lambda k: int(k) if k.isdigit() else k)[0]
    if pid not in pairs:
        raise SystemExit(f"no pair {pid!r} in {args.from_pairs}")
    side = pairs[pid][args.side]
    args.username, args.password = side["username"], side["password"]
    print(f"--- credentials: pair {pid} {args.side} ({args.username}) "
          f"from {args.from_pairs} ---")
elif not (args.username and args.password):
    raise SystemExit("give a username and password, or --from-pairs FILE")

SITE_URL = args.url or m.SITE_URL
prof = m.profile_for(SITE_URL)
PARTS = urlsplit(SITE_URL)


# The handler is the answer. Printed verbatim rather than summarised: a
# launch route guessed from a screenshot is how the funcrash probe lost a run.
_HANDLER_JS = """() => {
  const out = {};
  for (const name of ["goToCasinoLive", "casinoRedirect", "openGame",
                      "gotoCasino", "playGame"]) {
    try {
      const fn = window[name];
      if (typeof fn === "function") out[name] = fn.toString().slice(0, 1500);
    } catch (e) { out[name] = "unreadable: " + e; }
  }
  return out;
}"""

# Every element that could BE the tile, with the attributes a launcher reads.
_TILES_JS = """(want) => {
  const els = [...document.querySelectorAll('[onclick*="goToCasinoLive"]')];
  const w = (want || "").trim().toLowerCase();
  const rows = els.map(e => {
    const r = e.getBoundingClientRect();
    const img = e.querySelector("img");
    return {
      id: e.getAttribute("data-id"),
      provider: e.getAttribute("data-provider"),
      link: e.getAttribute("data-link"),
      type: e.getAttribute("data-type"),
      gametype: e.getAttribute("data-gametype"),
      tag: e.tagName.toLowerCase(),
      cls: (e.className || "").toString().slice(0, 90),
      alt: img ? (img.getAttribute("alt") || "") : "",
      text: (e.innerText || "").trim().replace(/\\s+/g, " ").slice(0, 40),
      w: Math.round(r.width), h: Math.round(r.height),
    };
  });
  const hit = rows.filter(r =>
      (r.alt || "").trim().toLowerCase() === w ||
      (r.text || "").trim().toLowerCase() === w ||
      (r.link || "").trim().toLowerCase() === w);
  return {total: rows.length, sample: rows.slice(0, 8), hit};
}"""


def show(label, obj):
    print(f"\n--- {label} ---")
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


def main_probe():
    proxy_conf = m.parse_proxy(args.proxy) if args.proxy else None
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not args.headed,
                                     args=m._ANTI_THROTTLE_ARGS)
        if args.ua:
            kw = {"user_agent": args.ua}
            if proxy_conf:
                kw["proxy"] = proxy_conf
            context = browser.new_context(**kw)
            print(f"    (forcing user agent: {args.ua[:60]}...)")
        else:
            context = m.new_site_context(browser, SITE_URL, proxy_conf)
        page = context.new_page()
        page.goto(SITE_URL, wait_until="domcontentloaded", timeout=60000)

        print("\n--- logging in ---")
        t0 = time.time()
        outcome, msgs = m.login(page, args.username, args.password, SITE_URL,
                                already_loaded=True)
        print(f"    {outcome} in {time.time() - t0:.0f}s"
              + (f" -- {'; '.join(msgs)[:200]}" if msgs else ""))
        if outcome != "ok":
            raise SystemExit("login did not complete -- nothing below it is readable")

        lobby = args.lobby or prof.casino_lobby_path
        url = f"{PARTS.scheme}://{PARTS.netloc}{lobby}"
        print(f"\n--- opening the lobby {url} ---")
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(6000)
        m.dismiss_popups(page)
        page.wait_for_timeout(2000)

        show("the site's own launch handlers, LOGGED IN",
             page.evaluate(_HANDLER_JS))
        tiles = page.evaluate(_TILES_JS, args.name)
        print(f"\n--- tiles: {tiles['total']} carry goToCasinoLive ---")
        show(f"a sample, and every match for {args.name!r}", tiles)

        if not args.open:
            print("\n(no --open, so nothing was clicked)")
            context.close()
            browser.close()
            return

        if not tiles["hit"]:
            print(f"\nno tile matched {args.name!r} -- not clicking blindly")
            context.close()
            browser.close()
            return

        # Attach BEFORE the click: the client fetches its config within the
        # first second or two, so a listener added after the tab is found has
        # already missed the request that matters.
        net = {"failed": [], "bad": [], "console": []}

        def _watch(p):
            p.on("requestfailed", lambda r: net["failed"].append(
                (r.url[:120], (r.failure or ""))))
            p.on("response", lambda r: (
                net["bad"].append((r.status, r.url[:120]))
                if r.status >= 400 else None))
            p.on("console", lambda msg: net["console"].append(
                f"{msg.type}: {msg.text[:400]}"))

        context.on("page", _watch)

        before = set(context.pages)
        print(f"\n--- clicking the {args.name!r} tile ---")
        clicked = page.evaluate("""(want) => {
          const w = (want || "").trim().toLowerCase();
          const els = [...document.querySelectorAll('[onclick*="goToCasinoLive"]')];
          const t = els.find(e => {
            const img = e.querySelector("img");
            const alt = img ? (img.getAttribute("alt") || "") : "";
            return alt.trim().toLowerCase() === w
                || (e.innerText || "").trim().toLowerCase() === w
                || (e.getAttribute("data-link") || "").toLowerCase() === w;
          });
          if (!t) return "";
          t.click();
          return t.getAttribute("data-id") || "?";
        }""", args.name)
        print(f"    clicked a tile with data-id={clicked!r}")

        game_page = None
        for _ in range(args.secs):
            page.wait_for_timeout(1000)
            fresh = [p for p in context.pages if p not in before]
            if fresh:
                game_page = fresh[0]
                break
            if urlsplit(page.url).netloc != PARTS.netloc:
                game_page = page
                print("    (it navigated the SAME tab, no new tab opened)")
                break
        if game_page is None:
            print(f"    no new tab and no navigation in {args.secs}s "
                  f"-- still at {page.url}")
            context.close()
            browser.close()
            return

        game_page.wait_for_timeout(8000)
        print(f"\n    landed on: {game_page.url[:160]}")
        print(f"    host     : {urlsplit(game_page.url).netloc}")

        # The client is Angular and boots slowly; a single early read reports
        # "not present" for selectors that are simply not rendered yet, which
        # reads as "wrong selectors". Poll instead.
        probes = ("button.btn-control.btn-bet", "app-bet-spinner .input",
                  "div.number-of-mines button.dropdown-toggle",
                  ".auto-cashout-countdown", ".lock-ui")
        found = {}
        deadline = time.time() + args.secs
        while time.time() < deadline and len(found) < len(probes):
            for f in game_page.frames:
                for sel in probes:
                    if sel in found:
                        continue
                    try:
                        if f.locator(sel).count():
                            found[sel] = (f.url or "")[:70] or "(main document)"
                    except Exception:
                        pass
            game_page.wait_for_timeout(1000)
        frames = [(f.name or "", (f.url or "")[:110]) for f in game_page.frames]
        show("frames", frames)
        for sel in probes:
            print(f"    {sel:<46} -> {found.get(sel, 'NOT PRESENT')}")

        # Where does the wallet live? On funcrash it is the OUTER operator
        # document (aviaport); here the game may be the tab's own document,
        # in which case that reader has nothing to read.
        try:
            bal = game_page.evaluate("""() => {
              const out = {};
              for (const s of [".balance .value", ".balance", "app-balance",
                               ".user-balance", "[class*=balance]"]) {
                const e = document.querySelector(s);
                if (e) out[s] = (e.innerText || "").trim()
                                  .replace(/\\s+/g, " ").slice(0, 60);
              }
              return out;
            }""")
            show("anything that looks like a balance, in the game document", bal)
        except Exception as e:
            print(f"    balance probe failed: {str(e)[:120]}")

        # When nothing matched, the useful question is what the document IS --
        # an error page, a too-small-screen apology, or a client that simply
        # has not booted. Guessing between those is what wastes a run.
        try:
            what = game_page.evaluate("""() => ({
              title: document.title,
              viewport: window.innerWidth + "x" + window.innerHeight,
              html_len: document.documentElement.outerHTML.length,
              body_text: (document.body ? document.body.innerText : "")
                           .trim().replace(/\\s+/g, " ").slice(0, 400),
              top_tags: [...document.body.children]
                          .map(e => e.tagName.toLowerCase()
                                    + (e.className ? "." + String(e.className)
                                         .split(" ").slice(0, 2).join(".") : ""))
                          .slice(0, 12),
              app_tags: [...new Set([...document.querySelectorAll("*")]
                          .map(e => e.tagName.toLowerCase())
                          .filter(t => t.startsWith("app-") || t.startsWith("kit-")))]
                          .slice(0, 25),
            })""")
            show("what this document actually is", what)
        except Exception as e:
            print(f"    document probe failed: {str(e)[:120]}")

        show("requests that FAILED outright", net["failed"][:15])
        show("responses >= 400", net["bad"][:15])
        show("console", net["console"][:20])

        # A failed request tells you THAT it failed; this tells you why, in
        # the browser's own words, from inside the page's own origin.
        for u in {u for u, _ in net["failed"]}:
            try:
                r = game_page.evaluate("""async (u) => {
                  try {
                    const res = await fetch(u, {method: 'GET'});
                    const t = await res.text();
                    return {ok: res.ok, status: res.status, len: t.length};
                  } catch (e) { return {error: String(e)}; }
                }""", u)
            except Exception as e:
                r = {"error": f"evaluate failed: {str(e)[:100]}"}
            print(f"    refetch {u[:80]} -> {r}")

        context.close()
        browser.close()


main_probe()

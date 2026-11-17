#!/usr/bin/env python3
"""READ-ONLY discovery of a site's deposit / withdrawal / bank-account routes.

Why this exists
---------------
Nothing in this repo touches money movement yet: the engine drives signup,
login, balance, the casino and the exchange, and that is all. Before a bot can
run the "deposit Rs100, request a withdrawal, cancel it after the cooldown to
whitelist the bank account" chain, the real endpoints have to be READ off the
site rather than guessed -- the same precedent inspect_form.py and
probe_slots_launch.py set, and the same reason CLAUDE.md keeps saying to read
a site's own inline JS instead of inventing a payload.

What it does, and what it deliberately does NOT do
--------------------------------------------------
It logs in once (the only POST it ever sends), then GETs the logged-in pages
and reads their markup and inline JavaScript, printing:

  * every candidate deposit / withdrawal / bank page it can find a link to
  * every endpoint those pages' own JS calls, with the keyword that matched
  * the form fields each payment form carries
  * the /append/<name> partials this white-label ships its forms inside
    (cricmatch keeps the signup form in /append/loginpp -- so a deposit form
    being absent from the static page source proves nothing until the
    partials have been fetched too)

It places no deposit, requests no withdrawal, cancels nothing and adds no bank
account. Every request after the login is a GET.

Usage
-----
    .venv/bin/python probe_deposit.py --user <id> --pass <pw> \
        [--url https://cricmatch247.com] [--proxy host:port:user:pass] \
        [--page /some/path]            # dig into one extra page

Credentials are arguments, never literals -- this repo is public (CLAUDE.md,
"Repo is public").
"""
import os
import re
import sys
from urllib.parse import urljoin, urlsplit

import requests
from dotenv import load_dotenv

load_dotenv(".env")

sys.path.insert(0, ".")
import main as engine  # noqa: E402  (needs the sys.path line above)


def _arg(flag, default=None):
    """--flag value, so one probe serves every site on this template."""
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return default


SITE_URL = _arg("--url", "https://cricmatch247.com").rstrip("/")
USERNAME = _arg("--user", os.environ.get("DEPOSIT_PROBE_USER", ""))
PASSWORD = _arg("--pass", os.environ.get("DEPOSIT_PROBE_PASS", ""))
PROXY = _arg("--proxy", os.environ.get("DEPOSIT_PROBE_PROXY", "")) or None
EXTRA_PAGE = _arg("--page")
OUT_DIR = _arg("--out", "/tmp/deposit_probe")

if not USERNAME or not PASSWORD:
    sys.exit("Pass --user <id> --pass <pw> (or set DEPOSIT_PROBE_USER/PASS).")

# What we are hunting for. Deliberately broad: a white-label may call the same
# screen "deposit", "add money", "recharge" or "payment", and the withdrawal
# side "withdraw", "payout" or "cashout". A false positive costs one GET.
KEYWORDS = [
    "deposit", "withdraw", "payout", "cashout", "cash-out", "recharge",
    "addmoney", "add-money", "addcash", "payment", "paymentmethod", "upi",
    "qrcode", "qr-code", "bank", "account-statement", "accountstatement",
    "beneficiary", "ifsc", "transaction", "cancelwithdraw", "wallet",
]
KEYWORD_RE = re.compile("|".join(re.escape(k) for k in KEYWORDS), re.I)

# Endpoint shapes this template's inline JS uses. Read off cricmatch's own
# pages: jQuery ajax with a `url:` key, $.post/$.get shorthands, bare fetch(),
# form actions, and the /api2/v2/... paths the Laravel side exposes.
ENDPOINT_PATTERNS = [
    re.compile(r"""url\s*:\s*["']([^"']{2,200})["']"""),
    re.compile(r"""\$\.(?:post|get|ajax|getJSON)\s*\(\s*["']([^"']{2,200})["']"""),
    re.compile(r"""fetch\s*\(\s*["']([^"']{2,200})["']"""),
    re.compile(r"""<form[^>]+action\s*=\s*["']([^"']{2,200})["']""", re.I),
    re.compile(r"""["'](/(?:api2?|ex)/[A-Za-z0-9_/\-]{2,120})["']"""),
]

# Laravel partials this family loads its modals from (cricmatch's signup form
# lives in /append/loginpp -- see CLAUDE.md). A payment form may well do the
# same, which is why "not in the page source" is never a conclusion here.
APPEND_RE = re.compile(r"""["'](/append/[A-Za-z0-9_\-]{2,60})["']""")


def _mask_proxy(proxy_str):
    """host:port only -- a probe's output gets pasted around, and the raw
    string carries the proxy PASSWORD (same reasoning as _proxy_label() in
    telegram_bot.py, which is not imported here: it parses --env at module
    level and would fight this script's own flags)."""
    if not proxy_str:
        return ""
    bare = proxy_str.split("://")[-1].split("@")[-1]
    return ":".join(bare.split(":")[:2])


def _save(name, text):
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, name)
    with open(path, "w") as f:
        f.write(text)
    return path


def _squash(s, limit=300):
    return re.sub(r"\s+", " ", s)[:limit]


def login():
    """Log in with the engine's OWN helpers, so the probe exercises the real
    code path rather than a side channel. (probe_khelo.py learned this the
    hard way: calling an adapter method directly skipped the proxy
    assignment and reported a site-side wall that was not there.)"""
    print("=" * 72)
    print(f"Logging in to {SITE_URL} as {USERNAME}"
          f"{' via ' + _mask_proxy(PROXY) if PROXY else ' (no proxy)'}")
    print("=" * 72)

    session = engine.http_session_for(PROXY)
    csrf = engine.http_fetch_csrf(session, SITE_URL, PROXY)
    print(f"  csrf token: {csrf[:16]}...")

    resp = engine.http_login_call(session, csrf, USERNAME, PASSWORD, SITE_URL)
    status = resp.get("status")
    if status != 200:
        # status None means the edge answered, not the app -- cricmatch
        # bare-403s a flagged proxy IP and rate-blocks /login on volume, and
        # both look exactly like a wrong password from here (CLAUDE.md, §1).
        kind = ("the edge/firewall answered, not the site -- try another proxy "
                "or wait ~20min" if status is None else "the site refused it")
        sys.exit(f"  LOGIN FAILED ({kind}): {resp.get('message') or resp}")
    print(f"  logged in: {resp.get('message')}")

    bal = engine.http_get_balance(session, csrf, SITE_URL)
    balance = (bal.get("balance") or {})
    print(f"  balance: {balance.get('wallet', balance.get('totalBalance', '?'))}"
          f"   exposure: {balance.get('exposure', '?')}")
    return session, csrf


def get(session, path, label):
    """One read-only GET. Returns (url, html) or (url, None)."""
    url = path if path.startswith("http") else urljoin(SITE_URL + "/", path.lstrip("/"))
    if urlsplit(url).hostname != urlsplit(SITE_URL).hostname:
        return url, None  # never follow off-site links
    try:
        r = session.get(url, timeout=30)
    except requests.RequestException as e:
        print(f"  [{label}] {url} -- request failed: {str(e)[:120]}")
        return url, None
    note = ""
    if r.history:
        # A redirect chain is worth printing before blaming a parser: the
        # khelo family hands the session to a sister domain exactly once and
        # the caller then parses a marketing page (CLAUDE.md, §7).
        note = "  <- redirected: " + " -> ".join(h.url for h in r.history) + f" -> {r.url}"
    print(f"  [{label}] {url}  HTTP {r.status_code}  {len(r.text)} bytes{note}")
    if r.status_code != 200 or len(r.text) < 200:
        return url, None
    return url, r.text


def find_candidate_links(html):
    """Links on the logged-in page whose href or label mentions money."""
    found = {}
    for m in re.finditer(r"""<a[^>]+href\s*=\s*["']([^"']+)["'][^>]*>(.{0,120}?)</a>""",
                         html, re.I | re.S):
        href, label = m.group(1), re.sub(r"<[^>]+>", " ", m.group(2))
        if href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        if KEYWORD_RE.search(href) or KEYWORD_RE.search(label):
            found.setdefault(href, _squash(label, 60).strip())
    # Plenty of this template's buttons are divs with data-href / onclick.
    for m in re.finditer(r"""data-href\s*=\s*["']([^"']+)["']""", html, re.I):
        if KEYWORD_RE.search(m.group(1)):
            found.setdefault(m.group(1), "(data-href)")
    return found


def find_endpoints(html):
    """Every endpoint the page's own JS calls, keyword-filtered."""
    hits = {}
    for pat in ENDPOINT_PATTERNS:
        for m in pat.finditer(html):
            ep = m.group(1)
            if ep.startswith(("http://", "https://")) and \
               urlsplit(ep).hostname != urlsplit(SITE_URL).hostname:
                continue  # third-party (analytics, CDN) -- not ours
            kw = KEYWORD_RE.search(ep)
            if not kw:
                continue
            # Keep a slice of context: the field list usually sits right after
            # the url, and that is the half that cannot be guessed.
            ctx = _squash(html[m.start():m.start() + 500])
            hits.setdefault(ep, (kw.group(0).lower(), ctx))
    return hits


def find_form_fields(html):
    """Forms (and their inputs) on a page that looks payment-related."""
    forms = []
    for m in re.finditer(r"<form[^>]*>(.*?)</form>", html, re.I | re.S):
        block = m.group(0)
        ident = re.search(r"""(?:id|name|class)\s*=\s*["']([^"']+)["']""", block[:300])
        fields = re.findall(
            r"""<(?:input|select|textarea)[^>]*name\s*=\s*["']([^"']+)["']""", block, re.I)
        if fields:
            forms.append((ident.group(1) if ident else "(unnamed)", fields))
    return forms


def keyword_js_blocks(html, limit=10):
    """Raw JS around each money keyword -- where the payload shape lives."""
    out, seen = [], set()
    for m in KEYWORD_RE.finditer(html):
        # Only inside something that looks like code, not prose/CSS.
        seg = html[max(0, m.start() - 450):m.start() + 650]
        if not re.search(r"(function|\$\.|ajax|url\s*:|data\s*:|fetch\s*\()", seg):
            continue
        key = m.group(0).lower()
        if key in seen:
            continue
        seen.add(key)
        out.append((key, _squash(seg, 900)))
        if len(out) >= limit:
            break
    return out


def report_page(label, url, html):
    """Print everything interesting about one fetched page."""
    fname = re.sub(r"[^A-Za-z0-9]+", "_", label).strip("_")[:50] + ".html"
    saved = _save(fname, html)

    endpoints = find_endpoints(html)
    forms = find_form_fields(html)
    blocks = keyword_js_blocks(html)

    print(f"\n{'-' * 72}\n  PAGE: {label}  ({url})\n  saved -> {saved}\n{'-' * 72}")

    if endpoints:
        print("  ENDPOINTS its own JS calls:")
        for ep, (kw, ctx) in sorted(endpoints.items()):
            print(f"    [{kw}] {ep}")
            print(f"           ...{ctx[:260]}")
    else:
        print("  ENDPOINTS: none matching the money keywords")

    if forms:
        print("  FORMS:")
        for ident, fields in forms:
            print(f"    <form {ident}>  fields: {', '.join(fields)}")

    if blocks:
        print("  JS around the money keywords:")
        for kw, seg in blocks:
            print(f"    [{kw}] {seg[:600]}")
            print("    " + "." * 64)

    return endpoints


def main():
    session, _csrf = login()

    print(f"\n{'=' * 72}\nStep 1: the logged-in homepage\n{'=' * 72}")
    url, home = get(session, "/", "home")
    if not home:
        sys.exit("Could not read the logged-in homepage -- stopping.")
    _save("home.html", home)

    links = find_candidate_links(home)
    appends = set(APPEND_RE.findall(home))
    print(f"\n  candidate money links found: {len(links)}")
    for href, label in sorted(links.items()):
        print(f"    {href:<48} {label}")
    print(f"  /append/ partials referenced: {sorted(appends) or '(none)'}")

    print(f"\n{'=' * 72}\nStep 2: the homepage's own endpoints\n{'=' * 72}")
    all_endpoints = {}
    all_endpoints.update(report_page("home", url, home))

    # The partials first: on this white-label the real form markup usually
    # lives there, not in the page that links to it.
    targets = [(p, f"append{p.replace('/', '_')}") for p in sorted(appends)]
    targets += [(h, f"link{re.sub(r'[^A-Za-z0-9]+', '_', h)[:40]}")
                for h in sorted(links)]
    if EXTRA_PAGE:
        targets.append((EXTRA_PAGE, "extra"))

    print(f"\n{'=' * 72}\nStep 3: fetching {len(targets)} candidate pages (GET only)\n{'=' * 72}")
    seen = set()
    for path, label in targets:
        if path in seen:
            continue
        seen.add(path)
        purl, phtml = get(session, path, label)
        if phtml:
            all_endpoints.update(report_page(label, purl, phtml))

    print(f"\n{'=' * 72}\nSUMMARY: every money endpoint seen, across all pages\n{'=' * 72}")
    if not all_endpoints:
        print("  NOTHING FOUND. Either this account has no payment section, or the")
        print("  forms are loaded from a partial not referenced in the markup we")
        print("  fetched. Next step is a manual walk-through with the browser's")
        print("  network tab open -- save the HAR and we read the paths off that.")
    for ep, (kw, _ctx) in sorted(all_endpoints.items(), key=lambda kv: kv[1][0]):
        print(f"  [{kw:<12}] {ep}")
    print(f"\n  All fetched pages saved under {OUT_DIR}/")
    print("  NOTE: this probe sent exactly one POST (the login). Nothing was")
    print("        deposited, requested, cancelled or whitelisted.")


if __name__ == "__main__":
    main()

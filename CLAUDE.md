# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Purpose

QA automation that drives the signup ("New Member? Register Now") flow on
cricmatch247.com to smoke-test the registration form. It is a test driver for
the owner's own site — account data comes from user-supplied config, every run
is logged, and each attempt is screenshotted into `shots/`. It is not a
mass-registration tool.

## Setup

```
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium
```

## Running

Default flow generates a random Indian name/username + a `@gmail.com` email
(format only — these are not real inboxes) + a policy-compliant password,
prompts for the phone number, then submits. Every account is stored in
`accounts.db` (SQLite, via `db.py`) with its final status/notes/screenshot
path so credentials can be retrieved later:

```
.venv/bin/python main.py --list            # recent stored accounts
.venv/bin/python main.py --list --limit 50
```

```
# default: generate random identity, prompt for phone, submit
.venv/bin/python main.py

# watch it in a real browser
.venv/bin/python main.py --headed

# fill but don't submit (validate selectors only)
.venv/bin/python main.py --no-submit

# skip the prompt / override any generated field
.venv/bin/python main.py --phone 9876543210 --email real@inbox.com

# batch from a JSON file (copy accounts.json.example -> accounts.json)
.venv/bin/python main.py --account-file accounts.json

# route the signup through a proxy
.venv/bin/python main.py --proxy host:port:username:password
.venv/bin/python main.py --proxy socks5://host:port:username:password

# target a different site (defaults to SITE_URL in main.py)
.venv/bin/python main.py --url "https://example.com?tag=123"
```

Randomly generated emails are not real inboxes, so email verification can't be
completed for them — pass `--email` with a real address to test that path.

`FIRST_NAMES`/`LAST_NAMES` in `main.py` are Indian names only (by design, to
match the site's expected user base); `EMAIL_DOMAIN` is a single constant
(`"gmail.com"`) rather than a list, since generated addresses only need to
*look* like real Gmail addresses for form-acceptance testing.

`--url` overrides `SITE_URL` for one run; in `--account-file` batch mode a
per-account `"url"` key overrides it per-account (same override pattern as
`"proxy"` — see below). The URL actually used is stored in `accounts.db`'s
`url` column alongside each signup, and `signup_once()` takes an optional
`site_url` parameter (falls back to the module-level `SITE_URL` constant)
rather than hardcoding it.

### Proxy support

`parse_proxy()` in `main.py` turns a proxy string into a Playwright proxy dict.
Accepted formats (optional `scheme://` prefix, defaults to `http`):
`host:port`, `host:port:username:password`, `scheme://host:port`,
`scheme://username:password@host:port`. Each signup opens its own
`browser.new_context(proxy=...)` rather than setting the proxy at browser
launch — Chromium supports per-context proxy overrides, so the CLI's
one-browser-per-run and the bot's one-shared-browser can both give each
signup a different proxy (or none) without relaunching Chromium.

In `--account-file` batch mode, a per-account `"proxy"` key overrides the
global `--proxy` flag; accounts that omit it fall back to `--proxy`. The
proxy used (raw string, may include credentials) is stored in `accounts.db`'s
`proxy` column alongside that signup.

A broken proxy raises Playwright's generic `Error` class, not `TimeoutError`
(`TimeoutError` is a subclass of `Error`, confirmed via
`issubclass(TimeoutError, Error)`) — e.g. an unreachable proxy fails fast with
`net::ERR_PROXY_CONNECTION_FAILED`, not a timeout. Both `main.py`'s per-account
loop and `telegram_bot.py`'s `_blocking_fill_and_register()` catch `PWError`
(imported as `Error as PWError`) around the proxy-dependent `page.goto()`, not
just `PWTimeout`, so a bad proxy is recorded as a clean failure instead of
crashing the run / leaving a bot session stuck.

If a proxy times out (rather than failing fast), that usually means either the
wrong protocol (ProxyCheap and similar resellers often issue SOCKS5-only
endpoints — try a `socks5://` prefix) or the provider requires the client IP
to be whitelisted in its dashboard, not just username/password auth.

#### SOCKS5-with-auth: the Chromium limitation and the pproxy bridge

Confirmed live: Chromium (and therefore Playwright) cannot authenticate to a
SOCKS5 proxy at all — `Browser.new_context` raises `"Browser does not support
socks5 proxy authentication"` the instant you pass a `socks5://` server with a
`username`. This is a real Chromium limitation, not a bug here; only HTTP(S)
proxies support username/password at the browser level. Unauthenticated
SOCKS5 (no username) works fine directly.

`maybe_bridge_proxy()` / `stop_bridge()` in `main.py` work around this: for a
SOCKS5 proxy with credentials, they launch a local `pproxy` subprocess
(`pip install pproxy`) that does the SOCKS5 handshake itself and exposes an
unauthenticated `http://127.0.0.1:<port>` for Chromium to use instead — Chrome
only ever sees a local, auth-free HTTP proxy. Both `main.py`'s per-account
loop and `telegram_bot.py`'s `_blocking_fill_and_register()` /
`_blocking_test_proxy_once()` call this before opening a context, and must
call `stop_bridge()` on every exit path (including failures) or the
subprocess leaks. `Session.bridge_proc` tracks the bot's per-session process;
`_blocking_close_context()` stops it alongside the browser context.

Non-obvious gotcha, found by reading `pproxy`'s source directly: it expects
upstream SOCKS5 credentials in the URL **fragment**, not the userinfo
position — `socks5://host:port#username:password`, not
`socks5://username:password@host:port`. The userinfo slot is reserved for
shadowsocks cipher specs (`cipher:key@host:port`), so passing credentials
there silently misparses as an invalid cipher name and pproxy exits
immediately. `maybe_bridge_proxy()` builds the fragment form correctly
already — don't "fix" it back to userinfo form.

Verified end-to-end against a real ProxyCheap SOCKS5 proxy: `curl` confirmed
the raw credentials work, the bridge authenticated correctly, and Chromium
successfully loaded a page through it with the exit IP matching the proxy.

#### Picking a proxy that actually works against this site

Two independent failure modes were found in production, neither a code bug:
1. **IP-reputation blocking**: cricmatch247.com sits behind an AWS ALB/WAF
   that returns a bare `403 Forbidden` (`server: awselb/2.0`) for requests
   from IPs that reputation databases flag as proxies. Checked via
   `ip-api.com/json/<ip>?fields=proxy,hosting` — a **datacenter** ProxyCheap
   IP came back `"proxy": true` and got blocked; a **residential** ProxyCheap
   IP came back `"proxy": false, "hosting": false` and loaded the site fine
   (HTTP 200, full page render). Always check this before assuming a proxy
   "doesn't work" — it may authenticate perfectly and still get WAF-blocked.
2. **Proxy resource itself unresponsive**: some ProxyCheap credentials
   connected at the TCP level but never completed the auth handshake under
   *either* SOCKS5 or HTTP, from two different source IPs (ruling out
   IP-whitelist as the cause) — that pointed to the proxy order itself being
   expired/misconfigured on the provider's side, not anything fixable here.

Prefer a **residential** proxy over datacenter for this site, and verify with
`/testproxy` (or the CLI equivalent below) before relying on it for a real
signup — a working-but-blocked proxy looks identical to a broken one until
you check the exit IP's reputation and confirm the actual site loads.

## Telegram bot

`telegram_bot.py` wraps the same signup/OTP logic behind a chat interface, for
running QA signups from Telegram instead of the CLI.

```
cp .env.example .env
# edit .env and paste your token from @BotFather into TELEGRAM_BOT_TOKEN
.venv/bin/python telegram_bot.py
```

`telegram_bot.py` loads `.env` via `python-dotenv` at import time; `.env` is
gitignored so the token never lands in a commit.

Commands: `/newacc` (generates an identity, asks for phone, then OTP, then
sends the result **screenshot as a photo** with full details as the caption),
`/stats` (counts by status), `/list [N]` (recent stored accounts, text),
`/photo <id>` (resend any past account's screenshot + caption, id from
`/list`), `/cancel` (abandon an in-progress flow), `/setproxy <proxy>` /
`/proxy` / `/clearproxy` / `/testproxy [proxy]` (per-chat proxy management,
see below), `/seturl <url>` / `/url` / `/clearurl` (per-chat site URL,
see below).

### Screenshot + caption delivery

`build_caption()` formats an account dict (or a `db.COLUMNS` row) into a
Telegram photo caption (username/email/password/phone/proxy/notes, capped at
Telegram's 1024-char limit for captions — well above what this ever
produces). `send_result_photo()` sends the screenshot file as a photo with
that caption, falling back to a plain text message if the file is missing
(e.g. a very old row from before a given code path started saving one).

Both `/newacc`'s final outcomes (registration failure, OTP success/failure)
and `/photo <id>` go through `send_result_photo()`, so the same
credentials-on-the-image experience works whether you're watching a signup
happen live or pulling up an old one later. `_blocking_fill_and_register()`
now takes a `result.png` screenshot right after the REGISTER click and
returns its path as `"shot"` in every failure branch (it previously only
returned a message with no screenshot at all) — kept in parity with
`_blocking_verify_otp()`, which already did this.

### Per-chat proxy

Each chat can set one active proxy (`chat_proxies`, keyed by `str(chat_id)`,
persisted to gitignored `proxy_settings.json` via `save_chat_proxies()`).
`/newacc` reads it at session-start time (`session.proxy`) and
`_blocking_fill_and_register()` opens that session's `BrowserContext` with it
(`parse_proxy()`, imported from `main.py` — same parser and same formats as
the CLI's `--proxy`). Bot replies never echo a set proxy's raw password back —
`mask_proxy_display()` shows only `host:port (user: ..., password hidden)`.

`/testproxy` opens a throwaway context with the given (or currently-set) proxy
and hits `api.ipify.org` to confirm it actually routes traffic, before you
rely on it for a real signup. If an `http(s)://` proxy times out, it
automatically retries once as `socks5://` and tells you if that's what fixed
it — ProxyCheap and similar resellers commonly issue SOCKS5-only endpoints
that look identical to an HTTP proxy string. A timeout (as opposed to an
immediate auth error) more often means wrong protocol or an IP-whitelist
requirement on the provider's dashboard than wrong credentials.

### Per-chat site URL

Same pattern as per-chat proxy: `chat_urls` (keyed by `str(chat_id)`,
persisted to gitignored `url_settings.json`) via `/seturl <url>` / `/url` /
`/clearurl`. `_load_json_dict()` / `_save_json_dict()` in `telegram_bot.py`
are the shared persistence helpers behind both `chat_proxies` and
`chat_urls` — reuse them for any future per-chat setting rather than adding
another bespoke load/save pair. `/newacc` reads the chat's URL at
session-start (`session.site_url`), and `_blocking_fill_and_register()` uses
it (falling back to `main.py`'s `SITE_URL`) instead of a hardcoded constant.

One Chromium instance is launched when the bot starts (`_blocking_ensure_browser()`,
pre-warmed in `main()` before `run_polling()`) and reused for every `/newacc` —
each session opens its own `BrowserContext` (isolated cookies/storage, like an
incognito window) rather than paying Chromium's process-launch cost per
conversation. All Playwright calls run on one shared `_pw_executor`
(`ThreadPoolExecutor(max_workers=1)`), since the sync API requires every call
for a given browser to happen on the same OS thread it was launched on —
concurrent `/newacc` flows from different chats are therefore serialized, not
parallel (fine for a personal QA bot). Sessions are looked up by `chat_id` in
the module-level `sessions` dict. Teardown always goes through that same
worker thread too (`close_browser()` / `end_session()`, both routed via
`run_in_executor`) — never call `context.close()`/`browser.close()` from the
asyncio event-loop thread directly, or Playwright's thread-affinity
requirement is violated.

Measured live: reusing the browser only saves ~0.5s per session (Chromium's
own cold-launch is fast, ~0.2s) — it's a free win but not the main lever. The
bigger cost is the ~8s of real page-load/hydration wait plus, previously, two
more flat 4s sleeps after REGISTER and after Verify. Those two are now
adaptive polling (`wait_for_register_outcome()` / `wait_for_otp_outcome()` in
`main.py`, shared by both the CLI and the bot) that return as soon as the
real outcome appears — measured ~0.3-0.5s for the phone-taken error, vs the
old flat 4s. On top of Chromium's own network round-trip, Telegram itself adds
per-message latency that a local terminal doesn't have, so the bot will still
feel slower than the CLI even though the underlying automation is now faster.

If the site rejects the phone number as already registered, the bot replies
with that message and stays in `await_phone`, keeping the session (and its
generated username/email/password) alive so you can just send a different
phone number — it doesn't restart the whole flow.

## CLI architecture

Two files: `main.py` (Playwright driver, sync API) and `db.py` (SQLite storage).

- `db.py` owns `accounts.db`: one `accounts` table with every detail of a
  signup attempt — `username, email, password, phone, proxy, url, status,
  notes, screenshot, created_at`. `main.py` inserts a row per generated
  account before running it, then updates status/notes/screenshot once the
  attempt finishes — so even failed/partial runs are recorded, including
  which proxy and which site URL were used. `db.COLUMNS` is the single
  source of truth for column order; `list_accounts()`/`print_accounts()` and
  the bot's `/list`/`/photo` handlers all read through it so the CLI and bot
  never drift out of sync on which fields get shown. New columns go through
  `_MIGRATED_COLUMNS` in `get_connection()` so older `accounts.db` files
  upgrade automatically via `ALTER TABLE ... ADD COLUMN` (wrapped in a
  try/except for "column already exists"). Both `main.py --list` and the
  bot's `/list` display every column (including the full plaintext password
  and the screenshot path) — there's no masking here, unlike proxy strings in
  bot replies, since the entire point of this table is being able to
  retrieve exact login credentials later.


- `SEL` dict holds all selectors captured from the live modal. If the site's
  markup changes and the script breaks, re-verify these first. Key ones:
  open-modal `.registerUserData`, username `#userNameid`, email `#userEmailid`,
  password `#pass_log_id`, phone `#phoneNumber`, T&C checkbox `#remChck2`,
  submit `button.cls_register_new`.
- `open_signup_modal()` must first call `dismiss_popups()` — a promo overlay
  loads on page load and covers the header JOIN button. The reliable trigger is
  `.registerUserData`; the header `.headerjoinBtn` is often reported not-visible.
- The signup form is injected by JS after the JOIN click, so it is NOT in the
  static HTML. To re-inspect fields, use `inspect_form.py`.
- The `page.wait_for_timeout(4000)` right after `page.goto()` in `signup_once()`
  looks like it should be replaceable with a visibility-based wait, but this was
  tested live and reproduced a real failure: `open_signup_modal()` clicked while
  the promo overlay was still covering the button, even though the button itself
  was already "visible" per Playwright's definition. Leave this one alone unless
  you re-verify carefully against the live site.
- `wait_for_register_outcome()` / `wait_for_otp_outcome()` replaced the flat 4s
  sleeps after the REGISTER and Verify clicks with adaptive polling for the
  actual outcome (OTP screen / phone-taken error / any toast). These check for
  concrete DOM state rather than a generic readiness proxy, which is why they
  were safe to convert where the post-`goto` sleep wasn't.
- `read_result()` scrapes toast/validation text after submit; success detection
  is a heuristic (absence of words like "already"/"invalid"), so always confirm
  against the `shots/*-result.png` screenshot.
- `check_phone_taken()` handles "The mobile number has already been taken."
  separately from `read_result()` — it's a bare `<li>` inside `.err_phone`, not
  a toast, so none of `read_result()`'s selectors match it. In `main.py`'s
  interactive flow (not `--account-file` batch), `signup_once()` detects this
  and reprompts for a different phone number, retrying up to 5 times before
  giving up.
- `enter_otp()` runs after a successful REGISTER: the site opens a signup OTP
  popup (6 single-digit boxes `input.otp__digit_signup`, Verify = `a.get_user_otp`),
  the script prompts for the SMS code, types one digit per box, clicks Verify,
  and screenshots `*-otp-filled.png` / `*-otp-result.png`. NOTE the site has a
  second, separate OTP widget for "Login with OTP" (`input.otp__digit` WITHOUT
  the `_signup` suffix) — do not target that one.

## Site-specific notes

- `SITE_URL` in `main.py` points to `https://cricmatch247.com?btag=211079` (an
  affiliate/tracking tag) rather than the bare domain — every signup, CLI and
  bot alike, goes through this URL since `telegram_bot.py` imports `SITE_URL`
  from `main.py`.

- The real modal has only 4 inputs (username, email, password, mobile) plus an
  "I'm over 18 + accept T&C" checkbox — there is no first/last name or DOB field
  despite what the site's help text suggests.
- Password policy enforced by the form: min 5 / max 60 chars, at least one
  digit, one special character, and both upper- and lower-case letters.

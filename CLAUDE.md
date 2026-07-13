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

# export every stored account to a CSV file and exit
.venv/bin/python main.py --export-csv                    # writes accounts_export.csv
.venv/bin/python main.py --export-csv my_accounts.csv

# filter --list / --export-csv to one status and/or one site URL
.venv/bin/python main.py --list --status success
.venv/bin/python main.py --export-csv success.csv --status success
.venv/bin/python main.py --export-csv by-url.csv --filter-url "https://example.com?tag=123"
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
# edit .env: TELEGRAM_BOT_TOKEN from @BotFather, and MASTER_ADMIN_ID
# (your own Telegram user ID -- message @userinfobot to get it)
.venv/bin/python telegram_bot.py
```

`telegram_bot.py` loads `.env` via `python-dotenv` at import time; `.env` is
gitignored so the token (and `MASTER_ADMIN_ID`) never land in a commit.

### Roles

Two roles, checked via `is_master(user_id)` / `is_admin(user_id)` (master
counts as admin too) and enforced with a `@require_role(check)` decorator on
every handler except `/start`:

- **master admin** — exactly one, fixed via `MASTER_ADMIN_ID` in `.env` and
  never changeable from inside the bot (so a compromised admin session can't
  self-promote). Can do everything: `/addadmin <id>` / `/removeadmin <id>` /
  `/admins`, `/setproxy` / `/proxy` / `/clearproxy` / `/testproxy`, `/seturl`
  / `/url` / `/clearurl`, and all data commands (`/list`, `/photo`,
  `/export`, `/stats`).
- **admin** — authorized by the master admin, persisted in gitignored
  `admins.json` (`admin_ids`, a set of Telegram user-id strings, via
  `save_admin_ids()`). Can only run `/newacc`, `/done`, and `/cancel`.
- **anyone else** — every gated handler replies "You are not authorized...
  Your Telegram user ID: `<id>`" so an unauthorized user can hand that ID to
  the master admin for `/addadmin`. `/start` is deliberately *not*
  `@require_role`-gated, since it's the one command that needs to show
  different content per role (including this ID-disclosure message) rather
  than a blanket rejection.

Proxy, site URL, and password are all **global**, not per-chat —
`global_settings` (persisted to gitignored `bot_settings.json` via
`save_settings()`) holds `{"proxy": ..., "url": ..., "password": ...}`, set
only by the master admin, and every admin's `/newacc` reads from it
(`session.proxy`, `session.site_url`, and an override of
`session.acct["password"]` after `gen_account()` already generated a random
one). This replaced an earlier per-chat-dict design (`chat_proxies`/
`chat_urls`) once "master sets it for everyone" became the actual
requirement.

`/setpassword <pw>` fixes every future signup to that exact password;
`/setpassword --random` removes the `"password"` key from `global_settings`
so `newacc()`'s `if global_settings.get("password"):` check is falsy again
and the random one from `gen_account()` is left as-is. There's no
"random" *value* stored anywhere — random mode is simply the absence of a
`"password"` key, which is also why `/password` reports it as `RANDOM
(default, per-signup)` rather than showing some placeholder.

Telegram's native "/" command-menu autocomplete is scoped per user via
`BotCommandScopeChat`, set in `post_init()` (a callback passed to
`Application.builder().post_init(...)`, run once before polling starts) and
also updated live inside `/addadmin`/`/removeadmin`. The **default** scope
(`BotCommandScopeDefault`) is set to an empty list, so a random user's "/"
menu shows nothing at all — they can still type a command manually and get
the `require_role` rejection, the menu is just a visibility/discoverability
control, not the actual enforcement (that's the decorator).

Commands (master unless noted): `/newacc` (admin+; starts a **continuous**
run of signups, see below), `/done` (admin+; stop after the current one),
`/cancel` (admin+; abort now, also stops the loop), `/stats` / `/stats
<btag>` (counts by status, and by btag; a btag argument narrows to that
btag's own status breakdown — see below), `/list [N]` (recent stored
accounts, text), `/photo <id>` (resend
any past account's screenshot + caption, id from `/list`), `/export [N]
[status] [url]` (CSV, defaults to successful signups only), `/setpassword
<pw>` / `/setpassword --random` / `/password` (global fixed-or-random
password mode), `/setproxy <proxy>` / `/proxy` / `/clearproxy` /
`/testproxy [proxy]` (global proxy), `/seturl <url>` / `/url` / `/clearurl`
(global site URL), `/btag <code>` / `/btag` (global site URL's `btag` query
param only, see below), `/addadmin <id>` / `/removeadmin <id>` / `/admins`.

`/btag <code>` rebuilds the global site URL keeping whatever scheme/host/path
the current one (or, if none is set, `SITE_URL`) already has, and replaces
just its query string with `btag=<code>` — so switching affiliate tags
doesn't require retyping the whole URL by hand. It reuses `main.py`'s
`extract_referral_code()` for the no-argument form (`/btag` alone shows the
currently-active code). Like `/seturl`, this writes to `global_settings["url"]`
and persists via `save_settings()`, so it's global across all admins, not
per-chat.

`/stats` with no arguments groups `accounts` by `status` (as before) and now
also by the `referral_code` column (`COALESCE(referral_code, '(none)')`, since
any signup made against the default `SITE_URL` before a `/seturl`/`/btag`
override has a `NULL` `referral_code`) — this is the per-btag signup count.
`/stats <btag>` instead filters `WHERE referral_code = ?` and shows just that
btag's own status breakdown (how many succeeded/failed/etc under that one
tag), mirroring how `/export`'s status/url filters narrow its CSV dump.

### Continuous signup loop

`/newacc` no longer means "one signup" — it adds the chat to `looping_chats`
(a module-level `set`) and calls `begin_signup(update, chat_id)`, which holds
the account-generation + session-creation + "send the phone number" logic
that used to live directly in `newacc()`. After a signup reaches a terminal
outcome inside `handle_message()` (registration failure, or the final
OTP success/failure) — the exact two places that used to call
`end_session()` + `del sessions[chat_id]` and stop — there's now an added
`if chat_id in looping_chats: await begin_signup(update, chat_id)`, so a
fresh account starts immediately with no further `/newacc` needed.

`/done` only removes the chat from `looping_chats`; it does **not** touch
`sessions`, so a signup already in flight (e.g. waiting on an OTP you haven't
sent yet) still completes normally — it just doesn't auto-restart afterward.
`/cancel` does both: clears `looping_chats` *and* tears down the current
session immediately via `end_session()`. Get this distinction right if you
touch either handler — `/done` is "stop after this one," `/cancel` is "stop
right now."

Verified via a full mock run of `handle_message()` (stubbing
`_blocking_fill_and_register`/`_blocking_verify_otp`/`_blocking_close_context`
rather than needing a real browser): `/newacc` → phone → OTP-success
produces a *different* account already sitting in `await_phone`, with zero
additional commands sent.

### Screenshot + caption delivery

`build_caption()` formats an account dict (or a `db.COLUMNS` row) into a
Telegram photo caption (username/email/password/phone/proxy/notes, capped at
Telegram's 1024-char limit for captions — well above what this ever
produces). `send_result_photo()` sends the screenshot file as a photo with
that caption, falling back to a plain text message if the file is missing
(e.g. a very old row from before a given code path started saving one).

**Success and failure are handled differently on purpose.** A *failed*
signup (registration failure, or OTP rejected/timed out) still goes through
`send_result_photo()` **and** `send_csv()` — the screenshot and the full
credentials are the actual diagnostic value there. A *successful* signup gets
neither: just a plain `f"Signup successful! (#{session.row_id})"` reply, with
no photo, no caption, and no CSV pushed into the chat — the account's
credentials stay in `accounts.db` and are retrievable later via `/list` or
`/export` (or `/photo` for the screenshot, though a working-form screenshot
adds little). This is deliberate: the admin explicitly asked for successful
signups to not spam the chat with details, only a bare confirmation. Don't
reintroduce `send_result_photo()`/`send_csv()` on the success path without
checking this was a deliberate choice, not an oversight.

`_blocking_fill_and_register()` takes a `result.png` screenshot right after
the REGISTER click and returns its path as `"shot"` in every failure branch
(it previously only returned a message with no screenshot at all) — kept in
parity with `_blocking_verify_otp()`, which already did this. It also now
saves a screenshot (`*-no-modal.png`) if `open_signup_modal()` itself fails,
which it never used to — that specific failure previously had zero visual
evidence.

### CSV export

`db.export_csv(conn, path, limit=None, status=None, url=None, row_id=None)`
writes `db.COLUMNS` rows to a CSV file — `row_id` for one specific account
(used after every `/newacc` outcome, success or failure, so the details
arrive as an actual file rather than only as text/caption), or
`limit`/`status`/`url` for a
bulk dump (`limit=None` means every row). `telegram_bot.py`'s `send_csv()`
wraps this in a `tempfile.NamedTemporaryFile`, sends it via
`reply_document()`, and deletes the temp file in a `finally` — follow that
pattern for any new CSV-producing command rather than writing into the repo
directory.

**`/export`'s default differs from `--export-csv`'s on purpose.** The bot's
`export_cmd()` defaults `status` to `"success"` — plain `/export` gives you
only successful signups, and you say `/export all` to get every status
(`status=None`). The CLI's `--export-csv` has no such default — it exports
everything unless you pass `--status`, matching `--list`'s existing
unfiltered-by-default behavior. Both are deliberate: `/export` is usually
"give me the accounts that worked," while the CLI flag follows ordinary
CLI convention (explicit opt-in filtering, nothing filtered by default).

`export_cmd()`'s argument parsing takes `N` (a row limit), a status word, and
a site URL, all in **any order** — `/export 50`, `/export failed`,
`/export https://example.com`, `/export https://example.com failed 20` all
work, since each arg is classified independently: `arg.isdigit()` → limit,
`arg.startswith(("http://", "https://"))` → URL filter, `arg.lower() ==
"all"` → clear the status filter, anything else → explicit status
(overriding the `"success"` default). `db.list_accounts()`/`export_csv()`'s
`url` parameter does an exact match against the `url` column, which is
`NULL` for any signup that used the default `SITE_URL` rather than an
explicit `/seturl`/`--url` override — filtering by a specific URL only
surfaces signups explicitly tagged with it, not the NULL/default ones.

Link-filtered export stayed **master-only**, same as unfiltered `/export` —
admins still cannot run any `/export` variant, a deliberate choice to keep
"admins can only create new accounts" intact rather than carve out an
exception per argument.

**`referral_code` column** — `main.py`'s `extract_referral_code(url)` pulls
the `btag` query-string value out of a site URL (e.g.
`"...?btag=211079"` → `"211079"`) via `urllib.parse.parse_qs`/`urlsplit`, so
it's a separate, always-present column even when `url` itself is `NULL`
(the default-`SITE_URL` case) — computed from `acct.get("url") or SITE_URL`
in both `main.py`'s `load_accounts()` and `telegram_bot.py`'s
`handle_message()`, right before `db.insert_account()`. This is specific to
this site's `btag` affiliate-tracking convention, not a generic
"parse any query param" facility — if you point this at a site using a
different tracking param name, `extract_referral_code()` needs updating
(or generalizing) to match.

`/testproxy` opens a throwaway context with the given (or currently-set) proxy
and hits `api.ipify.org` to confirm it actually routes traffic, before you
rely on it for a real signup. If an `http(s)://` proxy times out, it
automatically retries once as `socks5://` and tells you if that's what fixed
it — ProxyCheap and similar resellers commonly issue SOCKS5-only endpoints
that look identical to an HTTP proxy string. A timeout (as opposed to an
immediate auth error) more often means wrong protocol or an IP-whitelist
requirement on the provider's dashboard than wrong credentials. Bot replies
never echo a set proxy's raw password back — `mask_proxy_display()` shows
only `host:port (user: ..., password hidden)`.

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

If the site rejects the phone number as already registered, the bot records
the attempt as `phone_taken`, closes that browser context, and — same as any
other finished signup — moves straight on: if the chat is still in
`looping_chats` it calls `begin_signup()` again, generating a brand-new
account and prompting for a phone number, with no `/newacc` or manual retry
needed. This makes `phone_taken` a terminal outcome for that attempt (like
`success`/`failed`) rather than a pause waiting on the admin to supply a
different number for the same account.

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


- `SEL` dict holds all selectors captured from the live sites. **It now covers
  two platforms at once** via comma-joined CSS groups (see "Multi-site
  support" below): cricmatch247's markup (username `#userNameid`, email
  `#userEmailid`, password `#pass_log_id`, phone `#phoneNumber`, T&C checkbox
  `#remChck2`, submit `button.cls_register_new`, open-modal
  `.registerUserData`) and spin24star's Khelo markup (`#userNameKhelo` /
  `#emailKhelo` / `#passwordKhelo` / `#phoneKhelo` / `#signUpButtonKhelo`,
  open via `button.rj__join_now`). If a site's markup changes and the script
  breaks, re-verify these first.
- `open_signup_modal()` must first call `dismiss_popups()` — a promo overlay
  loads on page load and covers the header JOIN button. The reliable trigger is
  `.registerUserData`; the header `.headerjoinBtn` is often reported not-visible.
- The signup form is injected by JS after the JOIN click, so it is NOT in the
  static HTML. To re-inspect fields, use `inspect_form.py` (takes an optional
  URL argument, defaults to cricmatch247).
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
- `wait_for_register_outcome()` returns a `(outcome, messages)` **tuple** —
  `messages` is the `read_result()` snapshot captured the instant the error
  was spotted. Callers must use that instead of calling `read_result()` again
  afterward: snackbar toasts (spin24star) auto-dismiss, so a re-read moments
  later can come back empty and turn a real site message into
  "unknown error". Both `signup_once()` and the bot's
  `_blocking_fill_and_register()` were bitten by exactly this before the
  signature change.
- If a REGISTER rejection still ends with no visible message, the bot's
  `_blocking_fill_and_register()` appends the POST responses fired by the
  click (`status`, URL, first 150 chars of body) to the failure notes via a
  `page.on("response")` listener — that's the diagnostic for "the register
  API itself was blocked/hung" (e.g. a WAF-flagged proxy IP), which no
  screenshot can show. If *no* POST fired at all, the notes say the REGISTER
  click had no effect.
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

## Multi-site support (cricmatch247 + spin24star)

The driver supports two sites with **one** `SEL` dict rather than per-site
profiles: every single-selector key is a comma-joined CSS group
(`"#userNameid, #userNameKhelo"`), which works because the two platforms'
ids/classes never coexist on one page — each group resolves to exactly one
element per site, so Playwright's strict mode never trips. Site selection is
purely by URL (`--url` / `/seturl`); there is no site flag anywhere.

spin24star.com runs the "Khelo" white-label platform (assets under
`khelocdn`), inspected live via `inspect_form.py`. Differences that needed
handling, all inside `main.py`:

- **Register trigger**: no `.registerUserData`; instead several
  `button.rj__join_now` REGISTER buttons (`onclick="reg_page()"`, navigates
  to `/join-now` → `/?reg=1`), only one of which is visible. A game section
  (`.aviator_main_sec_root`) overlays it, so a plain click retries forever on
  "subtree intercepts pointer events" — the click **must be forced**.
  `open_signup_modal()` handles this in a dedicated branch (keyed on
  `SEL["open_modal_khelo"]` matching at all, so cricmatch's path is
  untouched): it force-clicks the first *visible* `rj__join_now`. It also
  gained a fast-path that returns immediately if the username field is
  already visible (Khelo shows the form directly at `/?reg=1`).
- **Intro overlay**: a full-screen SPRIBE/aviator walkthrough covers the whole
  page on load; its dismiss control is `div.skip_right_img` ("skip »"), added
  to `SEL["close_popup"]`.
- **Form fields**: `#userNameKhelo` / `#emailKhelo` / `#passwordKhelo` /
  `#phoneKhelo`, submit `#signUpButtonKhelo`. The T&C mark is NOT a real
  `input[type=checkbox]` (the only real checkbox on the page is the login
  form's `#rememberMe`) and renders already-checked — nothing to click, and
  the existing `cb.count()` guard skips `#remChck2` cleanly.
- **OTP**: 6 boxes `input.regOtpKhelo1`, verify `button.submitRegOtpMain`
  (appended to the `otp_verify` candidates). Same trap as cricmatch: the page
  also has separate login-OTP inputs (`input.otpNumberkhelo`) and
  forgot-password OTP inputs (`input.otpNumberFp`) — do not match those.

- **Error display**: rejections render as a top-right **snackbar**
  (`div.snackbar-container` holding a bare `<p>`, e.g. "Please enter valid
  mobile number") with no toast/alert/error class anywhere — none of
  `read_result()`'s original selectors matched it, which surfaced in
  production as `Register rejected: unknown error` after the full 12s
  `wait_for_register_outcome()` timeout. `.snackbar-container` is now in
  `read_result()`'s selector list; verified live that a rejected REGISTER now
  returns `outcome=error` immediately with the actual message text.

Verified live (2026-07-12/13): `--no-submit --url https://spin24star.com`
fills the whole register form correctly (screenshot confirmed all four
fields + pre-checked T&C), and a REGISTER click that the site rejects is
detected instantly with the real snackbar message. **Not yet verified live**
(needs a real phone number): the post-REGISTER OTP screen and OTP verify.
A taken phone on spin24star surfaces through the snackbar → `read_result()`
path as a plain `failed` outcome (the bot then just loops to the next
account) rather than cricmatch's dedicated `phone_taken` status
(`.err_phone` is cricmatch-specific markup).

### spin24star is behind an AWS WAF CAPTCHA (known blocker, not a code bug)

Diagnosed live (2026-07-13): a *real* signup on spin24star never reaches the
OTP screen because the register POST is **challenged by AWS WAF**. Full trace:
the REGISTER click fires a same-origin `POST https://spin24star.com/sign-up`,
which returns **HTTP 405 with header `x-amzn-waf-action: captcha`** and a
`<title>Human Verification</title>` HTML page instead of JSON. The site's own
AJAX handler doesn't surface this — the submit button just sticks on "Please
wait ..." — so with no visible message it bubbled up as `Register rejected:
unknown error`.

Key facts established, so nobody re-litigates this as a bug:
- It is **not** IP reputation. The failing proxy IP (`51.194.232.95`) checks
  clean on `ip-api.com` (`proxy:false, hosting:false`, residential ISP), and
  the same 405/captcha happens with **no proxy at all** from a clean IP.
- It is **not** a missing token. The `aws-waf-token` cookie *is* present after
  page load (alongside `AWSALB`/`AWSALBCORS`) in **both** headless and headed
  Chromium — and `/sign-up` still returns 405 `captcha` in both. The WAF rule
  demands an actually-*solved* CAPTCHA for the register action, which no
  browser-mode/stealth tweak provides.
- cricmatch247 does **not** CAPTCHA its register endpoint, which is why the
  same code path works there and not here.

Getting past this needs one of: (a) the **site owner exempts** the register
endpoint or a test IP/header from the WAF CAPTCHA rule (cleanest, since this
is the owner's own QA per the Purpose section), or (b) integrating a
**CAPTCHA-solving service** that supports AWS WAF CAPTCHA (e.g. CapSolver /
2Captcha) — real cost/work, out of scope for the current code. Do not
"fix" this in the driver by fiddling selectors or waits; the request is
being rejected at the edge before the app ever sees it.

`_blocking_fill_and_register()` now makes this legible instead of dumping WAF
telemetry tokens: it captures the same-origin register POST response (skipping
`token.awswaf.com` beacons + analytics) and, when the outcome has no visible
message, reports any `x-amzn-waf-action` header explicitly — e.g. `BLOCKED by
AWS WAF (x-amzn-waf-action: captcha, HTTP 405) on .../sign-up`. Verified live
that this is exactly what a spin24star `/newacc` now reports.

## Site-specific notes

- `SITE_URL` in `main.py` points to `https://cricmatch247.com?btag=211079` (an
  affiliate/tracking tag) rather than the bare domain — every signup, CLI and
  bot alike, goes through this URL since `telegram_bot.py` imports `SITE_URL`
  from `main.py`. To run signups against spin24star instead, set
  `/seturl https://spin24star.com` (bot) or pass `--url` (CLI).

- The real modal has only 4 inputs (username, email, password, mobile) plus an
  "I'm over 18 + accept T&C" checkbox — there is no first/last name or DOB field
  despite what the site's help text suggests.
- Password policy enforced by the form: min 5 / max 60 chars, at least one
  digit, one special character, and both upper- and lower-case letters
  (spin24star shows the same rule set as inline indicators on its register
  form, so one generated password satisfies both sites).

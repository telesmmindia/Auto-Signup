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

# skip the browser entirely, hit the register API directly (cricmatch only,
# see "--fast: HTTP-only signup" below; falls back to the browser for sites
# that don't support it)
.venv/bin/python main.py --fast --phone 9876543210

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

### `--fast`: HTTP-only signup, no browser at all

`main.py --fast` skips Chromium/Playwright entirely and hits cricmatch247's
register endpoint with plain `requests` calls. Discovered by capturing a real
Playwright run's network traffic (`page.on("request"/"response")`), then
confirmed live end-to-end with a raw `curl` replay that got back byte-
identical JSON to the browser flow, with **zero cookies/state carried over
from any earlier browser session** — a fresh `curl -c cookies.txt` run from
scratch worked, so this isn't riding on some Playwright-established session.
The whole thing turns out to be a stock Laravel app with no WAF/JS challenge
on this endpoint:

1. `GET /` → an `X-CSRF-TOKEN`/`_token` from the `<meta name="csrf-token">`
   tag, plus session cookies (`laravel_session`, `XSRF-TOKEN`, `AWSALB*`) set
   on the response — no JS execution needed to get either.
2. `POST /register` with `username, email, password, phone, otp=""` + the
   token → triggers the SMS, e.g.
   `{"status":205,"message":"OTP has been sent.","message_class":"success"}`.
3. The **same** `POST /register` again, now with the real `otp=<code>` →
   verifies it, e.g. `{"status":206,"message":"Please enter valid OTP",
   "message_class":"danger"}` for a wrong code.

This is ~10-20x faster (no browser launch, no page render, no adaptive-poll
waits) and lighter to run many of, but is a **more fragile, less honest**
test than driving the real UI: it hard-codes today's field names and JSON
response shape rather than exercising the actual form/JS, so a backend change
(renamed field, added CAPTCHA, different response shape) breaks it silently
instead of surfacing as a missing-selector error the way the browser path
does. Prefer `--fast` for volume/speed; prefer the default browser path when
you actually want to confirm the live UI still works end-to-end.

Implementation lives in `main.py`: `_http_session_for()` (builds a
`requests.Session`, translating a `--proxy` string the same way
`parse_proxy()` does — `requests` can authenticate to SOCKS5 directly via
PySocks, so unlike Chromium it needs no `pproxy` bridge), `http_fetch_csrf()`,
`http_register_call()`, and `http_signup_once()` (same result-dict shape as
`signup_once()` — `{"account","ok","messages","shot"}` — except `shot` is
always `None`, since there's no browser to screenshot). Site support is a
`SiteProfile` flag (`sites/base.py`): `supports_http_fast` (only
`sites/cricmatch.py` sets it `True`), plus `http_register_path` (default
`/register`) and `http_otp_digits` (default 6, since there's no DOM to count
digit boxes in without a browser). `main()` checks this flag per-account and
falls back to the normal Playwright path automatically for any site that
doesn't support it (spin24star: its register POST is gated by a real AWS WAF
JS challenge — see below — so `supports_http_fast` stays `False` there, by
design, not an oversight); a mixed batch (`--account-file` with accounts
across sites) only launches Chromium at all if at least one account in the
batch actually needs it.

`--fast --no-submit` is rejected outright (`--no-submit`'s whole point is
filling the DOM form without clicking submit; there's no DOM here to fill).
The interactive phone-number reprompt for a taken number still works the
same way as the browser path (`prompt_phone()`, up to 5 retries when
`interactive` — i.e. not `--account-file` batch mode); the OTP prompt
(`prompt_otp()`) is unconditional either way, same as `enter_otp()` in the
browser path.

**Not yet verified live: the "phone already taken" JSON shape.**
`_http_is_phone_taken()` guesses at it (`"taken"` + `"mobile"`/`"phone"` in
the message, modeled on the DOM error's known wording — see
`check_phone_taken()`) because triggering it for real requires a phone number
that already completed a full, verified registration, which wasn't available
to test against. If the guess is wrong, nothing is silently swallowed — the
raw server message still lands in `result["messages"]` via the generic-error
fallback, it just won't trigger the automatic re-prompt-for-a-different-
number behavior.

Verified live 2026-07-19: a real `--fast` run (dummy phone, dummy OTP) got
the SMS-sent response, prompted for the OTP exactly like the browser path,
correctly reported the server's real "Please enter valid OTP" rejection, and
stored the attempt in `accounts.db` with `screenshot=NULL` — no Chromium
process was ever spawned for the run.

## Telegram bot

`telegram_bot.py` wraps the same signup/OTP logic behind a chat interface, for
running QA signups from Telegram instead of the CLI.

```
cp .env.example .env
# edit .env: TELEGRAM_BOT_TOKEN from @BotFather, and MASTER_ADMIN_ID
# (your own Telegram user ID -- message @userinfobot to get it; add more than
# one master by separating ids with a comma or space, e.g.
# MASTER_ADMIN_ID=111111111,222222222)
.venv/bin/python telegram_bot.py
```

`telegram_bot.py` loads `.env` via `python-dotenv` at import time; `.env` is
gitignored so the token (and `MASTER_ADMIN_ID`) never land in a commit.

### Running one bot per site

`telegram_bot.py` supports an optional `--env <path>` CLI flag so the same
script can run as two (or more) independent bot processes, one per site,
instead of one bot juggling both via `/seturl`. This gives each site its own
bot identity/token in Telegram and, more importantly, its own worker
thread/browser — signups for different sites no longer serialize on the
single shared `_pw_executor`.

```
cp .env.example .env.cricmatch      # or .env.cricmatch.example -> .env.cricmatch
cp .env.example .env.spin24star
# edit each: a DIFFERENT TELEGRAM_BOT_TOKEN (from a second @BotFather bot),
# BOT_SITE_URL for that site, and distinct ADMINS_FILE / SETTINGS_FILE paths
.venv/bin/python telegram_bot.py --env .env.cricmatch
.venv/bin/python telegram_bot.py --env .env.spin24star   # separate terminal/tmux pane
```

`--env` is parsed from `sys.argv` at module level, before
`load_dotenv(_env_file, override=True)` runs. The `override=True` is load-
bearing, not decorative: `main.py` (which `telegram_bot.py` imports from)
already runs its own bare `load_dotenv()` as an import-time side effect,
which happens *before* `telegram_bot.py`'s own `load_dotenv()` call in
source order. python-dotenv defaults to `override=False` (first load wins),
so without the explicit `override=True` a real `.env` sitting in the repo
root would silently win over `--env .env.spin24star` for every key both
files define — this was caught live by importing the module with a `--env`
pointing at a throwaway file and asserting `BOT_TOKEN` came from it, not from
the repo's real `.env`.

Two new env vars support the split, both optional and inert for the
single-bot case:
- `BOT_SITE_URL` — locks this instance's default site (falls back to
  `main.SITE_URL` if unset). Every place the bot used to fall back to the
  bare `SITE_URL` import now falls back to this instead (`/url`, `/clearurl`,
  `/btag`, the OTP-flow `page.goto()`, and referral-code extraction) — so
  `/clearurl` on the spin24star instance resets to spin24star, not
  cricmatch247. `/seturl` still works per-instance if you want to
  temporarily point one bot elsewhere; `BOT_SITE_URL` only changes the
  *default*, it doesn't lock the door.
- `ADMINS_FILE` / `SETTINGS_FILE` — override the default `admins.json` /
  `bot_settings.json` paths. Required in practice for a two-process setup:
  both files are read once at import and rewritten via `save_admin_ids()` /
  `save_settings()`, so two processes sharing the same filename would
  clobber each other's admin list / proxy / password / URL on every write.
  Give each instance its own file (seed both with the same admin IDs via
  `/addadmin` on each bot if the same people should run both — there's no
  code-level sharing, just matching content by convention). `accounts.db`
  itself is NOT split this way — it's intentionally shared across instances
  since its `url`/`referral_code` columns already distinguish rows by site,
  so `/list`/`/stats`/`/export` give combined history by default.

### Roles

Two roles, checked via `is_master(user_id)` / `is_admin(user_id)` (master
counts as admin too) and enforced with a `@require_role(check)` decorator on
every handler except `/start`:

- **master admin** — one or more, fixed via `MASTER_ADMIN_ID` in `.env` (a
  single id, or several comma-/space-separated ids, e.g.
  `MASTER_ADMIN_ID=111111111,222222222`) and never changeable from inside the
  bot (so a compromised admin session can't self-promote). Every master is
  fully equal — there's no "primary" master and no way for one master to
  demote another; that only happens by editing `.env` and restarting. Can do
  everything: `/addadmin <id>` / `/removeadmin <id>` / `/admins`, `/setproxy` /
  `/proxy` / `/clearproxy` / `/testproxy`, `/seturl` / `/url` / `/clearurl`,
  and all data commands (`/list`, `/photo`, `/export`, `/stats`).
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
password mode), `/fast on` / `/fast off` / `/fast` (global HTTP-fast signup
mode, see below), `/setproxy <proxy>` / `/proxy` / `/clearproxy` /
`/testproxy [proxy]` (global proxy), `/seturl <url>` / `/url` / `/clearurl`
(global site URL), `/btag <code>` / `/btag` (global site URL's `btag` query
param only, see below), `/addadmin <id>` / `/removeadmin <id>` / `/admins`.

### `/fast`: HTTP-fast signup mode (bot side)

Same feature as the CLI's `--fast` (see the "`--fast`: HTTP-only signup, no
browser at all" section above), wired into the bot's chat-driven flow.
`/fast on` / `/fast off` sets `global_settings["fast"]` (persisted via
`save_settings()`, same as `proxy`/`url`/`password` — global across every
admin's `/newacc`, not per-chat); `/fast` with no args shows the current
state.

Whether a given signup actually goes through HTTP or the browser is decided
**once**, in `begin_signup()`, at the moment the session starts — not
per-message — since the site URL (which decides `supports_http_fast` via
`profile_for()`) is fixed for that session's whole life anyway:
`session.use_fast = fast_wanted and profile_for(session.site_url or
BOT_SITE_URL).supports_http_fast`. If `/fast` is ON but the resolved site
doesn't support it (spin24star), `begin_signup()` says so right in the
"send the phone number" prompt and falls back to the browser for that one
signup, same fallback behavior as the CLI.

`handle_message()`'s `await_phone`/`await_otp` branches each check
`session.use_fast` and dispatch to `_blocking_http_register()` /
`_blocking_http_verify_otp()` instead of `_blocking_fill_and_register()` /
`_blocking_verify_otp()` — both pairs return the same result-dict shape
(`ok`/`phone_taken`/`message`/`shot`, plus `digits` on a successful register),
so the rest of `handle_message()` (the phone-taken/failure/success reply
logic, `db.update_status()`, the continuous-loop auto-restart) doesn't need
to know or care which path ran. `shot` is always `None` for the fast path —
no browser, no screenshot — which `send_result_photo()` already handles by
falling back to a plain text message, so no changes were needed there.

**Load-bearing difference from the browser path: no thread-affinity
requirement.** `_blocking_fill_and_register()`/`_blocking_verify_otp()` MUST
run on `_pw_executors[session.slot]` (Playwright's sync API requires every
call for a given browser to happen on the thread that launched it — see the
module-level comment above `_pw_executors`). The HTTP-fast helpers touch no
Playwright object at all, so `handle_message()` dispatches them via
`loop.run_in_executor(None, ...)` (asyncio's default thread pool) instead —
meaning HTTP-fast signups don't consume, queue behind, or block a
`_pw_executors` slot at all, even if browser-based signups are running
concurrently on the same bot. `session.slot` is still assigned in
`begin_signup()` for a fast session (simpler than special-casing it there),
it's just never used.

State between the two HTTP calls (register, then OTP-verify) — the
`requests.Session`'s cookies and the CSRF token — lives on
`session.http_session` / `session.http_csrf`, the fast-path equivalent of
`session.context` / `session.page` for the browser path.
`_blocking_close_context()` resets both alongside `context`/`page` on every
`end_session()` call, whichever path was actually used (harmless no-op
reset for the one that wasn't).

Verified live 2026-07-19 by calling `_blocking_http_register()` then
`_blocking_http_verify_otp()` directly (bypassing Telegram itself): got the
real "OTP has been sent" response, then the real "Please enter valid OTP"
rejection for a dummy code — same round trip as the CLI's `--fast`, now
proven through the bot's own code path.

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

**Success and failure are both terse in chat, by design.** Neither outcome
pushes `send_result_photo()`/`build_caption()`/`send_csv()` into
`handle_message()` — a signup gets exactly one of `f"Signup successful!
(#{row_id})"` or `f"Signup failed. (#{row_id})"`, nothing else. The failure's
real reason (register-rejected message, WAF block, OTP error, etc.) is
**not** sent to chat — it's `logger.error()`'d to the console and stored in
`accounts.db`'s `notes`/`screenshot` columns via `db.update_status()`, same as
before; only the push-to-chat step was removed. This is deliberate (the admin
explicitly asked for it): credentials shouldn't land in the chat on every
failed attempt either, not just on success. `send_result_photo()` /
`build_caption()` / `send_csv()` still exist and are still used, just not
here — `/photo <id>` and `/export` are the master-only, explicitly-requested
ways to pull a screenshot+caption or a CSV for any stored account, success or
failure. Don't reintroduce them in `handle_message()` without checking this
was a deliberate choice, not an oversight.

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


- Per-site selectors + behavior live in **one profile file per site** under
  `sites/` (`sites/cricmatch.py`, `sites/spin24star.py`), each a `SiteProfile`
  (`sites/base.py`); the engine reads `profile_for(page.url).sel[...]` — there
  is no module-level `SEL` dict anymore (see "Multi-site support" below). If a
  site's markup changes and the script
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

The driver keeps **one shared engine** (`main.py`) and puts everything that
differs per site into **one profile file per site** under `sites/`. Each
`sites/<site>.py` exposes a `PROFILE = SiteProfile(...)` (`sites/base.py`)
holding that site's `sel` (selectors, single values) plus behavior flags:
`register_trigger` (`"modal"` vs `"forced_join"`), `has_terms_checkbox`,
`phone_taken_selector`, `result_selectors`, `tracking_param`,
`supports_casino`. `sites/__init__.py` registers them (`PROFILES`) and
`profile_for(url)` maps a URL's hostname to its profile (falling back to
`DEFAULT_PROFILE` = cricmatch for `None`/`about:blank`/unknown hosts, so no
call site crashes). Engine helpers resolve `prof = profile_for(page.url)` and
read `prof.sel[...]`; site selection is still purely by URL (`--url` /
`/seturl` / `BOT_SITE_URL`), no site flag. This replaced an earlier single
`SEL` dict of comma-joined cross-site groups (`"#userNameid, #userNameKhelo"`),
which didn't scale as sites diverged.

### Adding a new site (one-file-per-site workflow)

1. Capture its selectors: `.venv/bin/python inspect_form.py --url <newsite>`
   (the register form is JS-injected, so this drives the live page).
2. Copy `sites/spin24star.py` → `sites/<site>.py`; set `hostnames`, fill `sel`,
   and the behavior flags; register it in `sites/__init__.py`'s `PROFILES`.
3. `cp .env.spin24star.example .env.<site>`; set its `TELEGRAM_BOT_TOKEN`,
   `BOT_SITE_URL`, and **distinct** `ADMINS_FILE` / `SETTINGS_FILE` /
   `PAIRS_FILE` / `PAIR_RUNS_FILE` (two processes must not share these).
4. `.venv/bin/python telegram_bot.py --env .env.<site>` — that bot now runs
   only that site, no engine edits. (`supports_casino` defaults to False, so
   the casino/hedge commands refuse cleanly until you inspect + wire that
   site's login/casino selectors and flip it on.)

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
- **cloudscraper was tried and doesn't work, don't re-try it.** It's built to
  solve Cloudflare's own JS challenge and has no real JS engine — but
  Cloudflare here is only the CDN in front; the actual block is AWS WAF, which
  only issues a token to a client that executes its `challenge.js` in a real
  browser. Tested live: a `cloudscraper` GET got a normal 200 + valid CSRF
  token, but the POST to `/sign-up` came back a flat 403 (no token acquired at
  all). Playwright is the right tool here specifically *because* it's a real
  browser; anything that isn't one is a step backwards for this particular
  block.
- The block is **behavioral/rate-based, not fixed.** Tested live from a clean
  residential IP (`ip-api.com` confirms `proxy:false, hosting:false`): a fresh
  browser got 200 on every attempt; after several rapid signups in a row from
  the same IP, every subsequent attempt became 405 captcha, and continuing to
  hammer it escalated some attempts to a flat 403 (no CAPTCHA offered at all —
  CapSolver can't help with that one, there's nothing to solve). This is why
  pacing signups and rotating proxies matters even with CapSolver wired up:
  CapSolver handles the 405/captcha state, not the 403 hard-block state.

Getting past this needs one of: (a) the **site owner exempts** the register
endpoint or a test IP/header from the WAF CAPTCHA rule (cleanest, since this
is the owner's own QA per the Purpose section), or (b) a **CAPTCHA-solving
service** — which is now integrated (CapSolver, see below). Do not "fix" this
in the driver by fiddling selectors or waits; the request is rejected at the
edge before the app ever sees it.

#### CapSolver integration (auto-solving the WAF CAPTCHA)

Set `CAPSOLVER_API_KEY` in `.env` (both the bot and the CLI load it via
`load_dotenv()`; `main.capsolver_key()` reads it lazily so import order
doesn't matter). With no key set, everything below is skipped and a WAF block
is just reported as a clean failure — so cricmatch and key-less runs are
unaffected.

The whole flow lives in `main.py` and is shared by the CLI (`signup_once`)
and the bot (`_blocking_fill_and_register`) through **`submit_register(page,
acct, site_url, proxy)`** (note: no `context` param — it's derived from
`page.context`, since a WAF retry may replace both; see below):

1. `click_register_and_wait()` clicks REGISTER and captures the same-origin
   register POST's response (`{"response","action","body"}`), filtering out
   `token.awswaf.com` telemetry noise.
2. If the outcome is error/timeout, `is_waf_captcha(captured)` (header
   `x-amzn-waf-action: captcha|challenge`, or a `gokuProps` body), and a key is
   set: `parse_aws_waf_challenge()` pulls `key`/`iv`/`context` + `challenge.js`
   from the "Human Verification" page's inline `window.gokuProps`, and
   `solve_aws_waf_token()` hands them to CapSolver (`AntiAwsWafTask`, proxy
   passed so the solve happens from the signup's own egress IP).
3. **The retry opens a brand-new browser context**, injects the solved token
   into it via `apply_waf_token()`, closes the old (challenged) context, and
   resubmits there — deliberately NOT a reload in the original context/page.

**Why a new context, not a reload in place (root-caused live, 2026-07-13):**
injecting a valid, freshly-solved token into the SAME context that triggered
the CAPTCHA still returns 405 on the next request — verified by solving a
real challenge, injecting the token into the original context, and reloading:
still blocked. Injecting the *identical* token into a brand-new context
instead: a plain homepage GET immediately returns 200 with real site content.
So AWS WAF is tracking something beyond the token cookie (almost certainly
tied to `AWSALB`/session-level state) against that specific context, and no
cookie swap clears it — only a fresh context does. `submit_register()`
therefore returns `(outcome, msgs, captured, page)`, where `page` is a *new*
Page/context on the WAF-retry path and callers **must** switch to it:
- `signup_once()` reassigns its local `page` and stashes it in
  `result["page"]` so `main()`'s per-account loop closes the actually-live
  context instead of double-closing the already-closed original (or leaking
  the new one) — see the `finally` block in the CLI's per-account loop.
- `_blocking_fill_and_register()` reassigns its local `page` and resyncs
  `session.context, session.page = page.context, page` immediately after the
  call, so `_blocking_verify_otp()` and `_blocking_close_context()` (both
  read `session.page`/`session.context`) operate on the surviving context.

`fill_register_form()` (the 4 fields + T&C) was extracted so the initial fill
and the post-solve refill can't drift. `wait_for_register_outcome()` returning
`(outcome, msgs)` matters here too — a snackbar/toast is read at detection
time, before it auto-dismisses.

**Verified end-to-end live** (2026-07-13, funded key): a real spin24star
signup that hit the WAF CAPTCHA was solved, the fresh-context retry reached
the real OTP screen (`digits: 6`), and session cleanup closed without error.
cricmatch (no WAF, no CapSolver involvement) regression-checked clean after
the signature change.

## Casino game smoke test (login + place a Baccarat bet)

A separate feature from signup: `login()` / `open_casino_lobby()` /
`search_and_open_game()` / `place_baccarat_bet()` / `test_baccarat()` in
`main.py`, and `/testbaccarat <username> <password> [amount]` in
`telegram_bot.py` (master-only, mirrors `/testproxy`'s "share slot 0,
throwaway context, always clean up" pattern). Logs into an **existing**
account (not a freshly-generated one -- credentials are explicit args, not
looked up from `accounts.db`) and places a real bet on both Player and
Banker in a live Baccarat table, to confirm the third-party casino game
integration itself works, not just that the site loads. Doesn't write to
`accounts.db` -- different data lifecycle than the rest of this file (it
tests an account someone already has).

**Verified live only against cricmatch247** (2026-07-16, real account, a
real ₹100 bet on Player confirmed placed and read back via the game's own
UI). spin24star is not covered at all -- the new `SEL` keys
(`open_login`/`login_username`/`login_password`/`login_submit`/
`logged_in_indicator`/`casino_nav`) are single cricmatch247 values, not the
usual comma-joined cross-site groups the signup selectors use.

Key facts established live, so nobody re-guesses this:
- Login: click `a.cls_loginbtn` → fill `#user_login_id` / `#passwordId` →
  click `#loginbutton`. A logged-in session shows `#acctSec` (an "Account"
  link) in the header; that's the success indicator `login()` polls for.
- Casino nav: `a:has-text('Live Casino')`, then a category filter tab
  `a:has-text('Baccarat')` (there is no free-text game search box, only
  category tabs) -- both clicks must be **forced**, since cricmatch247 shows
  the same SPRIBE/Aviator walkthrough overlay documented for spin24star
  under Multi-site support above (`.skip_right_img`), which intercepts
  plain clicks on the nav the same way.
- Opening a game tile (e.g. `text=Baccarat A`) opens a **brand-new browser
  tab**, cross-origin at `ezugi.evo-games.com` (Evolution/Ezugi) -- the game
  is never embedded in the cricmatch247 page itself. Callers must track
  `context.pages` for the new tab and eventually close it separately
  (`test_baccarat()` does this in a `finally`).
- The bet table itself is a `<canvas>` video feed, but the **Player/Banker
  bet spots are real DOM elements**, not canvas-drawn -- confirmed by
  successfully reading the game's own "TOTAL BET" counter go from 0 to 100
  after a real click. This was the single biggest open risk going in (most
  live-dealer providers render everything on canvas/WebGL) and it did NOT
  materialize here; no coordinate-based clicking was needed.
- Bet-spot targeting is still the fragile part. Element class names are
  hashed/dynamic (e.g. `B5xqBh`, `Lnk7iq`) and not usable directly. Worse:
  the game's *collapsed* paytable/bet-limits tooltip contains the literal
  text "BANKER" (and every other spot's label) even while hidden, and a
  naive "find any element whose text matches the label" search can
  mistarget it -- this happened live and the resulting click bounced the
  page out to the general Evolution game lobby instead of placing a bet
  (caught safely: no money moved, see below). `main.py`'s
  `_TAG_BET_SPOT_JS` fixes this by excluding any element inside
  `[data-role*="bet-limits"]` / `[data-role*="tooltip"]`, plus anything
  off-screen, zero-sized, or larger than a small label box, before picking
  the smallest remaining match. The fix was applied but **not yet
  re-verified live** for the Banker side specifically (Player-side targeting
  was verified live and worked; live testing was paused by the user before
  a full Player+Banker round could be re-run against the hardened version).
- A decorative SVG "glow" overlay sits on top of the real bet-spot div,
  so `frame.locator(...).click()` needs `force=True` -- same
  "subtree intercepts pointer events" trap as the Khelo REGISTER button and
  the SPRIBE overlay elsewhere in this file.
- **Chip denomination is not selectable by this code.** Clicking a bet spot
  places whatever chip the game UI currently has pre-selected (observed
  live: this defaults to the table minimum). `amount` is therefore
  advisory -- `place_baccarat_bet()` never trusts it blindly; it reads the
  game's own "TOTAL BET" counter after each click and refuses to proceed
  (or reports a mismatch) if the actual placed amount doesn't match.
- **Table minimum is ₹100 per side** on both "Baccarat A" and "Baccarat B"
  (the only two live tables under cricmatch247's Baccarat category) --
  confirmed via the in-game "BET LIMITS" panel, twice. `/testbaccarat`
  defaults `amount` to 100 for this reason. A round-window retry loop
  (`place_baccarat_bet`'s `round_attempts`) exists because a click during
  the results/reveal phase between rounds is a silent no-op -- Evolution
  only accepts new bets during the live betting countdown.
- **Confirmed live: leaving the table before a round's betting timer
  expires voids any staged-but-unsubmitted chip placement, at no cost.**
  Evolution stages chip clicks client-side and only submits them to the
  server when the betting countdown naturally ends. During live testing, a
  mistargeted click navigated away from the table with a ₹100 Player chip
  already "placed" in the UI; the site's own wallet (`MY WALLET` /
  `EXPOSURE` in cricmatch247's header, independent of the Evolution iframe)
  confirmed afterward that balance and exposure were both unchanged from
  before the test began. Don't rely on this as a safety net going
  forward, though -- it's an artifact of leaving *before* the timer ends,
  not a guarantee; a bet that fully registers (like the confirmed Player
  100 in the same session) is real money, same as any other bet on the
  site.

## Paired-account hedge betting (`/pair`, `/pairs`, `/run`, `/stoprun`, `/runs`, `/runlog`)

A second, higher-level casino test built on the same engine: two accounts on
the **same live baccarat table** bet opposite sides (one Banker, one Player)
of the **same hand** each round. Because both bets ride one result, money
mostly just moves between the two accounts — only the ~5% banker commission
bleeds out on a Banker win — so you can generate large, controlled betting
volume to smoke-test the platform without draining balance fast.

Bot commands (all **master-only**, `@require_role(is_master)`):
- `/pair <user1> <pass1> <user2> <pass2>` — store a pair; **acc1 always bets
  Banker, acc2 always Player** (fixed). Returns a numeric pair id. Replies
  never echo passwords.
- `/pairs` — list stored pairs (id, banker username, player username, created);
  passwords omitted.
- `/delpair <id>` — remove a pair.
- `/run <pair_id> <amount> <rounds>` — log both accounts in, join the same
  table, and each round place `amount` on Banker (acc1) and `amount` on Player
  (acc2) on the same hand, until `rounds` is reached, either balance `< amount`,
  a round goes unhedged, or `/stoprun`. Streams per-round progress to the chat,
  each line prefixed `[Pair #<id>]` so concurrent runs stay distinguishable.
  **Multiple different pairs can run at once** (see "Concurrent runs" below);
  a pair already running, or one sharing an account with a running pair, is
  refused a second `/run` until it stops.
- `/stoprun [pair_id]` — stop one run after its current round, or with no
  argument, every currently-active run.
- `/runs [pair_id]` — list past runs (most recent first; all pairs, or one
  pair). Each line shows run id, pair id, both usernames, `rounds_done/
  requested`, amount, stop reason, and net balance change per side.
- `/runlog <run_id>` — the per-round balance progression of one past run
  (start balance → each round's B/P balance → final + net), plus any messages.

Persistence: two gitignored JSON files, both per-instance (env-overridable like
`ADMINS_FILE`/`SETTINGS_FILE`):
- **`pairs.json`** (override `PAIRS_FILE`) — the pair credentials. Holds
  **plaintext passwords**, gitignored via `pairs.json` / `pairs.*.json`.
  Structure: `{"next_id": N, "pairs": {"<id>": {"banker": {...},
  "player": {...}, "created_at": iso}}}`.
- **`pair_runs.json`** (override `PAIR_RUNS_FILE`) — the run history that
  `/runs`/`/runlog` read. **No passwords** (usernames + balances only), but
  still gitignored (`pair_runs.json` / `pair_runs.*.json`) since it's the
  owner's operational betting data. Structure: `{"next_id": N, "runs": [
  {run_id, pair_id, banker_username, player_username, amount,
  requested_rounds, rounds_done, stop_reason, started_at, ended_at,
  start_balance, final_balance, rounds:[{round,amount,banker,player}],
  messages, shots} ]}`. One record is appended by `run_cmd` after **every**
  `/run` (success or any stop reason), then `save_pair_runs()`. The per-round
  `rounds` list and `start_balance`/`ended_at` all come from the
  `run_paired_hedge` summary (`main.py`), which was extended to record them —
  don't drop those keys, `/runlog` reads them.

Engine: `run_paired_hedge(banker_creds, player_creds, amount, rounds,
site_url, progress, should_stop, browser=None)` in `main.py` reuses `login()` /
`open_casino_lobby()` / `search_and_open_game()` / `find_game_frame()` /
`wait_for_live_table()` / `_click_bet_spot()` / `_read_total_bet()`, plus new
`read_game_balance(frame)` (reads the Evolution frame's own
`data-role="balance-label-value"` readout, e.g. `₹1,891`) and `_open_table_for`
/ `_table_id` helpers. Key facts, all money-relevant:

- **Accounts with a bonus balance launch the game differently (confirmed
  live 2026-07-19).** New accounts carry bonus chips, so clicking a game tile
  pops a "CHOOSE CHIPS: bonus or real" gate. Two traps found live: the
  "REAL CHIPS" *label* has no click handler — the clickable element is
  `div.cls_play_act_bal.redirectLink` (the red amount button) — and choosing
  it navigates the *same tab* to the provider (`vt_id=` in the URL) instead
  of opening a new tab (`table_id=`). `_dismiss_choose_chips_modal()`,
  `search_and_open_game()` (returns the same Page on this path), and
  `_table_id()` all handle this now. Accounts with no bonus (ali789/asha788)
  never see the gate and keep the old new-tab flow — which is why runs on
  pair 1 worked while every fresh-account pair failed with "could not open
  the table" (and on throttled accounts, the session-dropped message).
  Untested edge: a pair where only ONE account has bonus chips would get
  `vt_id` on one side and `table_id` on the other, and the same-table check
  would abort (safely, no bets) even if both are on the same table.
- **Same physical table is required and confirmed.** Both accounts opening
  "Baccarat A" land on the same Evolution `table_id` (`oytmvb9m1zysmc44`,
  extracted from the game-tab URL). `run_paired_hedge` compares both tabs'
  `table_id` and aborts before any bet if they differ — otherwise the two bets
  wouldn't be on the same hand and it isn't a hedge.
- **Both bets go down back-to-back in one open window**, same fix as
  `place_baccarat_bet` (a >1s gap loses the window). Since the setup
  parallelization below (2026-07-19), the Banker context lives on the
  caller's thread and the Player context lives on its own thread
  (`player_exec`) — the Player click is submitted to `player_exec` first
  (non-blocking), then the Banker click runs inline on the calling thread,
  then the caller joins on the Player future. This fires both clicks
  genuinely concurrently on two OS threads rather than sequentially on one,
  and is at least as tight a window as the old same-thread back-to-back
  calls. Every other round-loop read that touches the Player side
  (`_betting_open`, `_read_total_bet`, `read_game_balance`, `_table_id`) is
  dispatched the same way: submit the Player-side call to `player_exec`
  first, do the Banker-side call inline, then `.result()` the Player future
  — so paired reads run concurrently too, not just the bet clicks.
- **Partial round → stop immediately** (deliberate choice): if only one side's
  bet lands (unhedged real exposure), the run halts, names the exposed account,
  and screenshots both tabs (`shots/hedge-partial-*.png`).
- **Setup (login → table-live) runs the two accounts in parallel on two
  threads/browsers (added 2026-07-19; previously sequential on one thread —
  see git history if you need the old single-thread version).**
  `run_paired_hedge` launches a **second, temporary** Playwright browser +
  single-worker `ThreadPoolExecutor` (`player_exec`, via `_launch_pw_browser`)
  for the Player side; the Banker side either reuses a caller-supplied
  `browser` (an optional param, kept for ad-hoc/test callers) or, the bot's
  normal path since concurrent runs shipped, launches its own temporary
  browser the same way via `_launch_pw_browser`, inline on whichever thread
  is running this call (see "Concurrent runs" below) — the two sides never
  share a browser or thread either way. `_open_table_with_retry`
  for the Player side is `player_exec.submit(...)`'d *before* the Banker call
  runs inline, so both accounts' login → casino lobby → join table → wait for
  live all happen concurrently — roughly halving the old 2-4 minute setup to
  close to the slower of the two accounts alone, not the sum. If either side
  fails or `/stoprun` fires mid-setup, whichever side already succeeded is
  closed on its own owning thread before the error propagates (see the
  nested try/except in `run_paired_hedge` — get this right if you touch it,
  it's easy to leak a context or double-close across threads). Each account's
  setup is still a real login + a real live-video game load, routed through a
  residential proxy when one is set — easily 1-2+ minutes per account even
  run in parallel, so `/run` still isn't instant. `_open_table_for()` takes
  `progress(str)` and reports one line per phase (🔑 login, 🎰 casino lobby,
  🃏 joining the table, 📡 waiting for it to load, ✅ ready) for each account,
  called from whichever thread owns that account, both funneling into the
  same chat via `progress`'s `run_coroutine_threadsafe` bridge (thread-safe to
  call from two threads concurrently) — don't drop these calls if you touch
  this function. `open_casino_lobby()`'s poll loop checks the lobby's own
  visibility BEFORE paying `dismiss_popups()`'s wait (only falling back to it
  if not yet visible), shaving up to ~1.3s per loop iteration on the common
  (already-open) path — a real, safe trim; the other fixed sleeps in this
  setup chain are left alone since several were confirmed-live load-bearing
  (see `signup_once`'s post-`goto` sleep note) and weren't re-verified safe to
  touch here. The temporary Player browser + `player_exec` are torn down in
  `run_paired_hedge`'s `finally`, on every exit path (success, any stop
  reason, or an exception) — never skip that cleanup if you touch the
  function, or a Chromium + Playwright driver process leaks per run.
  **Not yet verified live** — needs a real second account to confirm the
  concurrent setup and the parallel round-loop reads/clicks behave the same
  as the old sequential version did; test with `/run <id> 100 1` first.
- **v1 does NOT select a chip denomination.** It bets the table's default chip
  (the minimum, ~₹100 on Baccarat A) and verifies the actual size via each
  side's TOTAL BET. If `amount` doesn't match what the table placed, it stops
  after **one** (hedged, safe) round and tells you the real size to re-run with
  (`amount_mismatch`). Arbitrary chip selection is a future enhancement — the
  selectable chip rail is complex SVG (the `data-role="chip"` nodes found were
  hidden 0-value templates), so it was deliberately deferred rather than
  guessed at with real money.
- **Concurrent runs (multiple pairs at once), added 2026-07-19.** Each `/run`
  is fully self-contained — `run_paired_hedge` launches its OWN temporary
  Banker browser (via `_launch_pw_browser`, `browser=None` default) in
  addition to the temporary Player browser it already launched — so two
  different pairs' runs share no browser, thread, or Playwright object with
  each other, or with regular signups on `_pw_executors`. `telegram_bot.py`
  dispatches each `/run` onto `_run_executor`, a module-level
  `ThreadPoolExecutor(max_workers=MAX_CONCURRENT_RUNS)` (env-overridable,
  default 3) — as many `/run`s as there are free workers actually run in
  parallel; a `/run` beyond that queues on the executor rather than being
  rejected outright, though `run_cmd` also refuses new `/run`s once
  `len(_active_runs) >= MAX_CONCURRENT_RUNS` so the reply is immediate rather
  than a silent queue wait. `_active_runs` (module-level dict, `pair_id ->
  {"stop_event", "banker", "player"}`) replaced the old single `_run_active`
  bool / `_run_stop` Event — each run gets its own `threading.Event`, so
  `/stoprun <pair_id>` stops just that one and a bare `/stoprun` stops every
  active run. `run_cmd` refuses a second concurrent `/run` for a pair already
  in `_active_runs`, **and** refuses a pair that shares either account
  (username) with any other currently-active pair — betting the same login
  from two contexts at once would corrupt both runs' hedge, not just add
  parallelism. `/pairs` shows a `🏃 running` tag next to an active pair, and
  `/delpair` refuses to remove one mid-run. Every `progress()` line for a run
  is prefixed `[Pair #<id>]` (set in `_blocking_run_pair`) since concurrent
  runs' messages land in the same chat interleaved. **Not yet verified live
  with two pairs running simultaneously** — needs two real pairs (four
  accounts) to confirm the parallel-runs path; the underlying per-run engine
  (`run_paired_hedge` with `browser=None`) is otherwise identical to the
  already-verified single-run path, just without a pre-warmed slot-0 browser.
- **Progress from the worker thread → chat** uses
  `asyncio.run_coroutine_threadsafe(bot.send_message(...), loop)` (the loop is
  captured in `run_cmd` and passed into `_blocking_run_pair`) — a new
  thread→async bridge; the rest of the bot only ever sends before/after
  `run_in_executor`, not mid-blocking-call.

**Verified**: read-only checks confirmed the same-`table_id` assumption,
`read_game_balance`, and the loading-screen/window timing on a live table
(no money). The full paired placement needs a **second** real account (only
`asha788` was on hand) and spends real money hedged; test with `/run <id> 100 1`
first, then scale.

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

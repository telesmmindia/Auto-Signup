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

### Freeing the signup phone number (on by default; `--no-free-number` to disable)

Right after a signup's OTP is verified (cricmatch247 only), swap the
brand-new account onto a random throwaway phone number, so the **real** phone
number just used to receive the actual SMS OTP is freed up and can be entered
again for the next signup in the same run — useful when you only have one
real SMS-capable number and want to generate many test accounts against it in
a loop, instead of needing a fresh real number per signup.

**On by default** (`args.free_number` defaults to `True`, via the
`--free-number`/`--no-free-number` pair of `store_true`/`store_false`
`argparse` actions sharing one `dest`) — pass `--no-free-number` to keep a
signup's real phone number registered on the account instead of swapping it
out. This differs from `--fast`, which defaults off.

The real endpoint is `POST https://cricmatch247.com/send_otp_touser` with
body `_token=<csrf>&phone=<new_number>` on an **authenticated** session —
found live via manual Tamper Dev request interception (redirecting an
in-flight `getBalance` call to this path instead) against a real cricmatch247
account. **Confirmed live end-to-end 2026-07-22**: called against a real
account (`kabirdas4250`), then confirmed by reloading its Account Details page
that the Mobile Number field actually changed (`7566637976` →
`9226389176`), with **no OTP re-entry required**.

Two non-obvious things were required to get a clean response, both found by
trial and error against the real account, not guessed:
1. **The path is `/send_otp_touser`, not `/send_otp`.** An earlier version of
   this guessed `/send_otp` — misread off a small phone-screenshot of the
   interceptor UI, which visually truncates the URL field at "send_otp" while
   the real "_touser" suffix sits scrolled out of view. That guess was
   confirmed wrong live first (a hard 405, "Supported methods: GET, HEAD" —
   Laravel's routing layer rejecting it before any auth/CSRF check even
   runs), which is what proved a re-check was needed rather than trusting the
   video frame.
2. **The call needs a "settled" authenticated session, not a freshly-logged-in
   one.** Calling it immediately after login gets a generic 500
   (`{"message":"Server Error"}`), even with byte-for-byte identical
   headers/body to a real captured request. The real session had cookies
   (`domain_switch`, `screenwidth`, and Laravel-encrypted `username`/
   `password` cookies) that only appear after the login flow's own follow-up
   calls finish — confirmed by transplanting a real, settled browser
   session's cookies into a plain `requests.Session` and getting the same
   clean 200 that way too, so this isn't a "must be a real browser" quirk,
   just a "must wait for the session to finish settling" one.

Implementation (mirroring how CapSolver/WAF-retry logic is shared — see
`sites/base.py`'s `supports_free_number` flag, `True` only for cricmatch247,
and `free_number_path`, `"/send_otp_touser"`):
- **Browser path — CONFIRMED WORKING LIVE.** `free_phone_number(page,
  site_url)` in `main.py`, called from `signup_once()` right after
  `enter_otp()` returns a success result. Waits ~4s (the "settle" margin —
  in the real signup flow the account already went through register+OTP-
  verify by this point, which naturally takes a while, so this is a safety
  margin more than a hard requirement) then fires the request as a real
  in-page `fetch()` via `page.evaluate()` — **not** `page.context.request`,
  which kept 500ing even with an identical URL/body/headers during
  investigation (a separate, out-of-band HTTP client that doesn't carry
  whatever else a real in-page request does). Judges success the same way
  `http_is_error()` does (via the response's `message_class`), not just HTTP
  status.
- **Retries up to `FREE_NUMBER_MAX_ATTEMPTS` (15) times, waiting
  `FREE_NUMBER_RETRY_COOLDOWN_SECS` (45s) between each — ~10.5min worst case —
  for ANY failure, not just one specific error.** Went through three rounds
  of widening, all driven by real failures:
  1. Originally retried once, specifically for a post-OTP-verify redirect
     (the site's own JS navigating the page) landing right around the call
     and killing the execution context mid-call ("Execution context was
     destroyed, most likely because of a navigation"). Confirmed live this
     wasn't enough: a real continuous run hit `phone_taken` on its *next*
     signup because one round's free-number call failed for some other
     reason and was never retried, leaving the real number still attached to
     that earlier account — invisible until the following round broke, since
     success/failure here isn't shown in the bot's terse chat replies.
  2. Widened to 4 attempts / 10s apart (~40s) to cover that. Still not
     enough: a real run then hit a bare **403 Forbidden** (no JSON body,
     unlike the app-level 500 case) on the free-number call — an
     edge/WAF-level block from calling this endpoint too rapidly, not an
     application error, and it needs meaningfully longer to clear. Widened
     again to 10x20s (~3.3min) so a signup has a real chance to ride out a
     short rate-limit window rather than giving up early and letting the next
     signup in the run collide with a still-taken number (defeating the
     whole point of this feature).
  3. Still not enough: a real `/freenum` call (see below) hit the same bare
     403 and exhausted the entire 10x20s (~3.3min) budget without the block
     clearing. Widened again to the current 15x45s (~10.5min) on the theory
     that this specific edge-level block needs a longer cooldown than a
     transient app error, not just more attempts at the same short interval
     — **not yet re-confirmed live against a fresh 403** (the failing call
     that prompted this widening had already given up before the change
     landed).

  Both `free_phone_number()` and `http_free_phone_number()` retry the same
  way regardless of failure cause (exception, 500, 403, or a rejected
  `message_class`), reporting `"Free-number FAILED: gave up after 15
  attempts: ..."` only once the whole budget is exhausted.
- **`--fast` HTTP path — NOT CONFIRMED, likely still broken.**
  `http_free_phone_number(session, csrf_token, site_url)` in `main.py`, same
  call site convention as the browser path (including the same retry loop
  above). Uses the corrected path/headers, but a `--fast` signup's
  `requests.Session` never runs any client JS and so likely never acquires
  the `domain_switch`/`screenwidth`/`username`/`password` cookies the real
  fix turned out to need — meaning it may well hit the same generic 500 the
  browser path did before those cookies existed, attempt after attempt.
  Treat a `"Free-number FAILED: gave up after 15 attempts: HTTP 500"` here as
  an open question, not a regression, until someone runs a real `--fast`
  signup with free numbers on and checks.
- Both generate the new number via `gen_free_phone()` (a random 10-digit
  Indian-format mobile, first digit 6-9) and return `(ok, new_phone,
  message)`; the caller appends a `"Free-number: ..."` /
  `"Free-number FAILED: ..."` line to `result["messages"]` and sets
  `result["freed_phone"]` (only on success) rather than failing the whole
  signup if this step doesn't work — the account itself already registered
  successfully by this point, so a free-number failure is reported
  separately, not conflated with signup failure.
- `db.py`'s `accounts` table gained a `freed_phone` column (migrated in like
  `proxy`/`url`/`referral_code` were) via `db.update_freed_phone(conn, row_id,
  freed_phone)`, called from `main()`'s per-account loop whenever
  `res.get("freed_phone")` is set. `phone` keeps the **original**,
  OTP-verified signup number for the historical record (that's what actually
  received the real SMS) — `freed_phone` records what the account's mobile
  number was switched to afterward, so both are visible in `--list`/`--export-csv`
  without losing either fact.
- The free-number step is a no-op alongside `--no-submit` implicitly (not an
  explicit error, just unreachable code): both `signup_once()` and
  `http_signup_once()` return early on `--no-submit` before ever reaching a
  successful OTP verify, so it never fires in that mode regardless of the
  `--free-number`/`--no-free-number` setting.

**The account's Account Details page has no self-service "change mobile
number" UI at all** (confirmed live by dumping the real, authenticated page
DOM) — no edit icon, no change link, just a static "Verified" badge next to
Mobile Number that does nothing when clicked (no request fires). This
mechanism is not something a human user is expected to trigger through the
normal site UI; it's an internal endpoint reachable only by crafting the
request directly, same as this whole file's `--fast` HTTP-only signup path
already does for registration.

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

### Running one bot per site / per role (`--env` + `BOT_MODE`)

`telegram_bot.py` supports an optional `--env <path>` CLI flag so the same
script can run as two (or more) independent bot processes, one per site,
instead of one bot juggling both via `/seturl`. This gives each site its own
bot identity/token in Telegram and, more importantly, its own worker
thread/browser — signups for different sites no longer serialize on the
single shared `_pw_executor`.

The current production layout is cricmatch signup (`.env.cricmatch`,
`BOT_MODE=signup`), spin24star signup (`.env.spin24star`, `BOT_MODE=signup`),
cricmatch gameplay (`.env.gameplay`, `BOT_MODE=gameplay` — the casino/hedge
commands only), cricmatch stock market hedge (`.env.stockmarket`,
`BOT_MODE=stockmarket`), and cricmatch password-change (`.env.password`,
`BOT_MODE=password` — `/cp` only, since 2026-07-30).

```
cp .env.cricmatch.example .env.cricmatch
cp .env.spin24star.example .env.spin24star
cp .env.gameplay.example .env.gameplay
cp .env.stockmarket.example .env.stockmarket
cp .env.password.example .env.password
# edit each: a DIFFERENT TELEGRAM_BOT_TOKEN (one @BotFather bot per process),
# BOT_SITE_URL, BOT_MODE, and distinct ADMINS_FILE / SETTINGS_FILE paths
.venv/bin/python telegram_bot.py --env .env.cricmatch
.venv/bin/python telegram_bot.py --env .env.spin24star    # separate terminal/tmux pane
.venv/bin/python telegram_bot.py --env .env.gameplay      # separate terminal/tmux pane
.venv/bin/python telegram_bot.py --env .env.stockmarket   # separate terminal/tmux pane
.venv/bin/python telegram_bot.py --env .env.password      # separate terminal/tmux pane
```

`BOT_MODE` (`signup` | `gameplay` | `stockmarket` | `password` | `all`,
default `all` so a plain `.env` single-bot setup is unchanged) controls which
command set an instance exposes, enforced at handler-registration time in
`main()` — an out-of-mode command simply doesn't exist on that bot (Telegram
ignores it; there's no "wrong bot" reply), and the per-role "/" menus
(`ADMIN_COMMANDS`/`MASTER_COMMANDS`) plus `/start`'s help text are built
from the same `SIGNUP_ENABLED`/`GAMEPLAY_ENABLED`/`PASSWORD_ENABLED` flags so
they never advertise a command the instance doesn't have. The split:
- **signup**: `/newacc` `/done` `/cancel`, data (`/list` `/photo` `/export`
  `/stats`), `/setpassword` `/password` `/fast`, URL/btag commands.
- **gameplay**: `/testbaccarat`, `/pair` `/pairs` `/delpair`, `/run`
  `/stoprun` `/runs` `/runlog`. All master-only, so an admin on a
  gameplay-mode bot has nothing to run — `/start` tells them so. URL
  commands are excluded on purpose: gameplay always targets `BOT_SITE_URL`
  directly, `/seturl` never affected it.
- **password**: `/cp` only, master-only, its own exclusive mode
  like `stockmarket` (deliberately NOT part of `"all"`, so a plain single-bot
  setup never accidentally exposes a command that mutates a real account's
  login credential) — see `/cp`'s own section above.
- **both/all modes**: `/start` `/help`, proxy commands (`/setproxy` `/proxy`
  `/clearproxy` `/testproxy` — hedge runs route through the global proxy
  too), and admin management. The `handle_message` phone/OTP text handler is
  only registered in signup modes. Browser-slot warmup stays in both
  (gameplay's `/testbaccarat`/`/testproxy` use slot 0).

The real `.env.gameplay` points `PAIRS_FILE`/`PAIR_RUNS_FILE` at the
pre-split `pairs.cricmatch.json`/`pair_runs.cricmatch.json` on purpose —
gameplay moved off the cricmatch signup bot and took its pair/run history
with it (the signup bot no longer registers those commands, so nothing else
reads the files). Its `bot_settings.gameplay.json` was seeded as a copy of
`bot_settings.cricmatch.json` so the working proxy carried over. Each mode
being a separate process also means the gameplay bot's `/setproxy` is
independent of the signup bots' — set it on each bot it should apply to.

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
  `save_admin_ids()`). Can run `/newacc`, `/done`, `/cancel`, and the
  `/setphone` family (`/setphone` / `/addphone` / `/delphone` / `/phone`) —
  the phone-pool commands are the one exception to "admin = signup-only,"
  and an admin's own pool is exclusive to them, not shared with other admins
  or the master (see "`/setphone`: rotating a pool of real phone numbers
  across signups" below).
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

### `/freenumber`: freeing the signup phone number (bot side)

Same feature as the CLI's free-number step (see the "Freeing the signup phone
number" section above), wired into the bot's chat-driven flow the same way
`/fast` is, except **on by default**: `/freenumber on` / `/freenumber off`
sets `global_settings["free_number"]` (persisted via `save_settings()`,
global across every admin's `/newacc`, not per-chat); `/freenumber` with no
args shows the current state. Reading it always goes through
`global_settings.get("free_number", True)` (note the `True` default, unlike
`use_fast`'s plain `global_settings.get("fast")`) so an untouched/fresh
`bot_settings*.json` behaves as ON — only an explicit `/freenumber off`
turns it off, and that choice persists across restarts same as any other
`global_settings` key.

Decided once per session in `begin_signup()`, same lifecycle as
`session.use_fast`: `session.free_number = free_number_wanted and
prof.supports_free_number`. If free-number mode is (implicitly or
explicitly) ON but the resolved site doesn't support it (anything but
cricmatch247), `begin_signup()` says so right in the "send the phone number"
prompt (a `🔓` tag joins the `⚡` fast-mode tag there) and just skips that step
for the signup, same fallback pattern as `/fast`.

`_blocking_verify_otp()` (browser path) and `_blocking_http_verify_otp()`
(HTTP-fast path) both check `session.free_number` right after a successful
OTP verify and, if set, call `free_phone_number(session.page, ...)` /
`http_free_phone_number(session.http_session, session.http_csrf, ...)` —
appending a `"Free-number: ..."` / `"Free-number FAILED: ..."` note to the
success message and returning `"freed_phone"` in the result dict.
`handle_message()`'s `await_otp` branch persists it via
`db.update_freed_phone(conn, session.row_id, result["freed_phone"])`
alongside the existing `db.update_status()` call, whenever it's set.

Both `_blocking_verify_otp()` and `_blocking_http_verify_otp()` call the same
`free_phone_number()`/`http_free_phone_number()` functions the CLI uses (see
the "Freeing the signup phone number" section above for what's confirmed live
vs. not) — the browser path is confirmed working (real before/after mobile
number change on a real account), the `--fast` path is not yet confirmed and
may still 500 for the reason described there (missing login-only cookies).

### `/freenum`: freeing the phone number on an EXISTING account, on demand

A different entry point into the same mechanism, for a different situation:
`/freenumber` (above) frees a number automatically right after a signup's own
OTP verify. `/freenum <username> <password>` instead logs into an account you
already have (any account, not one this run just created) and frees its
current number on demand — e.g. an old account whose real number you want to
reuse for future signups, without re-running that signup.

Master-only, same restricted scope as `/testbaccarat` (takes another
account's credentials as a chat argument, spends no money but does mutate a
real account). `main.free_account_number(page, username, password, site_url)`
reuses `login()` (so it needs `supports_casino`'s login selectors — cricmatch
only, same requirement `/testbaccarat` has) then calls the same
`free_phone_number()` the automatic path uses, returning the same
`{"ok","messages","shot","freed_phone"}` shape as `test_baccarat()`.
`_blocking_free_number()` in `telegram_bot.py` runs it on `_pw_executors[0]`
with a throwaway context, mirroring `_blocking_test_baccarat()`'s pattern
exactly (including the global proxy and bridge cleanup).

**Verified live end-to-end 2026-07-24** against ali789: logged in, freed the
number, and the account's mobile number changed (confirmed `9660164029` was
the new number returned). A later real call on a different account hit the
bare-403 edge-block described above and exhausted the whole retry budget —
prompting the widening to 15x45s (~10.5min) noted there; that widening
itself is not yet re-confirmed against a fresh 403.

### `/cp`: changing the password on an EXISTING account

A separate, standalone bot mode (`BOT_MODE=password`, see "Running one bot
per site / per role" below), for the same kind of on-demand, arbitrary-
account action as `/freenum` above, but for the login password instead of
the phone number. Unlike every other credential-taking command in this file
(`/freenum`, `/testbaccarat`, `/pair`), credentials are NOT passed as
command arguments — `/cp` alone starts the flow (`cp_cmd()`, adds the chat
to the module-level `pending_changepassword` set and prompts for input),
then the very next plain-text message in that chat is parsed by
`handle_cp_message()` as `<username> <current_password> [new_password]` and
run through `change_account_password_via_login()`. This two-step shape was
a deliberate simplicity request (typing a long `/cp user pass newpass` line
is more error-prone than a short trigger + a follow-up message). If
`new_password` is omitted, a random policy-compliant one (via the same
`gen_password()` signup uses — 5-60 chars, upper/lower/digit/special) is
generated and reported back in the reply. `handle_cp_message()` only acts if
the chat is actually pending AND the sender is master — a stray text message
otherwise (there's nothing else for plain text to do in password-only mode)
is silently ignored.

**Finding the mechanism was genuinely greenfield** — unlike free-number
(which had `/send_otp_touser` from the start of that investigation), nobody
had ever looked for a change-password endpoint on this site before. A
read-only discovery pass (`inspect_account_settings.py`, modeled on
`inspect_wallet.py`) logged into a real account, dumped every password-
related DOM element, and captured same-origin network traffic — and found
**nothing**: the only password-reset mechanism anywhere in the page's
~650KB source (checked twice, across two live runs) was the OTP-based
"Forgot Password?" flow attached to the *login modal* (`#resetNewPassword`,
`#cnfmPassword`, `.resetBTN`), not a self-service change-password form
reachable from an authenticated session — the `#acctSec` "Account Details"
flyout turned out to be a static marketing/support sidebar (Quick Links,
Promotions, Help), with no internal link anywhere in the DOM to any
`/account`, `/profile`, or `/settings`-style route.

The real endpoint was instead found the same way `/send_otp_touser` was —
**manual HTTP request interception** against a real account's own browser
session (not automated discovery) — confirming `POST
https://cricmatch247.com/changePassword` with body
`oldPassword=<current>&newPassword=<new>&_token=<csrf>`, response `200`
`{"status":200,"msg":"Password updated successfully"}`. Two things worth
flagging so nobody re-guesses them:
1. **The response shape is its own convention**, not the `{"message",
   "message_class"}` shape most other endpoints here use (register,
   send_otp_touser) — the key is `"msg"`, not `"message"`, and there is no
   `"message_class"` field. `change_account_password()` (`main.py`) judges
   success via the body's own `"status"` field equalling `200`, not
   `http_is_error()`.
2. **The account needs a verified mobile number.** Confirmed live
   2026-07-30 against a real (phone-less) throwaway account: the call
   returns a clean `200` but a logical rejection, `"please add phone number
   before changing the password"` — not a bug, a real business rule on the
   site's side. The rejection message is surfaced verbatim in the bot
   reply, same as any other failure message.

**Verified live end-to-end 2026-07-30**: the exact request/response above
was captured from a real account's own session and confirmed both
directions afterward (new password logs in, old password is rejected) —
see `sites/cricmatch.py`'s `change_password_path` comment. `main.py`'s
`change_account_password()`/`change_account_password_via_login()` mirror
`free_phone_number()`/`free_account_number()`'s shape exactly (in-page
`fetch()` via `page.evaluate()`, not `page.context.request`, same reasoning
as free-number's confirmed Chromium-client-hints requirement) but were only
exercised against the phone-less rejection case directly — the success path
is confirmed via the user's own captured request, not yet via this repo's
own code hitting a 200 end-to-end. Deliberately **no retry loop** (unlike
free-number's 15x45s budget) — that width was earned empirically for a
different endpoint's rate-limit behavior; don't port it here speculatively,
only add one if live testing surfaces the same kind of WAF/rate-limit block.

`/cp` itself is `@require_role(is_master)`-gated (same restricted scope as
`/freenum`/`/testbaccarat` — mutates a real account's login credential);
`handle_cp_message()` re-checks `is_master()` on the follow-up message too,
since a `MessageHandler` isn't covered by that decorator.
`_blocking_change_password()` in `telegram_bot.py` runs it on
`_pw_executors[0]` with a throwaway context, mirroring
`_blocking_free_number()`'s pattern exactly. The **current/old** password
typed into the follow-up message is never echoed back in the reply (same
discipline as `/freenum`/`/testbaccarat`); the **new** password IS included
in a successful reply's caption, matching `build_caption()`'s existing norm
of including plaintext passwords in master-only chat captions for
successful signups.

**Text-only reply, no screenshot sent** (deliberate, by request) — unlike
`/freenum`/`/testbaccarat`, `handle_cp_message()` replies with a plain
`update.message.reply_text(...)`, not `send_result_photo()`. The screenshot
is still taken and stored on disk (`change_account_password_via_login()`
still sets `result["shot"]`), it just isn't pushed to chat here.

### `/setphone`: rotating a pool of real phone numbers across signups

Pairs with free-number mode (on by default): instead of asking for a phone
number on every `/newacc`, pin every future signup to a **rotating pool** of
real numbers — `/setphone <number> [number2] [number3] ...` sets
`global_settings["phones"]` (a list, persisted via `save_settings()`) plus
resets `global_settings["phone_idx"]` to 0; `/setphone --random` clears both
(default: prompt for a phone number each time, as before); `/addphone
<number>` / `/delphone <number>` add/remove one number without replacing the
rest of the pool; `/phone` shows the current pool, which number is up next,
and the round cooldown (see below). A single-number pool behaves exactly like
the old one-number `/setphone` did. **Migration**: a settings file still
holding the old single-value `global_settings["phone"]` key is converted to
`{"phones": [that number]}` at import time (see the migration block right
after `global_settings` loads), so upgrading needs no manual settings edit.

**Usable by admins too, not just the master admin (added 2026-07-29) — and
each admin's pool is EXCLUSIVE to them, not shared.** `/setphone`,
`/addphone`, `/delphone`, and `/phone` are now `@require_role(is_admin)`
instead of `@require_role(is_master)`. What a call actually reads/writes
depends on who's calling, via two helpers in `telegram_bot.py`:
- `_resolve_phone_store(user_id)` — used for every READ (`_next_fixed_phone()`,
  `_auto_restart()`'s cooldown check, `/phone`'s display). The master always
  reads `global_settings` (the shared/default pool). A plain admin reads
  their own entry in `admin_phones` (keyed by Telegram user id, gitignored,
  `ADMIN_PHONES_FILE` env-overridable per bot instance like `ADMINS_FILE`/
  `SETTINGS_FILE`) **if they have one**; otherwise they fall through and
  inherit the master's shared pool, same as before this change — so an admin
  who has never touched `/setphone` sees no behavior change at all.
- `_write_phone_store(user_id)` — used for every WRITE (`/setphone`,
  `/addphone`, the non-empty branch of `/delphone`). The master always
  writes `global_settings`. A plain admin always writes
  `admin_phones.setdefault(str(user_id), {})` — created fresh on first use —
  **never** the master's `global_settings`, so one admin's `/setphone` can
  never clobber another admin's pool or the master's shared one. An admin's
  first `/addphone` starts a brand-new pool from scratch; it does not copy or
  extend the inherited master pool.

`/setphone --random` for a plain admin removes their `admin_phones` entry
entirely (rather than leaving an empty record) so they go back to inheriting
whatever the master's pool currently is — not straight to ASK EACH TIME if
the master has one set. `/delphone` emptying an admin's own pool does the
same full-entry removal, for the same reason. The master's own `/setphone
--random` is unchanged: it clears `global_settings["phones"]`/`["phone_idx"]`
directly, same as before this feature existed.

This only changed the phone-pool commands — `/setpassword`, `/setproxy`,
`/seturl`/`/btag`, and every other "global" setting are still master-only and
still genuinely global, no per-admin exclusivity. Don't generalize this
per-admin-store pattern to those without being asked; phone pools specifically
needed it because two admins running continuous loops against the SAME fixed
number would otherwise race each other into `phone_taken` failures, which is
not a concern for a shared password or proxy.

**Why a pool instead of one number (added 2026-07-25, replacing the earlier
single-number-only version — see git history):** live testing of a
continuous `/newacc` loop with a single fixed number still intermittently hit
"number already in use," even with `free_phone_number()`'s generous retry
budget (main.py, up to ~10.5min worst case) — the free-number call itself
was reported successful, but the very next round's register call, fired
immediately after, sometimes raced ahead of the site's own backend actually
reflecting the swap. A single number has zero slack for that; a pool of N
numbers gives each one N-1 other rounds' worth of time before it's needed
again, which is a much bigger margin than any fixed per-call wait can
practically buy.

`_next_fixed_phone()` (telegram_bot.py) pops `phones[phone_idx % len(phones)]`
and advances+persists `phone_idx`, so rotation position survives a bot
restart. `begin_signup()` calls it right after building the session
(proxy/URL/fast/free-number all already decided by this point); if it returns
non-`None`, `begin_signup()` skips the "send the phone number" prompt
entirely and calls the shared `_submit_phone(update, chat_id, sub_id,
session, phone, tag=, fallback_note=)` helper directly with that round's
number — the exact same function `handle_message()`'s `await_phone` branch
calls after validating a manually-typed number (the branch's body was
extracted into this helper so both paths can't drift). Everything downstream
— phone_taken handling, failure handling, transitioning to `await_otp` — is
unchanged.

**`ROUND_COOLDOWN_SECS` (default 12s, env-overridable) — a pause before the
continuous loop auto-restarts, added alongside the pool for the same live
failure.** `_auto_restart(update, chat_id, sub_id)` replaced the three
inline `if (chat_id, sub_id) in looping_chats: await begin_signup(...)`
call sites (phone_taken, register-failure, and post-OTP-verify branches, all
in `_submit_phone`/`handle_message`) — it still checks `looping_chats` the
same way, but when a phone pool is set, it `asyncio.sleep(ROUND_COOLDOWN_SECS)`
first. This only applies in fixed-phone/pool mode: that's the only path where
a round's register call fires with no human pacing it (ask-each-time mode
already waits on someone to type the next number). Paired with this,
`free_phone_number()`'s pre-call settle wait (main.py) was widened from 4s to
8s for the same reason — both changes buy the site more time to make a
free-number swap visible before it's relied on again, one on the sending
side, one on the receiving side.

**Why this is expected to keep working across a whole continuous run**: each
number staying usable relies on free-number mode actually freeing it each
time it's used (see above) before rotation brings it back around. If a
free-number call ever fails for one round, that number's *next* turn in the
loop will get a `phone_taken` result and report that like any other run —
there's deliberately no fallback beyond the rest of the pool.

Verified via a mock run of `begin_signup()` (stubbing
`_blocking_fill_and_register`, no real browser/site involved): with
`global_settings["phones"]` set, it sent no "send the phone number" prompt at
all — straight from lane-start to `"⏳ Submitting the signup form
(<number>)..."` then `"📩 OTP sent..."`, with `session.stage` correctly
landing on `"await_otp"`. The pool/rotation/cooldown themselves were
exercised live 2026-07-25 against a real single-number pool (post-migration)
but **not yet against a multi-number pool** — do that before relying on it
for volume if the single-number "already in use" failure resurfaces even
with the wider settle wait.

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
- **A missed betting window or a one-sided (unhedged) landing retries the
  same round slot instead of ending the run (changed 2026-07-21; previously
  both were an immediate hard stop -- see git history for the old
  return-on-first-failure version).** The round loop in `run_paired_hedge` is
  now attempt-based rather than a plain `for rnd in range(rounds)`: neither
  case blocks the accounts from trying again, so retrying (after a
  `ROUND_RETRY_COOLDOWN_SECS=6` pause) is what actually gets a run to the
  requested `rounds` instead of stopping short on the first hiccup. A
  one-sided landing is real exposure for that one hand, but baccarat hands
  resolve themselves with no button to press (unlike Stock Market's live
  cash-out) -- the round loop waits out the settle (`game.settle_secs`,
  reusing the same 0/0 TOTAL BET poll the normal end-of-round path uses) so
  the retry starts clean, screenshots both tabs
  (`shots/hedge-partial-attempt<N>-*.png`), and logs it to
  `summary["unhedged_rounds"]` (persisted in `pair_runs.json`/`sheet_runs.json`
  and shown in `/runlog`) without counting it toward `rounds_done`.
  `consecutive_failures` (reset on every successful round) still gives up
  after `MAX_CONSECUTIVE_ROUND_FAILURES=5` in a row with zero progress --
  that pattern means something persistent (site down, WAF block, a table
  that's genuinely stuck), not a blip, so retrying forever would just burn
  time without ever reaching `rounds`. New stop reasons for that case:
  `repeated_unhedged_exposure`, `no_open_window` (now only reached after 5
  consecutive misses, not the first one), and `max_attempts_exceeded` (a
  `rounds * 4` / minimum-20 attempt ceiling, purely a worst-case safety
  valve). **Left as immediate hard stops, deliberately NOT retried**:
  `banker_out_of_balance`/`player_out_of_balance` (waiting doesn't refill a
  balance), `amount_mismatch` (waiting doesn't change a table's chip menu --
  retrying would just repeat the same wrong-size bet every round),
  `chip_select_failed`, `different_tables`/`setup_failed` (setup already gets
  its own 4-attempt retry via `_open_table_with_retry`, see above), and
  `stopped_by_user`. Stock Market's cash-out failure branches
  (`cashout_partial`/`cashout_divergence`/`no_cashout_window`) are also
  unchanged -- currently unreachable anyway since `STOCKMARKET.needs_cashout`
  is `False` (see "Cash-out is OFF" below), and a live, still-moving unclosed
  position is a fundamentally different risk than a settled baccarat hand
  (waiting there makes the exposure worse, not resolves it), so it wasn't
  folded into this same retry treatment.
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
  called from whichever thread owns that account — don't drop these calls if
  you touch this function. Since 2026-07-19 these setup-phase lines are
  routed through `run_paired_hedge`'s separate `setup_progress` callback
  (falls back to `progress` when not given, so CLI/ad-hoc callers are
  unchanged): the bot passes a console-only logger there (per the owner's
  explicit request — setup chatter was spamming the chat), so Telegram now
  gets only the "Run started" card, each `✅ Round N/M hedged` line, and the
  final summary card; the 🔑/🎰/🃏/📡/⏳ lines appear in the bot's console
  log only. `open_casino_lobby()`'s poll loop checks the lobby's own
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

## Stock Market Live hedge (`BOT_MODE=stockmarket`)

A second hedgeable game alongside Baccarat: Evolution's **Stock Market Live**,
where the two accounts bet **UP vs DOWN** on the same round and both positions
are **cashed out together** each round. Runs as its own bot instance
(`.env.stockmarket.example` -> `.env.stockmarket`, `BOT_MODE=stockmarket`),
reusing the same `/pair` `/pairs` `/delpair` `/run` `/stoprun` `/runs`
`/runlog` commands -- the game is fixed per instance exactly like
`BOT_SITE_URL` fixes the site, so `/run` needs no extra argument and there is
still only one `run_cmd`. `/testbaccarat` is deliberately excluded (it is
Baccarat-specific and stays on the gameplay bot).

Why it's attractive: the hedge bleeds only the game's **1% cash-out fee**,
versus Baccarat's ~5% banker commission, and the **table minimum is ₹10** vs
Baccarat A's ₹100 -- so live testing costs a tenth as much.

### `GameProfile` (`sites/games.py`)

Everything game-specific moved out of the engine into a `GameProfile`, the
game-level counterpart to `SiteProfile`. `BACCARAT` reproduces the previous
hardcoded behavior exactly (same roles, `window_mode="timer"`, same
30/150/40 timings) and is the default for every existing caller, so the
gameplay bot and its stored run history are unaffected. **Don't guess values
for a new game** -- run the read-only probes (below), read the dump, then fill
in a profile.

### How Stock Market differs (all confirmed live 2026-07-20)

- **Bet spots are `SM_Up` / `SM_Down`** -- a completely different convention
  from Baccarat's `bet-spot-Banker`. `_click_bet_spot()` therefore takes a
  **complete `data-role`**, not a suffix interpolated into `bet-spot-{}`.
- **There is no `circle-timer`, and role presence cannot detect the betting
  window at all** -- the visible role SET is byte-identical in every phase
  (verified by diffing across a full round: nothing changed). The phase lives
  in the **text** of `[data-role="instruction-message"]` ("PLACE YOUR BETS n"
  / "NEXT GAME SOON"), which is what `window_mode="instruction"` reads.
  Measured live: the window is open ~10s on a ~21s cycle -- tighter than
  Baccarat's ~15s, hence `place_secs=220` so one missed window doesn't end
  the run.
- **The game is not in cricmatch's catalogue.** 206 tiles across the lobby's
  Game Shows / Arcade Games / All (with "View All" expanded and lazy-load
  scrolled) contain no match, and the site's own search returns only football
  teams for "Stock". It is reachable **only** through Evolution's in-game
  lobby: open any Evolution game (the profile uses the already-verified
  Baccarat A as the door in), click `[data-role="lobby-button"]` bottom-right,
  search there, open the tile. That's `_open_via_provider_lobby()`.
- **The provider lobby is a SEPARATE iframe** (its URL carries `?iFrAmE=x`)
  and only that frame has the Search box; the game frame -- which is what
  `find_game_frame()` returns, since it has the most DOM nodes -- contains no
  search input at all, only `quick-chat-input`. This cost several failed
  attempts: typing into "the frame" silently did nothing every time.
  `_find_provider_lobby_frame()` identifies it by its own category tabs ("For
  You" / "Top Games" / "Game Shows"), which is stabler than matching the URL.
  The lobby overlay is also fragile -- **any stray click dismisses it** and
  drops back into the game -- so only click the three things needed.
- **`read_game_balance()` and `_read_total_bet()` work unchanged** on this
  game (verified: balance read ₹1,542, total bet 0).
- The LOBBY button only exists once the entry game's UI has rendered;
  `find_game_frame()` returns as soon as the frame has enough DOM nodes, which
  can still be the loading screen. Clicking too early silently does nothing
  and the lobby frame then never appears -- so `_open_via_provider_lobby()`
  **polls** for the button. This failed on the first real end-to-end run.

### Cash-out is OFF: settling is a complete hedge on its own

**Established live 2026-07-20** over four real ₹10/side rounds (pair 1): the
combined balance across both accounts went **3749 -> 3748 -> 3748 -> 3749**.
Money moved between the two accounts every round (±₹4-9), but the pair netted
~zero. Both sides hold equal, opposite positions on one round, so whatever the
chart does one gains what the other loses -- exactly like baccarat, where
nothing is cashed out either.

So `STOCKMARKET.needs_cashout` is **False**. Not cashing out is also strictly
*cheaper* (the 1% fee is charged on cash-out, so riding to settlement pays no
fee) and removes the timing risk entirely. This also answers the question that
was open before the first run: an un-cashed position settles normally, it is
not forfeited.

The cash-out implementation is retained and structurally correct, but **its
click does not yet register against the live button** -- runs 3 and 4 both
ended `cashout_failed` with both positions still open (harmlessly: both
failing leaves the pair hedged). Diagnosing it needs a real live position to
inspect the *enabled* button against, since it can only be reached by
actually betting. Don't flip `needs_cashout` back to True without a fresh
live-verified fix, not just a plausible theory (see next paragraph for why
that warning exists).

**It WAS flipped back to True once already, and re-broke.** The opacity-gate
theory above (`_cashout_enabled()` gating the click on the CASH OUT label's
CSS opacity, which doesn't track real enablement) was diagnosed and fixed
live 2026-07-20 (`_cashout_ready()` no longer gates on it, verified by a real
click landing while the old gate would have blocked it -- commit "Fix
cash-out: stop gating on the broken label-opacity signal"), and
`needs_cashout` was set back to `True` on the strength of that fix. It
re-broke anyway: a real ₹100/side run 2026-07-21 (pair 4, run #9) hit the
identical `cashout_failed` outcome, stopping after round 1/10. So the opacity
gate was *a* cause, not *the* cause -- something else about the click still
doesn't land reliably, and it isn't root-caused. `needs_cashout` is back to
`False`. Given cash-out was never necessary for a clean hedge in the first
place, there's no upside to chasing this further with real money instead of
just not cashing out.

### Cash-out, if it is ever re-enabled

The original risk analysis:



Baccarat bets are discrete: once placed, the hand resolves itself and there is
nothing to time. Stock Market runs a live chart and each side's PORTFOLIO
moves continuously until CASH OUT is pressed, so the two positions are a true
hedge only while they still sum to (stake_a + stake_b). **Every second between
the two cash-outs is real money** -- measured live, the chart can travel ~90%
inside ~20s.

So the engine cashes out as **early** as possible (the instant a position
exists, when both portfolios are still ~= their stakes) and fires both clicks
**concurrently** on their own threads, the same `player_exec.submit(...)` then
inline-call pattern the bet clicks use. Then:
- a side that didn't close is **retried once**; if it still hasn't, the run
  halts with `cashout_partial` and names the exposed account (that account is
  still riding the chart unhedged -- close it by hand).
- a **divergence guard** (`cashout_tolerance`, default 5%) stops the run with
  `cashout_divergence` if the realized total drifts from the stake, meaning
  the two cash-outs did not land together. It **detects** this after the fact;
  it cannot prevent it. That is why live testing starts at ₹10.

**`read_portfolio()` is the "is there a position" signal, NOT the button
state** -- confirmed live, the CASH OUT button reports `disabled=false` and
`opacity=1` even with nothing staked (it is styled purely by CSS class), so it
cannot distinguish the phases. Its text is `"PORTFOLIO\n1% FEE\n₹0.00"`,
hence parsing the **last** number rather than the first.

New stop reasons: `no_cashout_window`, `cashout_partial`, `cashout_divergence`
(all in `_REASON_LABEL`).

### Two pre-existing bugs this surfaced

1. **`_table_id()`'s regex was lowercase-only** (`[a-z0-9]+`), so it returned
   `None` for Stock Market's `StockMarket00001`. Because the same-table check
   only compares when *both* ids are truthy, that silently **disabled the one
   guard ensuring both accounts bet the same table**. The class now includes
   uppercase, and the check aborts with `different_tables` rather than
   proceeding when it cannot read both ids -- refusing to bet beats assuming
   a skipped check passed.
2. `_open_via_provider_lobby()` clicked LOBBY before the game had rendered it
   (see above).

### Discovery + verification scripts

All read-only, none place a bet. Follow the `inspect_casino.py` precedent:
run them, read the dump, *then* write selectors.
- `probe_evo_lobby.py <user> <pass>` -- maps the route to the game and dumps
  every `data-role` in it.
- `probe_stock_round.py <user> <pass>` -- watches a full round, sampling the
  instruction text, portfolio, cash-out state, total bet, balance and chips.
- `verify_stockmarket.py <user> <pass>` -- drives the **real**
  `_open_table_for(game=STOCKMARKET)` end to end, checks every readout and
  role resolves, and counts betting windows. Run this before any `/run`.
  Verified: table opens in ~70s, 10 windows in 200s, table id
  `StockMarket00001`.

**Not yet verified live: placing an actual bet, and a real cash-out.** Start
with `/run <pair> 10 1` (one round, ₹10/side) and check `/runlog` shows the
two balances netting to roughly zero minus the ~1% fee before scaling.

### Known gap: chip selection

The chip rail here is real DOM (`chip` x6, `chip-value` x6, `selected-chip`,
`double-button`, `undo-button`) unlike Baccarat's hidden SVG templates, and
the selected chip reads back (observed: `10`). So lifting the
`amount_mismatch` limitation looks feasible on this game -- but `chip-value`
elements render their number as SVG with empty `innerText`, so the values
aren't readable that way yet. Deliberately deferred rather than guessed at
with real money.

## Sheet-driven hedge runs (`sheet_watcher.py`)

A third way to kick off a paired hedge run, alongside the CLI and Telegram
`/pair`+`/run`: a shared Google Sheet queue
(`https://docs.google.com/spreadsheets/d/14unqPI3VsjfUqhhmg666lPJBeFQu3x9VEwSLk_o05dM`),
columns `PLAYER 1 | PASSWORD | PLAYER 2 | PASSWORD | BETS AMOUNTS | ROUNDS |
STATUS`. `sheet_watcher.py` polls it (default every `SHEET_POLL_SECONDS=20`)
and, for any row with A-F filled and STATUS empty, calls
`main.run_paired_hedge()` directly (Baccarat, matching the sheet's own title)
and writes the outcome back into STATUS (`⏳ queued` -> `🏃 running` -> a
result line with rounds hedged, stop reason, and each side's final
balance/net). Clearing STATUS on a row makes the watcher re-run it.

Run it against the gameplay bot's own site/proxy config rather than
duplicating it into a new env file:
```
.venv/bin/pip install gspread google-auth
.venv/bin/python sheet_watcher.py --env .env.gameplay
```
`--env` is parsed the same way as `telegram_bot.py`'s (see that section) --
`load_dotenv(_env_file, override=True)` after `import main` so `--env`
reliably wins over `main.py`'s own bare `load_dotenv()` import-time call.
`current_proxy()` re-reads `.env.gameplay`'s `SETTINGS_FILE`
(`bot_settings.gameplay.json`) on every run, so `/setproxy` on the gameplay
Telegram bot also applies here automatically -- no separate proxy config to
keep in sync.

**Deliberately standalone, not wired into `pairs.json`/`pair_runs.json`.**
Each sheet row already carries full credentials, so there's no need for a
persisted "pair id" -- and having two processes (this script + the gameplay
bot) write the *same* `pairs.cricmatch.json`/`pair_runs.cricmatch.json` file
concurrently would risk exactly the clobbering problem `ADMINS_FILE`/
`SETTINGS_FILE` were split per-bot-instance to avoid (see "Running one bot
per site / per role" above). Run history instead goes to its own
`sheet_runs.json` (gitignored, `SHEET_RUNS_FILE` to override), and STATUS in
the sheet itself is the primary at-a-glance result.

**No cross-process account-busy guard against the Telegram bot.** Within
`sheet_watcher.py`'s own process, two rows sharing a username are serialized
(an in-flight-usernames set, mirroring `run_cmd`'s `busy` check) -- but if
the gameplay bot's `/run` and this script pick the same account at the same
time, nothing stops it, unlike same-process runs which do collide-check.
Don't run a sheet-queued pair's accounts through `/run` manually while the
watcher might also pick up a row for them, and vice versa.

**Requires a Google Cloud service account** (Sheets API, not OAuth) since
this needs to run unattended with no browser-based consent step:
1. Google Cloud Console -> new (or existing) project -> enable the "Google
   Sheets API".
2. IAM & Admin -> Service Accounts -> Create -> any name -> skip granting it
   project roles (not needed, access is via the sheet share, not IAM) -> done.
3. Open the service account -> Keys -> Add Key -> JSON -> download it, save
   as `service_account.json` in the repo root (gitignored via
   `service_account*.json`; `SHEET_CREDENTIALS_FILE` overrides the path/name).
4. Open the sheet -> Share -> paste the service account's email (looks like
   `something@project-id.iam.gserviceaccount.com`, visible on its Cloud
   Console page or inside the downloaded JSON's `client_email` field) ->
   Editor access (needed for the STATUS write-back, not just Viewer).

Amounts/rounds are parsed leniently (`_clean_number()` strips `₹` and `,`
before `int()`), so `"100"`, `"₹100"`, and `"1,000"` all work in the BETS
AMOUNTS column. An unparsable or non-positive amount/rounds writes a `❌`
STATUS instead of raising, and the row is left alone (not retried) same as
any other terminal STATUS -- fix the cell value and clear STATUS to retry.

**Not yet run against the live sheet** -- built and import/wiring-verified
(env resolution, proxy passthrough, syntax) but no service account has been
created yet, so no row has actually been polled or run end-to-end.

## Sheet-driven balance checking (`balance_checker.py`)

A separate feature from the hedge sheet above, added on request: instead of
running hedges, this keeps a Google Sheet of plain username/password rows
updated with each account's current site wallet balance, on a recurring
poll -- e.g. to keep an eye on a pool of accounts' funds without checking
each one by hand.

Sheet layout (row 1 = header): `A: USERNAME | B: PASSWORD | C: BALANCE |
D: STATUS`. **Deliberately its own sheet, not the hedge one** -- different
columns, different purpose, and reusing the hedge sheet would risk this
script and `sheet_watcher.py` (or the Telegram bot's `/run`) writing over
each other's cells.

**Same queue semantics as the hedge sheet now (changed 2026-07-30, see git
history for the earlier "re-check every row every cycle" version):** a row
with A+B filled and an EMPTY STATUS gets checked once; STATUS is then set to
a result, which also means that row is left alone on every later poll. Add a
new row -> it gets picked up on the very next poll. Clear an existing row's
STATUS by hand to force a re-check. STATUS shows the outcome of the check
(`✅ checked <timestamp>` or `❌ <timestamp> — <error>`); BALANCE holds the
last **successfully** read number and is deliberately left alone on a failed
check, so a failed check doesn't blank out the last known-good figure --
though note a failed row does NOT auto-retry, since its STATUS is now
non-empty too; clear it to try again.

This replaced the original "re-check everything on every poll" design after
a live 2026-07-30 incident: at `BALANCE_POLL_SECONDS=300` it kept
re-triggering cricmatch247's `/login` rate-block (a ROLLING window, not a
fixed one -- see the "HTTP-fast balance checks" section below for the full
incident) because the WHOLE sheet got hit with a fresh login burst on every
single cycle, whether or not anything had changed. Checking only empty-STATUS
rows means most polls do nothing but a cheap read, so `BALANCE_POLL_SECONDS`
went back down to a short **20s** default (matching `sheet_watcher.py`'s
queue poll) with no rate-limit risk -- the login endpoint is now only hit
when a row is genuinely new (or manually cleared for a re-check), not on a
fixed schedule regardless of whether there's anything to do.

Engine (`main.py`):
- `read_wallet_balance(page, timeout_secs=30)` — reads the site's own header
  wallet balance (cricmatch247's "My Wallet"), **not** the in-game Evolution
  balance `read_game_balance()` reads elsewhere in this file (that one reads
  a live casino table's own frame; this reads the surrounding site chrome).
  **VERIFIED LIVE 2026-07-30** against a real account (`ali789`, via
  `inspect_wallet.py`, through the cricmatch bot's residential proxy) — the
  figure lives in `span.total_balance` (mirrored in `span.wallet_balance`
  under "Available", same number), now `sites/cricmatch.py`'s
  `sel["wallet_balance"]`. Two non-obvious things found live, both handled:
  1. Both spans are **empty in the raw HTML at page-load** — the site fills
     them in later via its own onload `getBalance()` call. Measured live:
     ~20s on a residential proxy. `read_wallet_balance()` polls (1s interval,
     `timeout_secs=30` default) instead of reading once right after login.
  2. A real navigation happens shortly after login (the same kind of
     redirect `free_phone_number()` already has to tolerate), which can
     kill the execution context mid-poll ("Execution context was
     destroyed"). Treated as "try again next tick," not a hard failure.

  Returns a `float` (rupees, may include paise — `"₹ 1,484.68"` → `1484.68`)
  or `None` if it never populates within the timeout; a caller must read
  `None` as "selector/timing needs revisiting," never as "balance is 0."
  Confirmed live end-to-end via `balance_checker.py --once` (below): read
  `ali789`'s real balance (₹1,484.68) and wrote it into the sheet correctly.
  `inspect_wallet.py` (mirrors the `inspect_form.py`/`probe_evo_lobby.py`
  precedent of "dump the DOM, then write selectors from what's real") logs
  into one real account read-only, waits for `read_wallet_balance()`, and
  dumps every wallet/₹-looking element — kept around for re-verifying the
  selector if the site's markup ever changes, not because the current one is
  still a guess.
- `check_account_balance(page, username, password, site_url=None)` — logs in
  via the existing `login()` (same requirement as `test_baccarat()`/
  `free_account_number()`: needs `supports_casino`'s login selectors,
  cricmatch247 only) then calls `read_wallet_balance()`. Returns
  `{"ok","balance","messages","shot"}`, same result-dict convention as
  `free_account_number()`. Read-only — never places a bet, never writes to
  `accounts.db` (operates on an account someone already has, not one this
  run generated).
- `run_balance_check(username, password, site_url=None, proxy=None)` — the
  one-call entry point `balance_checker.py` actually uses: launches its own
  throwaway browser + context (`_launch_pw_browser()` /
  `parse_proxy()`/`maybe_bridge_proxy()`, the same per-side pattern
  `run_paired_hedge()` uses), calls `check_account_balance()`, and tears
  everything down (context, proxy bridge, browser) on every exit path —
  mirrors `run_paired_hedge()` being one call for a whole hedge run rather
  than exposing Playwright plumbing to callers.

`balance_checker.py` itself mirrors `sheet_watcher.py`'s shape closely: same
`--env <path>` flag and `load_dotenv(_env_file, override=True)`-after-
`import main` gotcha, same `current_proxy()` pattern (re-reads `--env`'s
`SETTINGS_FILE` live every check, so `/setproxy` on that bot instance applies
automatically), same `--once` flag for a single pass. Config is separate from
the hedge sheet's env vars so the two can point at different spreadsheets:
`BALANCE_SHEET_SPREADSHEET_ID` (required — refuses to start without it, no
default sheet baked in, unlike `sheet_watcher.py`'s hardcoded hedge-sheet
ID), `BALANCE_SHEET_WORKSHEET_GID` (default `"0"`), `BALANCE_SHEET_CREDENTIALS_FILE`
(falls back to `SHEET_CREDENTIALS_FILE`, then `service_account.json` — reuse
the same service account as the hedge sheet, just share it with this new
sheet too), `BALANCE_POLL_SECONDS` (default 20), `BALANCE_MAX_CONCURRENT`
(default 1, same "size to your proxy/IP diversity" caveat as
`MAX_CONCURRENT_RUNS` elsewhere).

Run:
```
BALANCE_SHEET_SPREADSHEET_ID=<sheet id> .venv/bin/python balance_checker.py --env .env.cricmatch
```

**Setup steps are identical to the hedge sheet's** (see "Requires a Google
Cloud service account" above) — same service account, same JSON key, just
share the balance sheet with it too (Editor access, since BALANCE/STATUS get
written back).

**Verified live end-to-end 2026-07-30**: real sheet
(`1YtyE40zgSOq3h3azT36S2Kw1nSwsCh_QMnJ4USQHRu4`, shared with the service
account `sheet-bal-prince@prince-bal.iam.gserviceaccount.com`), real account
(`ali789`), `balance_checker.py --once --env .env.cricmatch` read a real
balance and wrote `BALANCE`/`STATUS` back correctly. `.env.cricmatch` now
carries `BALANCE_SHEET_SPREADSHEET_ID`/`BALANCE_SHEET_WORKSHEET_GID` for
this sheet so a plain `.venv/bin/python balance_checker.py --env
.env.cricmatch` (no extra env vars needed) runs it continuously.

### HTTP-fast balance checks (no browser)

`read_wallet_balance()`/`check_account_balance()`/`run_balance_check()`
above drive a real Playwright login (~20-30s per account: page load, DOM
wait, then the `getBalance()` poll). `http_check_account_balance()` in
`main.py` replaces that with plain `requests` calls, the same idea as
`--fast` signup: found live 2026-07-30 by capturing a real login's network
traffic (`probe_login_balance.py`) and confirming with a bare
`requests.Session` replay that got byte-identical JSON:

1. `GET` the site (`http_fetch_csrf()`, already used by `--fast` signup) →
   csrf token + session cookies.
2. `POST /login` with `username, password, remember_me=1, _token=<csrf>` →
   `{"status":200,"message":"Login Successfully","url":"?uid=..."}`.
3. `POST /api2/v2/getBalance` with just `_token=<csrf>` on the now-
   authenticated session → `{"status":200,"balance":{"wallet":1484.68,...}}`
   (also `main_balance`/`totalBalance`, same figure formatted with commas —
   `wallet` is used since it's already a bare float).

Confirmed live against both real test accounts (`ali789`, `asha788`) —
balances matched the browser-path readings exactly, in ~2.8s each versus
20-30s+. `sites/base.py`'s `supports_http_login` flag (`http_login_path`,
`http_balance_path`) gates this per site, same pattern as
`supports_http_fast`; only `sites/cricmatch.py` sets it `True` so far.
`balance_checker.py`'s `process_row()` picks `http_check_account_balance`
when the resolved site supports it, falling back to `run_balance_check`
(the Playwright path) otherwise — same fallback convention as `--fast`
signup.

**Load-bearing gotcha, found live the same day: don't raise
`MAX_CONCURRENT` past 1 for a sheet where every account shares one proxy
IP.** The HTTP-fast path is cheap enough that nothing stops running several
checks at once, but a real batch of 21 accounts at `MAX_CONCURRENT=5`
against `.env.cricmatch`'s single residential proxy got **every row**
blocked with a bare `403 Forbidden` on `/login` — the same edge-level
rate-block already documented above for `/register`/`/send_otp_touser`,
just triggered by concurrent logins instead of rapid sequential ones.
Retrying the poll made it worse, not better (each retry was itself another
5-wide burst from the same IP re-triggering the block), whereas the plain
`GET` for the csrf token kept succeeding throughout — only the POST-heavy
`/login` call was blocked, confirming this is a rate/burst rule, not a
full IP ban. `MAX_CONCURRENT` therefore defaults to **1**, not higher — a
serialized sweep through ~20 accounts still finishes in about a minute at
~3s/account, well inside the default 300s poll window, so there's no real
throughput reason to raise it unless a future setup gives each account its
own proxy IP.

**Request headers were hardened 2026-07-30 to look more like a real browser**,
after the user supplied a screenshot of a genuine `getBalance` call captured
via a mobile HTTP-interceptor tool: `_HTTP_FAST_USER_AGENT` (`main.py`)
switched from a generic Windows/Chrome-124 string to a real Mac/Chrome-127
one, and every HTTP-fast POST (`http_login_call`, `http_get_balance`,
`http_register_call`, `http_free_phone_number`) now sends `Origin` plus a
Chrome client-hints set (`sec-ch-ua`, `sec-ch-ua-mobile`,
`sec-ch-ua-platform`, `DNT`) via `_http_fast_browser_headers()` /
`_http_fast_origin()`, none of which the earlier bare `requests.Session` sent
at all. Rationale, not yet proven to actually reduce blocking: a plain
`requests` call has no real TLS/JA3 fingerprint to hide behind, so this
can't make it indistinguishable from a real browser -- but sending the same
*header set* a real Chrome does is a low-cost way to avoid the more obvious
"this client has almost no headers" tell some WAF rules key on, and doesn't
change behavior at all for a site that isn't scrutinizing this closely.
Verified live immediately after the change: `http_check_account_balance()`
still round-trips correctly end-to-end against a real account.

**The rate block turned out to be about VOLUME, not concurrency — found from
real sheet data 2026-07-30, after the queue-semantics change above (checking
only new rows) was already live.** With MAX_CONCURRENT=1 (fully serialized)
and no deliberate pacing between checks, a burst of new rows added to the
sheet at once got checked back-to-back roughly every ~9s (poll overhead +
the ~3s HTTP round trip). One such burst of ~20 accounts all succeeded, but
the next few rows added right after immediately hit the same bare `403` on
`/login` — meaning something like "roughly 20 logins within a few minutes
from one IP" trips the block, independent of whether those logins were
concurrent. Fix: `balance_checker.py`'s `_wait_for_turn()` now enforces a
minimum gap (`BALANCE_CHECK_SPACING_SECONDS`, default 30s) between the
*start* of each login attempt, called from inside `process_row()` so it
paces checks without blocking `poll_once()` from noticing new rows. This
spreads a burst of many new rows out over minutes instead of firing them as
fast as the executor can. The 30s default and the "~20 per few minutes"
threshold are both **inferred from one incident, not a controlled test** —
tune `BALANCE_CHECK_SPACING_SECONDS` up if 403s keep recurring, or down if
30s turns out to be far more conservative than necessary.

**Spacing alone didn't stop it from tripping, and once tripped it stays
blocked for a long time regardless of retry pacing — found from a real ~1740-
row / 2026-07-31 sheet timeline, after spacing above was already live.**
The full timeline showed: once a 403 appeared, essentially *every* attempt
afterward 403'd for a sustained ~20-minute stretch — spaced 30s apart the
whole time, so pacing bought nothing during an active block — then it
cleared completely on its own and a long run of checks succeeded again. Two
consequences at scale (this surfaced first at ~2k accounts): (1) ~20 minutes
of wasted 30s-paced attempts is a lot of dead time when there's a large
sheet still waiting to be checked, and (2) the pre-existing behavior wrote a
permanent `❌ ...` STATUS for every one of those blocked attempts — meaning
each one looked like a real (if boring) failure and would never be retried
without someone manually clearing that row's STATUS cell, which doesn't
scale past a handful of rows.

Fixed by treating an edge/WAF block as fundamentally different from a real
per-account result, both in `main.py` and `balance_checker.py`:
- `http_login_call()`/`http_get_balance()` already returned a synthetic
  `{"status": None, "message": "Non-JSON response (HTTP 403): ..."}` dict
  when the response isn't JSON (the WAF returns an HTML "403 Forbidden"
  page, not the app's normal JSON) — `status is None` was already a clean,
  pre-existing signal for "the app never saw this request," distinct from a
  real application rejection (wrong password, "Account has been Blocked",
  etc, which are always real JSON with a real status). `http_check_account_balance()`
  now surfaces that as `result["infra_block"] = True/False` (also set on a
  raw `RequestException` from `http_fetch_csrf()`, e.g. a proxy dying
  mid-block) rather than callers having to re-derive it from message text.
- `balance_checker.py`'s `process_row()`: on `infra_block=True`, does NOT
  write a terminal `❌` — it writes `"⏳ <ts> rate-limited, auto-retrying —
  <msg>"` instead, and `poll_once()` treats a `⏳`-prefixed STATUS the same
  as an empty one (still eligible for pickup) while `✅`/`❌` stay terminal
  as before. A blocked row now self-heals on a later poll with no manual
  sheet edit needed.
- A circuit breaker (`_trip_circuit_breaker()` / `_still_in_backoff()`,
  `BLOCK_BACKOFF_SECONDS`, env `BALANCE_BLOCK_BACKOFF_SECONDS`, default
  300s/5min) stops the waste during an active block: the first `infra_block`
  result pauses **every** row (not just the one that hit it) for the backoff
  window — `process_row()` checks this before even paying `_wait_for_turn()`'s
  spacing wait, so a paused attempt costs nothing. If the backoff-window probe
  is *still* blocked, it extends the pause by another full
  `BLOCK_BACKOFF_SECONDS` from that moment rather than resuming full-speed
  retries — covering a real ~20min block in ~4 cheap probes instead of ~40
  full wasted attempts at the old 30s pacing. Verified via a mocked
  `process_row()` run (fake checker returning `infra_block=True` twice then
  succeeding): confirmed a second call during an active backoff window skips
  the checker entirely (call count stayed flat), and the row's STATUS/BALANCE
  only got written once a genuine result came back.
- `BLOCK_BACKOFF_SECONDS`'s 300s default is a starting guess sized off the
  one ~20-minute incident observed so far (5min × ~4 extensions ≈ 20min) —
  **not a controlled test**, same caveat as `CHECK_SPACING_SECONDS`'s
  original 30s guess. Tune it up if a block is still visibly wasting probes,
  down if 5 minutes turns out far more conservative than the real block
  duration needs.
- The 57 rows already stuck with an old permanent `❌ ... 403 ...` STATUS
  from before this fix were manually cleared back to empty in the live sheet
  (2026-07-31) so they pick back up under the new self-healing behavior
  instead of sitting dead forever.

**This does not fix the underlying per-IP throughput ceiling** — a single
residential proxy IP still only clears about one login every
`CHECK_SPACING_SECONDS` (~2/min) during an unblocked window, so a sheet with
thousands of unchecked rows is still fundamentally slow to fully sweep on one
IP; the fix here is about not making the block *worse* or leaving rows
*permanently* stuck, not about raising overall throughput. Raising real
throughput at scale would need spreading logins across multiple proxy
IPs (not yet built) rather than any change to a single IP's pacing.

## Sheet-driven password changes (`password_changer.py`)

A third sheet-driven script (alongside `sheet_watcher.py`'s hedge queue and
`balance_checker.py`'s balance polling), for the same underlying feature as
the Telegram bot's `/cp` command (see "`/cp`: changing the password on an
EXISTING account" above) — a sheet-based front end so a batch of password
changes can be queued without going through the bot one at a time.

Sheet layout (row 1 = header): `A: USERNAME | B: PASSWORD | C: NEW PASSWORD
| D: STATUS`. **Deliberately its own sheet**, not reusing the balance or
hedge sheets — different columns/purpose, and sharing a sheet across
scripts risks the same clobbering problem the hedge/balance sheets were
already kept separate to avoid.

Same queue semantics as `balance_checker.py` (itself matching
`sheet_watcher.py`'s hedge queue): a row with `A`+`B` filled and an empty
`STATUS` is picked up and processed exactly once; `STATUS` is then set to a
result, so it won't be re-picked-up on the next poll. Clear `STATUS` by hand
to retry a row (e.g. after fixing a typo'd current password). `C` (NEW
PASSWORD) is optional — if left blank, `process_row()` generates one via
`main.gen_password()` (the same function `/cp` uses when its new-password
argument is omitted) and **writes it back into column C** before attempting
the change, so the sheet ends up holding the actual new password either
way, not just a "was randomly generated" note.

Engine: `main.run_change_account_password(username, current_password,
new_password, site_url=None, proxy=None)` — a standalone single-call entry
point mirroring `run_balance_check()`'s exact shape (launches its own
throwaway Playwright browser + context via `_launch_pw_browser()`/
`parse_proxy()`/`maybe_bridge_proxy()`, calls
`change_account_password_via_login()`, tears everything down on every exit
path). Added specifically so `password_changer.py` (or any other caller)
doesn't need to hold a `page`/`context` itself, same reasoning
`run_balance_check()` was added for `balance_checker.py`.

`password_changer.py` mirrors `balance_checker.py`'s shape closely: same
`--env <path>` / `load_dotenv(_env_file, override=True)` gotcha, same
`current_proxy()` pattern (re-reads `--env`'s `SETTINGS_FILE` live on every
change, so `/setproxy` on that bot instance applies automatically), same
`--once` flag. Config: `PASSWORD_SHEET_SPREADSHEET_ID` (required),
`PASSWORD_SHEET_WORKSHEET_GID` (default `"0"`),
`PASSWORD_SHEET_CREDENTIALS_FILE` (falls back to `SHEET_CREDENTIALS_FILE`,
then `service_account.json` — reuse the same service account, just share
this new sheet with it too), `PASSWORD_SHEET_POLL_SECONDS` (default 20),
`PASSWORD_SHEET_MAX_CONCURRENT` (default 1), and
`PASSWORD_SHEET_CHECK_SPACING_SECONDS` (default 30s).

**`MAX_CONCURRENT`/`CHECK_SPACING_SECONDS` default conservative for the same
reason `balance_checker.py`'s do** — this script logs in via real
Playwright just as much as that one does per row (there's no HTTP-fast path
for the change-password endpoint; unlike `balance_checker.py`'s
`http_check_account_balance()` shortcut, it hasn't been investigated
whether `change_account_password()`'s request works from a bare
`requests.Session` without the cookies a real browser session accumulates —
the same open question `free_phone_number()`'s HTTP path has), so it's
exposed to the identical login-volume rate block documented above. The 30s/1
defaults are carried over from that finding, not independently confirmed
against this specific endpoint.

Run:
```
PASSWORD_SHEET_SPREADSHEET_ID=<sheet id> .venv/bin/python password_changer.py --env .env.password
```

Setup is identical to the hedge/balance sheets (see "Requires a Google
Cloud service account" above) — same service account, same JSON key, just
share this sheet with it too (Editor access, since NEW PASSWORD/STATUS get
written back).

**Not yet run against a live sheet** — built following the confirmed-working
`balance_checker.py`/`sheet_watcher.py` pattern and the already-verified
`change_account_password_via_login()` engine call, but no sheet has been
created/shared with a service account for this yet, so no row has actually
been polled or processed end-to-end.

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

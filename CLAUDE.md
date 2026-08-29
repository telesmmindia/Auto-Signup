# CLAUDE.md

Guidance for Claude Code working in this repo.

## Communication style
- Plain English. No jargon or acronyms without explanation.
- Say what you're doing and why, briefly.
- Never dump a raw stack trace without a one-line plain summary first.

## Purpose

QA automation for the owner's own betting sites (cricmatch247, spin24star,
khelofun): drives the signup form, checks balances, changes passwords, and runs
hedged casino bets to smoke-test the platform. Account data comes from
user-supplied config; every run is logged and screenshotted into `shots/`. Not a
mass-registration tool.

## Setup

```
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium
```

---

# 1. CLI (`main.py` + `db.py`)

`main.py` is the Playwright engine (sync API); `db.py` owns `accounts.db`.

```
.venv/bin/python main.py                       # generate identity, prompt phone, submit
.venv/bin/python main.py --headed              # watch it
.venv/bin/python main.py --no-submit           # fill only, validate selectors
.venv/bin/python main.py --phone X --email Y   # override generated fields
.venv/bin/python main.py --account-file accounts.json   # batch (see .example)
.venv/bin/python main.py --proxy host:port:user:pass
.venv/bin/python main.py --url https://example.com?btag=123
.venv/bin/python main.py --fast --phone X      # no browser, HTTP only (cricmatch)
.venv/bin/python main.py --list [--limit N] [--status success] [--filter-url U]
.venv/bin/python main.py --export-csv [file.csv] [--status ...] [--filter-url ...]
```

Generated emails are `@gmail.com`-shaped but not real inboxes — pass `--email`
to test verification. `FIRST_NAMES`/`LAST_NAMES` are Indian names by design.

### `db.py`

One `accounts` table: `username, email, password, phone, proxy, url,
referral_code, freed_phone, status, notes, screenshot, created_at`. A row is
inserted before each attempt and updated after, so failures are recorded too.
`db.COLUMNS` is the single source of truth for column order — CLI and bot both
read through it. New columns go in `_MIGRATED_COLUMNS` so old DB files
auto-`ALTER TABLE`. Passwords are shown unmasked in `--list`/`/list` on purpose;
retrieving credentials is the whole point of the table.

`referral_code` comes from `extract_referral_code(url)`, which pulls the `btag`
query param. Site-specific to this affiliate convention — generalize it if you
point this at a site using a different param name.

### Proxies

`parse_proxy()` accepts `host:port`, `host:port:user:pass`, `scheme://host:port`,
`scheme://user:pass@host:port` (default scheme `http`). Each signup gets its own
`browser.new_context(proxy=...)`, so one browser can serve many proxies.
Per-account `"proxy"` in batch mode overrides `--proxy`.

**Gotchas, all confirmed live — don't re-derive:**
- A broken proxy raises Playwright's generic `Error`, **not** `TimeoutError`.
  Catch `PWError` around `page.goto()`, not just `PWTimeout`.
- **Chromium cannot authenticate to SOCKS5 at all** (`"Browser does not support
  socks5 proxy authentication"`). `maybe_bridge_proxy()`/`stop_bridge()` launch a
  local `pproxy` subprocess that does the SOCKS5 handshake and exposes an
  auth-free `http://127.0.0.1:<port>` to Chromium. Call `stop_bridge()` on
  **every** exit path or the subprocess leaks.
- pproxy wants upstream creds in the URL **fragment**:
  `socks5://host:port#user:pass`, not userinfo (that slot is a shadowsocks
  cipher spec and silently misparses). Don't "fix" it back.
- A timeout usually means wrong protocol (try `socks5://`) or the provider needs
  your IP whitelisted — not wrong credentials.
- **Prefer residential over datacenter.** cricmatch247 sits behind an AWS
  ALB/WAF that returns a bare `403` (`server: awselb/2.0`) for IPs flagged as
  proxies. Check with `ip-api.com/json/<ip>?fields=proxy,hosting` — a working
  proxy that's WAF-blocked looks identical to a broken one until you do.

### `--fast`: HTTP-only signup (cricmatch only)

Skips Chromium entirely, ~10-20x faster. Flow: `GET /` for the csrf token +
session cookies → `POST /register` with `otp=""` (triggers SMS) → same `POST
/register` with the real OTP. Stock Laravel, no WAF on this endpoint.

Functions: `_http_session_for()`, `http_fetch_csrf()`, `http_register_call()`,
`http_signup_once()` (same result shape as `signup_once()`, but `shot` is always
`None`). Gated by `SiteProfile.supports_http_fast` — only cricmatch. `main()`
falls back to Playwright per-account for other sites, and a mixed batch only
launches Chromium if some account needs it. `--fast --no-submit` is rejected.

**Trade-off:** it hard-codes today's field names and JSON shape, so a backend
change breaks it silently instead of surfacing as a missing selector. Use
`--fast` for volume, the browser path to actually confirm the UI works.

Not verified live: the "phone already taken" JSON shape
(`_http_is_phone_taken()` guesses it). If wrong, the raw message still reaches
`result["messages"]`; only the auto-reprompt won't trigger.

### Free-number: swapping a signup's phone off the account

`POST /send_otp_touser` with `_token` + `phone` on an authenticated session
changes an account's mobile number with **no OTP re-entry**, freeing the real
SMS-capable number for the next signup. On by default (`--no-free-number` to
disable). Gated by `supports_free_number` (cricmatch only).

- Browser path: `free_phone_number(page, site_url)`, called from `signup_once()`
  after OTP verify. Fires an **in-page `fetch()` via `page.evaluate()`** — NOT
  `page.context.request`, which 500s (an out-of-band client doesn't carry
  whatever a real in-page request does). Waits ~8s first for the session to
  "settle" (a freshly-logged-in session 500s).
- HTTP path: `http_free_phone_number()` — **likely still broken**, since a
  `requests.Session` never gets the login-only cookies the fix needed.
- Both retry `FREE_NUMBER_MAX_ATTEMPTS` (15) × `FREE_NUMBER_RETRY_COOLDOWN_SECS`
  (45s) ≈ 10.5min for **any** failure. The width was earned empirically against
  a bare-403 edge block; don't shrink it, don't port it to other endpoints.
- Failure doesn't fail the signup — the account already registered. It appends
  `"Free-number FAILED: ..."` to `result["messages"]`.
- `phone` keeps the original OTP-verified number; `freed_phone` records the swap.
- No-op under `--no-submit` (unreachable, both paths return early).

⚠️ **Memory says cricmatch closed this loophole ~Aug 1-3 2026** — the call still
replies "OTP sent" but no longer changes the number, so single-number pools jam.
Verify the number actually changed before trusting it. khelofun's route exists
but 500s deterministically.

`free_account_number(page, user, pass, site_url)` does the same on an
**existing** account (logs in first) — powers the bot's `/freenum`.

### Change password on an existing account

`POST /changePassword` with `oldPassword`/`newPassword`/`_token`, found by manual
request interception (a read-only DOM/network discovery pass found nothing — the
site has no self-service change-password UI at all; the only reset flow is the
login modal's OTP "Forgot Password?").

Two conventions worth not re-guessing:
1. **Response shape is its own thing**: `{"status":200,"msg":"..."}` — key is
   `"msg"`, no `"message_class"`. Judge success on `status == 200`, not
   `http_is_error()`.
2. **The account needs a verified mobile number**, else a clean 200 with
   `"please add phone number before changing the password"`. Real business rule.

`change_account_password()` / `change_account_password_via_login()` mirror the
free-number functions (in-page `fetch()`, same reasoning). **No retry loop** —
don't add one speculatively.

`http_change_account_password()` is the no-browser counterpart (login POST +
changePassword POST on one `requests.Session`, ~2s vs ~30s), gated by the
**separate** `supports_http_change_password` flag. That flag is deliberately
NOT implied by `supports_change_password`: cricmatch supports the endpoint but
only from an in-page `fetch()`, which is the same reason
`http_free_phone_number()` is believed broken. Only starexch has it on, and
only because it was verified live. It sets `infra_block` the same way
`http_check_account_balance()` does, and `password_changer.py` records that as
a retryable `⏳` row instead of burning the row on a failure that never
happened.

### Balance reading

- `read_wallet_balance(page)` — the **site header** wallet (`span.total_balance`),
  not the in-game Evolution balance. Both spans are empty at page load; the site
  fills them via its own `getBalance()` call ~20s later on a residential proxy,
  so this **polls** (1s, 30s default timeout). A post-login redirect can kill the
  execution context mid-poll — treat as "try again," not a failure. Returns a
  float or `None`; `None` means "revisit the selector/timing," never "zero."
- `check_account_balance(page, user, pass)` → `{"ok","balance","messages","shot"}`.
- `run_balance_check(user, pass, site_url, proxy)` — one call, launches and tears
  down its own browser. Same shape as `run_change_account_password()`.
- **HTTP-fast:** `http_check_account_balance()` — `GET /` for csrf →
  `POST /login` → `POST /api2/v2/getBalance` → `balance.wallet`. ~2.8s vs 20-30s.
  Gated by `supports_http_login`.

### Login rate blocking (important, affects every HTTP-fast caller)

cricmatch247's edge blocks `/login` with a bare `403` (HTML, not JSON) on
**volume**, not concurrency — roughly 20 logins within a few minutes from one IP
trips it, and once tripped it stays blocked ~20 minutes regardless of pacing.
Concurrency makes it worse (5-wide bursts blocked every row). The plain `GET`
keeps working throughout, so it's a rate rule on POST, not an IP ban.

Handling: `http_login_call()`/`http_get_balance()` return `{"status": None, ...}`
for a non-JSON response, which `http_check_account_balance()` surfaces as
`result["infra_block"] = True`. Callers must treat that as retryable
infrastructure, not a real per-account result. `_HTTP_FAST_USER_AGENT` +
`_http_fast_browser_headers()` send a realistic Chrome header set (unproven to
help, but cheap).

**A single residential IP clears ~2 logins/min. Real throughput at scale needs
more proxy IPs, not tuning.**

---

# 2. Signup engine notes

Per-site selectors live in `sites/<site>.py` as a `SiteProfile` (`sites/base.py`);
`profile_for(url)` maps hostname → profile, falling back to cricmatch for
unknown/`about:blank`. There is no module-level `SEL` dict. Site selection is
purely by URL (`--url` / `/seturl` / `BOT_SITE_URL`).

- `open_signup_modal()` must call `dismiss_popups()` first — a promo overlay
  covers the header JOIN button. Trigger `.registerUserData`, not
  `.headerjoinBtn` (often reported not-visible).
- The signup form is JS-injected after the JOIN click, so it's not in static
  HTML. Re-inspect with `inspect_form.py [--url ...]`.
- **Leave the `wait_for_timeout(4000)` after `page.goto()` alone.** Replacing it
  with a visibility wait was tested and reproduced a real failure: the button is
  "visible" per Playwright while the overlay still covers it.
- `wait_for_register_outcome()` / `wait_for_otp_outcome()` replaced the old flat
  4s sleeps with adaptive polling on concrete DOM state (safe, unlike the above).
  `wait_for_register_outcome()` returns **`(outcome, messages)`** — use those
  messages, don't re-call `read_result()`: spin24star's snackbars auto-dismiss
  and a re-read turns a real message into "unknown error".
- `read_result()` scrapes toasts/validation; success detection is a heuristic
  (absence of "already"/"invalid") — confirm against `shots/*-result.png`.
- `check_phone_taken()` is separate: cricmatch's message is a bare `<li>` in
  `.err_phone`, which no `read_result()` selector matches. Interactive CLI mode
  reprompts up to 5 times.
- `enter_otp()`: 6 boxes `input.otp__digit_signup`, verify `a.get_user_otp`.
  **The page has a second login-OTP widget (`input.otp__digit`, no `_signup`) —
  do not target it.**
- `fill_register_form()` is shared by the initial fill and the WAF-retry refill
  so they can't drift.
- If a rejection leaves no visible message, the bot appends the POST responses
  fired by the click (status/URL/first 150 chars) to the notes — that's the
  diagnostic for "the register API was blocked/hung," which no screenshot shows.

### Adding a site

1. `.venv/bin/python inspect_form.py --url <newsite>` to capture selectors.
2. Copy `sites/spin24star.py` → `sites/<site>.py`, set `hostnames`, `sel`, flags;
   register in `sites/__init__.py`'s `PROFILES`.
3. `cp .env.spin24star.example .env.<site>`; set `TELEGRAM_BOT_TOKEN`,
   `BOT_SITE_URL`, and **distinct** `ADMINS_FILE`/`SETTINGS_FILE`/`PAIRS_FILE`/
   `PAIR_RUNS_FILE`.
4. `.venv/bin/python telegram_bot.py --env .env.<site>`.

### Site-specific

**cricmatch247** — `SITE_URL` = `https://cricmatch247.com?btag=211079`. 4 form
inputs (username, email, password, mobile) + an over-18/T&C checkbox; no name or
DOB fields despite the help text. Password policy: 5-60 chars, ≥1 digit, ≥1
special, both cases (spin24star enforces the same, so one password fits both).

**khelofun** — same white-label Laravel template as cricmatch; register-modal
selectors are byte-identical (`#userNameid`/`#userEmailid`/`#pass_log_id`/
`#phoneNumber`/`#remChck2`/`button.cls_register_new`). Dismiss the SPRIBE overlay
(`.skip_right_img`) first. OTP selectors unverified. `supports_http_fast` stays
`False`.

**starexch555** — same white-label Laravel platform as cricmatch/khelofun, added
2026-08-19 for a three-tab Google Sheet (balance / password change / multi-id
wager). Confirmed live against a real account:
- Login selectors are **byte-identical to cricmatch's** (`a.cls_loginbtn` →
  `#user_login_id`/`#passwordId` → `#loginbutton`, `#acctSec`), and
  `span.total_balance` reads the header wallet. The login modal is fetched from
  `/append/loginpp` on demand, so **those ids are absent from the static page
  source** — don't conclude they're wrong by grepping the HTML.
- `supports_http_login=True`: `POST /login` → `{"status":200,"message":"Login
  Successfully","url":"?uid=..."}`, then `POST /api2/v2/getBalance` → the figure
  in `balance.balance`/`main_balance`/`totalBalance`. **There is no
  `balance.wallet` key here** (cricmatch has one) — the `totalBalance`/`balance`
  fallback in `http_check_account_balance()` is what makes this site work.
  Measured **1.8s** per account vs ~20-30s for a browser login.
- `supports_change_password=True` **and `supports_http_change_password=True`**:
  `POST /changePassword` works from a bare `requests.Session` — no browser, no
  in-page `fetch()`, unlike cricmatch. Verified end to end (changed, logged in
  with the new password, reverted). Response is the same `{"status":..,"msg":..}`
  convention: **200 = changed, 201 = refused** ("Please enter the valid current
  password"). Note that a wrong current password fails at the *login* step
  first, so the caller sees "Invalid username or password", not the 201.
- ⚠️ A first bare `POST /login` came back a **flat edge 403**, which looks
  exactly like cricmatch's rate block — it wasn't. The field is `username`;
  sending `userName` is refused at the edge before the app sees it. **Don't read
  a 403 here as a rate block without checking field names first.**
- `supports_casino=False`, and **the tournament cannot simply be pointed at this
  site** — see the next section. `supports_login=True` carries the verified
  login on its own (the two flags were split 2026-08-19 precisely for this).
- Signup/OTP selectors were **never inspected** — the ones in the profile are
  inherited cricmatch guesses, a starting point for `inspect_form.py`, nothing more.
- Config: `.env.starexch.balance` / `.env.starexch.password` /
  `.env.starexch.tournament`, all three pointing at one workbook but **different
  tab gids**, sharing `bot_settings.starexch.json` and using
  `tournament_state.starexch.json`.

### starexch's live casino: two baccarats, only one drivable

Mapped live 2026-08-19 with `probe_starexch_casino.py` (read-only, no bets).
starexch carries **both** an Ezugi-client baccarat and the real **Evolution**
tables. Picking the wrong one silently lands on a table this engine cannot
drive, so `casino_lobby_path` pins `?p=evolution`.

**The Evolution tables work with the existing engine unchanged.** Baccarat A
(`data-id` 85) and B (86) open at `ezugi.evo-games.com` — the same host and the
same markup cricmatch uses. Verified live on a starexch table: all 11 data-roles
the hedge/tournament engine needs are present (`bet-spot-Banker`/`Player`,
`circle-timer`, `balance-label-value`, `total-bet-label-value`, `chip`,
`chip-value`, `selected-chip`, `double-button`, `undo-button`, `lobby-button`),
and `find_game_frame()` / `wait_for_live_table()` / `read_game_balance()` /
`_read_total_bet()` / `_betting_open()` / `read_chips()` all work as-is.
`_open_table_for()` seats an account in **~38s**, and the betting window cycles
healthily (~15s open on a ~35s cycle — no sign of the `blind` no-timer fault).

**Only the ROUTE to the table differs**, which is what two new `SiteProfile`
flags express (`casino_lobby_mode` / `casino_tile_mode`, both defaulting to
cricmatch's behavior so no existing site changes):
- **Clicking a "Live Casino" nav element never works** — `div.nb_rdlink
  [data-href]`, `a[href*=live-casino]` and `text=Live Casino` all time out or
  no-op. `casino_lobby_mode="direct_url"` navigates straight to the lobby
  instead (`_open_casino_lobby_direct()`), and the session **survives** the
  load — the *opposite* of cricmatch, whose `open_casino_lobby()` warns a hard
  load drops the logged-in view. Don't "fix" it back to a click.
- Tiles open via the site's own handler,
  `div[onclick="goToCasinoLive(this)"][data-id][data-provider]`, on the tile's
  **image container**. The `<p class="game__name">` label is **inert** —
  clicking it silently does nothing, which reads as "the tile didn't open"
  rather than as an error (cost a probe run to find).
  `casino_tile_mode="go_to_casino_live"` (`_click_tile_go_to_casino_live()`)
  matches the tile's exact name, so "Baccarat A" can't pick up "All In
  Baccarat". There are no category tabs in this lobby — the provider is in the
  URL, so `search_and_open_game()`'s `category` argument is unused here.
- The lobby-recovery ladder in `_open_table_for()` follows the profile too; it
  used to hardcode a bare `/live-casino` and an `a:has-text('Baccarat')`
  readiness check, neither of which exists on starexch.

**The Ezugi client (`?p=ezugi`, Baccarat A–E) is the one to avoid.** For the
record, since it looks identical to a person: it opens in a new tab on a
**randomised** white-label host (`pxoki81qhmq.xoki81qhmq.com`, different every
launch) with **no iframe** — the game is the tab's own document — and exposes a
completely different vocabulary (`data-e2e="balance-value"` /
`"total-bet-value"` / `"betting-timer"` / `"chip-<value>"`, bet spots labelled
only by `span.bet-label`). None of the engine's selectors match it. It is
driveable in principle — its chip rail is cleaner than Evolution's, and its
chips' `data-disabled` flips between rounds, which would be a better
betting-window signal than the timer — but that is a whole second game driver
and there is no reason to build one while the Evolution tables are right there.

**spin24star** — runs the "Khelo" white-label:
- No `.registerUserData`; several `button.rj__join_now` (only one visible), and a
  game section overlays it — the click **must be forced**. There's a fast-path
  returning immediately if the username field is already visible (`/?reg=1`).
- Full-screen SPRIBE walkthrough on load, dismissed via `div.skip_right_img`.
- Fields `#userNameKhelo`/`#emailKhelo`/`#passwordKhelo`/`#phoneKhelo`, submit
  `#signUpButtonKhelo`. T&C is not a real checkbox and renders pre-checked.
- OTP: `input.regOtpKhelo1`, verify `button.submitRegOtpMain`. Do **not** match
  `input.otpNumberkhelo` (login) or `input.otpNumberFp` (forgot password).
- Errors render as a top-right `div.snackbar-container` — no toast/error class.
- A taken phone surfaces as a plain `failed`, not cricmatch's `phone_taken`
  (`.err_phone` is cricmatch-specific markup).

**winclash** — added 2026-08-29, and the first site here that is **not** the
cricmatch/khelofun/starexch white-label Laravel template. Everything below was
read live out of the page's own inline JavaScript; no account was created
during discovery. It is also the reason `SiteProfile` grew `register_path`,
`otp_mode`, `otp_length`, `otp_outcome_timeout_ms`, `username_max_len`, the
three `password_*` fields, `phone_taken_texts` and `user_agent`.

- **Signup is its own PAGE, `/join-now`, not a modal** — `register_trigger=
  "page_url"`, which navigates rather than hunting a JOIN button.
- Fields `#userName`/`#email`/`#password`/`#mobileNumber`, submit
  `#signUpButton`. **No T&C checkbox exists at all.**
- **The register call is two POSTs to the same endpoint**, same shape as
  cricmatch's `--fast` flow: `POST /sign-up` with
  `{_token, user_name, email, password, confirm_password, mobile_number, otp}`
  → `status 205` = OTP sent, → `status 1` = registered + `redirectTo`,
  anything else = rejection in `data.msg`.
- **The OTP is ONE `input.signup_verify_otp` (maxlength 6), not six boxes** —
  hence `otp_mode="single"`. Counting elements would ask for a **1-digit**
  OTP, which is exactly what happens if you point the old code at it.
  `otp_digit_count()` / `fill_otp()` in `main.py` own this split and are
  shared by the CLI and the bot so they can't drift.
- **Verifying is two round trips**, which is why `otp_outcome_timeout_ms` is
  30s here: `#confirmSignupOtpBtn_` POSTs `/api2/v2/confirmSignupOtp`, and
  only on `statusCode 251` does the page's own JS re-click `#signUpButton` to
  POST `/sign-up` with the code, wait 1s, and navigate to `/`. Success is
  therefore "the OTP step went away", not an inline message.
- **`otp_error` is deliberately `None`.** `notify()` is
  `Snackbar.show(...)` (confirmed by reading `notify.toString()` live) and it
  fires on the SUCCESS path too, so treating a visible `.snackbar-container`
  as an OTP error would report every successful signup as a failure. Errors
  are still scraped into the messages via `result_selectors`; they
  auto-dismiss after 3s, so use the messages `wait_for_register_outcome()`
  captured rather than re-reading.
- Do **not** match `input.otpNumber` (the login-with-OTP widget on the same
  page) or `#userOtp` (a dead `/otp-verify` dialog left in the markup).
- **The form's own JS rejects a generated identity unless it is shaped for
  this site**, before any request is sent — which surfaces as a mystery
  snackbar, not a selector error. Username 5-12 chars, letters and digits
  only (`pattern="...{5,12}$"`; the default first+last+tag runs 13-15).
  Password 6-12 with a lowercase, an uppercase and a digit — the
  special-character rule is **commented out** in their JS — and it must not
  equal the username or the mobile number, nor be a substring of the email.
  `gen_account(prof)`/`gen_password(prof)` read these off the profile;
  called with no profile they produce the original cricmatch shape
  byte-for-byte, so no existing site changed.
- `supports_http_fast` stays **False**: `/sign-up` is behind the same WAF as
  everything else, so a bare `requests.Session` has no token to present.
- Login selectors (`#user_login`/`#pass_eye_user`/`button.btnLogin`, all on
  `/join-now`) were **observed but never driven**, so `supports_login` and
  `supports_casino` stay off. Casino/stockmarket markup is uninspected.
- The exact wording for a re-used mobile number has **not been seen yet**, so
  `phone_taken_texts` is empty and a taken number surfaces as a plain `error`
  carrying the site's own message. Add the real substring there once observed
  and the loop starts treating it as the terminal-but-expected `phone_taken`.

### winclash's AWS WAF wall is on NAVIGATION, not just the POST

Different from spin24star's block below, and it needs different handling:
winclash walls **ordinary page loads**. Confirmed live 2026-08-29 in one
session: `GET /` answered **202 + `x-amzn-waf-action: challenge`** and
`GET /join-now` answered **405 + `x-amzn-waf-action: captcha`**, both
rendering a "Human Verification" page. Nothing on the site is reachable until
that clears, so it is handled *before* the form is looked for.

- **Headless Chromium's default user agent is flat-403'd.** Confirmed by
  running the same navigation twice, changing only the UA: the default
  (`...HeadlessChrome/...`) got `403 Forbidden` as the page **title on the
  homepage**, never reaching the form, while a real Chrome UA got served the
  site. Hence `SiteProfile.user_agent` and `new_site_context()`, which is now
  what the signup paths in both `main.py` and `telegram_bot.py` use instead
  of a bare `browser.new_context()`. This is not fingerprint cleverness —
  it's the one header that decides whether the site answers.
- **The soft `challenge` action clears itself for free**, once the page's own
  `challenge.js` has run (a few seconds). `wait_out_waf_wall()` waits it out,
  and `ensure_waf_cleared()` waits before spending anything, so an ordinary
  run costs no CapSolver credit.
- **The hard `captcha` action never clears on its own** and needs
  CapSolver — the same `parse_aws_waf_challenge()` / `solve_aws_waf_token()`
  / `apply_waf_token()` machinery `submit_register()` already used for
  spin24star's register POST, now reachable from navigation too via
  `ensure_waf_cleared()`. **Set `CAPSOLVER_API_KEY` for this site** (unlike
  cricmatch/khelofun, where it is unnecessary); without it a signup fails
  with a clear message instead of hanging. Verified end to end: a run that
  had been failing cleared the wall with a solved token and filled the form.
- **The solved token goes into a FRESH context**, never the one that was
  challenged — the same rule `submit_register()` established live (AWS WAF
  tracks session state beyond the cookie). So `ensure_waf_cleared()` and
  `open_signup_form()` can both hand back a **new page**, and every caller
  must rebind to it; the old context is closed for them.
- **`waf_wall_showing()` must check `window.gokuProps`, not only
  `#captcha-container`.** The ids are injected by the interstitial's script,
  so between `domcontentloaded` and that script running the wall reads as
  "clear" — which is how a blocked signup got reported as a missing JOIN
  button. `gokuProps` is inline, so it is readable immediately, and it
  appears in **neither** saved copy of the genuine homepage or `/join-now`,
  so it does not false-positive despite `challenge.js` loading site-wide.
- Like spin24star's, the block is **behavioural/rate-based**: a fresh browser
  gets served, and repeated rapid automated loads from one IP escalate to the
  hard captcha (observed flapping between the two within one testing
  session). Volume needs more proxy IPs, not tuning.

### spin24star's AWS WAF CAPTCHA (known blocker, not a bug)

The register POST returns **HTTP 405 + `x-amzn-waf-action: captcha`** and a
"Human Verification" page. Established, so nobody re-litigates it:
- Not IP reputation (fails from a clean residential IP with no proxy).
- Not a missing token (the `aws-waf-token` cookie is present in both headless and
  headed Chromium; still 405). The rule wants an actually-*solved* CAPTCHA.
- **cloudscraper doesn't work — don't retry it.** It targets Cloudflare's
  challenge; the block here is AWS WAF, which only issues a token to a client
  that runs its `challenge.js` in a real browser.
- The block is **behavioral/rate-based**: a fresh browser gets 200; after several
  rapid signups every attempt becomes 405, and hammering escalates to a flat 403
  with no CAPTCHA offered (CapSolver can't help with that one).

Don't "fix" this with selectors or waits — it's rejected at the edge.

**CapSolver integration:** set `CAPSOLVER_API_KEY` in `.env` (read lazily via
`capsolver_key()`; with no key everything below is skipped). Shared by CLI and
bot through `submit_register(page, acct, site_url, proxy)` (no `context` param —
derived from `page.context`, since a retry replaces both):
1. `click_register_and_wait()` clicks and captures the register POST, filtering
   `token.awswaf.com` telemetry noise.
2. On error + `is_waf_captcha()` + a key: `parse_aws_waf_challenge()` reads
   `key`/`iv`/`context` from the page's inline `window.gokuProps`, then
   `solve_aws_waf_token()` hands them to CapSolver (`AntiAwsWafTask`, proxy
   passed so the solve happens from the signup's own egress IP).
3. **The retry opens a brand-new browser context**, injects the token, closes the
   old one, and resubmits there. Root-caused live: injecting a valid token into
   the *same* context still 405s; the identical token in a *fresh* context works
   immediately. WAF tracks session state beyond the cookie.

`submit_register()` returns `(outcome, msgs, captured, page)` and callers **must**
switch to the returned `page`: `signup_once()` stashes it in `result["page"]` so
`main()` closes the live context; `_blocking_fill_and_register()` resyncs
`session.context, session.page`.

---

# 3. Telegram bot (`telegram_bot.py`)

Same signup/OTP logic behind a chat interface.

```
cp .env.example .env    # TELEGRAM_BOT_TOKEN from @BotFather, MASTER_ADMIN_ID
.venv/bin/python telegram_bot.py
```

### One process per site/role (`--env` + `BOT_MODE`)

`--env <path>` lets the same script run as several independent bots, each with
its own token, site, worker thread and browser (signups for different sites no
longer serialize).

Production layout: `.env.cricmatch` (signup), `.env.spin24star` (signup),
`.env.khelofun.signup` (signup), `.env.winclash` (signup), `.env.gameplay`
(gameplay), `.env.stockmarket` (stockmarket), `.env.khelofun.stockmarket`
(stockmarket), `.env.password` (password).

**`--env` is parsed from `sys.argv` at module level and loaded with
`load_dotenv(_env_file, override=True)`. The `override=True` is load-bearing:**
`main.py` runs its own bare `load_dotenv()` at import, which happens first, and
python-dotenv defaults to first-load-wins — without it a stray repo-root `.env`
silently beats `--env` for every shared key.

`BOT_MODE` (`signup`|`gameplay`|`stockmarket`|`password`|`all`, default `all`)
decides which handlers register. An out-of-mode command simply doesn't exist —
the "/" menus and `/start` help are built from the same flags so they never
advertise a missing command.
- **signup**: `/newacc` `/done` `/cancel`, `/list` `/photo` `/export` `/stats`,
  `/setpassword` `/password` `/fast` `/freenumber`, `/setphone` family, URL/btag.
- **gameplay**: `/testbaccarat`, `/pair` `/pairs` `/delpair`, `/run` `/stoprun`
  `/runs` `/runlog`, `/freenum`. All master-only. No URL commands (gameplay always
  targets `BOT_SITE_URL`).
- **password**: `/cp` only. Deliberately NOT part of `"all"`.
- **stockmarket**: the pair/run commands against Stock Market Live.
- **all modes**: `/start` `/help`, proxy commands, admin management.

Per-instance env vars: `BOT_SITE_URL` (this instance's default site — every
former fallback to the bare `SITE_URL` import now falls back here, so
`/clearurl` resets to the right site), and `ADMINS_FILE`/`SETTINGS_FILE`/
`ADMIN_PHONES_FILE`/`PAIRS_FILE`/`PAIR_RUNS_FILE`. **Two processes must not share
these files** — they're read at import and rewritten wholesale, so they'd clobber
each other. `accounts.db` **is** shared on purpose (its `url`/`referral_code`
columns already distinguish sites).

`.env.gameplay` deliberately points at the pre-split
`pairs.cricmatch.json`/`pair_runs.cricmatch.json` — gameplay moved off the signup
bot and took its history along.

### Roles

`is_master(user_id)` / `is_admin(user_id)` (master counts as admin), enforced by
`@require_role(check)` on every handler except `/start`.

- **master** — one or more ids in `MASTER_ADMIN_ID` (comma/space separated), never
  changeable from inside the bot. All masters are equal; no demotion except via
  `.env` + restart. Can do everything.
- **admin** — added by a master via `/addadmin <id>`, persisted in gitignored
  `admins.json`. Can run `/newacc` `/done` `/cancel` and the `/setphone` family.
- **anyone else** — every gated handler replies with their Telegram user ID so
  they can hand it to the master. `/start` is intentionally ungated (it's the one
  command that shows different content per role).

Telegram's "/" autocomplete is scoped per user via `BotCommandScopeChat` in
`post_init()` and updated in `/addadmin`/`/removeadmin`. The default scope is
empty, so a stranger's menu shows nothing — a visibility control, not
enforcement (that's the decorator).

### Global settings

`global_settings` (persisted to gitignored `bot_settings.json` via
`save_settings()`) holds `proxy`, `url`, `password`, `fast`, `free_number`,
`phones`, `phone_idx`. Master-only, global across all admins — this replaced an
earlier per-chat design.

- `/setpassword <pw>` fixes every future signup's password; `/setpassword
  --random` **removes** the key (random mode is the absence of a value, which is
  why `/password` says `RANDOM (default, per-signup)` rather than a placeholder).
- `/fast on|off` — decided **once** in `begin_signup()`:
  `session.use_fast = fast_wanted and profile_for(...).supports_http_fast`. If ON
  but unsupported, `begin_signup()` says so in the phone prompt and falls back.
  Fast helpers touch no Playwright object, so they run on the **default** thread
  pool (`run_in_executor(None, ...)`) and never consume a `_pw_executors` slot.
  State between the two calls lives on `session.http_session`/`session.http_csrf`.
- `/freenumber on|off` — same lifecycle, but **on by default**:
  `global_settings.get("free_number", True)`. Gated by `supports_free_number`.
- `/btag <code>` rebuilds the global URL keeping scheme/host/path and replacing
  the query with `btag=<code>`; bare `/btag` shows the active code.
- `/setproxy` `/proxy` `/clearproxy` `/testproxy [proxy]` — `/testproxy` opens a
  throwaway context and hits `api.ipify.org`; if an `http(s)://` proxy times out
  it retries once as `socks5://` and says if that fixed it. Replies never echo a
  proxy password (`mask_proxy_display()`).
- `/stats` groups by `status` and by `referral_code` (`COALESCE(..., '(none)')`);
  `/stats <btag>` shows that one btag's status breakdown.

### `/setphone`: rotating a pool of real numbers

`/setphone <n1> [n2] ...` sets `phones` + resets `phone_idx`; `--random` clears
both (back to prompting). `/addphone` `/delphone` `/phone`. A settings file
holding the old single `phone` key is migrated to `{"phones": [n]}` at import.

**Admin-usable, and each admin's pool is exclusive.** `_resolve_phone_store(uid)`
(reads) and `_write_phone_store(uid)` (writes): the master always uses
`global_settings`; a plain admin reads their own `admin_phones` entry **if they
have one** and otherwise inherits the master's pool, but always *writes* their own
entry — so one admin can never clobber another's or the master's. `--random` /
emptying via `/delphone` removes an admin's entry entirely, so they go back to
inheriting. **Don't generalize this per-admin pattern** to password/proxy/URL —
phone pools needed it because two admins looping on the same number race into
`phone_taken`, which isn't a concern for a shared password.

**Why a pool, not one number:** a single fixed number still intermittently hit
"already in use" even with free-number's ~10.5min retry budget — the free-number
call reported success but the next round's register raced ahead of the backend
reflecting it. A pool of N gives each number N-1 rounds of slack.

`_next_fixed_phone()` pops `phones[phone_idx % len]` and persists the advance, so
rotation survives a restart. `begin_signup()` calls it and, if non-`None`, skips
the phone prompt and calls `_submit_phone(...)` directly — the same helper
`handle_message()`'s `await_phone` branch calls, extracted so the two can't drift.

`ROUND_COOLDOWN_SECS` (12s, env-overridable): `_auto_restart()` sleeps this before
the loop restarts, but **only in pool mode** (ask-each-time is already paced by a
human typing). Paired with free-number's 8s pre-call settle wait — same problem,
one fix on each side.

### Continuous signup loop

`/newacc` adds the chat to `looping_chats` and calls `begin_signup()`. After any
terminal outcome, `_auto_restart()` starts a fresh account with no further
command needed.

- `/done` — removes from `looping_chats` only. A signup already in flight
  completes normally.
- `/cancel` — both: clears the loop **and** tears down the session now.

Get that distinction right if you touch either handler.

`phone_taken` is a terminal outcome (like success/failed) — it records, closes the
context, and moves to the next account rather than waiting for a new number.

### Screenshots and replies

`build_caption()` formats an account dict or `db.COLUMNS` row into a caption
(capped at Telegram's 1024 chars). `send_result_photo()` sends the screenshot
with it, falling back to text if the file is missing.

**Success and failure are both terse in chat, by design** — a signup gets exactly
`"Signup successful! (#id)"` or `"Signup failed. (#id)"`. The real reason is
logged to console and stored in `notes`/`screenshot`. This was an explicit
request (credentials shouldn't land in chat on every attempt). `/photo <id>` and
`/export` are the deliberate ways to pull them. **Don't reintroduce
`send_result_photo()` into `handle_message()`** thinking it's an oversight.

`_blocking_fill_and_register()` screenshots after the REGISTER click in every
failure branch, and saves `*-no-modal.png` if `open_signup_modal()` itself fails.

### CSV export

`db.export_csv(conn, path, limit, status, url, row_id)`. `send_csv()` wraps it in
a `NamedTemporaryFile`, sends via `reply_document()`, deletes in a `finally` —
follow that pattern rather than writing into the repo directory.

**`/export` defaults to `status="success"`** (say `/export all` for everything);
the CLI's `--export-csv` has no default filter. Both deliberate. Args parse in any
order: digits → limit, `http(s)://` → URL, `all` → clear filter, anything else →
status. The `url` filter is an exact match, so it never matches the `NULL`
default-`SITE_URL` rows. `/export` stays master-only in all forms.

### Browser lifecycle

One Chromium is launched at startup (`_blocking_ensure_browser()`, pre-warmed in
`main()`) and reused; each session opens its own `BrowserContext`. **All
Playwright calls for a browser must run on the thread that launched it** — hence
`_pw_executors` (single-worker pools). Teardown goes through the same worker
thread; never call `context.close()`/`browser.close()` from the event loop.

Measured: browser reuse saves only ~0.5s. The real costs are ~8s of page
load/hydration plus Telegram's own per-message latency, so the bot always feels
slower than the CLI.

### `/cp`: change password (password-mode bot)

`/cp` alone starts the flow (adds the chat to `pending_changepassword`); the next
plain message is parsed as `<username> <current_password> [new_password]`. This
two-step shape was a deliberate simplicity request. Omitted new password → a
random one via `gen_password()`, reported in the reply. `handle_cp_message()`
re-checks `is_master()` itself (a `MessageHandler` isn't covered by the
decorator) and silently ignores stray text otherwise.

The old password is never echoed; the new one **is** included on success (matching
`build_caption()`'s norm for master-only chats). **Text-only reply, no screenshot**
— deliberate; the shot is still taken and stored on disk.

### `/freenum <username> <password>`

Frees the number on an account you already have (vs `/freenumber`, which fires
automatically after a signup). Master-only. Uses `main.free_account_number()`,
which needs `supports_casino`'s login selectors — cricmatch only. Runs on
`_pw_executors[0]` with a throwaway context, like `_blocking_test_baccarat()`.

---

# 4. Casino: baccarat smoke test + paired hedge

## `/testbaccarat <user> <pass> [amount]` (master-only)

`login()` / `open_casino_lobby()` / `search_and_open_game()` /
`place_baccarat_bet()` / `test_baccarat()` in `main.py`. Logs into an **existing**
account (credentials are args, not from `accounts.db`) and places a real bet, to
confirm the third-party game integration works. Writes nothing to `accounts.db`.

**Verified against cricmatch247 only.** The login/casino `sel` keys are single
cricmatch values, not cross-site.

Established live:
- Login: `a.cls_loginbtn` → `#user_login_id` / `#passwordId` → `#loginbutton`.
  `#acctSec` in the header is the logged-in indicator `login()` polls for.
- ⚠️ `#acctSec` alone was a **false positive** (present when logged out), causing
  fake "please add phone number" errors and fake ✅ on password changes. `login()`
  now verifies real auth via `getBalance`.
- Casino nav: `a:has-text('Live Casino')` then category tab
  `a:has-text('Baccarat')` (there's no free-text game search, only tabs). Both
  clicks must be **forced** — cricmatch shows the same SPRIBE overlay
  (`.skip_right_img`) that intercepts clicks.
- A game tile opens a **new browser tab**, cross-origin at `ezugi.evo-games.com`.
  Never embedded. Track `context.pages` and close it separately.
- The table is a `<canvas>` video feed, but **the Player/Banker bet spots are real
  DOM** — no coordinate clicking needed. This was the biggest risk going in and
  didn't materialize.
- Bet-spot targeting is the fragile part: class names are hashed, and the
  *collapsed* paytable tooltip contains the literal text "BANKER" even while
  hidden — a naive text match mistargets it (this happened live and bounced out to
  the lobby). `_TAG_BET_SPOT_JS` excludes `[data-role*="bet-limits"]` /
  `[data-role*="tooltip"]`, off-screen, zero-sized and oversized elements, then
  picks the smallest match.
- A decorative SVG glow sits over the real spot, so clicks need `force=True`.
- **Chip denomination isn't selectable here** — a click places whatever chip is
  pre-selected. `amount` is advisory: `place_baccarat_bet()` reads the game's own
  "TOTAL BET" counter after each click and refuses/reports a mismatch.
- **Table minimum is ₹100/side** on Baccarat A and B (the only two tables).
  `round_attempts` retries because a click during the results phase is a silent
  no-op — Evolution only accepts bets during the countdown.
- **Leaving before the timer expires voids staged chips at no cost** (confirmed:
  a mistargeted click left a ₹100 chip placed, wallet and exposure unchanged).
  Don't rely on it as a safety net — a bet that fully registers is real money.

## Paired hedge (`/pair` `/pairs` `/delpair` `/run` `/stoprun` `/runs` `/runlog`)

Two accounts on the **same live table** bet opposite sides of the **same hand**,
so money mostly moves between them — only the ~5% banker commission bleeds. All
master-only.

- `/pair <u1> <p1> <u2> <p2>` — **acc1 always Banker, acc2 always Player** (fixed).
  Returns a pair id. Replies never echo passwords.
- `/run <pair_id> <amount> <rounds>` — streams per-round progress prefixed
  `[Pair #<id>]`.
- `/stoprun [pair_id]` — one run, or all.
- `/runs [pair_id]`, `/runlog <run_id>`.

Persistence (both gitignored, both env-overridable per instance):
`pairs.json` (**plaintext passwords**) and `pair_runs.json` (no passwords, but
still the owner's operational data). `run_cmd` appends a run record after **every**
`/run`, success or not; `/runlog` reads the per-round `rounds` list plus
`start_balance`/`ended_at` from `run_paired_hedge`'s summary — don't drop them.

### `run_paired_hedge(...)` — the engine

Reuses `login()`/`open_casino_lobby()`/`search_and_open_game()`/
`find_game_frame()`/`wait_for_live_table()`/`_click_bet_spot()`/`_read_total_bet()`
plus `read_game_balance(frame)` (Evolution's own
`data-role="balance-label-value"`) and `_open_table_for`/`_table_id`.

**Money-relevant facts:**
- **Bonus balance changes the launch path.** New accounts pop a "CHOOSE CHIPS"
  gate. The "REAL CHIPS" *label* has no handler — the clickable element is
  `div.cls_play_act_bal.redirectLink` — and choosing it navigates the **same tab**
  to the provider (`vt_id=`) instead of opening a new tab (`table_id=`). Handled
  by `_dismiss_choose_chips_modal()`. Untested edge: a pair where only one side
  has bonus would get `vt_id` vs `table_id` and abort on the same-table check
  (safely, no bets).
- **Same physical table is required.** Both tabs' `table_id` are compared and the
  run aborts before any bet if they differ, or if either can't be read — refusing
  to bet beats assuming a skipped check passed. (`_table_id()`'s regex was
  lowercase-only, which silently disabled this guard for `StockMarket00001`.)
- **Setup runs both accounts in parallel** on two threads/browsers. The Player
  side gets a temporary browser + `player_exec` (single-worker pool) via
  `_launch_pw_browser()`; the Banker side either reuses a caller-supplied
  `browser` or launches its own. If either side fails or `/stoprun` fires
  mid-setup, whichever side succeeded is closed **on its own owning thread** —
  easy to leak or double-close across threads, get it right. Both are torn down in
  the `finally` on every exit path or a Chromium + driver leaks per run.
- **Both bets go down back-to-back in one window** (a >1s gap loses it). The
  Player-side call is `player_exec.submit(...)`'d first, the Banker call runs
  inline, then the future is joined — genuinely concurrent on two threads. Every
  paired read (`_betting_open`, `_read_total_bet`, `read_game_balance`,
  `_table_id`) uses the same pattern.
- Setup progress lines (🔑 login, 🎰 lobby, 🃏 joining, 📡 waiting, ✅ ready) go
  through a separate `setup_progress` callback; the bot routes it to the console
  only (chat was too noisy), so Telegram sees just the start card, `✅ Round N/M
  hedged` lines, and the summary. Don't drop the calls.

**Retry vs stop.** The round loop is attempt-based, not `for rnd in range(rounds)`:
- **Retried** (after `ROUND_RETRY_COOLDOWN_SECS=6`): a missed window, a one-sided
  landing (waits out `game.settle_secs`, screenshots both tabs, logs to
  `summary["unhedged_rounds"]`, doesn't count toward `rounds_done`), and a
  both-sides-equal-but-short stake. `MAX_CONSECUTIVE_ROUND_FAILURES=5` in a row
  with zero progress gives up (`repeated_unhedged_exposure`, `no_open_window`,
  `short_stake`), plus a `rounds*4`/min-20 ceiling (`max_attempts_exceeded`).
- **Immediate stops, deliberately not retried**: `banker_out_of_balance` /
  `player_out_of_balance` (waiting doesn't refill), `amount_mismatch` (waiting
  doesn't change the chip menu), `chip_select_failed`, `different_tables`,
  `setup_failed` (setup has its own 4-attempt retry), `stopped_by_user`.

**Concurrent runs.** Each `/run` is self-contained (own browsers, own threads) and
dispatched onto `_run_executor` (`ThreadPoolExecutor(MAX_CONCURRENT_RUNS)`, env,
default 3). `_active_runs` maps `pair_id → {stop_event, banker, player}`, so
`/stoprun <id>` stops one and bare `/stoprun` stops all. `run_cmd` refuses a pair
already running **and** a pair sharing any username with a running pair — betting
one login from two contexts corrupts both hedges. `/pairs` tags running pairs;
`/delpair` refuses mid-run. Progress reaches chat via
`asyncio.run_coroutine_threadsafe(bot.send_message(...), loop)` — the only
thread→async bridge in the bot.

### Stock Market Live (`BOT_MODE=stockmarket`)

Evolution's Stock Market Live: the two accounts bet **UP vs DOWN** on the same
round. Same `/pair`/`/run` commands; the game is fixed per instance like
`BOT_SITE_URL`, so `/run` needs no extra argument. Attractive because the table
minimum is **₹10** (vs Baccarat's ₹100) and a settled hedge bleeds ~nothing.

`GameProfile` (`sites/games.py`) is the game-level counterpart to `SiteProfile`.
`BACCARAT` reproduces the old hardcoded behavior exactly and is the default.
**Don't guess values for a new game** — run the probes, read the dump, then fill
in a profile.

Differences, all confirmed live:
- Bet spots are **`SM_Up`/`SM_Down`**, so `_click_bet_spot()` takes a complete
  `data-role`, not a suffix interpolated into `bet-spot-{}`.
- **No `circle-timer`, and role presence can't detect the window** — the visible
  role set is byte-identical in every phase. The phase lives in the *text* of
  `[data-role="instruction-message"]` ("PLACE YOUR BETS n" / "NEXT GAME SOON"),
  which `window_mode="instruction"` reads. The window is ~10s on a ~21s cycle
  (tighter than baccarat's ~15s), hence `place_secs=220`.
- **The game isn't in cricmatch's catalogue** (206 tiles checked, plus the site's
  own search). It's reachable only through Evolution's in-game lobby: open
  Baccarat A, click `[data-role="lobby-button"]`, search there.
  (`_open_via_provider_lobby()`.)
- **The provider lobby is a separate iframe** (`?iFrAmE=x`); only it has the
  Search box. `find_game_frame()` returns the *game* frame (most DOM nodes), which
  has no search input — typing into "the frame" silently does nothing.
  `_find_provider_lobby_frame()` identifies it by its category tabs, which is
  stabler than the URL. **Any stray click dismisses the overlay** — click only the
  three things needed.
- The LOBBY button only exists once the entry game has rendered, and
  `find_game_frame()` can return during the loading screen — so
  `_open_via_provider_lobby()` **polls** for the button.
- `read_game_balance()` and `_read_total_bet()` work unchanged.

**Cash-out is OFF (`needs_cashout = False`) and should stay off.** Four real
₹10/side rounds netted ~zero across the pair (3749→3748→3748→3749): both sides
hold equal opposite positions, so settling is already a complete hedge. Not
cashing out is also *cheaper* (the 1% fee is charged on cash-out) and removes all
timing risk. An un-cashed position settles normally, it isn't forfeited.

The cash-out code is retained and structurally correct but **its click doesn't
reliably register**. It was flipped back to `True` once on the strength of a real
fix (`_cashout_ready()` no longer gates on the CASH OUT label's CSS opacity, which
doesn't track enablement) and **re-broke** on a real ₹100/side run. So the opacity
gate was *a* cause, not *the* cause, and it isn't root-caused. Don't flip it back
without a fresh live-verified fix. If it ever is re-enabled: cash out as **early**
as possible and fire both clicks concurrently; a side that doesn't close is
retried once, then `cashout_partial` names the exposed account; a
`cashout_tolerance` (5%) divergence guard stops with `cashout_divergence` — it
**detects** after the fact, it can't prevent. Note `read_portfolio()` is the
"is there a position" signal, **not** the button state — the button reports
`disabled=false`/`opacity=1` with nothing staked, and its text is
`"PORTFOLIO\n1% FEE\n₹0.00"`, hence parsing the **last** number.

### Arbitrary bet amounts

`amount` no longer has to be one of the rail's chip values — any amount they sum
to works (₹150 = 100+50). The same plan is replayed on both sides every round so
the stakes stay equal.

The rail is real DOM (`chip` ×6, `chip-value` ×6, `selected-chip`, `double-button`,
`undo-button`). `chip-value` renders its number as SVG with empty `innerText`, so
`read_chips()` reads `data-value`/`textContent`, **never** `innerText`.

- Solver lives in `chip_plan.py` (`plan_stake()` + `group_plan()`), moved out of
  `tournament.py` because `tournament.py` imports `main`, so `main` can't import
  back. `tournament.py` keeps a thin wrapper with its own defaults.
- The plan is computed **once** before the round loop. An unreachable amount
  aborts **before any bet** and names the closest reachable size — it never
  silently rounds down.
- `HEDGE_MAX_BET_CLICKS` (6, env) caps clicks per side. This trades against the
  ~10s window: a window closing mid-stake leaves the sides staked **unequally**,
  which is real exposure. The cost of a small budget is only which amounts are
  reachable, never a half-placed bet. (`tournament.py` uses 8 inside baccarat's
  wider window.)
- `_place_stake()` switches denomination between groups via `select_chip_fast()`
  (~5s deadline) — `select_chip()`'s 75s default is right for the once-before-the-
  loop call but would eat a whole window mid-round. The largest chip is
  pre-selected before the loop and re-selected between multi-chip rounds.
- TOTAL BET verification is a **poll** (up to 5s), not a flat sleep — several
  clicks reach their final total later than one does.
- Post-placement: **both sides equal but short** → still hedged, retry the slot
  (`short_stake` only after 5 in a row). **Sides landed different amounts** → the
  *difference* is real one-sided exposure; `unhedged_rounds` records the gap and
  names the account holding it.

Not yet run live: do one `/run <pair> 150 1` and check `/runlog` shows ₹150/side.

## Knockout tournament (`tournament.py`, `tournament_runner.py`)

Many accounts play baccarat down to **one winner holding the whole pot** — the
opposite goal from `run_paired_hedge`, which exists to keep a pair's combined
balance flat. Each pair stakes `min(both balances)` on one hand.

**Game choice is not configurable and the reason matters.** A knockout needs the
loser's entire stake to move. Baccarat costs ~1.2%/round (5% commission on the
~46% of Banker wins) and a tie just returns both bets. Roulette's zero and dragon
tiger's tie *destroy* money rather than move it. **Stock Market is unusable** —
payout is proportional to how far the chart travelled, so the loser keeps most of
their stake and nobody is ever knocked out.

**Baccarat's chip rail IS real** (`data-role="chip"`, values 100/500/2500/10000/
50000/100000) — contradicting `sites/games.py`'s `selectable_chips=False`. It's
only interactive **during** the betting window: between rounds exactly one chip
node renders at 32×32 with `cursor:auto` (a display, not a control), which is why
an earlier probe concluded it was fake. So chip selection must happen **inside**
the window, alongside the bet clicks — not before the loop the way
`run_paired_hedge` does for Stock Market. `verify_table_chips()` re-checks the
rail against a live seat and refuses to bet on a mismatch.

**Groups, not a flat bracket.** A flat bracket re-pairs every survivor globally
each round, so with ~10 browsers that means logging everyone back in — ~200
logins for 100 accounts, well past the ~20-logins-in-a-few-minutes 403 block.
Playing a group of ~10 all the way down to one winner keeps pairings inside the
group: ~110 logins and ~50 hands instead of ~200 and ~99. Same end state.

`Seat` = one account, one browser, one OS thread (Playwright thread affinity —
every touch goes through `.call()`).

### Rules that were each worth real money

- **Knocked out means DRAINED, not "lost a hand."** A stake is only
  `min(both balances)`, so a richer account that loses keeps the difference.
  Eliminating it there stranded that money: a mock 100-account run ended with the
  winner holding 36.6% of the pot and one account knocked out of the final still
  holding 30,009. Replaying until the loser can't cover the table minimum drains
  each loser to under ~100.
- **The bye goes to the SMALLEST balance** — a short stack otherwise drags its
  opponent's stake down and strands money in the winner.
- **Classify a placed bet by what's ACTUALLY on the table, not by whether it
  matches what was asked.** A real run read TOTAL BET 600 and 500 against a wanted
  900; because neither equalled 900 the old check reported "no money at risk" and
  moved on. Both bets were real, the hand ran, and the accounts moved −500/+570
  with the run recording neither. **Zero is the only reading that means nothing
  was staked.**
- **Each chip click is confirmed against TOTAL BET before the next is sent.**
  Firing blind, a 400 stake (four 100-chip clicks) landed as 100 on one side and
  200 on the other — repeated clicks at the same coordinates arrive as
  double-clicks and the game drops them. `CLICK_SPACING_SECS` spaces them up
  front, which is cheaper than detecting each drop.
- `place_stake()` **tops up a short stake** while the window is still open, and
  stops the moment it closes (topping up after would stack onto the next round).
- `MAX_BET_CLICKS = 8` trades stranded money against the ~15s window: 6 clicks
  strands up to 7.4%, 8 → 2.4%, 10 → 1.4%. The ~100 sub-chip remainder is a floor
  no budget fixes.
- `DEFAULT_TABLE_MAX = 200_000` is the conservative one of the two caps the BET
  LIMITS panel showed; betting over the real cap is silently rejected, which
  leaves a side unhedged. Raise it only after reading the expanded panel.

### A group only ends when one account holds the pot

**The only thing that removes an account is being drained below the table
minimum.** Every other outcome — `tie`, `not_placed`, `unhedged`, `error` —
**replays the pair** after `RETRY_WAIT_SECS` (20s), because all of those causes
are transient and retrying instantly just misses the same window again. Balances
are re-read from the live table at the top of every hand, so even an unhedged or
unreadable one self-corrects on the replay.

**This was worth the whole pot (2026-08-11).** The last hand of a five-account
group came back `not_placed` — "no betting window opened in time, no money was
staked" — and the old code dropped *both* finalists, emptying `survivors` and
ending the group with `winner: null` and ₹4,850 stranded across two live
accounts. Nothing had gone wrong with the money; the code treated "nothing
happened" the same as "something broke". **Don't drop an account on a non-`ok`
status again.**

**`not_placed` vs `no_window` — replay one, re-seat the other (2026-08-17).** A
khelofun run played 150 hands over **2h25m and staked nothing**: every hand
timed out in `wait_for_window_open()` with `timer=None`, i.e.
`[data-role="circle-timer"]` was never present, so `_betting_open()` answered
False forever. Seating had succeeded and the chip-rail check had passed, so the
frame was the real game frame — only the phase signal was missing. The give-up
ladder worked exactly as designed (10 stalled hands × 3 stages) but each hand
first burned the full `WINDOW_WAIT_SECS`, which is where the 2.5 hours went. No
money was lost; nothing was ever staked.

So `wait_for_window_open()` now returns a fourth value, **`"blind"`**: the frame
answers every poll but the window never opened *once* in `wait_secs` — six full
~40s table cycles at the default. A seat that misses one edge is unlucky and
worth replaying; a seat that has never seen a window is on a table that is not
dealing to it, and only a fresh seat can fix that. `blind` seats therefore ride
the same `dead_seats` channel as a dead frame and end the group immediately
(status `no_window`), turning ~145 minutes of futile replay into ~4. Plain
`False` now means only "windows are opening, we just missed the edge" — verified
by test: a frame open at every poll (joined mid-window, no closed→open edge)
returns `False`, **not** `blind`, so the new case can't over-trigger.

**Root cause: Chromium background-throttling. Found and fixed 2026-08-19.**
Chromium throttles timers, rAF and rendering in any page it considers
backgrounded or occluded. `circle-timer` is an **animated** element, so a
throttled page keeps answering DOM queries — no errors, no dead frame, the
chip-rail check still passes — while never *drawing* the timer at all. That is
precisely the "timer=None forever" fault, and it is why `blind` could not tell
it apart from a table that wasn't dealing.

Measured on starexch with `probe_baccarat_window.py`, 5 seats on one table,
one browser per seat, sampled once a second for 100s:

| seat (open order) | timer seen, before | after |
|---|---|---|
| 1st | **0**/98 | 31/98 |
| 2nd | **0**/98 | 42/98 |
| 3rd | 22/98 | 45/98 |
| 4th | 7/98 | 46/98 |
| 5th | 33/98 | 46/98 |

Zero errors on every seat in both runs, and a **lone** seat on the same table
saw 30/99 — so the table was dealing normally throughout. The before-gradient
tracks how long a page had sat un-focused, nothing about the account or table.

The fix is `_ANTI_THROTTLE_ARGS` in `main.py` (`--disable-background-timer-
throttling`, `--disable-backgrounding-occluded-windows`,
`--disable-renderer-backgrounding`), passed by `_launch_pw_browser()`. **Any
new launch path that will hold a live table open must pass them too.**

This **supersedes** the earlier "not established" verdict and corrects one of
its conclusions: concurrency *was* the trigger after all. The 2026-08-18
khelofun probe that "ruled out concurrency" held four seats and saw 27/60 on
each — consistent with this, since that probe's seats were all young. What
matters is not how many seats exist but **how long a page has been
un-focused**, which is why a short 4-seat sample missed it and a real
2h25m tournament did not. The bonus-balance / `vt_id` suspicion is also moot:
starexch launches every table as `vt_id=` and works fine once un-throttled.

Give-up ladder, widest to narrowest — each layer only fires when the one below
has genuinely stopped helping:
- A `blind`/`table_lost` seat ends the group at once — replaying cannot mend
  either, only a fresh seat can.
- `MAX_STALLED_HANDS` (10) consecutive hands knocking nobody out ends the group.
- `MAX_GROUP_HANDS` (60) total hands ends it regardless.
- A group that ends without a winner returns its still-funded seats, and
  `run_tournament` **carries them into the next stage with their real balances**
  rather than marking them eliminated.
- `MAX_STALLED_STAGES` (3) stages with zero eliminations ends the tournament. A
  stage replay re-seats everyone in a fresh browser with a fresh login, which
  fixes what an in-group replay can't (a dead frame, a seat on the wrong table),
  so it's worth doing more than once.
- `play_group` **never names the first survivor as winner just to have one** —
  that would report a winner who doesn't hold the money. A run that can't finish
  sets `summary["unfinished"]` and a problem naming exactly who holds what.

### Seat decay: a seat is only reliable while it is young

**Measured on starexch 2026-08-22 with `probe_seat_decay.py`, and it explains
every "no betting window opened in time" report.** Ten seats on one table,
opened 15s apart, sampled once a second for ~3 minutes:

| seat, in the order it was opened | 1st | 2nd | 3rd | 4th | 5th | 6th | 7th | 8th | 9th | 10th |
|---|---|---|---|---|---|---|---|---|---|---|
| betting windows seen (of 175) | 16 | 31 | 32 | 48 | 45 | 46 | 60 | 64 | 63 | 80 |
| video feed live (of 175) | 45 | 81 | 81 | 121 | 121 | 121 | 157 | 157 | 157 | 174 |

Perfectly monotonic in **open order**, with the video feed decaying in step.
The oldest seat was about two minutes older than the youngest and saw a fifth
as many windows. Extrapolated, a seat is useless after roughly 5-10 minutes —
while a tournament wants to hold one for half an hour. That is the whole fault:
hands 1-2 of a group land, then everything goes blind.

**What it is NOT** — each ruled out by its own run, don't re-litigate:
- *Tab visibility.* Every seat reports `document.visibilityState === "visible"`,
  and `bring_to_front()` on half the seats changed nothing.
- *The video stream.* `--noautoplay` and pausing every `<video>` changed nothing.
- *An idle session.* `--poke` clicks a chip (not a bet) every 15s; no change.
- *Machine CPU/RAM.* 32 cores at ~40%, 123GB free, and renderers never errored.
- *Seat count.* 10 seats behave like 6; both work when freshly opened.
- *The proxies.* The decay is monotonic in open order while proxies are handed
  out round-robin, so seats 1 and 6 share an exit IP and do **not** match.
- *The site or the table.* Ten fresh seats on the very table a tournament had
  just failed on saw windows ~40% of samples, exactly the ~15s-open-of-~35s
  cycle.

**Reloading the game tab does not repair a decayed seat** — it loses the frame
outright (`find_game_frame` returns nothing), and reloading five seats at once
knocked out the other five as well. Only a fresh seat helps.

**What follows from it**, and what the code now does:
- `WINDOW_WAIT_SECS` cut 240s → 100s. A long wait cannot help: when it fails
  the group ends and is re-seated anyway, so four minutes of waiting bought the
  one repair that works four minutes late *and* aged every other seat by four
  minutes.
- **Seat fast so every seat is young at hand 1.** With `TOURNAMENT_LOGIN_SPACING`
  at 20s, ten seats take 200s to open — the first seat is ~4 minutes old before
  the first bet, i.e. already half dead. Now that `_LoginPacer` queues **per
  exit IP**, five proxies at 5s spacing is one login per IP every 25s — safer
  than the old global 20s ever was, and the group is seated in 50s.
- Keep groups small enough to seat quickly; a group that takes longer to seat
  than a seat survives can never work.

### Speed: where a run's time actually goes

Measured on the real state files, not estimated. The 2026-08-19 starexch run
(5 accounts, 3 stages, 11 hands) took **24 minutes**, split roughly:

| cost | share | why |
|---|---|---|
| 2 × `no_window` hands | ~8 min | each burns the full `WINDOW_WAIT_SECS` (240s) |
| re-seating for 3 stages | ~6 min | and both extra stages were *caused* by those two hands |
| the 9 real hands | ~6 min | ~40s per baccarat cycle — the irreducible part |

So on a small roster the tournament is mostly overhead, not gameplay, and the
`no_window` path is the single biggest item. Two changes attack it:

**Cross-seat blind detection (`_join_window_waits`).** Every seat in a group is
at the SAME physical table and is dealt the SAME window at the same moment, so
the other seats are a live control. Once one seat reports a fresh window and
`CROSS_SEAT_GRACE_SECS` (60s, about 1.5 table cycles) passes, a seat that has
still reported nothing is not unlucky — it is not being dealt to, i.e. `blind`.
That verdict now lands in ~100s instead of 240s, and since a blind seat ends
the group it saves the re-seat it would have triggered too. `should_stop` on
`wait_for_window_open` is what makes the laggards return promptly — **it is not
optional**: a thread still stuck in that poll loop holds its seat's executor,
and therefore its Chromium, open past `close()`.

This only ever decides to STOP EARLY — it can never cause a bet. A seat wrongly
called blind costs one re-seat, not money. When *every* seat is blind there is
no control to compare against and the full 240s is still spent, which is the
case that deadline was earned for.

**`_LoginPacer` queues per exit IP, not globally.** The block is per IP, so ten
seats on one proxy still go out one per `login_spacing`, but ten seats over five
proxies go out five at a time. This is the line that turns extra proxy IPs into
actual seating speed — the thing CLAUDE.md keeps saying is the only real fix.

**The floor.** A hand is one ~40s table cycle and a knockout needs ~log2(N)
of them at best (7 for 100 accounts, 5 for 30), so ~4-6 minutes of play is the
hard floor no code change touches, plus ~40s to load a table. Everything above
that floor is seating, re-seating and stalls. A sub-10-minute run is reachable
for a roster that fits in one group with no re-seat; it is not reachable for
100 accounts on one machine, where the wall is how many live-video Chromiums
the box can hold.

### 30 browsers at once is a self-inflicted outage (`TOURNAMENT_MAX_SEATS`)

The 2026-08-23 starexch run (30 accounts, `TOURNAMENT_PARALLEL_GROUPS=10`,
i.e. 30 live-video Chromiums at once) spent **94 minutes finishing nothing**:
every full-size group returned `no_window` on every hand across 7 stages,
seats failed with "game tab opened but its UI frame never loaded", and the
ONE group that happened to hold just 3 seats played 3 clean hands and produced
its winner immediately. Same code, same site, same proxies — the only variable
was concurrent browsers. The box is the ceiling, not the site.

`tournament_runner.clamp_lanes()` therefore caps `group_size × lanes` at
`TOURNAMENT_MAX_SEATS` (default 12) by reducing lanes, loudly. It never
touches `group_size` (that changes the bracket the operator asked for). Raise
the budget only after `probe_seat_decay.py` shows THIS machine holding more
seats healthily.

### Running on a cheaper table (`TOURNAMENT_LOBBY_TILE`, live chip rail)

Baccarat A's smallest chip is ₹100, which is where both the leftovers and a
lot of the hands come from: an account holding 160 can only stake 100, so
draining it takes extra hands and knocks it out still holding up to ₹99. A
table whose smallest chip is ₹10 drains a loser in ONE hand and strands at
most ₹9 — the single biggest speed *and* cleanliness lever.

- `TOURNAMENT_LOBBY_TILE=<tile name>` points the whole tournament at a table
  in **Evolution's own in-game lobby** (same hop Stock Market uses:
  Baccarat A → LOBBY button → search). The profile is a `replace()` of
  `BACCARAT`, so bet spots/timer/balance selectors are unchanged — only pick
  baccarat-family tables this way. Set `TOURNAMENT_TABLE_MIN` to match.
- The table's chip rail is **read live off the first seat** of each group
  (`verify_table_chips(expected=None)`) and every stake is planned from what
  the rail actually offers; `TOURNAMENT_CHIPS` exists as an exact-verify
  override but normally stays unset. `plan_stake`/`place_stake`/`play_hand`
  all take `chips` now — the top-up path included, which used to silently
  re-plan with the hard-coded ₹100 rail.
- `probe_lobby_tables.py --env <F>` (read-only, one seat, no bets) is how a
  table gets chosen: default mode dumps every lobby tile matching each search
  term; `--open "<tile>" --secs 90` actually switches to the named table and
  reports its real clickable rail, window cadence, and BET LIMITS text, then
  prints the exact env lines to set.

### The cleanup run: leftovers die on Auto-Roulette (`TOURNAMENT_GAME=roulette`)

The main knockout stays on baccarat (cheapest mover of money), but its ₹100
smallest chip means every eliminated id keeps up to ₹99. The fix is a SECOND
knockout over the same sheet with `TOURNAMENT_GAME=roulette` +
`TOURNAMENT_TABLE_MIN=10` (`.env.starexch.cleanup`): Evolution's
**Auto-Roulette**, min/chip ₹10, the pairs betting **red vs black**. Same
engine, same bracket rules — everything funnels into one account and each id
ends below ₹10.

Mapped read-only on starexch 2026-08-25 (`probe_lobby_tables.py --open
"Auto-Roulette" --spots`):
- The board is an SVG; spots carry `data-bet-spot-id="red"/"black"/…`, NOT
  baccarat's `data-role="bet-spot-…"`. `_TAG_BET_SPOT_JS` and
  `wait_for_live_table` now resolve both vocabularies (role names never
  collide).
- `circle-timer` (30/75 samples ≈ a 40%-open cycle), `balance-label-value`,
  `total-bet-label-value` and the `data-role="chip"` rail
  (10/50/100/500/2500/10000) all match the engine unchanged. RED/BLACK limits
  ₹10–200,000.
- **Zero (2.7% of spins) takes BOTH stakes** — destroyed, not moved. That is
  pennies on ₹10–90 cleanup stakes and exactly why roulette must never carry
  the main pot. `play_hand` classifies "both balances went down" as a replay
  (`tie` status), covering roulette's zero and dragon tiger's half-tie.
- Dragon Tiger (min ₹50, `bet-spot-dragon`/`bet-spot-tiger`, timer works,
  rail 50/100/…, tie takes half at 7.4%) was mapped too and works with a
  profile if ever needed — roulette beats it for cleanup (₹10 floor vs ₹50).

### Speed: groups play in parallel (`--parallel-groups`)

**What was already parallel:** every pair inside a group bets on the SAME hand
at the same table, so a group of 10 is 5 pairs on 1 hand, not 5 hands. That has
always been true and is why `play_hand` takes a list of pairs.

**What was serial:** the groups themselves. `run_tournament` played group 1 to
completion, then group 2, and so on. A group runs up to `MAX_GROUP_HANDS` (60)
hands on a ~40s baccarat cycle ≈ 40 min, so 100 accounts / 10 per group = ten
groups ≈ 6-7 hours. That is exactly where the time went.

Groups now run on their own threads, `parallel_groups` at a time (CLI
`--parallel-groups`, env `TOURNAMENT_PARALLEL_GROUPS`, **default 1** so nothing
changes until it is asked for). Ten groups over 3 lanes is ~2.5 hours instead
of ~7.

- **The machine runs `group_size × parallel_groups` Chromiums at once.** That
  is the only real ceiling — this is machine capacity, same as `group_size`
  always was. Preflight prints the total.
- **Logins are paced globally, not per lane.** `_LoginPacer` is one shared gate
  that hands out login slots `login_spacing` apart; `seat_accounts` books each
  seat's slot through it. So N lanes still produce the same logins/minute as
  one lane did, which is the whole point — the site hard-blocks (bare 403,
  ~20 min) at roughly 20 logins in a few minutes from one IP. **Any new code
  that opens a seat must go through the pacer**, or parallelism silently
  multiplies the login rate.
- **`progress` and `on_account` are serialised behind one lock** inside
  `run_tournament` (`emit()` / `account_update()`). Neither callback was
  written to be re-entered — `tournament_runner`'s `progress` flushes rows to
  the Google Sheet, and gspread is not thread-safe.
- Log lines are tagged `[g3] …` when more than one lane is running; without
  that the interleaved output reads like one very confused group.
- `run_one_group()` returns its outcome instead of writing into `summary`; the
  main thread merges each group as its lane finishes (`as_completed`), so the
  state file and sheet keep up with a long stage. `stage_rec["groups"]` is
  re-sorted into group order afterwards, since lanes finish out of order.
- **A group that raises no longer aborts the tournament.** `group_failed()`
  logs it, records a problem, and carries that group's accounts into the next
  stage untouched — the same rule as everywhere else here: nothing leaves the
  bracket except an account that was actually drained.

**`group_size` is the better lever when the machine can take it.** One big
group is strictly faster than several parallel ones: everybody is seated ONCE,
there is no second stage, and a hand still covers every pair at the table
regardless of how many there are. Lanes exist for when the box cannot hold
every seat at once.

The worry that a bigger group would blow through `MAX_GROUP_HANDS` (60) does
not survive measurement. Simulating the real `pair_up()` and drain rule over
200 seeds:

| accounts in one group | hands, balances equal | hands, ±50% apart (worst) |
|---|---|---|
| 10 | 5 | 7 (14) |
| 20 | 6 | 10 (24) |
| 50 | 9 | 12 (21) |
| 100 | 11 | 14 (29) |

So the cap has margin even at 100. Hand count grows with log(N), not N,
because every pair plays the same hand. **Starting balances matter more than
group size**: equal balances make every stake `min(a,b)` drain the loser
outright, while spread-out balances leave remainders that have to be replayed —
worth 30-40% of the hands.

### Seating: retried, and never a silent drop

`seat_accounts()` opens a browser + live table per account, retrying failures
`SEAT_ATTEMPTS` (3) times with `SEAT_RETRY_WAIT_SECS` (30s) between. Each retry
builds a **brand-new** browser and logs in again — the observed failure ("could
not open the 'Baccarat A' table") leaves a half-loaded frame that nothing short
of a fresh context recovers — and lands on the **next proxy** in the rotation,
since a rate-limited exit IP is one way this fails. A failed seat is closed
before the retry, or each attempt leaks a Chromium and a pproxy.

**A below-minimum account is pulled from the retry loop after attempt 1.**
A ₹0/below-min account cannot open a live table at all (confirmed 2026-08-18),
so once the between-attempts HTTP diagnose reads its balance the remaining
browser attempts are skipped — it still returns as a failure and is classified
"already out, nothing stranded". Measured: six ₹0 accounts × 3 attempts was
most of a 9-minute cleanup stage that played one hand.

**Don't retry blind — ask the site why first.** `login()`'s timeout message
("credentials rejected **or** the login was throttled") cannot tell those apart,
and they need opposite responses. Between seating attempts,
`diagnose_account()` does one ~3s HTTP login (vs ~40s for a browser seat, which
also spends a login against the very rate limit that may be the problem) and
returns one of four states:

| state | meaning | response |
|---|---|---|
| `ok` | credentials fine, balance is real | it's the table/browser path — retry after `SEAT_RETRY_WAIT_SECS` |
| `blocked` | edge/WAF answered, not the app (`infra_block`) | wait `SEAT_BLOCK_WAIT_SECS` (300s) — the block runs ~20min and holds regardless of pacing |
| `rejected` | app refused (wrong password, locked) | **stop retrying** — nothing can fix it |
| `unknown` | couldn't tell | short retry |

**An account that never seats must not vanish from the bracket.** It used to be
appended to `problems` and then simply not carried forward — so a winner could be
declared while a funded account's balance sat outside the tournament entirely,
the same silent-stranding bug as the `not_placed` one above. What happens now
depends on the diagnosis:
- `blocked` → **not eliminated at all**, and its money is not "stranded" — the
  run never actually reached it. It stays in the bracket for the next stage to
  retry, with a problem note saying to re-run once the block clears.
- `rejected` → eliminated with a "credentials refused" note; flagged for a human.
- balance **below the table minimum** → clean elimination, "nothing is
  stranded", and **no** `problems` entry. It was already out.
- **funded or unreadable** → a `problems` entry naming the exact amount the
  tournament could not move.

Two live failures drove this:
- **2026-08-11 22:34** — three of five accounts failed to seat, and all three
  were the ones already drained to ~0 by the previous run. Consistent with the
  site refusing a live table to an account with no real balance.
  **Now confirmed (2026-08-18)**: seating five khelofun accounts at once,
  the only one at **₹0** (`sureshyadav2393`) failed with "could not open the
  'Baccarat A' table" on both attempts, while every account holding **₹5**
  seated fine. So it is not a rate block or bad luck — a zero-balance account
  simply cannot open a live table. Login still succeeds, which is why this
  looks like a table/browser fault rather than an account one. `diagnose_account`
  already reads the balance first, so a below-minimum account is eliminated
  cleanly instead of being retried three times.
- **2026-08-12 02:59** — all three accounts failed at *login*, and the run
  marked every one "eliminated, balance left stranded" when in fact nothing had
  been checked at all. That report was actively misleading, which is what the
  `blocked` state above exists to prevent.

`run_tournament` writes `summary` to `state_path` after every group, so a crash
still leaves a record of who held what. `login_spacing` staggers the seat opens —
~10 simultaneous logins from one IP is exactly what trips the 403 block.

⚠️ **Give every instance its own `TOURNAMENT_STATE_FILE`.** Both
`.env.tournament.cricmatch` and `.env.tournament.khelofun` shipped pointing at a
bare `tournament_state.json`, so whichever ran last silently erased the other's
record — the exact clash `tournament@.service` warns about. Split 2026-08-18
into `tournament_state.cricmatch.json` / `tournament_state.khelofun.json`
(`tournament_state.*.json` is already gitignored). The env files are gitignored,
so this lives on the server and has to be re-done on any fresh checkout.

`tournament_runner.py` passes `range_name=`/`values=` to every `ws.update()`;
gspread reversed that argument order, and the positional form warns and will
eventually break.

**`tournament_runner.py --env <file> --check`** runs `diagnose_account()` over
the whole roster (~3s each, `TOURNAMENT_CHECK_SPACING` = 5s apart) and prints
`ok`/`blocked`/`rejected`/`unknown` per account. No browser, no bets. Run it
whenever a tournament reports "login did not complete" — that message cannot
tell a wrong password from a rate block, and finding out via browser seats costs
~40s each *plus* a real login against the very limit that may be the cause. The
first `blocked` stops the sweep (everything after it would be measuring the
block, and each attempt extends it). Verified against khelofun 2026-08-14: its
`/login` + `/api2/v2/getBalance` answer exactly like cricmatch's, including a
JSON `"Invalid Username or Password"` for a bad account, so the diagnosis is
trustworthy on both sites.

### Discovery scripts (all read-only, none place a bet)

Run them, read the dump, *then* write selectors — same precedent as
`inspect_form.py`.
- `inspect_form.py` — register form fields.
- `inspect_casino.py`, `inspect_wallet.py`, `inspect_account_settings.py`.
- `probe_evo_lobby.py <user> <pass>` — route to the game, dumps every `data-role`.
- `probe_stock_round.py <user> <pass>` — samples a full round.
- `probe_login_balance.py` — captures the login/getBalance network calls.
- `verify_stockmarket.py <user> <pass>` — drives `_open_table_for(game=STOCKMARKET)`
  end to end. **Run before any `/run`.**
- `probe_seat_decay.py --env F [--seats N] [--secs N] [--spacing N]` — the one
  that found seat decay. Pulls accounts straight from the tournament's own
  sheet (no credentials typed or printed), seats N of them and samples every
  table once a second, printing per-seat window/video/visibility counts. **The
  per-seat spread is the point** — every seat similar means the setup works,
  nobody seeing a window means the site or egress, and first-opened seats
  scoring far below last-opened ones is seat decay. Flags exist to re-run each
  ruled-out cause: `--front` (visibility), `--noautoplay` (video), `--poke`
  (idle session), `--reload` (does not repair, breaks the seat), `--direct`
  (skip the proxies). Places no bets.
- `probe_lobby_tables.py --env F [--open "<tile>"] [--secs N]` — finds a
  cheaper table: dumps Evolution-lobby search results, or opens one named
  table read-only and reports its live chip rail + bet limits, ending with
  the exact `TOURNAMENT_LOBBY_TILE`/`TOURNAMENT_TABLE_MIN` lines to set.
- `probe_baccarat_window.py <user> <pass> [--env F] [--seats N] [--secs N]` —
  seats via `tournament.Seat` (the exact tournament path, proxies included) and
  samples the live table once a second: whether `circle-timer` is present, the
  chip rail's size and how much of it is clickable, TOTAL BET, and every
  visible `data-role`/short text. Prints which roles and texts **come and go**
  across phases, so a new betting-window detector can be written from a dump.
  Run this whenever a run reports "no betting window opened in time".
  `--seats N` with a comma-separated username list holds N tables at once,
  which is the only way to reproduce tournament conditions — a single seat
  cannot show a load-dependent fault. Places no bets.

⚠️ Memory: Janvi/Myank session drops are a temporary velocity throttle (rapid
automated logins; ~30-60min cooldown). ali789/asha788 rarely trip it.

---

# 5. Sheet-driven scripts

Four scripts poll a Google Sheet and act on rows. All share:
- `--env <path>` with the same `load_dotenv(..., override=True)`-after-`import
  main` ordering gotcha (except `channel_info.py`, which doesn't import `main`).
- `--once` for a single pass.
- `current_proxy()` re-reads that env's `SETTINGS_FILE` live, so `/setproxy` on
  the matching bot applies automatically.
- **Queue semantics**: a row with its inputs filled and an EMPTY STATUS is
  processed exactly once, then STATUS is set. Clear STATUS by hand to retry. A
  `⏳`-prefixed STATUS (infra block / flood wait) counts as eligible and
  self-heals; `✅`/`❌` are terminal.
- Each uses its **own sheet** — sharing one would risk cross-script clobbering.

**Service account setup (same for all):** Google Cloud → enable the Sheets API →
IAM → Service Accounts → create (no project roles needed) → Keys → JSON → save as
`service_account.json` in the repo root (gitignored). Then share each sheet with
the account's `client_email` as **Editor** (write-back is required).

## `sheet_watcher.py` — hedge queue

Sheet: `PLAYER 1 | PASSWORD | PLAYER 2 | PASSWORD | BETS AMOUNTS | ROUNDS | STATUS`.
Calls `main.run_paired_hedge()` (Baccarat) and writes rounds hedged / stop reason
/ each side's final balance and net back into STATUS.

```
.venv/bin/pip install gspread google-auth
.venv/bin/python sheet_watcher.py --env .env.gameplay
```

Standalone on purpose — history goes to its own `sheet_runs.json`, not
`pairs.json`/`pair_runs.json`, since two processes writing those concurrently
would clobber. Amounts parse leniently (`₹`/`,` stripped). **No cross-process
busy guard against the Telegram bot** — don't `/run` a pair whose accounts the
watcher might also pick up.

## `balance_checker.py` — balance polling

Sheet: `USERNAME | PASSWORD | BALANCE | STATUS`. BALANCE holds the last
**successful** read and is left alone on failure, so an error never blanks a
known-good figure.

```
.venv/bin/python balance_checker.py --env .env.cricmatch
```

Config: `BALANCE_SHEET_SPREADSHEET_ID` (required, no default),
`BALANCE_SHEET_WORKSHEET_GID` (`"0"`), `BALANCE_SHEET_CREDENTIALS_FILE`,
`BALANCE_POLL_SECONDS` (20), `BALANCE_MAX_CONCURRENT` (**1**),
`BALANCE_CHECK_SPACING_SECONDS` (30), `BALANCE_BLOCK_BACKOFF_SECONDS` (300).

`process_row()` uses `http_check_account_balance` where `supports_http_login`,
falling back to `run_balance_check`.

**All four rate-limit defaults exist because of the `/login` block described in
§1** — don't raise them casually:
- `MAX_CONCURRENT=1`: a 5-wide burst on one proxy IP got every row 403'd.
- `_wait_for_turn()` enforces `CHECK_SPACING_SECONDS` between login *starts*,
  called inside `process_row()` so it doesn't block `poll_once()` from noticing
  new rows.
- `_trip_circuit_breaker()`/`_still_in_backoff()`: the first `infra_block` pauses
  **every** row for `BLOCK_BACKOFF_SECONDS`; a still-blocked probe extends by
  another full window. Covers a ~20min block in ~4 cheap probes instead of ~40
  wasted attempts. Checked *before* paying the spacing wait, so a paused attempt
  costs nothing.
- The 30s and 300s numbers are inferred from single incidents, not controlled
  tests. Tune against how often 403s recur.

## `password_changer.py` — bulk password changes

Sheet: `USERNAME | PASSWORD | NEW PASSWORD | STATUS`. A blank NEW PASSWORD gets
one from `gen_password()`, **written back into column C** before the attempt, so
the sheet always holds the real new password.

Engine: `main.run_change_account_password(...)`, shaped exactly like
`run_balance_check()`.

Config: `PASSWORD_SHEET_SPREADSHEET_ID` (required),
`PASSWORD_SHEET_WORKSHEET_GID`, `PASSWORD_SHEET_CREDENTIALS_FILE`,
`PASSWORD_SHEET_POLL_SECONDS` (20), `PASSWORD_SHEET_MAX_CONCURRENT` (1),
`PASSWORD_SHEET_CHECK_SPACING_SECONDS` (30).

Conservative defaults because there's **no HTTP-fast path** here — every row is a
real Playwright login, exposed to the same volume block. Whether
`change_account_password()` works from a bare `requests.Session` is an open
question (same as free-number's HTTP path).

**Not yet run against a live sheet.**

## `channel_info.py` — Telegram channel details (Telethon)

Nothing to do with the signup sites. Columns:
`A CHANNEL | B TITLE | C USERNAME | D ID | E TYPE | F MEMBERS | G POSTS |
H DESCRIPTION | I CREATED | J LAST POST | K FLAGS | L LINK | M STATUS`.
The header is written automatically if the sheet is empty (the other scripts need
a hand-made one). `HEADER`/`LAST_COL` are the single source of truth for the
layout — adding a column means editing those two constants and the details dict.

- POSTS and LAST POST come from **one** request: `get_messages(..., limit=1)`
  returns a `TotalList` whose `.total` is the full history count. Both blank (never
  zero) when history isn't readable.
- `normalize_target()` accepts `@name`, `name`, `t.me/name`, `t.me/s/name`,
  `telegram.me`/`.dog`, invite links (`t.me/+hash`, `/joinchat/hash`), or a bare
  numeric id → `("public"|"invite"|"id", value)`.
- **Needs a real user account, not a bot token** — bots can't resolve arbitrary
  usernames or read invite links. `TELEGRAM_API_ID`/`TELEGRAM_API_HASH` from
  my.telegram.org; the first run signs in interactively and saves to
  `CHANNEL_SESSION` (gitignored `*.session`).
- **No concurrency, and don't add any.** Telegram rate-limits per **account**, not
  per IP, so parallelism buys nothing and risks a multi-hour flood-wait. Rows go
  one at a time with `CHANNEL_SPACING_SECONDS` (3s); a FloodWait pauses every row
  via module-level `_blocked_until`.
- Invite links use `CheckChatInviteRequest`, which reads info **without joining**.
  This script never joins, posts, or messages.
- `write_row()` updates `B..M` in **one** `ws.update()` — Google's per-minute write
  quota is the real ceiling. A failed fetch writes only STATUS.

Config: `CHANNEL_SHEET_SPREADSHEET_ID` (required), `CHANNEL_SHEET_WORKSHEET_GID`,
`CHANNEL_SHEET_CREDENTIALS_FILE`, `CHANNEL_POLL_SECONDS` (20),
`CHANNEL_SPACING_SECONDS` (3), `CHANNEL_DESCRIPTION_LIMIT` (1000).

```
cp .env.channels.example .env.channels
.venv/bin/python channel_info.py --env .env.channels
```

**Not yet run against a real Telegram account or sheet.**

---

# 6. bit.ly click bot (`prince bot/`)

Self-contained mini-app: its own `.env`, own admin list, no Playwright, no
`accounts.db`. An aiogram bot where an admin pastes
`<link> <platform> <count> [delay] [mode] [parallel]` and a pool of
`WORKER_COUNT` workers fires that many HTTP GETs with randomized realistic
headers and referers.

⚠️ Memory: this runs in production on an Ubuntu box at
`~/PycharmProjects/Auto-Signup`; config is edited there directly, and pushing from
that box needs a token remote.

### Concurrency (`parallel`), not `delay`

`demo_job()` used to be fully sequential (`await bounded_send()` in a `for` loop),
which made the adjacent `Semaphore(50)` dead code and capped throughput at ~1-2
clicks/sec. **Don't reintroduce a one-at-a-time loop.**

It now runs `concurrency` **lanes** pulling from a shared counter:
- `Task.concurrency`/`Schedule.concurrency`, optional last positional arg,
  default `DEFAULT_CONCURRENCY` (env `CONCURRENCY`, 20; ceiling `MAX_CONCURRENCY`,
  200). Schedules saved before this read back at the current default.
- `delay` is now only a pause **inside** a lane; default dropped to 0. Throughput
  ≈ `concurrency / (rtt + delay)`.
- Each lane gets its own thread from a **per-job** `ThreadPoolExecutor` sized to
  `concurrency` — not `asyncio.to_thread`'s shared pool, which caps ~32 threads
  and would silently throttle a high `parallel`.
- A thread-local `requests.Session` per lane reuses the proxy tunnel + TLS
  handshake. **Cookies are cleared before each request** — don't drop that
  `clear()`, persisted cookies risk bit.ly deduping clicks.

`WORKER_COUNT` multiplies this (3 jobs × 20 lanes = 60 concurrent requests through
one proxy). If 502s climb, lower `CONCURRENCY` — one proxy endpoint is the ceiling.

### A failed request isn't a click

`count` means that many **answered** requests: a lane that gets a non-2xx/3xx or
an exception hands the click back for another lane to retry. Guards:
`ATTEMPT_MULTIPLIER` (3, so `count*3+20` attempts max),
`MAX_CONSECUTIVE_FAILURES` (40), `DEAD_LINK_STATUSES` (404/410 → stop now).
Exhausting any of these **raises**, so the bot renders ❌ with real counts rather
than a ✅ on a short delivery. `send_clicks()` returns
`(status, target_or_error, site_missed)`; `REQUEST_TIMEOUT` (env, 12s) bounds one
attempt.

### `FOLLOW_REDIRECTS`

Code default `false` (bit.ly counts the click when it *serves* the redirect).
**`prince bot/.env` sets `true`**, because this deployment wants the `btag`
traffic landing on cricmatch247.

**The two hops are separate requests and only the second is retried.**
`send_clicks()` fetches bit.ly with `allow_redirects=False`, then issues its own
GET to `Location` with the same headers, retrying just that hop
`DESTINATION_ATTEMPTS` (3) times. **Do NOT collapse this back to
`allow_redirects=True`**: a destination failure would fail the whole click, the
lane would retry, and bit.ly would serve *and count* a second redirect — silently
inflating the total. A click whose destination never loaded still counts but is
tallied in `missed_site` and surfaced in the progress line and summary.

### Measured through the real proxy

bit.ly redirect 2.4s median; cricmatch247 page ~5.2s / **441 KB**; one full click
~7.6s. So ~8 clicks/min at 1 lane, ~158/min at 20.

**The proxy is healthy — the drops are a capacity limit.** Exit IP checks clean on
`ip-api.com` and cricmatch247 answers 200 through it, so this is **not** the AWS
WAF 403 problem. 20 lanes × 441 KB ≈ 1 MB/s sustained through one residential
line, which is what produces the `RemoteDisconnected`/`502`. The retry logic
contains the damage but can't fix the cause. **Tune `CONCURRENCY` against the
`missed the site` counter** — that's the feedback signal. Raising the real ceiling
needs more proxy IPs, not more lanes.

### Daily schedules (`schedules.py`)

```
/schedule <link> <platform> <min>-<max> <start> <end> [HH:MM] [delay] [mode] [parallel]
/schedules            # list with next fire time
/delschedule <id>
```

Each night at `run_time` (default 01:00) every in-window schedule queues one job
with a fresh random count via `pick_count()`. A single number means a constant
count. Dates accept `YYYY-MM-DD`, `DD-MM-YYYY`, `today`, `tomorrow`, `+N`.

- **The clock is `SCHEDULE_TIMEZONE` (default `Asia/Kolkata`), not the server's.**
  `config.py` resolves it once (falling back to local time); every `datetime.now()`
  goes through `bot.now_local()`.
- **A missed run still fires, same day only** — `due()` is "in range AND not run
  today AND now ≥ run_time", so a bot that was down at 1 AM sends at 9 AM. A whole
  missed day is never backfilled.
- **`mark_run()` is called BEFORE `fire()`**, deliberately: a slow/failing send can
  then never double-send a day. The cost is a failed send loses that day — the
  right trade, since a duplicate day is worse than a missing one.
- Results post into the `chat_id` captured at creation, tagged
  `🕐 Daily schedule #N`.
- `enqueue(task, bot, chat_id, note="")` takes no `Message`, so the scheduler and
  the paste-a-line path share one code path.
- `schedules.json` (gitignored) persists atomically via `.tmp` + `replace()`,
  including `last_run` so a restart can't re-fire a sent day.
- The scheduler is a task in `main()`'s `workers` list, cancelled on shutdown.

### `/stopschedule` (alias `/stop`)

Stops jobs **in flight**; `/delschedule` cancels a daily schedule for good.
Different things — don't merge them. Bare form stops every live job,
`/stopschedule <job id>` stops one (ids from `/status`).

**Cooperative, not a kill.** `tasks.request_stop()` only sets `Task.stopping`;
`demo_job`'s lane loop checks it each iteration and winds down, so in-flight
requests finish. **Any future handler must check `task.stopping` itself** or it
will ignore the command entirely.

`tasks._live` tracks a job from `track()` (in `bot.enqueue`) to `untrack()` (in
`worker()`'s `finally`), so a still-queued job can be stopped too — `worker()`
checks `stopping` before starting and reports "stopped before it started."

A stopped job **returns** rather than raising, so it renders as a normal ✅
("stopped on request at 73/400 clicks") — it was asked for, not a failure. The
`RuntimeError` path stays for genuine failures.

`tasks.py`'s validation is split into `parse_link`/`parse_platform`/`parse_count`/
`parse_delay`/`parse_mode`/`parse_concurrency` so `parse_task()` (one-off) and
`parse_schedule()` (recurring) can't drift.

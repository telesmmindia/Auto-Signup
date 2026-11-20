# Incident log

Every production problem, newest first. **Read this before debugging** — most
problems here have happened before. **Add an entry after every fix or
diagnosis**, even if nothing was changed (e.g. "site-side, nothing to fix").

Search it: `grep -n -i "<site or error text>" INCIDENTS.md`

Rules for entries:
- Quote the **exact error text** the bot/log showed, so a grep finds it.
- Say whether it **happened before** (link the earlier entry's date).
- Never write credentials, proxy passwords or full phone numbers — the repo
  is public. Use a prefix like "9218… numbers".
- Keep it short; put long explanations in CLAUDE.md and point to them.

Template:

```
## YYYY-MM-DD — <site/bot>: <one-line symptom>
- **Seen as:** exact message in chat / log
- **Cause:** what was actually wrong (site-side / proxy / our code / numbers)
- **Seen before?** no / yes — <date of earlier entry>
- **Fix:** what was changed (commit, env setting, restart) or "none, site-side"
- **If it comes back:** what to check first
```

---

## 2026-11-20 — starexch signup: "btag se user create nhi ho rahe / signup reflect nhi ho rahe"
- **Seen as:** client: signups not showing; `/export https://starexch365.com?btag=230853`
  answered "No accounts to export."; "Signup failed. (#7621)".
- **Cause:** accounts ARE created — 25 `success` rows (#7597–#7629) in the first
  ~45 min, each confirmed by logging into the new account, all stored with
  `referral_code=230853`. `/export`'s URL filter is an exact match and rows are
  stored under `https://starexch555.com?btag=230853`, so the starexch365 URL
  matches nothing. Failures: 4× "OTP rejected: Something went wrong. Please
  request OTP again." (site refused the code in 2–5s) and 2× proxy connection
  errors. Whether the site CREDITS the btag cannot be seen from a player
  account (no referral info on any logged-in page) — only the affiliate
  dashboard shows it. Our flow walks `/?btag=` → `/setcookie` and holds the
  `btag` cookie on both register POSTs, but through the rotating proxy each
  request leaves from a different IP, unlike a real visitor.
- **Seen before?** similar — winclash 2026-09-04 (cookie set but not credited
  until the `/setcookie` route was walked; that route IS walked here).
- **Fix:** none yet. Use `/export` or `/export https://starexch555.com?btag=230853`.
- **If it comes back:** compare with ONE manual browser signup via the
  starexch365 link — if that shows in the dashboard and ours don't, move the
  bot to fixed residential IPs (one IP per signup) and/or `/fast off`.

## 2026-11-20 — signup bots: 2–3 min per signup; per-bot OTP settings ignored
- **Seen as:** operators report 2–3 min per signup. Logs: `OTP step got no
  answer after 60.1s` and `login check 1/2` on khelofun, spin24star and
  starexch — whose env files set `HTTP_OTP_TIMEOUT_SECS=15/20` and
  `HTTP_OTP_CONFIRM_LOGIN_TRIES=8`.
- **Cause:** our code. `telegram_bot.py` imports `main` BEFORE it loads the
  `--env` file, and `main` copies those settings into constants at import, so
  every bot ran on main's defaults (60s, 2 checks, 10s apart). The 2026-11-19
  tuning never took effect. Measured median per signup (logs): ~12–26s to the
  OTP screen, ~30–35s waiting for the SMS + typing, ~66–72s on the code step
  (60s of it waiting for an answer the site never sends), ~12–23s between
  signups.
- **Seen before?** yes — 2026-11-19 entries tuned these values; they were
  never applied.
- **Fix:** `telegram_bot.py` re-applies `HTTP_OTP_*`, `HEDGE_MAX_BET_CLICKS`,
  `WAF_TOKEN_TTL_SECS` from the env onto `main` right after `load_dotenv`.
  Patched in the VPS's own copy (backup `telegram_bot.py.bak-envfix-20261120`);
  khelofun, spin24star, starexch signup restarted. First signup after: `no
  answer after 20.0s`, `login check 2/8`, verify step 40s (was 66–77s).
- **If it comes back:** compare the `after Ns` / `check i/N` numbers in the log
  with the env file. The rest (SMS + typing, the site not answering the code
  step) is not ours to shorten.

## 2026-11-20 — "Instamatch exch bot": every ID "Invalid Username or Password"
- **Seen as:** `/relogin` → `✅ 0 logged in, ❌ 5 failed`; `/ids` → each of the
  5 IDs `Invalid Username or Password` (log: `❌ <id>: Invalid Username or
  Password`).
- **Cause:** wrong site, not wrong passwords. The Telegram bot is the
  starexch365 exchange instance (`.env.starexch365.exchange`), pointed at
  starexch555 — but the client's sheet holds **instamatch88.com** IDs
  (instamatch.com / instamatch365.com 301 there). On instamatch88 all 5 log
  in with ~₹5,000 each. instamatch88 is the khelstake/starexch Laravel
  platform with one route moved: sport pages are `/sports-exchange/<sport>`
  (`/exchange/cricket-exchange` 404s).
- **Seen before?** no.
- **Fix:** `exchange_sites.InstamatchSite` (KhelstakeSite with the sport
  routes changed; host + brand registered). VPS env now
  `EXCHANGE_SITE_URL=https://instamatch88.com`, `EXCHANGE_SITE_NAME=instamatch88`
  (backups `*.bak-20261120-insta`); bot restarted → `Login: 5 OK, 0 failed`.
  Read-only check through the adapter: 19 cricket series, matches, event page
  (400-char limit blob, Match Odds), live odds OPEN, unmatched list empty.
- **If it comes back:** an "Invalid Username or Password" on EVERY ID means
  ask which site the IDs belong to before touching passwords. No bet placed
  on instamatch88 yet — run 🧪 Test 1 ID first.

## 2026-11-20 — starexch365 (new client link): signup + exchange set-up
- **Seen as:** request for a signup and exchange bot for
  `starexch365.com?btag=230853`.
- **Cause:** not a new site. Every starexch365 path answers `301` to the same
  path on **starexch555.com**; the btag then goes `/?btag=` → `302 /setcookie`
  → `btag` cookie, exactly as on starexch555. starexch555's signup form
  (`/append/loginpp`) and register handler are identical to cricmatch's once
  host/logos/token are masked — they had never been checked before.
- **Seen before?** no.
- **Fix:** `sites/starexch.py` signup marked checked, `supports_http_fast=True`
  with `http_status_otp_sent=[205]`/`http_status_registered=[200]`. Verified
  without an SMS: fast path got csrf + btag cookie and a real validator refusal
  (`{"status":201,"message":{"username":"The username has already been
  taken."}}`, judged as an error); browser `--no-submit` filled the form in
  12.5s. New `.env.starexch.signup.example`; point the bot at
  `https://starexch555.com?btag=230853`, never the starexch365 URL. The
  exchange side runs as its own bot.
- **Deployed 2026-11-20 on the VPS** (tmux `autosignup`, auto-restart loops):
  window "starexch signup" → `telegram_bot.py --env .env.starexch.signup`
  (fast on, DataImpulse mobile proxy, log `logs_starexch_signup.log`); window
  "starexch365 exch" → `exchange.py --env .env.starexch365.exchange`, accounts
  in the client's own sheet "STAREXCH 365", tab `exch` (gid 0), residential
  pool, concurrency 3, log `logs_starexch365_exchange.log`. Tab starts empty
  (0 accounts) — add USERNAME | PASSWORD rows, then `/relogin`.
- **If it comes back:** no real signup yet — do one `/newacc` on a real number
  and log into it. If the code step fails, re-check the 205/200 statuses.

## 2026-11-19 — spin24star signup: 70–80s after the code; "Server Error" accounts existed
- **Seen as:** log `OTP step got no answer after 60.1s (ReadTimeout)` then
  `login check 1/2 (12s): account exists` — every success. Also
  `OTP verify FAILED -- OTP rejected: Server Error` on #7515/#7520.
- **Cause:** spin24star creates the account well within 60s but never answers
  `/verifyOtpSignup`; its env had no `HTTP_OTP_TIMEOUT_SECS`, so the bot waited
  the full default 60s. "Server Error" (Laravel 500) also came AFTER the
  account was created — both such accounts log in.
- **Seen before?** yes — the timeout half, same day (#7466/#7467).
- **Fix:** `.env.spin24star`: `HTTP_OTP_TIMEOUT_SECS=15`, `TRIES=8`,
  `GAP_SECS=5`. `http_verify_signup_otp()` now also checks by login when the
  OTP step answers a bare "Server Error". Rows 7515/7520 set to `success`.
- **If it comes back:** read the `login check i/N` lines; if the first check
  is always "not yet", the 15s wait is too short for the site that day.

## 2026-11-19 — khelofun signup: each signup takes 2–3 minutes
- **Seen as:** operators report 2–3 min per signup.
- **Cause (measured):** khelofun's account-creating OTP call has not answered
  within 20s on ANY success that day (all 216 notes say `ReadTimeout`), so
  every signup waits 20s and then confirms by login. Each login check through
  the mobile proxy costs ~8–14s, almost all of it the proxy opening a new
  connection (even a 0 KB file took 5s). Plus SMS delivery and typing.
- **Seen before?** yes — 2026-11-18 evening, khelofun create call >20s.
- **Fix:** checks every 5s instead of 10 (`HTTP_OTP_CONFIRM_LOGIN_GAP_SECS=5`,
  `TRIES=8` in `.env.khelofun.signup`). New log lines: `OTP code received`,
  `OTP step got no answer after Ns`, `login check i/N (Ns): …`, `verify step
  took Ns` — read those before tuning further.
- **If it comes back:** the slow part is khelofun's create call (site-side).

## 2026-11-19 — khelofun signup: correct OTP → "Signup failed"
- **Seen as:** `OTP verify FAILED -- OTP rejected: Something went wrong. Please
  request OTP again.` (36 of ~300 that day). Those accounts do NOT exist
  (login check: `Invalid Username or Password`), so the failure is real.
- **Cause (likely):** late codes. 30 of 36 came >60s after the OTP screen;
  no success came later than ~2 min. On the rotating mobile proxy an idle
  connection dies after 60–120s, so a late code left from a NEW IP. Measured
  that evening: idle 150s → new connection; with a HEAD / every 30s → same
  connection. Not proven which of "new IP" or "OTP expired" khelofun objects to.
- **Seen before?** partly — 2026-11-19 morning entry blamed the 9218… series;
  the timing pattern covers those too (gaps 70–1967s).
- **Fix:** `telegram_bot.py` keeps a fast signup's connection alive while
  waiting for the OTP (`_start_otp_keepalive`, `OTP_KEEPALIVE_SECS`=30).
  Patched in the repo and in the VPS's own diverged copy (backup
  `telegram_bot.py.bak-keepalive-20261119`); khelofun, spin24star,
  playcric365 and funcrash signup bots restarted.
- **If it comes back:** compute OTP-screen→outcome gaps for the failures. If
  late codes still fail with the keep-alive on, the OTP itself expires (~1–2
  min) — enter codes faster; nothing in code can fix that.

## 2026-11-19 — khelofun signup: every signup fails instantly after one refusal
- **Seen as:** chat `❌ Signup failed.` seconds after sending the number; log
  `Couldn't load the site (check the proxy?) ... ProxyError('Unable to connect
  to proxy', OSError('Tunnel connection failed: 401 Unauthorized`.
- **Cause:** one register got `Something went wrong. Please try again` (#7479),
  so the bot rested the main proxy (rotating DataImpulse mobile) for
  `PROXY_BLOCK_REST_SECS=7200` and switched to the `/proxypool` backups — two
  proxy-cheap logins whose credentials now answer `401 unauthorized`. Resting a
  ROTATING proxy buys nothing (each connection is a new IP anyway).
- **Seen before?** no (proxy-cheap logins worked 2026-11-11).
- **Fix:** removed the dead proxy-cheap entries (all 401) from the
  `/proxypool` of khelofun, cricmatch, playcric365 and spin24star (each
  settings file backed up as `.bak-dead-pool-20261119`) and restarted all four
  bots (also clears the in-memory rest). With an empty pool a rested
  main proxy is still used. Separately, #7477 `OTP rejected: Something went
  wrong. Please request OTP again` was a real failure (account does not log in).
- **If it comes back:** test each `/proxypool` entry before trusting it; a
  `401`/`407` from the tunnel is the proxy account, not the site.

## 2026-11-19 — spin24star signup: "Signup failed" after OTP, accounts existed
- **Seen as:** chat `❌ Signup failed. (#7466)` / `(#7467)`; log
  `OTP verify FAILED -- OTP verify request failed: ... /verifyOtpSignup
  (Caused by ProxyError('Unable to connect to proxy', RemoteDisconnected(...`
  and `... Read timed out. (read timeout=60)`.
- **Cause:** the OTP step's request through the rotating DataImpulse mobile
  proxy dropped / took >60s (khelofun's bot was hitting the same timeouts at
  the same minutes). The site created both accounts anyway — both log in with
  `{"status":200,"message":"Login Success"}`. spin24star had no login check,
  so the bot called them failed.
- **Seen before?** yes — same pattern on khelofun 2026-11-18 (9 "failed"
  accounts that existed).
- **Fix:** spin24star now confirms a timed-out OTP step by logging in
  (`http_signup_login_check`, login `POST /api2/login` with the username in
  `email`). Rows 7466/7467 corrected to `success` by hand. Bot restarted.
- **If it comes back:** a "failed" with `Read timed out` / `ProxyError` on the
  OTP step — log into the account before re-sending the number. If the
  timeouts are constant, the mobile proxy is the slow part, not the site.

## 2026-11-19 — khelofun signup: slow to confirm, OTP not arriving
- **Seen as:** signup ✅ came 1–1.5 min after sending the OTP; log
  `SUCCESS -- Registration confirmed by logging in -- the site did not answer
  the OTP step (ReadTimeout)`. Also `OTP rejected: Something went wrong.
  Please request OTP again`, `Please enter valid OTP`, and
  `Couldn't load the site (check the proxy?) ... Read timed out`.
- **Cause:** (1) khelofun's account-creating POST takes >60s to answer (since
  2026-11-18 evening); the bot waited the full 60s before checking by login.
  (2) The rotating DataImpulse mobile proxy loads khelofun in 3–19s vs 2–3s on
  a fixed IP. (3) OTP failures were all on the 9218… number series after
  ~10:40, each number re-sent within minutes — khelofun's SMS side, not ours
  (it answered "OTP sent" every time). Other series (8287…) worked.
- **Seen before?** yes — 2026-11-18 (same slow create call).
- **Fix:** commit 1a62853 made the login-check retries env-tunable;
  `.env.khelofun.signup` on the VPS now has `HTTP_OTP_TIMEOUT_SECS=20`,
  `HTTP_OTP_CONFIRM_LOGIN_TRIES=5`, `HTTP_OTP_CONFIRM_LOGIN_GAP_SECS=10`.
  Bot restarted 11:07. Fixed proxies NOT an option — operator says they are
  blacklisted on khelofun.
- **If it comes back:** OTP not arriving → check which number series and
  whether the same number was re-sent repeatedly; rest that series ~30 min.
  Slow → time the homepage through the proxy (`curl -x ... -w %{time_total}`).

## 2026-11-18 — khelofun signup: 9 "failed" signups were actually created
- **Seen as:** `OTP verify request failed: Read timed out`; re-sending the
  number then gave "already taken".
- **Cause:** khelofun's create call >20s; the account was made anyway.
- **Seen before?** no
- **Fix:** commit d2ce111 — wait `HTTP_OTP_TIMEOUT_SECS` (60) then confirm by
  logging in before calling it failed. 9 rows set to `success` by hand.
- **If it comes back:** log in with the row's credentials before believing
  "failed".

## 2026-11-16 — cricmatch fast signup: OTP prompt for a code never sent
- **Seen as:** bot asked for an OTP; user re-sent one number 5× in 3 min.
- **Cause:** cricmatch refusals stopped carrying `message_class`, so every
  refusal read as "OTP sent".
- **Fix:** profile got `http_status_otp_sent=[205]` / `http_status_registered=[200]`.
- **If it comes back:** print the raw register reply; check status codes.
  Details: CLAUDE.md "cricmatch's fast path needs explicit statuses".

## 2026-11-12 — spin24star signup: every number refused, whole proxy pool walked
- **Seen as:** `Something went wrong. Please try again` on every attempt.
- **Cause:** per-exit-IP signup limit (~3 per few min); resting IPs ran out.
- **Fix:** fast mode + rotating mobile proxy (no browser, no WAF token).
- **If it comes back:** check proxy traffic (`407 TRAFFIC_EXHAUSTED`) first.

## 2026-11-10 — playcric365 signup: "REGISTER click had no effect"
- **Seen as:** `REGISTER click had no effect` on every signup.
- **Cause:** site answered `Something went wrong` to used proxy IPs; the page
  rendered nothing, so the bot guessed. Per-IP signup limit.
- **Fix:** DataImpulse mobile proxy + fast mode.
- **If it comes back:** read the site's actual register reply in the notes.

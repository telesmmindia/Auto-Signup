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
- **Fix:** removed the dead proxy-cheap entries from
  `bot_settings.khelofun.signup.json` (backup `.bak-dead-pool-20261119`),
  restarted the bot (clears the in-memory rest). With an empty pool a rested
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

"""spin24star.com -- runs the "Khelo" white-label platform (assets under
khelocdn). Signup selectors captured live via inspect_form.py --url. Its
register endpoint is guarded by an AWS WAF CAPTCHA in the BROWSER (handled
generically by the engine's CapSolver path) -- but not over plain HTTP, which
is why supports_http_fast is on since 2026-11-12; see the flag's comment
below.

Login + live casino mapped 2026-11-22 for a Stock Market bot, read off the
site's own pages and JS (logged out AND logged in -- the tile handler is
stubbed to `login_page()` while logged out, same trap as playcric365). The
casino half is winclash's platform, not the Laravel template's: tiles are
`.casinoLink` with data-link/data-provider, and the logged-in handler builds
/casinoRedirect?q=<id>&provider=<prov>&type=casino -- the profile's default
casino_launch_path -- so the engine's direct_game_url route applies as is.
Game ids come from the site's own POST /home/get_casinonew_filter_data
(filter_value[]=evolution, 184 titles, answers anonymously)."""
from .base import SiteProfile
from .cricmatch import GENERIC_RESULT_SELECTORS

PROFILE = SiteProfile(
    key="spin24star",
    hostnames=["spin24star.com"],
    # Khelo REGISTER is one of several header buttons (only one visible); a game
    # section overlays it, so open_signup_modal() force-clicks the visible one.
    register_trigger="forced_join",
    # T&C mark renders pre-checked and isn't a real checkbox -- nothing to click.
    has_terms_checkbox=False,
    # A taken phone has no dedicated element; it arrives as the snackbar
    # "The mobile number is already in use" (seen live 2026-10-04, #4207).
    # Matching that text makes it a phone_taken outcome, so chat says WHY
    # instead of a bare "Signup failed." Phone-specific phrase only, same
    # reasoning as winclash: a taken EMAIL must not be misfiled as a phone
    # problem.
    phone_taken_selector=None,
    phone_taken_texts=["mobile number is already in use",
                       "mobile number has already been taken"],
    # A per-IP signup limit, not a fault: see SiteProfile.ip_refused_texts.
    ip_refused_texts=["something went wrong. please try again"],
    # Khelo rejections render as a top-right snackbar (a bare <p> inside this
    # container) with no toast/alert/error class -- add it to the scrape set.
    result_selectors=GENERIC_RESULT_SELECTORS + [".snackbar-container"],
    tracking_param="btag",
    # The default 10s was too short: on a slow evening (register taking
    # 35-40s) a correct code was reported "wrong/expired" while the account
    # registered anyway -- the number then came back "already in use".
    otp_outcome_timeout_ms=30000,
    # Read out of the page's own otpVerify(): POST /verifyOtpSignup ->
    # {"statusCode":201} = registered (then it redirects to /), 301 = wrong
    # code (it clears the boxes and DISABLES Verify). The call is
    # async:false, so judge by this reply, not by the screen -- see
    # SiteProfile.otp_verify_endpoint.
    otp_verify_endpoint="/verifyOtpSignup",
    otp_verify_ok_codes=(201,),
    # ---- HTTP-only signup, on since 2026-11-12 ----
    # The browser path could no longer get a signup through: the site refuses
    # a fixed exit IP after ~3 signups ("Something went wrong. Please try
    # again", see ip_refused_texts) and the bot was walking its whole backup
    # pool one IP per attempt. A rotating proxy is the fix, and the reason
    # this profile said supports_http_fast=False -- "its register POST is
    # gated by an AWS WAF CAPTCHA that only a real browser can solve" -- is
    # about the BROWSER path: the WAF token is minted on the page load and is
    # bound to that exit IP, so a per-connection rotating proxy invalidates it
    # before REGISTER is clicked. Over plain HTTP there is no page load and no
    # token to invalidate, which is exactly how funcrash (the other AWS-WAF
    # site here) was unblocked on 2026-11-11.
    #
    # Confirmed live 2026-11-12 through the rotating mobile proxy, with no
    # account created: GET /?btag= answers 200 with the csrf-token meta tag and
    # sets the btag cookie, and POST /sign-up carrying the fields below reaches
    # the application and is answered by its own validator --
    # {"status":201,"message":"Password cannot be same as email"} -- with NO
    # x-amzn-waf-action header and no Cloudflare challenge.
    supports_http_fast=True,
    # Read out of the page's own registrationSection(): it POSTs to
    # URL_signUpUser = 'https://spin24star.com/sign-up' with exactly these five
    # fields. They are winclash's names, not cricmatch's, despite this being a
    # Laravel white-label -- which is why they are read off the handler rather
    # than inherited.
    http_register_path="/sign-up",
    http_register_fields={
        "username": "user_name", "email": "email", "password": "password",
        "phone": "mobile_number", "otp": "otp", "token": "_token",
    },
    # The code is redeemed by otp_verify_endpoint above, which COMPLETES the
    # registration on its own -- there is no second register POST here. See
    # main.http_verify_otp_endpoint(). path:'rg' selects the registration flow
    # on an endpoint shared with login and forgot-password.
    http_otp_flow="verify_endpoint",
    http_otp_verify_extra={"path": "rg"},
    # MUST stay non-empty: spin24star's refusals carry no message_class (the
    # probe above is one), so an empty list falls back to the message_class
    # check and reads every refusal as a success -- the same fault playcric365
    # and khelofun were given explicit statuses to avoid. 201 is a REFUSAL on
    # the register endpoint, so it must never be listed; the 201 the OTP
    # endpoint answers with is judged separately by otp_verify_ok_codes.
    http_status_otp_sent=[205],
    http_status_registered=[200],
    # A /verifyOtpSignup that times out does NOT mean the signup failed: on
    # 2026-11-19 two in a row timed out / lost the proxy and BOTH accounts
    # existed. So, like khelofun, a timed-out OTP step logs in with the new
    # credentials before deciding. Login is POST /api2/login with the username
    # in `email` (its own #login_formkhelo handler), verified live that day:
    # {"status":200,"message":"Login Success"}, and a wrong password gets
    # {"status":201,"message":"Invalid Username or Password"}. Only this
    # check is turned on -- supports_http_login would also claim an HTTP
    # balance read, which is not mapped here.
    http_login_path="/api2/login",
    http_login_user_field="email",
    http_signup_login_check=True,

    # ---- login + live casino (Stock Market bot), mapped 2026-11-22 ----
    # The header LOGIN button (button.rj__login, two in the DOM, one visible)
    # opens the #login_khelo_* form; driven in headless Chromium that day.
    # Ignore #user_login and login_page()'s #app_1/#login_p_username: the
    # first is a hidden legacy form and the second is not in the page at all.
    supports_login=True,
    supports_casino=True,
    # The snackbar login() scrapes for a rejection also shows "Please
    # Wait..." the moment LOGIN is clicked and "Login Success" when it works
    # (the #login_formkhelo handler's own notify() calls) -- winclash's trait
    # exactly, so the same filter.
    benign_texts=["please wait", "login success"],
    # LOAD-BEARING. Without an auth check login() falls back to the header
    # indicator, which the white-labels here have shown visible while logged
    # out. GET /api/getBalance answers 401 "Unauthorized." logged out and
    # {"status":200,"balance":{...}} logged in (measured 2026-11-22), which
    # is exactly the shape the in-page check accepts. It is GET-only -- a
    # POST is 405 -- hence http_balance_method. supports_http_login stays
    # False: this check needs the browser session only.
    verify_auth_in_page=True,
    http_balance_path="/api/getBalance",
    http_balance_method="GET",
    # The AWS WAF walls PAGE LOADS too once an IP has done a few automated
    # logins (measured 2026-11-22 from one laptop IP: the first verify run
    # was served clean, a few logins later the post-login page /?q=<id>,
    # /homepageGames and /api/getBalance all answered 405 + captcha). So the
    # login/casino path clears the wall first and replays a solved token
    # from a fresh context, exactly like winclash -- CAPSOLVER_API_KEY is
    # required on the Stock Market bot.
    waf_on_navigation=True,
    # ...but NOT on the no-browser signup path: the live signup bot runs
    # --fast through a rotating proxy, where a solved token is useless (it is
    # bound to an exit IP that changes per connection). False keeps
    # http_get_page() exactly as it was before this flag existed.
    http_waf_on_navigation=False,
    # Tables open by the site's own launch URL, not by clicking a tile:
    # /casinoRedirect?q=<id>&provider=evolution&type=casino (the default
    # casino_launch_path), read out of the logged-in .casinoLink handler.
    # Same reasons as winclash: the tile handler shows an "add money" popup
    # instead of launching when both balances are zero, and Stock Market is
    # in this site's OWN catalogue, so the Evolution-lobby hop is skipped.
    casino_launch_mode="direct_game_url",
    casino_provider="evolution",
    casino_game_ids={
        "Baccarat A": 86,      # same ids as winclash/funcrash
        "Baccarat B": 87,
        "Stock Market": 1584,
    },
    sel={
        # ---- signup ----
        "open_modal_khelo": "button.rj__join_now",
        # Includes the full-screen SPRIBE/aviator intro walkthrough's "skip >>"
        # control, plus the generic closers (harmless if absent).
        # .app_download_close: a full-screen "Download the app" promo added
        # ~2026-10-03 that covers REGISTER; without it every signup failed with
        # "Could not open the signup modal (JOIN button)".
        "close_popup": [".app_download_close", ".skip_right_img", ".mnPopupClose", ".pgSoftClsBtn",
                        ".support_popup_close", ".areSurecancelBtn",
                        "button:has-text('Close')"],
        "username": "#userNameKhelo",
        "email": "#emailKhelo",
        "password": "#passwordKhelo",
        "phone": "#phoneKhelo",
        "submit": "button#signUpButtonKhelo",
        # Signup OTP boxes (NOT the login-OTP input.otpNumberkhelo / forgot-pw
        # input.otpNumberFp, which must not be matched).
        "otp_popup": ".otpRegisterForm",
        "otp_digits": "input.regOtpKhelo1",
        "otp_verify": ["button.submitRegOtpMain"],
        "otp_error": ".otp_error",

        # ---- login (driven live 2026-11-22) ----
        "open_login": "button.rj__login",
        "login_username": "#login_khelo_username",
        "login_password": "#login_khelo_password",
        "login_submit": "#login_formkhelo",
        # Only consulted on the lobby route and when no auth check exists;
        # login() verifies through /api/getBalance here. .headUserName is in
        # the logged-in header only.
        "logged_in_indicator": ".headUserName",
        # Unused on the direct_game_url route; kept so no caller KeyErrors.
        "casino_nav": "a[href*='live-casino']",
        # getBalance() writes main_balance here (static "0" until it lands).
        "wallet_balance": ".rj_coin_sec .wallet_balance",
    },
)

"""spin24star.com -- runs the "Khelo" white-label platform (assets under
khelocdn). Signup selectors captured live via inspect_form.py --url. Its
register endpoint is guarded by an AWS WAF CAPTCHA in the BROWSER (handled
generically by the engine's CapSolver path) -- but not over plain HTTP, which
is why supports_http_fast is on since 2026-11-12; see the flag's comment
below. Login/casino markup is NOT inspected, so supports_casino=False."""
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
    supports_casino=False,
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
    },
)

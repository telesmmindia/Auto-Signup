"""spin24star.com -- runs the "Khelo" white-label platform (assets under
khelocdn). Signup selectors captured live via inspect_form.py --url. Its
register endpoint is guarded by an AWS WAF CAPTCHA (handled generically by the
engine's CapSolver path). Login/casino markup is NOT inspected, so
supports_casino=False."""
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

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
    # A taken phone surfaces (if at all) through the snackbar / generic scrape,
    # not a dedicated element, so there's no distinct phone_taken status here.
    phone_taken_selector=None,
    # Khelo rejections render as a top-right snackbar (a bare <p> inside this
    # container) with no toast/alert/error class -- add it to the scrape set.
    result_selectors=GENERIC_RESULT_SELECTORS + [".snackbar-container"],
    tracking_param="btag",
    supports_casino=False,
    sel={
        # ---- signup ----
        "open_modal_khelo": "button.rj__join_now",
        # Includes the full-screen SPRIBE/aviator intro walkthrough's "skip >>"
        # control, plus the generic closers (harmless if absent).
        "close_popup": [".skip_right_img", ".mnPopupClose", ".pgSoftClsBtn",
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

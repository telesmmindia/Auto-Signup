"""cricmatch247.com -- the original site. Selectors captured live via
inspect_form.py; login + live-casino selectors captured live 2026-07-16."""
from .base import SiteProfile

# Generic toast / SweetAlert / inline-validation selectors, shared as the base
# result-scrape set (a site can extend this -- see spin24star's snackbar).
GENERIC_RESULT_SELECTORS = [
    ".toast", ".toast-message", ".swal2-title", ".swal2-html-container",
    ".error_msg", ".invalid_msg", "[class*=toast]", "[class*=alert]",
]

PROFILE = SiteProfile(
    key="cricmatch",
    hostnames=["cricmatch247.com"],
    register_trigger="modal",
    has_terms_checkbox=True,
    phone_taken_selector=".err_phone",
    result_selectors=GENERIC_RESULT_SELECTORS,
    tracking_param="btag",
    supports_casino=True,
    sel={
        # ---- signup ----
        "open_modal": [".registerUserData", "button.headerjoinBtn",
                       "button.cls_reg_btn", ".join__btn"],
        "close_popup": [".mnPopupClose", ".pgSoftClsBtn", ".support_popup_close",
                        ".areSurecancelBtn", "button:has-text('Close')"],
        "username": "#userNameid",
        "email": "#userEmailid",
        "password": "#pass_log_id",
        "phone": "#phoneNumber",
        "terms": "#remChck2",
        "submit": "button.cls_register_new",
        # Signup OTP screen (NOT the "Login with OTP" widget -- input.otp__digit
        # without the _signup suffix, which must not be matched).
        "otp_popup": ".signup_otp_popup",
        "otp_digits": "input.otp__digit_signup",
        "otp_verify": ["a.get_user_otp", ".vf_otpBtn a", ".vf_num_otpSec a.mb-button",
                       ".signup_otp_popup a:has-text('Verify')"],
        "otp_error": ".otp_error",
        # ---- login + live-casino (cricmatch only) ----
        "open_login": "a.cls_loginbtn",
        "login_username": "#user_login_id",
        "login_password": "#passwordId",
        "login_submit": "#loginbutton",
        "logged_in_indicator": "#acctSec",
        # Sidebar Live Casino link with a real href (NOT the top-nav
        # href="javascript:;" tab, which no-ops under the SPRIBE overlay).
        "casino_nav": "a:has-text('Live Casino'):not([href=\"javascript:;\"])",
    },
)

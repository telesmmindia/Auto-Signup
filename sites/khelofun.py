"""khelofun.com -- same white-label template as cricmatch247 (confirmed live
2026-08-01: identical Laravel csrf-token/session-cookie setup, and the exact
same d2g8jl9s27zu.cloudfront.net/wmsc/ CDN asset paths). Login markup
(a.cls_loginbtn, #acctSec) matches byte-for-byte in khelofun's raw homepage
HTML too, which is why supports_casino/login selectors below are copied
straight from cricmatch.py rather than re-inspected from scratch.

Only the login -> wallet-balance -> change-password path has been exercised
here (balance_checker.py / password_changer.py, per the owner's request) --
the HTTP-fast signup/free-number endpoints (supports_http_fast,
supports_free_number) are deliberately left at their False defaults since
they were never inspected for this site; don't flip them on without
confirming the way cricmatch's were (see CLAUDE.md's "--fast" / "Freeing
the signup phone number" sections for the live-verification bar to clear)."""
from .base import SiteProfile
from .cricmatch import GENERIC_RESULT_SELECTORS

PROFILE = SiteProfile(
    key="khelofun",
    hostnames=["khelofun.com"],
    register_trigger="modal",
    has_terms_checkbox=True,
    phone_taken_selector=".err_phone",
    result_selectors=GENERIC_RESULT_SELECTORS,
    tracking_param="btag",
    # Confirmed live 2026-08-01: khelofun's raw homepage HTML already contains
    # a.cls_loginbtn and #acctSec, the same login-trigger/logged-in markers
    # cricmatch.py uses -- same template family. Needed for login() to run at
    # all (it refuses cleanly when this is False), which both the balance
    # check's Playwright fallback and the password-change path go through.
    supports_casino=True,
    # Inferred from the identical platform fingerprint, NOT yet confirmed with
    # a real khelofun login (no test credentials were available while wiring
    # this up) -- unlike cricmatch's, which was verified end-to-end
    # (ali789/asha788). Run balance_checker.py --once against one real account
    # first and check the sheet's STATUS column before trusting a full sweep.
    supports_http_login=True,
    http_login_path="/login",
    http_balance_path="/api2/v2/getBalance",
    # Same caveat as supports_http_login above -- inferred from the shared
    # template, not yet round-tripped against a real khelofun account.
    supports_change_password=True,
    change_password_path="/changePassword",
    sel={
        # ---- signup (carried over from cricmatch.py, UNVERIFIED for
        # khelofun -- re-run inspect_form.py against khelofun before relying
        # on these for an actual signup) ----
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
        "otp_popup": ".signup_otp_popup",
        "otp_digits": "input.otp__digit_signup",
        "otp_verify": ["a.get_user_otp", ".vf_otpBtn a", ".vf_num_otpSec a.mb-button",
                       ".signup_otp_popup a:has-text('Verify')"],
        "otp_error": ".otp_error",
        # ---- login + wallet (confirmed present in khelofun's raw HTML) ----
        "open_login": "a.cls_loginbtn",
        "login_username": "#user_login_id",
        "login_password": "#passwordId",
        "login_submit": "#loginbutton",
        "logged_in_indicator": "#acctSec",
        "casino_nav": "a:has-text('Live Casino'):not([href=\"javascript:;\"])",
        "wallet_balance": "span.total_balance, span.wallet_balance",
    },
)

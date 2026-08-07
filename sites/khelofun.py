"""khelofun.com -- same white-label template as cricmatch247 (confirmed live
2026-08-01: identical Laravel csrf-token/session-cookie setup, and the exact
same d2g8jl9s27zu.cloudfront.net/wmsc/ CDN asset paths). Login markup
(a.cls_loginbtn, #acctSec) matches byte-for-byte in khelofun's raw homepage
HTML too, which is why supports_casino/login selectors below are copied
straight from cricmatch.py rather than re-inspected from scratch.

The login -> wallet-balance -> change-password path was exercised first
(balance_checker.py / password_changer.py). free_number_path is now also
turned on (phone_freer.py) -- same "/send_otp_touser" path as cricmatch,
INFERRED from the shared template, not yet confirmed live against a real
khelofun account (cricmatch's was confirmed live 2026-07-22 by comparing a
real account's Mobile Number before/after; khelofun's hasn't had that same
before/after check yet). Run phone_freer.py --once against one real row
first and confirm the account's mobile number actually changed before
trusting a full sheet sweep.

Signup selectors confirmed live 2026-08-07: inspecting the real page (after
dismissing the SPRIBE overlay via .skip_right_img, same trap documented
above) showed the register modal's fields byte-for-byte identical to
cricmatch's -- #userNameid / #userEmailid / #pass_log_id / #phoneNumber /
#remChck2 / button.cls_register_new -- and a full `main.py --no-submit`
dry run against khelofun.com filled the form end-to-end with no errors.
OTP widget/verify selectors are still UNVERIFIED (no real phone/OTP was
used in that check) -- re-confirm those on the first real signup.

supports_http_fast (HTTP-only signup) is still left at its False default --
out of scope for what's been requested so far, and the register network
traffic wasn't captured for this site; don't flip it on without doing the
same live network capture cricmatch's went through."""
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
    # Same caveat again -- see the module docstring's phone_freer.py note.
    supports_free_number=True,
    free_number_path="/send_otp_touser",
    sel={
        # ---- signup (carried over from cricmatch.py, UNVERIFIED for
        # khelofun -- re-run inspect_form.py against khelofun before relying
        # on these for an actual signup) ----
        "open_modal": [".registerUserData", "button.headerjoinBtn",
                       "button.cls_reg_btn", ".join__btn"],
        # ".skip_right_img" dismisses the SPRIBE/Aviator walkthrough overlay
        # that covers the whole page on load -- confirmed live 2026-08-01,
        # same overlay documented for spin24star/cricmatch under "Multi-site
        # support" in CLAUDE.md. Without it, login()'s LOGIN-button click was
        # silently intercepted by the overlay and timed out every attempt.
        "close_popup": [".skip_right_img", ".mnPopupClose", ".pgSoftClsBtn",
                        ".support_popup_close", ".areSurecancelBtn", "button:has-text('Close')"],
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

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

supports_http_fast is ON since 2026-11-11, and the REQUEST half is proven
live (through the rotating mobile proxy, no account created): GET / answers
200 with the csrf-token meta tag and the usual laravel_session/XSRF-TOKEN/
AWSALB cookies, and a POST /register carrying cricmatch's default field
names reaches the application and is answered by Laravel's own validator --
{"status":201,"message":{"email":"The email must be a valid email address.",
"password":"The password must be at least 5 characters."}} for a
deliberately invalid identity. So the path, the field names and the csrf
handling are right.

⚠️ The two SUCCESS statuses below are NOT yet seen live -- they need a real
phone and a real SMS, exactly as winclash's and funcrash's did. They are
set explicitly rather than left empty because khelofun's refusals carry NO
message_class (see the body above), and the cricmatch convention of judging
by message_class would therefore read every refusal as a SUCCESS -- the
same fault playcric365 was given explicit statuses to avoid. Do one real
/newacc and check accounts.db before running volume; if a status is wrong
the raw reply still reaches result["messages"], so it will say so rather
than fail silently."""
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
    # HTTP-only signup -- see the module docstring for what is proven live
    # (the csrf GET and the register POST's request half) and what is not
    # (these two statuses). The field names, /register path and csrf source
    # are all cricmatch's defaults, which is what the probe confirmed.
    supports_http_fast=True,
    # MUST stay non-empty: khelofun's refusals carry no message_class, so an
    # empty list falls back to the message_class check and reads every
    # refusal as a success. 201 is a refusal here, so never list it.
    http_status_otp_sent=[205],
    http_status_registered=[200],
    # A per-IP signup limit, not a fault (2026-11-14, on the Geonix IPs): about
    # 4 signups from one address within minutes, then every register from it is
    # answered with this for 80+ minutes. Matching it makes the attempt a
    # BLOCKED result, so the bot rests that proxy (PROXY_BLOCK_REST_SECS, set to
    # 2h in .env.khelofun.signup) instead of retrying into the refusal.
    ip_refused_texts=["something went wrong. please try again"],
    # Same caveat as supports_http_login above -- inferred from the shared
    # template, not yet round-tripped against a real khelofun account.
    supports_change_password=True,
    change_password_path="/changePassword",
    # Same caveat again -- see the module docstring's phone_freer.py note.
    supports_free_number=False,
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

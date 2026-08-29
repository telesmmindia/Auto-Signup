"""winclash.com -- a DIFFERENT platform from the cricmatch/khelofun/starexch
white-label Laravel template every other site here runs on. Selectors and the
signup call sequence were captured live 2026-08-29 by loading the real page
in Chromium and reading its own inline JavaScript (no account was created
during discovery).

What makes it different, and why the engine grew flags for each:

* The signup form is its OWN PAGE at /join-now, not a modal behind a JOIN
  button -- hence register_trigger="page_url".
* The whole site sits behind an AWS WAF *challenge* (challenge.js, not the
  CAPTCHA action spin24star gets). Fetching /join-now cold returns a bare
  403/202; loading the homepage first lets challenge.js mint the
  `aws-waf-token` cookie, after which /join-now loads normally in the same
  context. signup_once() already loads the homepage before opening the form,
  so this works out; open_signup_modal() retries once in case the token
  isn't ready yet.
* The signup OTP is ONE <input maxlength=6> (input.signup_verify_otp), not
  cricmatch's six single-character boxes -- hence otp_mode="single".
* Verifying the OTP takes TWO round trips, which is why otp_outcome_timeout_ms
  is widened: clicking #confirmSignupOtpBtn_ POSTs /api2/v2/confirmSignupOtp,
  and only on statusCode 251 does the page's own JS re-click #signUpButton to
  POST /sign-up with the code, wait 1s, and navigate to "/".
* There is no T&C checkbox on the form at all.

The register call itself (read out of the page's own handler):
    POST /sign-up  {_token, user_name, email, password, confirm_password,
                    mobile_number, otp}
      -> status 205  = OTP sent, show the verify step
      -> status 1    = registered, redirect to data.redirectTo
      -> anything else = rejection, message in data.msg
supports_http_fast stays False: that endpoint is behind the same WAF
challenge, so a bare requests.Session has no token to present.

Form rules, taken from the page's validation() and the click handler rather
than guessed -- gen_account()/gen_password() are driven by the profile fields
below so a generated identity can't be bounced by the form's own JS:
  username  5-12 chars, letters and digits only (the field strips anything
            else on input and carries pattern="...{5,12}$")
  password  6-12 chars, needs a lowercase, an uppercase and a digit; the
            special-character rule is commented out in their JS, and the
            password must not equal the username or the mobile number, nor
            appear as a substring of the email
  mobile    10 digits matching ^(0|91)?[6-9][0-9]{9}$
"""
from .base import SiteProfile
from .cricmatch import GENERIC_RESULT_SELECTORS

PROFILE = SiteProfile(
    key="winclash",
    hostnames=["winclash.com"],
    # Signup is a page, not a modal: /join-now.
    register_trigger="page_url",
    register_path="/join-now",
    # No T&C checkbox anywhere on the form.
    has_terms_checkbox=False,
    # One input, six digits, two-step verify (see the module docstring).
    otp_mode="single",
    otp_length=6,
    otp_outcome_timeout_ms=30000,
    # Enforced by the form's own JS -- see the docstring.
    username_max_len=12,
    password_min_len=6,
    password_max_len=12,
    password_needs_special=False,
    # No dedicated element for a taken number; every rejection, this one
    # included, comes back through notify() -> a Snackbar toast.
    phone_taken_selector=None,
    # Required, not cosmetic -- see the field's comment in sites/base.py.
    user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"),
    # NOT yet observed live -- the exact server wording for a re-used mobile
    # number is unknown until a signup actually hits one. Until then a taken
    # number surfaces as a plain `error` carrying the site's own message,
    # which is correct if less specific. Add the real substring here (lower
    # case) once seen, and the loop starts treating it as `phone_taken`.
    phone_taken_texts=[],
    # notify() is Snackbar.show(..., pos: "top-right", duration: 3000) --
    # confirmed live by reading notify.toString() in the page. Same markup
    # spin24star uses, and the same 3s auto-dismiss, so callers must use the
    # messages wait_for_register_outcome() captured rather than re-reading.
    result_selectors=GENERIC_RESULT_SELECTORS + [".snackbar-container"],
    tracking_param="btag",
    # Not inspected: the casino markup and the login/getBalance calls. The
    # login selectors below are the real ones off /join-now (that page hosts
    # the login form too) but nothing has been driven end to end yet, so the
    # flags stay off and login()/balance/casino refuse cleanly.
    supports_casino=False,
    supports_login=False,
    supports_http_fast=False,
    supports_free_number=False,
    supports_change_password=False,
    sel={
        # ---- signup (all confirmed present and visible on /join-now) ----
        # Nothing overlays the form on this site; the generic closers are
        # kept because dismiss_popups() runs unconditionally and they are
        # harmless no-ops when absent.
        "close_popup": [".mnPopupClose", ".pgSoftClsBtn", ".support_popup_close",
                        ".areSurecancelBtn", "button:has-text('Close')"],
        "username": "#userName",
        "email": "#email",
        "password": "#password",
        "phone": "#mobileNumber",
        "submit": "#signUpButton",
        # The signup OTP step. NOT input.otpNumber (the login-with-OTP
        # widget on the same page) and NOT #userOtp (a dead /otp-verify
        # dialog left in the markup) -- neither must ever be matched.
        "otp_popup": ".regStepTwo",
        "otp_digits": "input.signup_verify_otp",
        "otp_verify": ["#confirmSignupOtpBtn_"],
        # There is NO inline OTP error element on this site, and the
        # snackbar must not stand in for one: notify() fires on the SUCCESS
        # path too (statusCode 251), so treating a visible snackbar as "the
        # OTP was rejected" would report every successful signup as a
        # failure. None here means the engine judges the OTP purely by
        # whether the verify step went away -- on success this site navigates
        # to "/" -- while read_result() still scrapes the snackbar for the
        # human-readable reason either way.
        "otp_error": None,
        # ---- login: observed on /join-now, NOT yet driven end to end ----
        "open_login": "button.clsLoginClick",
        "login_username": "#user_login",
        "login_password": "#pass_eye_user",
        "login_submit": "button.btnLogin",
        "logged_in_indicator": "#acctSec",
    },
)

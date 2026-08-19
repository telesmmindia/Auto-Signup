"""starexch555.com -- same white-label Laravel platform as cricmatch247 and
khelofun (identical login modal, wallet header and JSON API), added 2026-08-19.

Everything flagged True below was confirmed live against a real account on
2026-08-19, not inferred from the family resemblance:

  * login       -- probe_login_balance.py drove a real browser login: the
                   cricmatch login selectors (a.cls_loginbtn -> #user_login_id /
                   #passwordId -> #loginbutton) matched byte for byte, and
                   read_wallet_balance() read the header wallet correctly.
  * HTTP-fast   -- POST /login (username/password/remember_me/_token) ->
                   {"status":200,"message":"Login Successfully","url":"?uid=..."},
                   then POST /api2/v2/getBalance (_token) -> status 200 with the
                   figure in balance.balance / balance.main_balance /
                   balance.totalBalance. NOTE there is no balance.wallet key
                   here (cricmatch has one); http_check_account_balance()'s
                   totalBalance/balance fallback is what makes this site work.
                   Measured 1.8s end to end vs ~20-30s for a browser login.
  * changePassword -- POST /changePassword (oldPassword/newPassword/_token)
                   answers the same {"status":..,"msg":..} shape cricmatch
                   uses. Confirmed with a deliberately wrong current password:
                   {"status":201,"msg":"Please enter the valid current
                   password"}, i.e. 200 = changed, 201 = refused. Unlike
                   cricmatch this endpoint answers a bare requests.Session
                   (no browser, no login-only cookies needed).

A first bare POST /login from this machine came back as a flat edge 403, which
looked like cricmatch's rate block -- it was not. The field is "username", and
sending "userName" is refused at the edge before the app sees it. Don't read a
403 here as a rate block without checking the field names first.

NOT verified, deliberately left False:
  * supports_casino -- the site does host Evolution/Ezugi Baccarat, but its
    Live Casino entry point is a <div data-href="/live-casino"> rather than
    cricmatch's <a>, so open_casino_lobby()'s selector does not match. Leaving
    this False makes the casino/tournament paths refuse cleanly instead of
    mis-clicking uninspected markup. Flip it only after a live capture.
  * signup (register modal / OTP) -- never inspected on this site. The selectors
    below are inherited from the cricmatch template as a starting point for
    inspect_form.py; treat them as unconfirmed guesses.
  * supports_free_number -- /send_otp_touser never probed here.
"""
from .base import SiteProfile
from .cricmatch import GENERIC_RESULT_SELECTORS

PROFILE = SiteProfile(
    key="starexch",
    hostnames=["starexch555.com"],
    register_trigger="modal",
    has_terms_checkbox=True,
    phone_taken_selector=".err_phone",
    result_selectors=GENERIC_RESULT_SELECTORS,
    tracking_param="btag",

    # Not inspected -- see the module docstring.
    supports_casino=False,
    supports_http_fast=False,
    supports_free_number=False,

    # Confirmed live 2026-08-19.
    supports_http_login=True,
    http_login_path="/login",
    http_balance_path="/api2/v2/getBalance",
    supports_change_password=True,
    change_password_path="/changePassword",
    # Confirmed live 2026-08-19: a bare requests.Session that has only done
    # POST /login can change the password -- no browser, no in-page fetch()
    # needed (unlike cricmatch). Verified on both the refusal path (wrong
    # current password -> status 201) and a real change.
    supports_http_change_password=True,

    sel={
        # ---- signup: inherited from the cricmatch template, UNCONFIRMED here.
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

        # ---- login: confirmed live 2026-08-19 (the modal is fetched from
        # /append/loginpp on demand, so these ids are absent from the static
        # page source -- don't conclude they're wrong by grepping the HTML).
        "open_login": "a.cls_loginbtn",
        "login_username": "#user_login_id",
        "login_password": "#passwordId",
        "login_submit": "#loginbutton",
        "logged_in_indicator": "#acctSec",

        # ---- casino: NOT inspected (supports_casino is False). starexch's
        # Live Casino tile is a div[data-href], not an <a>, so this cricmatch
        # selector is known NOT to match -- it is a placeholder, not a guess
        # worth trying.
        "casino_nav": "a:has-text('Live Casino'):not([href=\"javascript:;\"])",

        # Header wallet -- confirmed live 2026-08-19 (read ₹180.00, matching
        # the getBalance JSON). Empty at page load, filled by the site's own
        # getBalance call, so read_wallet_balance() must keep polling.
        "wallet_balance": "span.total_balance, span.wallet_balance",
    },
)

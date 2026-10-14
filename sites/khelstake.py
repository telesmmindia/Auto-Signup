"""khelstake24.com -- same white-label Laravel platform as cricmatch247,
khelofun and starexch (same d2g8jl9s27zu.cloudfront.net/wmsc/ assets, csrf
meta tag, a.cls_loginbtn / #acctSec login markup), added 2026-10-09 for the
multi-ID exchange bet bot (exchange.py -- a STANDALONE Telegram bot with its
own EXCHANGE_BOT_TOKEN, not a BOT_MODE of telegram_bot.py).

Confirmed live 2026-10-09 against a real account:
  * HTTP login -- POST /login (username/password/remember_me/_token) ->
                  {"status":200,"message":"Login Successfully","url":"?uid=..."},
                  then POST /api2/v2/getBalance -> balance.wallet (a float,
                  cricmatch-style) plus balance.exposure. So
                  http_check_account_balance() works unchanged.

Only the exchange (Back/Lay on Match Odds) is in use here. Signup, casino,
free-number and change-password were never probed on this site, so they stay
off; the signup selectors below are the shared template's, inherited as a
starting point for inspect_form.py and nothing more.
"""
from .base import SiteProfile
from .cricmatch import GENERIC_RESULT_SELECTORS

PROFILE = SiteProfile(
    key="khelstake",
    hostnames=["khelstake24.com"],
    register_trigger="modal",
    has_terms_checkbox=True,
    phone_taken_selector=".err_phone",
    result_selectors=GENERIC_RESULT_SELECTORS,
    tracking_param="btag",

    supports_login=True,
    supports_casino=False,
    supports_http_fast=False,
    supports_free_number=False,

    supports_http_login=True,
    http_login_path="/login",
    http_balance_path="/api2/v2/getBalance",

    sel={
        "open_modal": [".registerUserData", "button.headerjoinBtn",
                       "button.cls_reg_btn", ".join__btn"],
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

        "open_login": "a.cls_loginbtn",
        "login_username": "#user_login_id",
        "login_password": "#passwordId",
        "login_submit": "#loginbutton",
        "logged_in_indicator": "#acctSec",

        "wallet_balance": "span.total_balance, span.wallet_balance",
    },
)

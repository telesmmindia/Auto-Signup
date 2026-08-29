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
Live casino / Stock Market, all mapped live 2026-08-29 on a real account:

* Login is POST /api2/v2/login -> {"status":200,"id":<user id>,
  "message":"Login Success"}, followed by a redirect to /?redirecting=<id>.
  The header then renders .headUserName with the username and .wallet_balance
  with the figure. Balance is POST /api/getBalance ->
  {"status":200,"balance":{...,"wallet":0,"main_balance":"0.00",
  "totalBalance":"0.00",...}}.
  WARNING, not yet resolvable: the only account available for this work held
  0.00, so it is NOT known whether http_check_account_balance's preferred
  "wallet" key carries the real figure here or is always 0 with the money in
  main_balance/totalBalance. Both read 0.00, which is correct but tells the
  two apart not at all. Confirm on a FUNDED account before trusting a
  winclash balance reading.

* A table is launched by /casinoRedirect?q=<id>&provider=<prov>&type=casino,
  read straight out of the site's own .casinoLink click handler, which hands
  that URL to window.open(..., "_blank"). Navigating there directly lands on
  ezugi.evo-games.com/frontend/evo/r2/#...&table_id=StockMarket00001 -- the
  SAME Evolution client this engine already drives everywhere else.

* The lobby tile is deliberately NOT used. It is <p class="playBtn
  casinoLink" data-link="<id>" data-provider="<prov>">, which is only visible
  on hover and is paginated behind the lobby's own search box; worse, its
  click handler REFUSES to launch when both the real and bonus balances are
  zero (it opens an "add money" popup instead). The redirect URL launches the
  table regardless, which is what made this mappable at all on an empty
  account.

* The Evolution table needs NO engine changes. Verified by pointing the
  existing readers at it on a live Stock Market table:
      _table_id            -> "StockMarket00001"
      find_game_frame      -> found (evo-games.com)
      wait_for_live_table  -> True (STOCKMARKET profile)
      read_game_balance    -> 0        _read_total_bet -> 0
      read_portfolio       -> 0
      read_chips           -> [10, 50, 100, 200, 500, 2500], selected 10
      betting window       -> open in 21 of 50 samples over 100s, the
                              instruction banner cycling "PLACE YOUR BETS n"
                              -> "NEXT GAME SOON" exactly as sites/games.py's
                              window_mode="instruction" expects
  Chip rail and table minimum match cricmatch's Stock Market (Rs 10), so
  sites/games.py's STOCKMARKET profile applies unchanged.

* NOT verified, and it cannot be until an account here holds money: placing
  an actual bet, and therefore a real hedge round. Everything above is a
  read. The site's own handler proves a zero-balance account is refused a
  table through the UI, so a funded account is needed for any real run.

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
    # OBSERVED LIVE 2026-08-29: registering a second account on a number
    # already used comes back as the snackbar "The mobile number is already
    # in use." (rejected at the register POST, before any SMS is sent, so
    # nothing is spent finding out).
    #
    # Matched on the phone-specific phrase rather than a bare "already in
    # use": if this site words a taken EMAIL the same way, the looser string
    # would misfile that as a phone problem and the continuous loop would
    # rotate to the next number for a fault a new number cannot fix. The
    # second entry is cricmatch's phrasing, harmless here and correct if
    # this site ever uses it.
    phone_taken_texts=["mobile number is already in use",
                       "mobile number has already been taken"],
    # notify() is Snackbar.show(..., pos: "top-right", duration: 3000) --
    # confirmed live by reading notify.toString() in the page. Same markup
    # spin24star uses, and the same 3s auto-dismiss, so callers must use the
    # messages wait_for_register_outcome() captured rather than re-reading.
    result_selectors=GENERIC_RESULT_SELECTORS + [".snackbar-container"],
    tracking_param="btag",
    # This site's snackbar carries progress AND success text, not just
    # errors -- the same trait that makes otp_error deliberately None above.
    # "Please wait" appears the instant the login button is clicked and
    # "Login Success" the instant it works, and both land in the very element
    # login() scrapes to surface a real rejection, so each one in turn was
    # reported as the reason the login failed. Neither is a failure.
    benign_texts=["please wait", "login success"],
    # Login and the live casino are both DRIVEN LIVE (2026-08-29) -- see the
    # "Live casino" notes at the bottom of this file.
    supports_login=True,
    supports_casino=True,
    # The balance endpoint is NOT /api2/v2/getBalance. That path 404s here
    # (a real application 404 -- {"message": ""} -- not a WAF block; the
    # site's own JS asks for it too and gets the same 404, evidently a
    # leftover). The live one is /api/getBalance, captured off the wire on a
    # real logged-in session.
    http_login_path="/api2/v2/login",
    http_balance_path="/api/getBalance",
    # An in-page fetch of that endpoint IS a trustworthy proof of login, so
    # login() verifies real auth here rather than trusting the header marker.
    # supports_http_login stays False all the same: a bare requests.Session
    # cannot reach any of this, because the AWS WAF wall in front of every
    # winclash URL only lets through a client that has run challenge.js.
    verify_auth_in_page=True,
    supports_http_login=False,
    # The login form lives on /join-now alongside signup. The homepage has a
    # header login bar too, but it does not always finish rendering -- see
    # SiteProfile.login_path.
    login_path="/join-now",
    # A full-page overlay eats both plain and forced clicks on the login
    # button -- see SiteProfile.login_click_mode.
    login_click_mode="js",
    # Every page load here can be met by an AWS WAF interstitial, so browser
    # flows clear it before looking for page content.
    waf_on_navigation=True,
    # No lobby, no tile: navigate straight to the site's own launch redirect.
    casino_launch_mode="direct_game_url",
    casino_provider="evolution",
    # Ids as winclash's own /casinoGamesList returns them. Only tables this
    # engine actually drives are listed; add one by reading its id from that
    # endpoint, never by guessing.
    casino_game_ids={
        "Stock Market": "1027",
        "Baccarat A": "86",
        "Baccarat B": "87",
        "Auto-Roulette": "220",
    },
    supports_http_fast=False,
    supports_free_number=False,
    supports_change_password=False,
    sel={
        # ---- signup (all confirmed present and visible on /join-now) ----
        # Nothing overlays the signup form, but a cashback popup DOES cover
        # the header once logged in -- its backdrop
        # (div.overlay.overlay--active, z-index 201) intercepts every click
        # on the page. .modalClose is that popup's own close button. The rest
        # are generic closers kept because dismiss_popups() runs
        # unconditionally and they are harmless no-ops when absent.
        "close_popup": [".modalClose", ".mnPopupClose", ".pgSoftClsBtn",
                        ".support_popup_close", ".areSurecancelBtn",
                        "button:has-text('Close')"],
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
        # ---- login: these four DRIVEN LIVE 2026-08-29 (logged into a real
        # account created by this engine and landed on the account view).
        # The login form is hosted on /join-now itself, alongside signup.
        "open_login": "button.clsLoginClick",
        "login_username": "#user_login",
        "login_password": "#pass_eye_user",
        "login_submit": "button.btnLogin",
        # NOT #acctSec. That is the cricmatch/starexch marker and it does not
        # exist here at all -- checked while genuinely logged in and it
        # counted 0, so inheriting it would have made every winclash login
        # look failed. The header's own username element is the real one.
        "logged_in_indicator": ".headUserName",
        # Header wallet figure. .wallet_balance exists here (1 node);
        # cricmatch's .total_balance does NOT, so don't carry that over.
        # Read live on a logged-in account: it renders "0.00" and also
        # carries data-actual / data-wager / data-mode attributes, which the
        # site's own tile handler uses to decide real vs bonus chips. Still
        # unconfirmed against a NON-ZERO balance -- see the note at the
        # bottom of this file.
        "wallet_balance": ".wallet_balance",
    },
)

"""playcric365.com -- the same white-label Laravel platform as cricmatch247,
khelofun and starexch555. Added 2026-10-22 for a signup bot + a Stock Market
gameplay bot.

Mapped read-only 2026-10-22 over plain HTTP (no account, no bet, nothing
registered). What that pass established, and what it could NOT:

CONFIRMED (read off the site's own markup/JS):
  * No WAF at all. A bare curl of "/" answers 200 with Apache/2.4.41, a
    Laravel session + XSRF-TOKEN cookie and a <meta name="csrf-token">.
    So no CAPSOLVER_API_KEY is needed here, unlike winclash/spin24star, and
    waf_on_navigation stays False.
  * The register + login forms are NOT in the homepage source. They are
    fetched on demand from /append/loginpp, exactly like starexch -- so
    don't conclude the ids below are wrong by grepping the homepage. That
    partial was fetched directly and carries, byte-identically to
    cricmatch's: #userNameid, #userEmailid, #pass_log_id, #phoneNumber,
    #remChck2, button.cls_register_new (register) and a.cls_loginbtn,
    #user_login_id, #passwordId, #loginbutton (login).
  * The signup OTP widget is cricmatch's too: six input.otp__digit_signup
    boxes plus a.get_user_otp to verify. The partial also carries the
    SECOND, login-with-OTP widget (input.otp__digit with no _signup
    suffix) -- the same trap cricmatch has. Do not target it.
  * The register endpoint is POST /register and login is POST /login
    (both read out of the partial's own $.ajax calls), which is the
    cricmatch convention.
  * Live casino: tiles are div[onclick="goToCasinoLive(this)"] carrying
    data-id/data-provider -- starexch's vocabulary, not cricmatch's text
    tiles, hence casino_tile_mode below. The catalogue comes from the
    site's own POST /displaygames (game=<category>), and under provider
    "evolution" it lists **Baccarat A = data-id 85 and Baccarat B = 86** --
    the same ids starexch uses, as a shared template predicts.
  * Stock Market is NOT in this site's own catalogue (every category
    /displaygames offers was swept: allgames/new/baccarat/roulette/
    blackjack/teenpatti/andar_bahar/poker/indiangames/topgames -- no
    "Stock" anywhere). That is cricmatch's situation, not winclash's, so
    the STOCKMARKET GameProfile's via_provider_lobby hop (open Baccarat A,
    click Evolution's LOBBY button, search there) is the route, and it
    needs no change here.

NOT CONFIRMED -- no account was available, so everything below the login
wall is inference from the template, and the first run must verify it:
  * The login/casino selectors have never been DRIVEN here. Logged out,
    the site's own goToCasinoLive() is stubbed to just open the login
    modal ($(".cls_loginbtn").click()), so the real tile-launch handler
    could not be read at all. casino_lobby_mode/casino_tile_mode are set
    to starexch's pair because the markup matches starexch's, not because
    a table was opened. Run verify_baccarat.py (and verify_stockmarket.py)
    with a real account before any /run -- both are read-only.
  * supports_http_fast stays False. /register exists and is almost
    certainly the same JSON endpoint cricmatch's --fast path drives, but
    "almost certainly" is how --fast breaks silently (it hard-codes the
    field names and JSON shape), so the browser path carries signups here
    until one real HTTP signup has been checked against accounts.db.
  * supports_http_login / supports_change_password / supports_free_number
    all stay False: /api2/v2/getBalance, /changePassword and
    /send_otp_touser were never seen in this site's markup and were never
    probed. Balances therefore come from the header wallet selector, the
    same way winclash's do.
"""
from .base import SiteProfile
from .cricmatch import GENERIC_RESULT_SELECTORS

PROFILE = SiteProfile(
    key="playcric365",
    hostnames=["playcric365.com"],
    register_trigger="modal",
    has_terms_checkbox=True,
    phone_taken_selector=".err_phone",
    result_selectors=GENERIC_RESULT_SELECTORS,
    tracking_param="btag",

    # Login markup confirmed present in /append/loginpp and identical to
    # cricmatch's; never driven (no account). supports_login is what lets
    # the stockmarket bot try at all -- if these are wrong the login fails
    # with a clear "could not find" rather than mis-clicking something.
    supports_login=True,
    supports_casino=True,
    # starexch's pair, because the lobby markup is starexch's: tiles are
    # div[onclick="goToCasinoLive(this)"], and the provider lives in the
    # lobby URL rather than in a category tab. ?p=evolution matters -- this
    # site also carries an Ezugi-client baccarat (data-provider="ezugi",
    # e.g. "Casino Marina Baccarat B") whose markup the engine cannot drive.
    casino_lobby_mode="direct_url",
    casino_lobby_path="/live-casino?p=evolution",
    casino_tile_mode="go_to_casino_live",
    # Read live out of POST /displaygames (game=baccarat) 2026-10-22. Only
    # consulted when casino_launch_mode == "direct_game_url" (it is not
    # here -- this site uses the lobby), so these are recorded as verified
    # fact for whoever needs them, not as the active route.
    casino_game_ids={"Baccarat A": 85, "Baccarat B": 86},

    # VERIFIED LIVE 2026-10-23 on a real account, and this flag is
    # LOAD-BEARING -- it is not just a speed option.
    #
    # login() can only confirm a session is genuinely authenticated when a
    # profile offers an auth endpoint (main.py: can_verify_auth =
    # supports_http_login or verify_auth_in_page). Without one it falls back
    # to the header indicator, #acctSec -- and on this site #acctSec is
    # VISIBLE WHILE LOGGED OUT (measured: 1 visible before login, 1 after),
    # exactly the false positive CLAUDE.md warns about for this white-label
    # family. The engine could therefore believe it was logged in, walk to
    # the live-casino lobby, be served the GUEST lobby (which lists no live
    # tables), fail to find the tile, and then -- because a LOGIN button is
    # on screen -- report "the site dropped your login session ... an
    # account-level restriction". That message blamed the account for what
    # was really an unverified login. Turning this on makes login() poll the
    # balance endpoint for up to 30s and only return ok on a real session.
    #
    # Measured on playcric365 with an in-page fetch:
    #   logged out -> POST /api2/v2/getBalance  401 {"message":"Unauthenticated."}
    #   logged in  -> POST /api2/v2/getBalance  200 {"status":200,"balance":{...}}
    # so it cleanly separates the two states. The figure lives in
    # balance.balance / balance.main_balance (no balance.wallet key here,
    # same as starexch -- http_check_account_balance()'s totalBalance/balance
    # fallback is what reads it).
    supports_http_login=True,
    http_login_path="/login",
    http_balance_path="/api2/v2/getBalance",

    # Still never probed on this site. See the module docstring.
    supports_http_fast=False,
    supports_change_password=False,
    supports_free_number=False,

    sel={
        # ---- signup: every id below was read out of this site's own
        # /append/loginpp partial on 2026-10-22, not inherited blind.
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
        # The signup OTP screen. NOTE the partial also carries the
        # login-with-OTP widget (input.otp__digit, no _signup suffix) --
        # matching that one would drive the wrong form.
        "otp_popup": ".signup_otp_popup",
        "otp_digits": "input.otp__digit_signup",
        "otp_verify": ["a.get_user_otp", ".vf_otpBtn a", ".vf_num_otpSec a.mb-button",
                       ".signup_otp_popup a:has-text('Verify')"],
        "otp_error": ".otp_error",

        # ---- login: present in /append/loginpp, never driven.
        "open_login": "a.cls_loginbtn",
        "login_username": "#user_login_id",
        "login_password": "#passwordId",
        "login_submit": "#loginbutton",
        "logged_in_indicator": "#acctSec",

        # Unused while casino_lobby_mode is "direct_url" (no nav element on
        # this site responds to a click when logged out, same as starexch);
        # kept so no call site can KeyError on it.
        "casino_nav": "a:has-text('Live Casino'):not([href=\"javascript:;\"])",

        # Header wallet. Empty at page load on every site in this family --
        # the site fills it with its own getBalance() call seconds later,
        # which is why read_wallet_balance() polls instead of reading once.
        "wallet_balance": "span.total_balance, span.wallet_balance",
    },
)

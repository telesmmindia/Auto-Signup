"""funcrash.com -- the SAME platform as winclash (the "rjm"/jeet white-label),
not the cricmatch/khelofun/starexch Laravel template. Mapped read-only
2026-10-21 from the site's own markup and endpoints plus a client screen
recording of a real hand being played; no account was touched during
discovery.

What the evidence was:

* The homepage sets a `jeet_session` cookie and carries winclash's exact
  login markup -- button.clsLoginClick / button.btnLogin, #user_login,
  #pass_eye_user -- plus /join-now, the `casinoLink` tile class and the
  /casinoRedirect?q=<id>&provider=<prov> launch URL. That is winclash's
  template byte for byte, so this profile is a copy of sites/winclash.py
  with the ids re-read for this site.
* It is behind the same AWS WAF. A plain GET of /join-now answers
  202 + `x-amzn-waf-action: challenge` from `server: awselb/2.0`, with an
  EMPTY body on an XHR-shaped request and the 2407-byte `gokuProps`
  interstitial on a navigation-shaped one -- the exact pair of behaviours
  documented for winclash in CLAUDE.md. So waf_on_navigation is True and
  CAPSOLVER_API_KEY is REQUIRED here, same as winclash.
* Game ids came from the site's own /casinoGamesList (paginated POST,
  provider=evolution), not from guesses: Baccarat A is 86 and Baccarat B is
  87 -- the same ids winclash uses, which is what a shared template would
  predict.
* The client's recording shows a funcrash account opening Baccarat A into
  `ezugi.evo-games.com`, i.e. THE EVOLUTION CLIENT this engine already
  drives everywhere else: the live P/B/T spots, the roadmaps, the circle
  countdown timer, TOTAL BET, UNDO and x2 DOUBLE are all the usual markup.
  Table limits render as "Baccarat A Rs 100-500,000", so the minimum stake
  is Rs 100 a side, same as cricmatch's and winclash's Baccarat A.

NOT verified live yet (needs one run of verify_baccarat.py with real
credentials): the login actually completing, the header balance selector,
and the table id behind game 86. Everything in sel[] below is inherited from
the identical winclash markup rather than confirmed on this host.

SIGNUP IS OUT OF SCOPE here. The client wants gameplay only, so
supports_http_fast stays False and the signup selectors are winclash's
unverified -- run inspect_form.py before trusting them.
"""
from .base import SiteProfile
from .cricmatch import GENERIC_RESULT_SELECTORS

PROFILE = SiteProfile(
    key="funcrash",
    hostnames=["funcrash.com"],

    # ---- the AWS WAF, confirmed on the wire (see the docstring) ----
    waf_on_navigation=True,
    # Required, not cosmetic: headless Chromium's own UA is what winclash's
    # identical WAF flat-403s, and there is no reason to find out the hard
    # way whether this host is configured the same.
    user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"),

    # ---- login + live casino ----
    supports_login=True,
    supports_casino=True,
    # The login form is part of /join-now, which is sturdier than the header
    # bar on a slow load -- the reason winclash points here too.
    login_path="/join-now",
    # The template's full-page div.overlay.overlay--active eats both plain
    # and forced clicks; an in-page element.click() is what fires the login.
    login_click_mode="js",
    # Route exists (a bare POST answers 419 CSRF, not 404).
    http_login_path="/api2/v2/login",
    # /api/getBalance answers (302 to login when anonymous).
    # /api2/v2/getBalance is NOT the one -- it answers the WAF, and on
    # winclash the same path is a genuine application 404.
    http_balance_path="/api/getBalance",
    # An in-page fetch of the balance endpoint is trustworthy proof of auth;
    # a bare requests.Session is not, because of the WAF.
    verify_auth_in_page=True,
    supports_http_login=False,
    # This template's snackbar carries progress and success text, not only
    # errors, so neither of these is a reason to call a login failed.
    benign_texts=["please wait", "login success"],

    # ---- how a table is opened ----
    # No lobby hop: the site's own tile handler builds this URL, and unlike
    # the tile it launches the table even on a zero balance.
    casino_launch_mode="direct_game_url",
    casino_provider="evolution",
    # Read out of funcrash's own /casinoGamesList (provider=evolution),
    # 2026-10-21. Only tables this engine is pointed at are listed -- add one
    # by reading its id from that endpoint, never by guessing.
    casino_game_ids={
        "Baccarat A": "86",
        "Baccarat B": "87",
        # Spribe, not Evolution -- a different provider, a different host and
        # a different client altogether. Read live 2026-10-30 out of the
        # site's OWN catalogue (POST /homepageGames, which answers
        # anonymously), and driven live: q=3139 reaches the real client.
        # NOTE the id is what "q" takes, NOT the catalogue row's "link" slug
        # -- launching with q=mines loads a permanently blank page that looks
        # exactly like a broken launch. That cost a probe run to find.
        "Mines": "3139",
    },
    casino_game_providers={"Mines": "spribe"},
    # The launch redirect lands on aviaport.spribegaming.com, whose document
    # is just a header; the game itself is an iframe at
    # turbo.spribegaming.com/mines. Matching the bare registrable host covers
    # both, and both were seen live.
    casino_game_hosts={"Mines": "spribegaming.com"},

    # ---- signup: NOT inspected on this host, inherited from winclash ----
    register_trigger="page_url",
    register_path="/join-now",
    has_terms_checkbox=False,
    otp_mode="single",
    otp_length=6,
    otp_outcome_timeout_ms=30000,
    username_max_len=12,
    password_min_len=6,
    password_max_len=12,
    password_needs_special=False,
    phone_taken_selector=None,
    phone_taken_texts=["mobile number is already in use",
                       "mobile number has already been taken"],
    result_selectors=GENERIC_RESULT_SELECTORS + [".snackbar-container"],
    supports_http_fast=False,
    supports_free_number=False,
    supports_change_password=False,

    sel={
        "close_popup": [".modalClose", ".mnPopupClose", ".pgSoftClsBtn",
                        ".support_popup_close", ".areSurecancelBtn",
                        "button:has-text('Close')"],
        # ---- signup (winclash's, UNVERIFIED on funcrash) ----
        "username": "#userName",
        "email": "#email",
        "password": "#password",
        "phone": "#mobileNumber",
        "submit": "#signUpButton",
        "otp_popup": ".regStepTwo",
        "otp_digits": "input.signup_verify_otp",
        "otp_verify": ["#confirmSignupOtpBtn_"],
        "otp_error": None,
        # ---- login: these four are present in funcrash's own markup ----
        "open_login": "button.clsLoginClick",
        "login_username": "#user_login",
        "login_password": "#pass_eye_user",
        "login_submit": "button.btnLogin",
        # NOT #acctSec (that is the Laravel template's marker and does not
        # exist on this one). Both of these render only once logged in, so
        # they are not greppable from the anonymous homepage -- they come
        # from the identical winclash markup and want confirming live.
        "logged_in_indicator": ".headUserName",
        "wallet_balance": ".wallet_balance",
    },
)

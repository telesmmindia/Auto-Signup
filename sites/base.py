"""Per-site profile: everything that differs between the sites this driver
supports, so the shared engine in main.py stays site-agnostic. One profile
lives in sites/<site>.py; the registry in sites/__init__.py selects one by URL.

To add a site, copy an existing sites/<site>.py, capture its selectors with
inspect_form.py, and register it in sites/__init__.py -- no engine edits. See
the "Multi-site support" section of CLAUDE.md."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SiteProfile:
    """Site-specific selectors + behavior flags. The engine reads these instead
    of hardcoding per-site markup."""

    key: str                       # short id, e.g. "cricmatch"
    hostnames: list                # host substrings that map a URL to this site
    sel: dict                      # this site's selectors (single values)

    # Register trigger: "modal" (a JOIN button opens an in-page modal, e.g.
    # cricmatch) or "forced_join" (Khelo: several REGISTER buttons, one visible,
    # force-click it -- a game overlay intercepts a plain click).
    register_trigger: str = "modal"

    # Does the register form have a real T&C checkbox to tick? (Khelo renders it
    # pre-checked and it isn't a real <input>, so there's nothing to click.)
    has_terms_checkbox: bool = True

    # Path the signup form lives at, used only when register_trigger ==
    # "page_url" (winclash: /join-now). Navigated to from the already-loaded
    # homepage rather than fetched cold -- see open_signup_modal().
    register_path: str = "/join-now"

    # How the signup OTP is entered.
    #   "digits" -- cricmatch/spin24star: N single-character boxes, one
    #               element per digit, so the DOM itself says how long the
    #               code is (len == locator.count()).
    #   "single" -- winclash: ONE <input maxlength=6>. The element count is
    #               1 regardless of code length, so otp_length below is the
    #               only thing that knows how many digits to ask for.
    otp_mode: str = "digits"

    # How many digits the signup OTP has. Only consulted when
    # otp_mode == "single" (in "digits" mode the box count is authoritative
    # and this is ignored, so existing sites are unaffected).
    otp_length: int = 6

    # How long to wait for the OTP screen to resolve after clicking Verify.
    # cricmatch verifies in ONE call, so 10s is ample. winclash needs two
    # round trips (POST /api2/v2/confirmSignupOtp, then the page's own JS
    # re-clicks SIGN UP to POST /sign-up with the code) plus a 1s timer and a
    # full navigation to "/", so it gets a wider window.
    otp_outcome_timeout_ms: int = 10000

    # Longest username the site's register form accepts, or 0 for "no limit
    # worth enforcing here". winclash's #userName carries
    # pattern="...{5,12}$", so gen_account() has to build a SHORTER username
    # than the first+last+tag default, which runs 13-15 characters.
    username_max_len: int = 0

    # Password rules, enforced by gen_password() so a generated password is
    # never rejected by the form's own JS before a request is even sent.
    # The defaults reproduce the original cricmatch policy (5-60 chars, at
    # least one digit, one special, both cases) exactly, so every existing
    # site keeps generating byte-identically shaped passwords.
    # winclash caps the field at 12 characters and dropped its
    # special-character rule (the check is commented out in its own JS), so
    # it takes a shorter, letters+digits-only password.
    password_min_len: int = 5
    password_max_len: int = 60
    password_needs_special: bool = True

    # User-agent string every browser context for this site must send, or
    # None to leave Playwright's own default in place (which is what every
    # site here used before, so none of them change).
    #
    # winclash needs one. Headless Chromium announces itself as
    # "...HeadlessChrome/..." in navigator.userAgent, and winclash's AWS WAF
    # flat-403s that string outright -- confirmed live 2026-08-29 by running
    # the same navigation twice, once with the default UA and once with a
    # real Chrome UA: the default got "403 Forbidden" as the page title on
    # the HOMEPAGE, never even reaching the signup form, while the real UA
    # got served the site. This is not fingerprint-evasion cleverness; it is
    # the one header that decides whether the site answers at all.
    user_agent: str = None

    # Lower-cased substrings that identify a "this mobile number is already
    # registered" rejection when the site has no dedicated element for it
    # (phone_taken_selector=None) and only says so through a toast. A match
    # promotes a generic `error` to the distinct `phone_taken` outcome, which
    # the continuous-signup loop treats as terminal-but-expected (record it,
    # move to the next number) rather than as a failure. Empty -> the
    # promotion never fires, which is every site that had this behaviour
    # before.
    phone_taken_texts: list = field(default_factory=list)

    # Selector for the inline "mobile number already taken" error, or None if
    # the site surfaces it (if at all) through the generic result scrape instead
    # of a dedicated element (Khelo). Drives the distinct `phone_taken` status.
    phone_taken_selector: str = None

    # Selectors read_result() scrapes after submit (toasts / snackbars / inline
    # validation). Differs per site: Khelo errors are a top-right snackbar.
    result_selectors: list = field(default_factory=list)

    # Query-string param carrying this site's affiliate/referral code
    # (cricmatch: "btag"). Drives extract_referral_code().
    tracking_param: str = "btag"

    # Whether the live-casino selectors below are present/inspected for this
    # site. False -> casino/hedge/tournament commands refuse cleanly instead
    # of mis-clicking uninspected markup.
    supports_casino: bool = False

    # How to reach the Live Casino lobby.
    #   "nav_click"  -- cricmatch/khelofun: click the sidebar Live Casino
    #                   link. A hard page load is NOT safe there (confirmed
    #                   live: it lands on a logged-out-looking homepage).
    #   "direct_url" -- starexch: page.goto(casino_lobby_path). Confirmed
    #                   live 2026-08-19 that the session SURVIVES the load
    #                   there, and that no nav click works at all (every
    #                   candidate times out or silently no-ops).
    casino_lobby_mode: str = "nav_click"

    # Path used only when casino_lobby_mode == "direct_url". starexch groups
    # the lobby by PROVIDER, and the provider matters: "?p=All" surfaces a
    # third-party "Baccarat" whose table markup the engine cannot drive,
    # while "?p=evolution" lists the real Evolution tables (Baccarat A/B).
    casino_lobby_path: str = "/live-casino/?p=evolution"

    # How to open a game tile in that lobby.
    #   "text_click"         -- cricmatch: click a category tab, then the
    #                           tile's text.
    #   "go_to_casino_live"  -- starexch: the tile's clickable element is
    #                           div[onclick="goToCasinoLive(this)"] on the
    #                           tile IMAGE; the <p class="game__name"> label
    #                           is inert (clicking it silently does nothing).
    #                           There are no category tabs to click either --
    #                           the provider is already in the lobby URL.
    casino_tile_mode: str = "text_click"

    # How many digits this site's mobile field holds. Its <input> carries
    # maxlength, so anything longer is SILENTLY TRUNCATED by the browser --
    # typing a country-coded 919304199756 into winclash's 10-character field
    # leaves 9193041997, a different and entirely wrong number. The site then
    # (correctly) never says "already in use", and sends the OTP to a phone
    # nobody is holding, so the signup dies at the OTP step with no clue why.
    # normalize_phone() uses this to strip a leading country code / trunk 0
    # before anything is typed. 10 suits every site here (all Indian).
    phone_digits: int = 10

    # Country dialling code stripped from an over-long number, without the +.
    phone_country_code: str = "91"

    # Path the login form reliably lives on, relative to the site root, or
    # "" for "the homepage has it" (every site but winclash).
    #
    # winclash renders a login bar in the homepage header, but NOT
    # dependably: on a slower machine/link the same homepage load sometimes
    # finishes without #user_login ever becoming visible, which read as
    # "could not find the LOGIN button" and made the login look rejected.
    # /join-now carries the login form as part of the page itself, so it is
    # there every time. Confirmed live 2026-08-29 on both a laptop and the
    # production server.
    login_path: str = ""

    # How the login form's submit button is clicked.
    #   "click" -- an ordinary Playwright click (every site but winclash).
    #   "js"    -- call the element's own .click() from inside the page.
    # winclash needs "js" and force=True is NOT a substitute: a full-page
    # <div class="overlay overlay--active"> (the backdrop of its cashback
    # popup, z-index 201) sits over the header login bar, and a forced click
    # still dispatches at those coordinates, so the OVERLAY receives it and
    # the login silently never fires -- confirmed live 2026-08-29 by watching
    # the network: a forced click produced no request at all, while an
    # in-page .click() produced POST /api2/v2/login -> 200 immediately.
    login_click_mode: str = "click"

    # Whether an in-page fetch() of http_balance_path is a trustworthy proof
    # that the browser session is really logged in. This is deliberately
    # SEPARATE from supports_http_login, which means something stricter --
    # "a bare requests.Session can do this too". winclash's balance endpoint
    # answers perfectly from inside the page but is unreachable to `requests`
    # (its AWS WAF walls any client that hasn't run challenge.js), so it needs
    # exactly one of the two flags and not the other. login() accepts either,
    # so no existing site changes.
    verify_auth_in_page: bool = False

    # Lower-cased substrings that appear in this site's result/snackbar
    # elements but are NOT failures -- progress text the site shows while a
    # request is in flight. login() scrapes those same elements to surface a
    # real rejection ("Invalid Username or Password") promptly, so without
    # this a loader would be reported as the reason the login failed.
    # winclash renders "Please wait" the moment the login button is clicked,
    # which made every login fail with that as its message. Empty everywhere
    # else, so no existing site's judgement changes.
    benign_texts: list = field(default_factory=list)

    # Whether the LOGIN selectors alone are inspected. Split out from
    # supports_casino 2026-08-19: starexch's login is verified live while its
    # casino navigation is not (different markup), and the two used to be one
    # flag, so login() refused on a site whose login demonstrably works.
    # login() accepts either flag, so every existing site keeps working
    # unchanged with just supports_casino set.
    supports_login: bool = False

    # Whether this site walls ordinary page NAVIGATION behind an AWS WAF
    # interstitial (winclash: a plain GET of the homepage answers 202 with
    # x-amzn-waf-action: challenge, and /join-now can answer 405 + captcha).
    # When True, every flow that drives a browser here clears the wall before
    # looking for anything on the page -- otherwise login() spends its whole
    # timeout hunting a LOGIN button on an interstitial and reports "could not
    # find the LOGIN button", which is true but useless. False everywhere
    # else, where the check would be a pointless page evaluation.
    waf_on_navigation: bool = False

    # How a live-table game is launched.
    #   "lobby"            -- cricmatch/starexch: open the Live Casino lobby,
    #                         then click the game's tile.
    #   "direct_game_url"  -- winclash: navigate straight to the site's own
    #                         launch redirect, skipping the lobby entirely.
    # winclash uses the direct route for two reasons, both confirmed live
    # 2026-08-29. First, its lobby tile is a <p class="playBtn casinoLink">
    # that only becomes visible on hover and is paginated behind a search box,
    # so clicking it is far more fragile than a URL. Second -- and this is the
    # money one -- the tile's own click handler REFUSES to launch anything
    # when the account's balance is zero (it opens an "add money" popup
    # instead), while the redirect URL below launches the table regardless.
    casino_launch_mode: str = "lobby"

    # URL template used only when casino_launch_mode == "direct_game_url".
    # Formatted with {id} and {provider}; read straight out of winclash's own
    # .casinoLink click handler.
    casino_launch_path: str = "/casinoRedirect?q={id}&provider={provider}&type=casino"

    # Provider slug and per-table numeric ids for the direct route above, as
    # the site's own /casinoGamesList returns them. Only the tables this
    # engine actually drives are listed -- add one by reading its id out of
    # that endpoint, never by guessing.
    casino_provider: str = "evolution"
    casino_game_ids: dict = field(default_factory=dict)

    # Whether this site's register endpoint has been confirmed (live, via a
    # captured network trace + a raw curl replay -- see CLAUDE.md) to be a
    # plain JSON POST with no browser-only requirement (no JS challenge, no
    # WAF captcha), so --fast can skip Chromium entirely and hit it with
    # `requests`. False (e.g. spin24star, whose register POST is gated by an
    # AWS WAF CAPTCHA that only a real browser can solve) -> --fast falls
    # back to the normal Playwright flow for that site.
    supports_http_fast: bool = False

    # Path (relative to the site's origin) the register form's JS actually
    # POSTs to, used only when supports_http_fast is True.
    http_register_path: str = "/register"

    # Number of OTP digits the SMS code has, used only when supports_http_fast
    # is True (there's no DOM to count digit boxes in without a browser).
    http_otp_digits: int = 6

    # Whether this site's mobile-number field can be overwritten post-signup
    # by POSTing a new number to `free_number_path` on the now-authenticated
    # session -- confirmed live (manual request interception) that this
    # updates the account's registered mobile with NO further OTP entry
    # required, unlike the OTP-gated number used at signup time. Lets a
    # single real phone number be reused across many signups: right after an
    # account registers, its number is swapped to a random throwaway one,
    # freeing the real one for the next signup. False (e.g. spin24star, not
    # inspected) -> the free-number step is skipped for that site.
    supports_free_number: bool = False

    # Path (relative to the site's origin) that accepts the new phone number
    # for the free-number swap above. Same endpoint the signup OTP itself
    # goes through, but called on an authenticated session instead of an
    # anonymous one.
    free_number_path: str = "/send_otp"

    # Whether logging into an EXISTING account and reading its wallet balance
    # can be done with plain `requests` (no browser) -- confirmed live for
    # cricmatch by capturing a real Playwright login's network traffic, then
    # replaying it with a bare requests.Session and getting byte-identical
    # JSON. False -> balance checks fall back to the Playwright login() path.
    supports_http_login: bool = False

    # Path the login form's JS POSTs to, used only when supports_http_login.
    http_login_path: str = "/login"

    # Path that returns the logged-in account's wallet balance as JSON, used
    # only when supports_http_login.
    http_balance_path: str = "/api2/v2/getBalance"

    # Whether an EXISTING, logged-in account's password can be changed via a
    # direct POST to change_password_path (oldPassword/newPassword/_token) --
    # confirmed live via a captured real request/response (see CLAUDE.md).
    # False -> /changepassword refuses cleanly instead of guessing an
    # unconfirmed endpoint.
    supports_change_password: bool = False

    # Path (relative to the site's origin) that accepts oldPassword/
    # newPassword/_token and changes the logged-in account's password. Used
    # only when supports_change_password is True.
    change_password_path: str = "/changePassword"

    # Whether the change_password_path POST above works on a bare
    # requests.Session (no browser at all), i.e. the endpoint needs nothing
    # the login POST didn't already give the session. This is NOT implied by
    # supports_change_password: cricmatch's own change-password call is
    # fired as an in-page fetch() precisely because an out-of-band client
    # misbehaves there (same reason http_free_phone_number() is believed
    # broken -- a requests.Session never receives the login-only cookies the
    # in-page path relies on). Confirmed live per site before flipping this
    # on. True -> password_changer.py takes the ~2s HTTP route instead of a
    # ~30s Playwright login, which also keeps it off the /login volume block.
    supports_http_change_password: bool = False

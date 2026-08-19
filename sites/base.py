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

    # Whether the LOGIN selectors alone are inspected. Split out from
    # supports_casino 2026-08-19: starexch's login is verified live while its
    # casino navigation is not (different markup), and the two used to be one
    # flag, so login() refused on a site whose login demonstrably works.
    # login() accepts either flag, so every existing site keeps working
    # unchanged with just supports_casino set.
    supports_login: bool = False

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

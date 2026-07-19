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

    # Whether the login + live-casino selectors below are present/inspected for
    # this site. False -> casino/hedge commands refuse cleanly instead of
    # mis-clicking uninspected markup.
    supports_casino: bool = False

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

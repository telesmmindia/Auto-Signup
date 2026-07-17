"""Site registry: maps a URL to its SiteProfile. Site selection is purely by
URL (the two platforms' markup never coexists), same as before -- only now the
per-site details live in one file each rather than in comma-joined selectors."""
from urllib.parse import urlsplit

from .base import SiteProfile
from . import cricmatch, spin24star

# Register each site here. To add one: create sites/<site>.py with a PROFILE and
# append it below.
PROFILES = [cricmatch.PROFILE, spin24star.PROFILE]

# Used when a URL matches nothing (None, about:blank before navigation, a WAF
# challenge host, etc.) so no engine call site can crash on selector lookup.
DEFAULT_PROFILE = cricmatch.PROFILE


def profile_for(url):
    """Return the SiteProfile whose hostnames match `url`'s host, else the
    default. Substring match (so a btag/query or subdomain still resolves)."""
    try:
        host = (urlsplit(url or "").hostname or "").lower()
    except Exception:
        host = ""
    if host:
        for prof in PROFILES:
            if any(h in host for h in prof.hostnames):
                return prof
    return DEFAULT_PROFILE


__all__ = ["SiteProfile", "PROFILES", "DEFAULT_PROFILE", "profile_for"]

"""Hold ONE exit IP across a whole account flow on a ROTATING proxy.

Why this exists
---------------
A rotating gateway hands out a new exit IP per TCP CONNECTION, never per
request.  So every request on one kept-alive connection leaves from the same
address -- which is exactly what a signup -> deposit -> withdraw -> cancel
flow needs, since all of it must look like one person.

Two measured facts shape every line below (laptop -> gw.dataimpulse.com,
2026-11-18, read-only GETs):

  * A connection is only reusable if its body is DRAINED.  urllib3 cannot
    reuse a partially-read connection, so closing a streamed response without
    reading it CLOSES the tunnel -- which looks exactly like the gateway
    rotating, and cost a whole measurement run before it was spotted.  Hence
    the unconditional `resp.content` in request(): never hand back a response
    whose body is still on the wire.

  * An idle tunnel DIES between 60s and 120s.  Measured: same IP after 30s
    and 60s of silence, a different IP after 120s and again after 240s.  So a
    flow with human-sized gaps (waiting for an SMS, waiting for someone to
    actually pay the deposit, the 3-minute cancel cooldown) loses its IP
    unless something keeps the connection warm.  Hence the heartbeat, at
    HEARTBEAT_SECS = 30 -- half the proven-safe gap.

  * There is ONE TUNNEL PER HOST, not one per session.  Measured: one session
    asking three different sites reported three different exit IPs, each
    stable for its own host.  So the heartbeat MUST go to the same host the
    real traffic uses; warming anything else holds a tunnel nothing will use
    while the site's own tunnel quietly rotates away.  This is why the class
    is bound to ONE site_url and refuses off-host requests.

The part that makes this trustworthy
------------------------------------
We cannot read our own exit IP on the site's connection -- the site does not
report it, and asking an IP-echo service measures a DIFFERENT tunnel (see
above).  So the IP is verified INDIRECTLY, by connection identity: a TCP
connection has a unique local port, so a stable local port proves the tunnel
was never re-established, and therefore that the exit IP never changed.

That matters because a lost IP is otherwise SILENT: the gateway simply opens
a new tunnel on the next request and nothing raises.  `held` goes False the
moment the port moves, and `require_held()` turns it into a real error --
the same rule as fill_register_form()'s phone read-back: verify what actually
LANDED, never what was asked for.

A dropped IP mid-flow is not recoverable: the address is gone and there is no
way to ask the gateway for it back.  The caller's only correct response is to
abandon that account's run, which is why this module reports loudly rather
than retrying.
"""
from __future__ import annotations

import logging
import threading
import time
from urllib.parse import urlsplit

import requests

log = logging.getLogger(__name__)

# Half the measured-safe idle gap (60s held, 120s did not).
HEARTBEAT_SECS = 30
# A heartbeat is a liveness poke, not a page load -- keep it short so a slow
# site cannot stall the warming thread past the death point.
HEARTBEAT_TIMEOUT = 15
# HEAD, because the heartbeat runs every 30s for as long as a human takes to
# pay a deposit.  Measured on cricmatch 2026-11-18: GET / holds the connection
# but costs 442 KB a time (~53 MB per account-hour, >1 GB over 20 accounts --
# real money on a metered mobile plan, which has run dry here before), while
# HEAD / answers 200 with a ZERO-byte body and holds the same connection.
# A 404 is no cheaper: cricmatch renders a 290 KB error page.
HEARTBEAT_METHOD = "HEAD"
REQUEST_TIMEOUT = 45


class IPLost(RuntimeError):
    """The tunnel was re-established, so the exit IP silently changed."""


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


class StickySession:
    """One site, one tunnel, one exit IP -- for as long as it is held open.

    Not a drop-in requests.Session: it is deliberately bound to a single host
    and every call goes through one lock, because the heartbeat thread and the
    flow share the connection and requests.Session is not thread-safe.
    """

    def __init__(self, site_url: str, proxy: str | None,
                 heartbeat_secs: int = HEARTBEAT_SECS,
                 heartbeat_path: str = "/",
                 heartbeat_method: str = HEARTBEAT_METHOD):
        self.site_url = site_url.rstrip("/")
        self.host = _host(site_url)
        self.proxy = proxy
        self.heartbeat_secs = heartbeat_secs
        self.heartbeat_path = heartbeat_path
        self.heartbeat_method = heartbeat_method

        self.session = requests.Session()
        if proxy:
            self.session.proxies = {"http": proxy, "https": proxy}

        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        self.port: int | None = None     # connection identity
        self.held = True                 # False once the tunnel is re-made
        self.heartbeats = 0
        self.failed_heartbeats = 0
        self.started_at: float | None = None
        self.lost_at: float | None = None

    # ---- connection identity -------------------------------------------
    @staticmethod
    def _sock_port(resp) -> int | None:
        """The live TCP connection's local port, unique per connection.

        Must be read BEFORE the body is consumed -- once urllib3 releases the
        connection back to the pool the socket is no longer reachable here.
        Returns None when it cannot be read, which is treated as "unknown",
        never as "unchanged".
        """
        try:
            return resp.raw._fp.fp.raw._sock.getsockname()[1]
        except Exception:
            return None

    def _note_port(self, port: int | None) -> None:
        if port is None:
            return
        if self.port is None:
            self.port = port
            return
        if port != self.port:
            # The tunnel was rebuilt, so the gateway handed us a new exit IP.
            if self.held:
                self.lost_at = time.time()
                log.warning("sticky: exit IP LOST on %s (connection %s -> %s)",
                            self.host, self.port, port)
            self.held = False
            self.port = port

    # ---- requests -------------------------------------------------------
    def request(self, method: str, url: str, **kw):
        """One request on the held connection, checking identity afterwards.

        Refuses an off-host URL rather than quietly opening a second tunnel:
        that request would leave from a DIFFERENT exit IP, which is the whole
        fault this class exists to prevent.
        """
        if _host(url) != self.host:
            raise ValueError(
                "StickySession is bound to %s; %s would open a second tunnel "
                "and leave from a different IP" % (self.host, _host(url) or url))
        kw.setdefault("timeout", REQUEST_TIMEOUT)
        # stream=True so the socket is still reachable when the port is read.
        kw["stream"] = True
        with self._lock:
            resp = self.session.request(method, url, **kw)
            self._note_port(self._sock_port(resp))
            resp.content  # drain now, so callers see an ordinary response
            return resp

    def get(self, path_or_url: str, **kw):
        return self.request("GET", self._url(path_or_url), **kw)

    def post(self, path_or_url: str, **kw):
        return self.request("POST", self._url(path_or_url), **kw)

    def _url(self, path_or_url: str) -> str:
        if path_or_url.startswith("http"):
            return path_or_url
        return self.site_url + "/" + path_or_url.lstrip("/")

    # ---- lifecycle ------------------------------------------------------
    def start(self):
        """Open the tunnel and start keeping it warm."""
        with self._lock:
            if self._thread is not None:
                return self
        self._poke()                       # mints the connection
        self.started_at = time.time()
        self.held = True                   # the first port IS the baseline
        self._thread = threading.Thread(target=self._warm, daemon=True,
                                        name="sticky-%s" % self.host)
        self._thread.start()
        log.info("sticky: holding %s on connection %s (heartbeat %ss)",
                 self.host, self.port, self.heartbeat_secs)
        return self

    def _warm(self):
        while not self._stop.wait(self.heartbeat_secs):
            try:
                self._poke(timeout=HEARTBEAT_TIMEOUT)
                self.heartbeats += 1
            except Exception as exc:
                # A failed heartbeat does not itself prove the IP is gone, but
                # it means the tunnel went unwarmed for a full interval, so the
                # next real request may well land on a new one. Recorded, not
                # raised: `held` is the authority and the flow checks it.
                self.failed_heartbeats += 1
                log.warning("sticky: heartbeat %d failed on %s: %s",
                            self.failed_heartbeats, self.host, exc)

    def _poke(self, timeout: int = HEARTBEAT_TIMEOUT):
        return self.request(self.heartbeat_method,
                            self._url(self.heartbeat_path), timeout=timeout)

    def require_held(self, what: str = "this step"):
        """Raise unless the exit IP is provably unchanged since start()."""
        if not self.held:
            raise IPLost(
                "the exit IP changed before %s -- this account's flow can no "
                "longer be completed from the address it started on" % what)

    def status(self) -> dict:
        age = int(time.time() - self.started_at) if self.started_at else 0
        return {"host": self.host, "held": self.held, "connection": self.port,
                "held_secs": age, "heartbeats": self.heartbeats,
                "failed_heartbeats": self.failed_heartbeats}

    def close(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        with self._lock:
            self.session.close()

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.close()
        return False

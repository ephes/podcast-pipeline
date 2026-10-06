"""Request guards for the local (127.0.0.1) web UIs.

The dashboard and the pick UI listen on loopback, but any web page the user
has open can still send requests to them, and DNS rebinding can make a
foreign hostname resolve to 127.0.0.1. These checks keep both UIs usable only
from their own pages:

* the ``Host`` header must name a loopback host (and the server's own port
  when it is known), which defeats DNS rebinding;
* state-changing requests (POST/PUT/PATCH/DELETE) must be same-origin: an
  ``Origin`` header must match ``http://<Host>``, and a ``Sec-Fetch-Site``
  header, when present, must be ``same-origin`` or ``none``;
* state-changing requests must declare ``Content-Type: application/json``.
  Browsers cannot send that cross-site without a CORS preflight, which these
  servers never approve, so ``text/plain`` or form posts from other sites are
  rejected even by browsers that omit ``Origin``.
"""

from __future__ import annotations

from dataclasses import dataclass

UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_LOOPBACK_HOSTNAMES = frozenset({"127.0.0.1", "localhost", "[::1]"})
_SAME_ORIGIN_FETCH_SITES = frozenset({"same-origin", "none"})


@dataclass(frozen=True)
class GuardRejection:
    status: int
    message: str


def _split_host_header(host: str) -> tuple[str, int | None] | None:
    host = host.strip().lower()
    if not host:
        return None
    if host.startswith("["):
        end = host.find("]")
        if end == -1:
            return None
        hostname, rest = host[: end + 1], host[end + 1 :]
    else:
        hostname, sep, port_text = host.partition(":")
        rest = f":{port_text}" if sep else ""
    if not rest:
        return hostname, None
    if not rest.startswith(":") or not rest[1:].isdigit():
        return None
    return hostname, int(rest[1:])


def check_request(
    *,
    method: str,
    host: str | None,
    origin: str | None,
    sec_fetch_site: str | None,
    content_type: str | None,
    expected_port: int | None,
) -> GuardRejection | None:
    """Return a rejection for a request that fails the local-UI guards, else ``None``."""
    if host is None:
        return GuardRejection(403, "Missing Host header")
    parsed = _split_host_header(host)
    if parsed is None or parsed[0] not in _LOOPBACK_HOSTNAMES:
        return GuardRejection(403, "Forbidden host")
    if expected_port is not None and parsed[1] != expected_port:
        return GuardRejection(403, "Forbidden host")

    if method.upper() not in UNSAFE_METHODS:
        return None

    if origin is not None and origin.strip().lower() != f"http://{host.strip().lower()}":
        return GuardRejection(403, "Cross-origin request rejected")
    if sec_fetch_site is not None and sec_fetch_site.strip().lower() not in _SAME_ORIGIN_FETCH_SITES:
        return GuardRejection(403, "Cross-site request rejected")

    media_type = (content_type or "").split(";", 1)[0].strip().lower()
    if media_type != "application/json":
        return GuardRejection(415, "Content-Type must be application/json")
    return None

"""Download one image from a public https URL the user gave (``gif_open`` with a link).

Guard rails, since the URL comes from a chat:
- https only, default port, no credentials in the URL;
- every hop's host must resolve only to public addresses (no localhost, LAN, link-local, metadata);
- redirects are followed by hand (at most 4) and each hop is checked again;
- the body is streamed with a hard size cap and must decode as an image (checked by the caller);
- a GIPHY/Tenor-style page is accepted by reading its ``og:image`` link once.

No cookies, no credentials and no Hermes sign-in are ever attached.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import urljoin, urlsplit

MAX_BYTES = 40 * 1024 * 1024
MAX_HOPS = 4
TIMEOUT_S = 25.0
USER_AGENT = "gif-tool/1.0 (Hermes Agent plugin)"

MAX_SCAN_BYTES = 256 * 1024   # an og:image link sits in <head>; never scan a whole page
MAX_TAG_BYTES = 1024          # one <meta ...> tag; longer ones are skipped
MAX_META_TAGS = 2000

# These run on one tag at a time (at most MAX_TAG_BYTES), never on the page, so a hostile page
# cannot make matching slow.
_OG_KEY = re.compile(rb"""(?:property|name)\s*=\s*["']og:image(?::url)?["']""", re.I)
_CONTENT = re.compile(rb"""content\s*=\s*["']([^"'<>\s]{1,2048})["']""", re.I)


def og_image(page: bytes) -> str | None:
    """The ``og:image`` link in an HTML page, or ``None``. Linear time on any input."""
    page = page[:MAX_SCAN_BYTES]
    lower = page.lower()
    at = 0
    for _ in range(MAX_META_TAGS):
        at = lower.find(b"<meta", at)
        if at < 0:
            return None
        end = lower.find(b">", at, at + MAX_TAG_BYTES)
        if end < 0:           # unterminated or oversized tag: step past it
            at += 5
            continue
        tag = page[at:end]
        at = end + 1
        if _OG_KEY.search(tag):
            found = _CONTENT.search(tag)
            if found:
                return found.group(1).decode("ascii", "replace").replace("&amp;", "&")
    return None


class FetchError(Exception):
    """A failure the user can act on; ``str()`` is the message to show."""


def check_url(url: str) -> str:
    """Return the host of a URL that is safe to request, or raise :class:`FetchError`."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise FetchError("that is not a valid URL") from None
    if parts.scheme != "https":
        raise FetchError("only https:// links are supported")
    if parts.username or parts.password:
        raise FetchError("links with a username or password are not supported")
    if port not in (None, 443):
        raise FetchError("only the default https port is supported")
    host = parts.hostname or ""
    if not host:
        raise FetchError("that is not a valid URL")
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError:
        raise FetchError(f"could not find the site {host}") from None
    for info in infos:
        address = ipaddress.ip_address(info[4][0].split("%")[0])
        if not address.is_global:
            raise FetchError("that link points to a private or local address, which GIF Tool will not open")
    return host


def _check_peer(resp) -> None:
    """Refuse a connection that landed on a non-public address.

    ``check_url`` resolves the host before the request and httpx resolves it again to connect, so
    the answer can change in between. This checks the address actually connected to, before any
    of the body is read.
    """
    try:
        stream = resp.extensions.get("network_stream")
        peer = stream.get_extra_info("server_addr") if stream is not None else None
        address = ipaddress.ip_address(str(peer[0]).split("%")[0]) if peer else None
    except (ValueError, TypeError, IndexError, AttributeError):
        return
    if address is not None and not address.is_global:
        raise FetchError("that link points to a private or local address, which GIF Tool will not open")


def _get(url: str, limit: int) -> tuple[bytes, str, str]:
    """GET with manual, re-checked redirects. Returns ``(body, content_type, final_url)``."""
    import httpx

    for _ in range(MAX_HOPS + 1):
        host = check_url(url)
        try:
            with httpx.Client(follow_redirects=False, timeout=TIMEOUT_S, trust_env=False) as client:
                with client.stream("GET", url, headers={"User-Agent": USER_AGENT,
                                                        "Accept": "image/*,text/html;q=0.5"}) as resp:
                    _check_peer(resp)
                    if resp.status_code in (301, 302, 303, 307, 308):
                        location = resp.headers.get("location")
                        if not location:
                            raise FetchError(f"{host} sent a redirect without a destination")
                        url = urljoin(url, location)
                        continue
                    if resp.status_code != 200:
                        raise FetchError(f"{host} answered HTTP {resp.status_code}")
                    declared = resp.headers.get("content-length", "")
                    if declared.isdigit() and int(declared) > limit:
                        raise FetchError(f"the file is over the {limit // (1024 * 1024)} MB limit")
                    chunks, total = [], 0
                    for chunk in resp.iter_bytes():
                        total += len(chunk)
                        if total > limit:
                            raise FetchError(f"the file is over the {limit // (1024 * 1024)} MB limit")
                        chunks.append(chunk)
                    return b"".join(chunks), resp.headers.get("content-type", "").lower(), url
        except FetchError:
            raise
        except httpx.TimeoutException:
            raise FetchError(f"{host} did not answer within {TIMEOUT_S:g} seconds") from None
        except httpx.HTTPError as exc:
            raise FetchError(f"could not download from {host} ({type(exc).__name__})") from None
    raise FetchError("too many redirects")


def fetch_image(url: str) -> tuple[bytes, str]:
    """Download the image at ``url``. Returns ``(bytes, file_name)``. Raises :class:`FetchError`."""
    url = url.strip()
    body, ctype, final = _get(url, MAX_BYTES)
    if "text/html" in ctype:
        link = og_image(body)
        if not link:
            raise FetchError("that link is a web page, not an image. Use a direct link to the GIF file.")
        target = urljoin(final, link)
        body, ctype, final = _get(target, MAX_BYTES)
        if "text/html" in ctype:
            raise FetchError("that link is a web page, not an image. Use a direct link to the GIF file.")
    name = urlsplit(final).path.rsplit("/", 1)[-1] or "image"
    return body, re.sub(r"[^A-Za-z0-9._-]+", "_", name)[:120]

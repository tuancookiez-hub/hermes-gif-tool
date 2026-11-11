"""Offline tests for the link fetcher's page parsing (no network)."""

import importlib.util
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("gif_tool_fetch", ROOT / "fetch.py")
fetch = importlib.util.module_from_spec(spec)
sys.modules["gif_tool_fetch"] = fetch
spec.loader.exec_module(fetch)


def test_og_image_property_first():
    page = b'<html><head><meta property="og:image" content="https://a.example/x.gif"></head></html>'
    assert fetch.og_image(page) == "https://a.example/x.gif"


def test_og_image_content_first_and_entities():
    page = b"<head><META content='https://a.example/x.gif?a=1&amp;b=2' name='og:image:url' /></head>"
    assert fetch.og_image(page) == "https://a.example/x.gif?a=1&b=2"


def test_og_image_skips_other_meta_tags():
    page = b'<meta charset="utf-8"><meta name="description" content="hi"><meta property="og:image" content="/y.webp">'
    assert fetch.og_image(page) == "/y.webp"


def test_og_image_none():
    assert fetch.og_image(b"<html><body>no tags</body></html>") is None
    assert fetch.og_image(b"") is None
    assert fetch.og_image(b'<meta property="og:title" content="x">') is None


def test_og_image_ignored_past_scan_limit():
    page = b" " * fetch.MAX_SCAN_BYTES + b'<meta property="og:image" content="https://a.example/x.gif">'
    assert fetch.og_image(page) is None


@pytest.mark.parametrize("unit", [b"<meta ", b"<meta property='og:image' ", b"<meta content='x' ", b"<meta" + b"a" * 2000])
def test_hostile_page_is_fast(unit):
    page = unit * (2 * 1024 * 1024 // len(unit) + 1)
    start = time.perf_counter()
    assert fetch.og_image(page) is None
    assert time.perf_counter() - start < 1.0


def test_peer_check_refuses_private_address():
    class Stream:
        def __init__(self, addr):
            self.addr = addr

        def get_extra_info(self, name):
            return self.addr if name == "server_addr" else None

    class Resp:
        def __init__(self, addr):
            self.extensions = {"network_stream": Stream(addr)}

    fetch._check_peer(Resp(("93.184.216.34", 443)))
    for bad in (("127.0.0.1", 443), ("10.0.0.5", 443), ("169.254.169.254", 443), ("::1", 443, 0, 0)):
        with pytest.raises(fetch.FetchError):
            fetch._check_peer(Resp(bad))

    class Bare:
        extensions = {}

    fetch._check_peer(Bare())

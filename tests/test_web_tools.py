"""web_search and fetch_page with faked HTTP (no network)."""

import httpx
import pytest

from jarvis import keystore, web_tools


class FakeResponse:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


@pytest.fixture
def no_keys(monkeypatch):
    monkeypatch.delenv("SERPER_API_KEY", raising=False)
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)


def test_search_prefers_serper_then_brave_then_ddgs(monkeypatch, no_keys):
    calls = []
    monkeypatch.setattr(web_tools, "ENGINES", (
        ("serper", "SERPER_API_KEY", lambda q, n, key: calls.append(("serper", key)) or [{"title": "S", "url": "u", "snippet": "s"}]),
        ("brave", "BRAVE_API_KEY", lambda q, n, key: calls.append(("brave", key)) or [{"title": "B", "url": "u", "snippet": "s"}]),
        ("ddgs", "", lambda q, n, key: calls.append(("ddgs", key)) or [{"title": "D", "url": "u", "snippet": "s"}]),
    ))
    assert web_tools.web_search("cats")["engine"] == "ddgs"  # no keys at all
    keystore.set("BRAVE_API_KEY", "brave-secret")
    assert web_tools.web_search("cats")["engine"] == "brave"
    monkeypatch.setenv("SERPER_API_KEY", "serper-secret")
    out = web_tools.web_search("cats", num=3)
    assert out["engine"] == "serper" and out["results"][0] == {"title": "S", "url": "u", "snippet": "s"}
    assert ("serper", "serper-secret") in calls


def test_search_falls_back_when_an_engine_fails(monkeypatch, no_keys):
    def boom(q, n, key):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(web_tools, "ENGINES", (
        ("serper", "SERPER_API_KEY", boom),
        ("ddgs", "", lambda q, n, key: [{"title": "D", "url": "u", "snippet": ""}]),
    ))
    monkeypatch.setenv("SERPER_API_KEY", "SECRETKEY")
    assert web_tools.web_search("x")["engine"] == "ddgs"
    monkeypatch.setattr(web_tools, "ENGINES", (("serper", "SERPER_API_KEY", boom),))
    out = web_tools.web_search("x")
    assert "serper" in out["error"] and "SECRETKEY" not in out["error"]
    assert "error" in web_tools.web_search("  ")


def test_serper_and_brave_parse_responses(monkeypatch):
    seen = {}

    def post(url, json=None, headers=None, timeout=None):
        seen["serper"] = (url, json, headers)
        return FakeResponse({"organic": [{"title": "T", "link": "https://a", "snippet": "S"}]})

    def get(url, params=None, headers=None, timeout=None):
        seen["brave"] = (url, params, headers)
        return FakeResponse({"web": {"results": [{"title": "T2", "url": "https://b", "description": "D"}]}})

    monkeypatch.setattr(web_tools.httpx, "post", post)
    monkeypatch.setattr(web_tools.httpx, "get", get)
    assert web_tools._serper("q", 5, "K1") == [{"title": "T", "url": "https://a", "snippet": "S"}]
    assert seen["serper"][1] == {"q": "q", "num": 5} and seen["serper"][2]["X-API-KEY"] == "K1"
    assert web_tools._brave("q", 5, "K2") == [{"title": "T2", "url": "https://b", "snippet": "D"}]
    assert seen["brave"][2]["X-Subscription-Token"] == "K2"


PAGE = """<html><head><title>Joe's Bakery</title><style>.x{}</style></head><body>
<script>var secret = 'nope';</script>
<h1>Joe's Bakery</h1><p>Best bread in town. Call us: +1 (415) 555-0134 or 020 7946 0958.</p>
<p>Order id 1234567890123 on 2024-01-15 shipped.</p>
<a href="mailto:hello@joes.example?subject=Hi">mail</a> also info@joes.example
<a href="tel:+14155550134">call</a>
<a href="https://www.instagram.com/joesbakery/">ig</a>
<a href="/linkedin-redirect">x</a><a href="https://linkedin.com/company/joes-bakery">li</a>
<a href="https://twitter.com/intent/tweet?text=hi">share</a><a href="https://x.com/joesbakery">x</a>
<a href="https://facebook.com/sharer.php?u=1">fbshare</a><a href="https://youtube.com/@joes">yt</a>
<a href="https://www.tiktok.com/@joes">tt</a>
</body></html>"""


REAL_CLIENT = httpx.Client


def _client(monkeypatch, handler):
    monkeypatch.setattr(web_tools.httpx, "Client",
                        lambda **kw: REAL_CLIENT(transport=httpx.MockTransport(handler), **kw))


def test_fetch_page_extracts_text_and_contacts(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, text=PAGE, headers={"content-type": "text/html; charset=utf-8"})

    _client(monkeypatch, handler)
    out = web_tools.fetch_page("joes.example/home", max_chars=500)
    assert requests[0].url == "https://joes.example/home" and "Jarvis" in requests[0].headers["user-agent"]
    assert out["title"] == "Joe's Bakery" and "Best bread" in out["text"] and "secret" not in out["text"]
    assert out["emails"] == ["hello@joes.example", "info@joes.example"]
    assert "+14155550134" in out["phones"] and "+1 (415) 555-0134" in out["phones"]
    assert not any("1234567890123" in p or "2024" in p for p in out["phones"])
    platforms = {link["platform"]: link["url"] for link in out["social_links"]}
    assert set(platforms) == {"instagram", "linkedin", "x", "youtube", "tiktok"}
    assert platforms["x"] == "https://x.com/joesbakery"  # share intents and sharer links are skipped


def test_fetch_page_truncates_and_reports_errors(monkeypatch):
    big = "<html><title>t</title><body>" + "word " * 5000 + "</body></html>"
    _client(monkeypatch, lambda r: httpx.Response(200, text=big, headers={"content-type": "text/html"}))
    out = web_tools.fetch_page("https://a.example", max_chars=1000)
    assert len(out["text"]) <= 1000 and out["truncated"]
    _client(monkeypatch, lambda r: httpx.Response(404, text="no"))
    assert "404" in web_tools.fetch_page("https://a.example")["error"]
    _client(monkeypatch, lambda r: httpx.Response(200, content=b"%PDF", headers={"content-type": "application/pdf"}))
    assert "Unsupported" in web_tools.fetch_page("https://a.example/x.pdf")["error"]
    assert "http" in web_tools.fetch_page("ftp://a.example")["error"]

    def refuse(request):
        raise httpx.ConnectError("refused")

    _client(monkeypatch, refuse)
    assert "Could not fetch" in web_tools.fetch_page("https://a.example")["error"]


def test_fetch_page_caps_download_size(monkeypatch):
    _client(monkeypatch, lambda r: httpx.Response(200, content=b"<html><body>" + b"a" * 3_000_000,
                                                  headers={"content-type": "text/html"}))
    out = web_tools.fetch_page("https://a.example", max_chars=50000)
    assert len(out["text"]) <= web_tools.MAX_BYTES


def test_phone_finder_is_conservative():
    text = "Call +44 20 7946 0958, (212) 555-0147, 555.123.4567 or 1234567890123. Born 1999-12-31. Version 1.2.3."
    found = web_tools.find_phones(text)
    assert "+44 20 7946 0958" in found and "(212) 555-0147" in found
    assert not any("1999" in p or "1234567890123" in p for p in found)

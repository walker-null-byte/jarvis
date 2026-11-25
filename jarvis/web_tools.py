"""web_search and fetch_page: one obvious pair of web tools.

web_search tries Serper, then Brave (keys from the keystore or environment), then
the key-free ddgs library. fetch_page downloads one page and returns readable text
plus contact details (emails, phone numbers, social profile links).
"""

import os
import re
from urllib.parse import urljoin, urlparse

import httpx

from . import keystore

USER_AGENT = "Mozilla/5.0 (compatible; Jarvis/1.1; personal assistant)"
FETCH_TIMEOUT = 15
MAX_BYTES = 2_000_000
SCAN_CHARS = 300_000
SEARCH_TIMEOUT = 15

SOCIAL_HOSTS = {
    "instagram": ("instagram.com",),
    "linkedin": ("linkedin.com",),
    "x": ("x.com", "twitter.com"),
    "facebook": ("facebook.com", "fb.com"),
    "youtube": ("youtube.com", "youtu.be"),
    "tiktok": ("tiktok.com",),
}
# Share buttons and tracking pixels are not profiles.
_SHARE_HINT = re.compile(r"sharer|/share|intent/|/dialog/|plugins/|/tr\?|login|signup|/hashtag/", re.I)
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63})*\.[A-Za-z]{2,}")
_PHONE = re.compile(r"(?<![\w/])\+?\(?\d[\d\s().-]{6,}\d(?![\w/])")
_DATE = re.compile(r"^\d{4}[-./]\d{1,2}[-./]\d{1,2}$|^\d{1,2}[-./]\d{1,2}[-./]\d{2,4}$")
_IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".css", ".js")


def _key(name):
    return keystore.get(name) or os.environ.get(name) or ""


# ---------- web_search ----------

def _serper(query, num, key):
    r = httpx.post("https://google.serper.dev/search", json={"q": query, "num": num},
                   headers={"X-API-KEY": key, "Content-Type": "application/json"}, timeout=SEARCH_TIMEOUT)
    r.raise_for_status()
    return [{"title": x.get("title", ""), "url": x.get("link", ""), "snippet": x.get("snippet", "")}
            for x in r.json().get("organic", [])]


def _brave(query, num, key):
    r = httpx.get("https://api.search.brave.com/res/v1/web/search",
                  params={"q": query, "count": min(num, 20)},
                  headers={"X-Subscription-Token": key, "Accept": "application/json"}, timeout=SEARCH_TIMEOUT)
    r.raise_for_status()
    return [{"title": x.get("title", ""), "url": x.get("url", ""), "snippet": x.get("description", "")}
            for x in r.json().get("web", {}).get("results", [])]


DDGS_BACKENDS = ("auto", "google", "bing", "yandex")  # auto often returns nothing for site: queries


def _ddgs(query, num, _key_unused=None):
    """Key-free search: try the ddgs backends in turn until one returns results."""
    from ddgs import DDGS
    errors = []
    for backend in DDGS_BACKENDS:
        try:
            found = DDGS(timeout=15).text(query, max_results=num, backend=backend)
        except Exception as e:
            errors.append(f"{backend}: {_short(e)}")
            continue
        if found:
            return [{"title": x.get("title", ""), "url": x.get("href", ""), "snippet": x.get("body", "")}
                    for x in found]
    if errors and len(errors) == len(DDGS_BACKENDS):
        raise RuntimeError("; ".join(errors))
    return []


ENGINES = (("serper", "SERPER_API_KEY", _serper), ("brave", "BRAVE_API_KEY", _brave), ("ddgs", "", _ddgs))


def web_search(query: str, num: int = 10):
    """Search the web. Returns a list of {title, url, snippet} and which engine answered.

    Args:
        query (str): What to search for (site: and quotes work on most engines)
        num (int): How many results to return
    """
    try:
        num = max(1, min(int(num), 20))
    except (TypeError, ValueError):
        num = 10
    if not str(query).strip():
        return {"error": "query is empty"}
    problems = []
    for engine, key_name, search in ENGINES:
        key = _key(key_name) if key_name else ""
        if key_name and not key:
            continue
        try:
            results = search(query, num, key)
        except Exception as e:  # network, quota, bad key, library missing: try the next engine
            problems.append(f"{engine}: {_short(e)}")
            continue
        if results:
            return {"engine": engine, "results": results[:num]}
        problems.append(f"{engine}: no results")
    return {"error": "Search failed. " + "; ".join(problems), "results": []}


def _short(error):
    return str(error).replace("\n", " ")[:160] or error.__class__.__name__


# ---------- fetch_page ----------

def fetch_page(url: str, max_chars: int = 6000):
    """Download a web page and return its readable text, title, emails, phone numbers and social links.

    Args:
        url (str): http(s) address of the page
        max_chars (int): Maximum characters of page text to return
    """
    parsed = urlparse(url if "://" in url else "https://" + url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return {"error": "Only http and https URLs can be fetched"}
    url = parsed.geturl()
    try:
        max_chars = max(200, min(int(max_chars), 50000))
    except (TypeError, ValueError):
        max_chars = 6000
    try:
        with httpx.Client(follow_redirects=True, timeout=FETCH_TIMEOUT,
                          headers={"User-Agent": USER_AGENT, "Accept": "text/html,text/plain;q=0.9,*/*;q=0.5"}) as client:
            with client.stream("GET", url) as response:
                kind = response.headers.get("content-type", "").split(";")[0].strip().lower()
                body = bytearray()
                for chunk in response.iter_bytes():
                    body += chunk
                    if len(body) > MAX_BYTES:
                        break
                final_url, status = str(response.url), response.status_code
                encoding = response.encoding or "utf-8"
    except httpx.HTTPError as e:
        return {"error": f"Could not fetch {url}: {_short(e)}"}
    if status >= 400:
        return {"error": f"{url} answered HTTP {status}", "status": status}
    if kind and not (kind.startswith("text/") or "html" in kind or "xml" in kind or "json" in kind):
        return {"error": f"Unsupported content type: {kind}", "status": status}
    text = bytes(body[:MAX_BYTES]).decode(encoding, errors="replace")
    if "html" in kind or not kind or text.lstrip()[:200].lower().startswith(("<!doctype", "<html")):
        return _describe_html(text, final_url, status, max_chars)
    return {"url": final_url, "status": status, "title": "", "text": text[:max_chars],
            "truncated": len(text) > max_chars, "emails": [], "phones": [], "social_links": []}


def _describe_html(html, url, status, max_chars):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    title = (soup.title.get_text(" ", strip=True) if soup.title else "")[:200]
    hrefs = [a["href"].strip() for a in soup.find_all("a", href=True)]
    emails = {h[7:].split("?")[0] for h in hrefs if h.lower().startswith("mailto:")}
    phones = {re.sub(r"[^\d+]", "", h[4:]) for h in hrefs if h.lower().startswith("tel:")}
    social = _social_links([urljoin(url, h) for h in hrefs])
    for tag in soup(["script", "style", "noscript", "svg", "template", "iframe"]):
        tag.decompose()
    text = re.sub(r"[ \t\r\f\v]+", " ", soup.get_text("\n"))
    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    scan = text[:SCAN_CHARS]  # bounded work on huge pages
    emails |= {e for e in _EMAIL.findall(scan) if not e.lower().endswith(_IMAGE_EXT)}
    phones |= set(find_phones(scan))
    return {"url": url, "status": status, "title": title, "text": text[:max_chars],
            "truncated": len(text) > max_chars, "emails": sorted(e.strip(".") for e in emails)[:20],
            "phones": sorted(p for p in phones if p)[:20], "social_links": social}


def find_phones(text):
    """Phone-number-looking strings: 9-15 digits, and written like a phone (+, brackets or separators)."""
    found = []
    for m in _PHONE.finditer(text):
        raw = m.group(0).strip(" .-")
        digits = re.sub(r"\D", "", raw)
        if not 9 <= len(digits) <= 15 or _DATE.match(raw):
            continue
        if not (raw.startswith("+") or re.search(r"[\s().-]", raw)):
            continue  # a bare run of digits is more likely an id
        found.append(re.sub(r"\s+", " ", raw))
    return list(dict.fromkeys(found))


def _social_links(urls):
    out, seen = [], set()
    for link in urls:
        parsed = urlparse(link)
        host = parsed.netloc.lower().removeprefix("www.").removeprefix("m.")
        for platform, hosts in SOCIAL_HOSTS.items():
            if any(host == h or host.endswith("." + h) for h in hosts):
                path = parsed.path.strip("/")
                if path and not _SHARE_HINT.search(link) and (platform, link.rstrip("/")) not in seen:
                    seen.add((platform, link.rstrip("/")))
                    out.append({"platform": platform, "url": link})
                break
    return out[:20]


web_search.read_only = True
fetch_page.read_only = True

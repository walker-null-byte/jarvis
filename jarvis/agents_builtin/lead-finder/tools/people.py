"""Lead Finder tools: profile discovery and public contact lookup.

search_profiles turns "indie game developer, India" into the right site: search and
keeps only real profile pages (not posts, reels or company pages), which small
models otherwise get wrong. find_contact searches for a person's own pages and
collects emails/phones *with the page they were seen on*; it never guesses.
"""

import re
from urllib.parse import urlsplit

INSTAGRAM_RESERVED = {"p", "reel", "reels", "explore", "stories", "tv", "accounts", "about", "legal",
                      "directory", "developer", "web", "challenge", "direct", "tags", "locations"}
SKIP_CONTACT_HOSTS = ("instagram.com", "linkedin.com", "facebook.com", "x.com", "twitter.com",
                      "tiktok.com", "youtube.com", "pinterest.", "google.")
JUNK_EMAIL = re.compile(r"(example\.|sentry|wixpress|@domain\.|noreply|no-reply|\.png$|\.jpg$|@2x)", re.I)
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")


def profile_url(url, platform):
    """The canonical profile URL if `url` is a person's profile page on `platform`, else ''."""
    parts = urlsplit(url.strip())
    host = parts.netloc.lower()
    path = [p for p in parts.path.split("/") if p]
    if platform == "instagram" and host.endswith("instagram.com") and len(path) == 1:
        handle = path[0]
        if re.fullmatch(r"[A-Za-z0-9._]{1,30}", handle) and handle.lower() not in INSTAGRAM_RESERVED:
            return f"https://www.instagram.com/{handle}/"
    if platform == "linkedin" and host.endswith("linkedin.com") and len(path) >= 2 and path[0] == "in":
        return f"https://www.linkedin.com/in/{path[1]}/"
    return ""


def parse_title(title, platform):
    """(name, handle_or_headline) from a search result title."""
    title = (title or "").strip()
    if platform == "instagram":
        match = re.match(r"^(.*?)\s*\(@([A-Za-z0-9._]+)\)", title)
        if match:
            return match.group(1).strip() or match.group(2), "@" + match.group(2)
        return re.split(r"\s+[•|·-]\s+", title)[0].strip(), ""
    title = re.split(r"\s*\|\s*LinkedIn", title)[0]  # search engines sometimes glue several titles together
    name, _, headline = title.partition(" - ")
    return name.strip(), headline.strip()


def instagram_snippet(snippet):
    """Instagram result snippets look like '666 Followers, … - Giri Prasad | Game Developer (@x) on Instagram: "bio"'.
    Returns (display name, bio, followers)."""
    followers = re.match(r"^\s*([\d.,]+[KkMm]?)\s+Followers", snippet or "")
    match = re.search(r"Posts\s+-\s+(.*?)\s*\(@[A-Za-z0-9._]+\)\s+on Instagram:\s*[\"“]?(.*)$", snippet or "", re.S)
    if not match:
        return "", (snippet or "").strip(), followers.group(1) if followers else ""
    bio = match.group(2).strip().rstrip('"”')
    return match.group(1).strip(), bio, followers.group(1) if followers else ""


def register(ctx):
    def search(query, num):
        """Serper or Brave with this agent's own keys, else Jarvis's web_search."""
        serper = ctx.settings.get("SERPER_API_KEY")
        if serper:
            response = ctx.http.post("https://google.serper.dev/search", json={"q": query, "num": num},
                                     headers={"X-API-KEY": serper})
            response.raise_for_status()
            return "serper", [{"title": r.get("title", ""), "url": r.get("link", ""), "snippet": r.get("snippet", "")}
                              for r in response.json().get("organic", [])]
        brave = ctx.settings.get("BRAVE_API_KEY")
        if brave:
            response = ctx.http.get("https://api.search.brave.com/res/v1/web/search",
                                    params={"q": query, "count": min(num, 20)},
                                    headers={"X-Subscription-Token": brave, "Accept": "application/json"})
            response.raise_for_status()
            return "brave", [{"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("description", "")}
                             for r in response.json().get("web", {}).get("results", [])]
        result = ctx.call("web_search", query=query, num=num)
        if isinstance(result, dict) and result.get("results"):
            return result.get("engine", "web"), result["results"]
        # The key-free engine often returns nothing for site: queries; Google/Bing/Yandex via ddgs usually do.
        problems = [result.get("error", "no results") if isinstance(result, dict) else "no results"]
        try:
            from ddgs import DDGS
        except ImportError:
            raise RuntimeError(problems[0]) from None
        for backend in ("google", "bing", "yandex"):
            try:
                found = DDGS(timeout=15).text(query, max_results=num, backend=backend)
            except Exception as error:
                problems.append(f"{backend}: {str(error)[:80]}")
                continue
            if found:
                return f"ddgs-{backend}", [{"title": r.get("title", ""), "url": r.get("href", ""),
                                             "snippet": r.get("body", "")} for r in found]
        raise RuntimeError("; ".join(problems))

    @ctx.tool
    def search_profiles(query: str, platform: str = "instagram", location: str = "", num: int = 10) -> dict:
        """Find public Instagram or LinkedIn profiles of people matching a description. Keeps only real profile pages, marks people already saved, and returns name, profile_url and the search snippet for each.

        Args:
            query: who to look for, in a few words, e.g. "indie game developer" or "game studio founder"
            platform: instagram or linkedin
            location: optional city, region or country
            num: how many search results to scan (1-20)
        """
        platform = str(platform).strip().lower()
        if platform not in ("instagram", "linkedin"):
            return {"error": "platform must be instagram or linkedin"}
        try:
            num = max(1, min(int(num), 20))
        except (TypeError, ValueError):
            num = 10
        site = "site:instagram.com" if platform == "instagram" else "site:linkedin.com/in"
        full = " ".join(p for p in (site, str(query).strip(), str(location).strip()) if p)
        try:
            engine, results = search(full, num)
        except Exception as error:
            if not str(location).strip():
                return {"error": f"search failed: {error}", "query": full,
                        "note": "Try again in a moment with other words, or use web_search."}
            full = " ".join(p for p in (site, str(query).strip()) if p)  # retry without the location
            try:
                engine, results = search(full, num)
            except Exception as error2:
                return {"error": f"search failed: {error2}", "query": full,
                        "note": "Try again in a moment with other words, or use web_search."}
        known = set()
        for lead in ctx.records("leads").all():
            if lead.get("profile_url"):
                known.add(lead["profile_url"].lower().replace("://www.", "://").rstrip("/"))
        profiles, saved, skipped = [], 0, 0
        for result in results:
            url = profile_url(result.get("url", ""), platform)
            if not url:
                skipped += 1
                continue
            if url.lower().replace("://www.", "://").rstrip("/") in known:
                saved += 1
                continue
            name, extra = parse_title(result.get("title", ""), platform)
            snippet = (result.get("snippet") or "").strip()
            if platform == "instagram":
                display, bio, followers = instagram_snippet(snippet)
                # "Giri Prasad | Game Developer": the person's name comes first
                name = display.split(" | ")[0].strip() if display else name
                entry = {"name": name, "handle": extra, "profile_url": url, "platform": platform,
                         "display_name": display, "bio": bio[:300], "followers": followers}
            else:
                entry = {"name": name, "headline": extra, "profile_url": url, "platform": platform,
                         "snippet": snippet[:300]}
                country = urlsplit(result.get("url", "")).netloc.lower().split(".")[0]
                if len(country) == 2:  # in.linkedin.com: the profile's country (in = India, uk, de, …)
                    entry["linkedin_country"] = country
            profiles.append(entry)
        ctx.log(f"search_profiles {full!r}: {len(profiles)} new, {saved} known, {skipped} not profiles ({engine})")
        ideal = ctx.settings.get("ideal_lead", "")
        minimum = ctx.settings.get("min_score", 3)
        note = (f"Now score each profile 1-5 against the ideal lead ({ideal}) from its name, bio and headline. "
                "If the ideal lead names a place, someone clearly elsewhere is a poor fit; linkedin_country 'in' means India. "
                "Each fit_reason must say something specific about that person. "
                f"Call records_add with every profile scoring {minimum} or more (name, platform, profile_url, "
                "headline = their bio or headline, location if stated, fit_score, fit_reason, found_via = this query). "
                "Skip brands, fan pages and people who clearly don't fit. Then search again with different words "
                "if you need more. Don't fetch instagram.com or linkedin.com pages (they block robots).")
        if not profiles:
            note = ("No new profiles for this query. Try again with other words (a job title, 'founder', 'studio', "
                    "the kind of work, without the location) or the other platform.")
        return {"query": full, "engine": engine, "profiles": profiles, "already_saved": saved,
                "not_profiles": skipped, "note": note}

    @ctx.tool
    def find_contact(name: str, hint: str = "", website: str = "") -> dict:
        """Look up a person's public business email, phone and website. Searches for their own pages, reads up to 3 of them, and returns every email/phone found together with the page it was on. Never guesses.

        Args:
            name: the person's name
            hint: what they do or their studio/company/city, to find the right person
            website: their own website if already known
        """
        pages, seen = [], set()
        if website:
            pages.append(website)
        query = f'"{name}" {hint} contact email'.strip()
        try:
            _, results = search(query, 8)
        except Exception as error:
            results = []
            ctx.log(f"find_contact search failed: {error}")
        snippets = []
        for result in results:
            url = result.get("url", "")
            host = urlsplit(url).netloc.lower()
            for email in EMAIL_RE.findall(result.get("snippet", "")):
                if not JUNK_EMAIL.search(email):
                    snippets.append({"email": email, "source": url, "seen_in": "search snippet"})
            if url and not any(h in host for h in SKIP_CONTACT_HOSTS) and host not in seen:
                seen.add(host)
                pages.append(url)
        found = []
        for url in pages[:3]:
            page = ctx.call("fetch_page", url=url, max_chars=1500)
            if not isinstance(page, dict) or page.get("error"):
                continue
            emails = [e for e in page.get("emails", []) if not JUNK_EMAIL.search(e)]
            found.append({"page": page.get("url", url), "title": page.get("title", ""),
                          "emails": emails[:5], "phones": page.get("phones", [])[:3],
                          "social_links": [s["url"] for s in page.get("social_links", [])][:6],
                          "mentions_name": name.split()[0].lower() in (page.get("text", "") + page.get("title", "")).lower()})
        return {"name": name, "pages": found, "emails_in_snippets": snippets[:5],
                "note": "Only use a contact that appears above, with its page as contact_source. "
                        "Set contact_verified true only if the page clearly belongs to this person or their business. "
                        "If nothing fits, leave email empty."}

import os
import json
import hashlib
import datetime as dt
from dateutil import tz
import requests
import feedparser

# ==========
# Inställningar (enkla att ändra senare)
# ==========
DAYS_BACK = 7
MAX_ITEMS = 30  # hur många länkar vi skickar till AI för syntes
LANG = "sv"

# Välj modell via GitHub Secret/Env (valfritt).
# Default är en billigare, snabb modell. Du kan byta till t.ex. "gpt-4o" senare.
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
BRAVE_API_KEY = os.environ.get("BRAVE_API_KEY")


def now_stockholm():
    return dt.datetime.now(tz=tz.gettz("Europe/Stockholm"))


def read_lines(path: str) -> list[str]:
    with open(path, "r", encoding="utf-8") as f:
        return [
            ln.strip()
            for ln in f.readlines()
            if ln.strip() and not ln.strip().startswith("#")
        ]


def fetch_rss_items(feed_urls: list[str]) -> list[dict]:
    cutoff = now_stockholm() - dt.timedelta(days=DAYS_BACK)
    items = []

    for url in feed_urls:
        d = feedparser.parse(url)
        feed_title = (d.feed.get("title") or "RSS").strip()

        for e in d.entries[:80]:
            title = (e.get("title") or "").strip()
            link = (e.get("link") or "").strip()
            summary = (e.get("summary") or e.get("description") or "").strip()

            # Datum (om det finns)
            published_dt = None
            if e.get("published_parsed"):
                published_dt = dt.datetime(*e.published_parsed[:6], tzinfo=dt.timezone.utc).astimezone(tz.gettz("Europe/Stockholm"))
            elif e.get("updated_parsed"):
                published_dt = dt.datetime(*e.updated_parsed[:6], tzinfo=dt.timezone.utc).astimezone(tz.gettz("Europe/Stockholm"))

            if not title or not link:
                continue
            if published_dt and published_dt < cutoff:
                continue

            items.append({
                "source": feed_title,
                "title": title,
                "url": link,
                "snippet": summary[:500],
                "published": published_dt.isoformat() if published_dt else ""
            })

    return items


def brave_search(query: str) -> list[dict]:
    """Valfritt: om BRAVE_API_KEY saknas hoppar vi över webbsök."""
    if not BRAVE_API_KEY:
        return []

    url = "https://api.search.brave.com/res/v1/web/search"
    headers = {
        "Accept": "application/json",
        "X-Subscription-Token": BRAVE_API_KEY
    }
    params = {
        "q": query,
        "count": 10,
        "freshness": "week"
    }

    r = requests.get(url, headers=headers, params=params, timeout=30)
    r.raise_for_status()
    data = r.json()

    out = []
    for it in data.get("web", {}).get("results", [])[:10]:
        title = (it.get("title") or "").strip()
        link = (it.get("url") or "").strip()
        desc = (it.get("description") or "").strip()
        if not title or not link:
            continue
        out.append({
            "source": "Brave Search",
            "title": title,
            "url": link,
            "snippet": desc[:500],
            "published": ""
        })
    return out


def stable_id(item: dict) -> str:
    """Skapar en stabil identifierare så vi kan ta bort dubletter."""
    u = (item.get("url") or "").split("#")[0].strip().lower()
    t = (item.get("title") or "").strip().lower()
    base = (u + "|" + t).encode("utf-8")
    return hashlib.sha256(base).hexdigest()


def dedupe(items: list[dict]) -> list[dict]:
    seen = set()
    out = []
    for it in items:
        sid = stable_id(it)
        if sid in seen:
            continue
        seen.add(sid)
        out.append(it)
    return out


def openai_summarize(items: list[dict]) -> str:
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY saknas. Lägg den som GitHub Secret senare.")

    system = f"""
Du är en redaktör som skriver ett veckobrev om AI och ledarskap på {LANG}.
Skriv kort, tydligt och användbart.

Struktur (i Markdown):
# Veckans omvärldsbevakning (AI & ledarskap)

## TL;DR
- (3 bullets)

## Viktigaste teman
- (3–5 bullets med 1–2 meningar vardera)

## Utvalda länkar
Lista 8–12 länkar.
Varje punkt: **Titel** – 1 mening varför det spelar roll. (URL)

Regler:
- Svenska.
- Undvik repetitioner/dubletter.
- Inga långa citat från källor (bara sammanfatta).
"""

    payload = {
        "week_ending": now_stockholm().date().isoformat(),
        "items": items[:MAX_ITEMS],
    }

    resp = requests.post(
        "https://api.openai.com/v1/responses",
        headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": OPENAI_MODEL,
            "input": [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            "text": {"format": "text"},
        },
        timeout=90,
    )
    resp.raise_for_status()
    data = resp.json()

    # Robust hämtning av text (Responses API kan variera lite i form)
    if isinstance(data, dict) and data.get("output_text"):
        return data["output_text"].strip()

    text = ""
    for item in data.get("output", []):
        if item.get("type") == "message":
            for c in item.get("content", []):
                if c.get("type") in ("output_text", "text"):
                    text += c.get("text", "")
    return text.strip()


def write_docs(markdown_text: str):
    os.makedirs("docs", exist_ok=True)
    with open("docs/index.md", "w", encoding="utf-8") as f:
        f.write(markdown_text + "\n")


def main():
    feeds = read_lines("feeds.txt")
    queries = read_lines("queries.txt")

    items = []
    items += fetch_rss_items(feeds)

    for q in queries:
        items += brave_search(q)

    items = dedupe(items)

    # sortera lite på datum (de som saknar datum hamnar sist)
    items.sort(key=lambda x: x.get("published", ""), reverse=True)

    newsletter_md = openai_summarize(items)
    write_docs(newsletter_md)

    print("Klart: docs/index.md uppdaterad.")


if __name__ == "__main__":
    main()

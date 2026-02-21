import os
import json
import hashlib
import datetime as dt
from typing import Any, Dict, List

import requests
import feedparser
from dateutil import tz

# =========================
# ENKLA INSTÄLLNINGAR
# =========================
DAYS_BACK = 7
MAX_ITEMS = 30
LANG = "sv"

OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
BRAVE_API_KEY = os.environ.get("BRAVE_API_KEY")


# =========================
# HJÄLPFUNKTIONER
# =========================
def now_stockholm() -> dt.datetime:
    return dt.datetime.now(tz=tz.gettz("Europe/Stockholm"))


def read_lines(path: str) -> List[str]:
    with open(path, "r", encoding="utf-8") as f:
        return [ln.strip() for ln in f.readlines() if ln.strip() and not ln.strip().startswith("#")]


def stable_id(item: Dict[str, Any]) -> str:
    u = (item.get("url") or "").split("#")[0].strip().lower()
    t = (item.get("title") or "").strip().lower()
    return hashlib.sha256((u + "|" + t).encode("utf-8")).hexdigest()


def dedupe(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    out = []
    for it in items:
        sid = stable_id(it)
        if sid in seen:
            continue
        seen.add(sid)
        out.append(it)
    return out


# =========================
# INSAMLING
# =========================
def fetch_rss_items(feed_urls: List[str]) -> List[Dict[str, Any]]:
    cutoff = now_stockholm() - dt.timedelta(days=DAYS_BACK)
    items: List[Dict[str, Any]] = []

    for url in feed_urls:
        d = feedparser.parse(url)
        feed_title = (d.feed.get("title") or "RSS").strip()

        for e in d.entries[:100]:
            title = (e.get("title") or "").strip()
            link = (e.get("link") or "").strip()
            summary = (e.get("summary") or e.get("description") or "").strip()

            published_dt = None
            if e.get("published_parsed"):
                published_dt = dt.datetime(*e.published_parsed[:6], tzinfo=dt.timezone.utc).astimezone(
                    tz.gettz("Europe/Stockholm")
                )
            elif e.get("updated_parsed"):
                published_dt = dt.datetime(*e.updated_parsed[:6], tzinfo=dt.timezone.utc).astimezone(
                    tz.gettz("Europe/Stockholm")
                )

            if not title or not link:
                continue
            if published_dt and published_dt < cutoff:
                continue

            items.append(
                {
                    "source": feed_title,
                    "title": title,
                    "url": link,
                    "snippet": summary[:500],
                    "published": published_dt.isoformat() if published_dt else "",
                }
            )

    return items


def brave_search(query: str) -> List[Dict[str, Any]]:
    """
    Brave är valfritt:
    - saknas BRAVE_API_KEY -> returnerar []
    - om Brave krånglar -> loggar varning och fortsätter (så RSS-nyhetsbrevet ändå byggs)
    """
    if not BRAVE_API_KEY:
        return []

    url = "https://api.search.brave.com/res/v1/web/search"
    headers = {"Accept": "application/json", "X-Subscription-Token": BRAVE_API_KEY}
    params = {"q": query, "count": 10, "freshness": "week"}

    try:
        r = requests.get(url, headers=headers, params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"[WARN] Brave Search misslyckades för query='{query}': {e}")
        return []

    out: List[Dict[str, Any]] = []
    for it in data.get("web", {}).get("results", [])[:10]:
        title = (it.get("title") or "").strip()
        link = (it.get("url") or "").strip()
        desc = (it.get("description") or "").strip()
        if not title or not link:
            continue
        out.append(
            {
                "source": "Brave Search",
                "title": title,
                "url": link,
                "snippet": desc[:500],
                "published": "",
            }
        )
    return out


# =========================
# OPENAI (RESPONSES API)
# =========================
def call_openai_weekly_editor(system_text: str, user_text: str) -> str:
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY saknas. Lägg den i GitHub Secrets.")

    req = {
        "model": OPENAI_MODEL,
        # Typed content för Responses API
        "input": [
            {"role": "system", "content": [{"type": "input_text", "text": system_text}]},
            {"role": "user", "content": [{"type": "input_text", "text": user_text}]},
        ],
        # Rätt format: objekt med type
        "text": {"format": {"type": "text"}},
        # Viktigt: annars kan du få 400 om input blir för långt
        "truncation": "auto",
        # Valfritt men ofta önskvärt i automationer
        "store": False,
    }

    resp = requests.post(
        "https://api.openai.com/v1/responses",
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
        json=req,
        timeout=120,
    )

    # Om det blir fel vill vi se varför direkt i Actions-loggen
    if resp.status_code >= 400:
        print("[ERROR] OpenAI API svarade med fel:")
        print(resp.text)

    resp.raise_for_status()
    data = resp.json()

    # Docs beskriver att output_text är den smidigaste vägen om den finns
    if isinstance(data, dict) and data.get("output_text"):
        return data["output_text"].strip()

    # Fallback-parse
    text = ""
    for item in data.get("output", []):
        if item.get("type") == "message":
            for c in item.get("content", []):
                if c.get("type") == "output_text":
                    text += c.get("text", "")
    return text.strip()


def build_newsletter(items: List[Dict[str, Any]]) -> str:
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
""".strip()

    payload = {
        "week_ending": now_stockholm().date().isoformat(),
        "items": items[:MAX_ITEMS],
    }
    user = json.dumps(payload, ensure_ascii=False)

    return call_openai_weekly_editor(system, user)


def write_docs(markdown_text: str):
    os.makedirs("docs", exist_ok=True)
    with open("docs/index.md", "w", encoding="utf-8") as f:
        f.write(markdown_text + "\n")


def main():
    feeds = read_lines("feeds.txt")
    queries = read_lines("queries.txt")

    items: List[Dict[str, Any]] = []
    items += fetch_rss_items(feeds)

    for q in queries:
        items += brave_search(q)

    items = dedupe(items)
    items.sort(key=lambda x: x.get("published", ""), reverse=True)

    if not items:
        write_docs("# Veckans omvärldsbevakning\n\nInga källor hittades denna vecka.")
        print("Inga items hittades. Skrev en tom sida.")
        return

    try:
    newsletter_md = build_newsletter(items)
except Exception as e:
    # Fallback: publicera en enkel sida så att workflowet inte failar
    print(f"[WARN] Kunde inte skapa AI-sammanfattning: {e}")
    top_links = items[:20]
    lines = [
        "# Veckans omvärldsbevakning (AI & ledarskap)",
        "",
        "## Status",
        "⚠️ Kunde inte generera AI-sammanfattning just nu (t.ex. kvot/billing/rate limit).",
        "",
        "## Länkar som hittades",
    ]
    for it in top_links:
        lines.append(
            f"- **{it.get('title','(utan titel)')}** ({it.get('source','källa')}) – {it.get('url','')}"
        )
    newsletter_md = "\n".join(lines)

write_docs(newsletter_md)
print("Klart: docs/index.md uppdaterad.")


if __name__ == "__main__":
    main()

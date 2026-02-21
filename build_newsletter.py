import os
import json
import hashlib
import datetime as dt
from typing import Any, Dict, List
from urllib.parse import urlparse
from collections import Counter

import requests
import feedparser
from dateutil import tz

# =========================
# INSTÄLLNINGAR
# =========================
DAYS_BACK = 7
MAX_ITEMS = 40                 # input till AI (större = mer bredd, men dyrare)
MAX_SOURCE_LIST = 250          # hur många länkar vi listar på sources-sidan
LANG = "sv"

OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
BRAVE_API_KEY = os.environ.get("BRAVE_API_KEY")

# Vissa RSS:er är kinkiga med default user agent, så vi sätter en tydlig
feedparser.USER_AGENT = "OmvarldsbevakningBot/1.0 (+https://github.com)"


# =========================
# HJÄLPFUNKTIONER
# =========================
def now_stockholm() -> dt.datetime:
    return dt.datetime.now(tz=tz.gettz("Europe/Stockholm"))


def read_lines(path: str) -> List[str]:
    with open(path, "r", encoding="utf-8") as f:
        return [ln.strip() for ln in f.readlines() if ln.strip() and not ln.strip().startswith("#")]


def domain_of(url: str) -> str:
    try:
        d = urlparse(url).netloc.lower()
        if d.startswith("www."):
            d = d[4:]
        return d
    except Exception:
        return ""


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

        kept = 0
        for e in d.entries[:150]:
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
                    "domain": domain_of(link),
                }
            )
            kept += 1

        # Debug i Actions-loggen så vi ser vad som faktiskt hämtas
        print(f"[RSS] {url} -> entries={len(d.entries)} kept_last_{DAYS_BACK}d={kept}")

    return items


def brave_search(query: str) -> List[Dict[str, Any]]:
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
    results = data.get("web", {}).get("results", [])[:10]
    print(f"[BRAVE] query='{query}' results={len(results)}")

    for it in results:
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
                "domain": domain_of(link),
            }
        )
    return out


# =========================
# OUTPUT: sources-sida (diagnos + transparens)
# =========================
def write_sources_page(items: List[Dict[str, Any]]):
    # Räkna domäner
    domains = [it.get("domain", "") for it in items if it.get("domain")]
    counts = Counter(domains)
    top = counts.most_common(30)

    lines = [
        "---",
        "title: Alla källor",
        "---",
        "",
        f"_Uppdaterad: {now_stockholm().date().isoformat()}_",
        "",
        "## Top-domäner i input",
        "",
    ]
    for d, c in top:
        lines.append(f"- **{d}** — {c}")

    lines += [
        "",
        "## Alla länkar (urval)",
        "",
        f"Visar upp till {MAX_SOURCE_LIST} länkar som samlades in (efter dedupe).",
        "",
    ]

    for it in items[:MAX_SOURCE_LIST]:
        title = it.get("title") or "(utan titel)"
        src = it.get("source") or "källa"
        url = it.get("url") or ""
        dom = it.get("domain") or ""
        lines.append(f"- **{title}** ({src}, {dom}) — {url}")

    os.makedirs("docs", exist_ok=True)
    with open("docs/sources.md", "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


# =========================
# OPENAI (RESPONSES API)
# =========================
def call_openai_weekly_editor(system_text: str, user_text: str) -> str:
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY saknas. Lägg den i GitHub Secrets.")

    req = {
        "model": OPENAI_MODEL,
        "input": [
            {"role": "system", "content": [{"type": "input_text", "text": system_text}]},
            {"role": "user", "content": [{"type": "input_text", "text": user_text}]},
        ],
        "text": {"format": {"type": "text"}},
        "truncation": "auto",
        "store": False,
    }

    resp = requests.post(
        "https://api.openai.com/v1/responses",
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
        json=req,
        timeout=120,
    )

    if resp.status_code >= 400:
        print("[ERROR] OpenAI API svarade med fel:")
        print(resp.text)

    resp.raise_for_status()
    data = resp.json()

    if isinstance(data, dict) and data.get("output_text"):
        return data["output_text"].strip()

    text = ""
    for item in data.get("output", []):
        if item.get("type") == "message":
            for c in item.get("content", []):
                if c.get("type") == "output_text":
                    text += c.get("text", "")
    return text.strip()


def build_newsletter(items: List[Dict[str, Any]]) -> str:
    week = now_stockholm().date().isoformat()

    system = f"""
Du är en redaktör som skriver ett veckobrev om AI och ledarskap på {LANG}.
Skriv kort, tydligt och användbart.

VIKTIGT OM KÄLLOR:
- Välj länkar från så många olika domäner som möjligt.
- I "Utvalda länkar": försök ha MINST 8 olika domäner (om input tillåter).
- Efter varje länk: skriv domän i parentes.

Struktur (i Markdown):

_Uppdaterad: {week}_

## TL;DR
- (3 bullets)

## Viktigaste teman
- (3–5 bullets med 1–2 meningar vardera)

## Utvalda länkar
Lista 10–14 länkar.
Varje punkt: **Titel** – 1 mening varför det spelar roll. (domän) URL

Avsluta med:
## Alla källor
- Länk: sources.html
""".strip()

    payload = {
        "week_ending": week,
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

    # Skriv alltid sources-sidan så vi ser vad som faktiskt samlades in
    write_sources_page(items)

    if not items:
        write_docs("_Uppdaterad: " + now_stockholm().date().isoformat() + "_\n\n## Status\n\nInga källor hittades denna vecka.")
        print("Inga items hittades. Skrev en tom sida.")
        return

    # FAILSAFE: om OpenAI strular -> publicera länkar ändå
    try:
        newsletter_md = build_newsletter(items)
    except Exception as e:
        print(f"[WARN] Kunde inte skapa AI-sammanfattning: {e}")
        top_links = items[:20]
        lines = [
            "_Uppdaterad: " + now_stockholm().date().isoformat() + "_",
            "",
            "## Status",
            "⚠️ Kunde inte generera AI-sammanfattning just nu.",
            "",
            "## Länkar som hittades",
        ]
        for it in top_links:
            lines.append(
                f"- **{it.get('title','(utan titel)')}** ({it.get('source','källa')}, {it.get('domain','')}) — {it.get('url','')}"
            )
        lines += [
            "",
            "## Alla källor",
            "- sources.html",
        ]
        newsletter_md = "\n".join(lines)

    write_docs(newsletter_md)
    print("Klart: docs/index.md och docs/sources.md uppdaterade.")


if __name__ == "__main__":
    main()

import os
import json
import hashlib
import datetime as dt
from typing import Any, Dict, List
from urllib.parse import urlparse
from collections import Counter, defaultdict

import requests
import feedparser
from dateutil import tz

# =========================
# INSTÄLLNINGAR
# =========================
DAYS_BACK = 14
MAX_ITEMS_TO_AI = 40          # hur många (diversifierade) länkar vi skickar till AI
MAX_PER_DOMAIN = 1            # hård gräns: max länkar per domän in i AI
MAX_SOURCE_LIST = 300         # hur många länkar vi listar på sources-sidan
LANG = "sv"

OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
BRAVE_API_KEY = os.environ.get("BRAVE_API_KEY")

# RSS: vissa feeds kräver en tydlig user agent
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


def parse_iso_dt(s: str) -> dt.datetime:
    # Tomma datum hamnar längst bak
    if not s:
        return dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)


def select_diverse(items: List[Dict[str, Any]], max_total: int, max_per_domain: int) -> List[Dict[str, Any]]:
    """
    Hård diversitet:
    - max_per_domain per domän
    - round-robin över domäner
    - sorterar varje domän på nyast först
    """
    buckets: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for it in items:
        dom = it.get("domain") or domain_of(it.get("url", "")) or "unknown"
        it["domain"] = dom
        buckets[dom].append(it)

    # sortera inom varje domän på publicerad tid (nyast först)
    for dom in buckets:
        buckets[dom].sort(key=lambda x: parse_iso_dt(x.get("published", "")), reverse=True)

    # sortera domäner efter hur mycket de har (störst först)
    domains = sorted(buckets.keys(), key=lambda d: len(buckets[d]), reverse=True)

    picked: List[Dict[str, Any]] = []
    per_dom = Counter()

    # round-robin plock
    made_progress = True
    while len(picked) < max_total and made_progress:
        made_progress = False
        for dom in domains:
            if len(picked) >= max_total:
                break
            if per_dom[dom] >= max_per_domain:
                continue
            if not buckets[dom]:
                continue
            picked.append(buckets[dom].pop(0))
            per_dom[dom] += 1
            made_progress = True

    print(f"[DIVERSE] picked={len(picked)} domains={len([d for d,c in per_dom.items() if c>0])} max_per_domain={max_per_domain}")
    top = per_dom.most_common(12)
    print("[DIVERSE] per-domain:", ", ".join([f"{d}:{c}" for d,c in top]))
    return picked


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
        for e in d.entries[:200]:
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

    results = data.get("web", {}).get("results", [])[:10]
    print(f"[BRAVE] query='{query}' results={len(results)}")

    out: List[Dict[str, Any]] = []
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
# OUTPUT: sources-sida
# =========================
def write_sources_page(items: List[Dict[str, Any]]):
    domains = [it.get("domain", "") for it in items if it.get("domain")]
    counts = Counter(domains)
    top = counts.most_common(50)

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


def build_newsletter(diverse_items: List[Dict[str, Any]]) -> str:
    week = now_stockholm().date().isoformat()

    system = f"""
Du skriver ett veckobrev om AI & ledarskap på {LANG}.
VIKTIGT: Inputen du får är redan diversifierad. Följ detta strikt:
- "Utvalda länkar": 10–14 punkter
- Max 2 länkar per domän
- Visa domän i parentes efter varje länk
- Undvik att ta flera länkar om exakt samma händelse
- Filtrera bort sådant som inte tydligt handlar om AI, ledarskap, policy, säkerhet, styrning eller organisationers användning av AI.
- Undvik konsument-tech, musik/produktnyheter och gaming om det inte har tydlig AI/ledarskap-vinkel.
- Om en länk känns off-topic: välj en annan från inputlistan.

Struktur (Markdown):

_Uppdaterad: {week}_

## TL;DR
- (3 bullets)

## Viktigaste teman
- (3–5 bullets)

## Utvalda länkar
Varje punkt: **Titel** – 1 mening varför det spelar roll. (domän) URL

Avsluta med:
## Alla källor
- sources
""".strip()

    payload = {"week_ending": week, "items": diverse_items}
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

    write_sources_page(items)

    if not items:
        write_docs("_Uppdaterad: " + now_stockholm().date().isoformat() + "_\n\n## Status\n\nInga källor hittades denna vecka.")
        return

    diverse = select_diverse(items, max_total=MAX_ITEMS_TO_AI, max_per_domain=MAX_PER_DOMAIN)

    try:
        newsletter_md = build_newsletter(diverse)
    except Exception as e:
        print(f"[WARN] Kunde inte skapa AI-sammanfattning: {e}")
        top_links = diverse[:20]
        lines = [
            "_Uppdaterad: " + now_stockholm().date().isoformat() + "_",
            "",
            "## Status",
            "⚠️ Kunde inte generera AI-sammanfattning just nu.",
            "",
            "## Länkar som hittades (diversifierat urval)",
        ]
        for it in top_links:
            lines.append(f"- **{it.get('title','(utan titel)')}** ({it.get('domain','')}) — {it.get('url','')}")
        lines += ["", "## Alla källor", "- sources"]
        newsletter_md = "\n".join(lines)

    write_docs(newsletter_md)
    print("Klart: docs/index.md och docs/sources.md uppdaterade.")


if __name__ == "__main__":
    main()

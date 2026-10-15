#!/usr/bin/env python3
"""Henter alle kilder i config.yaml, filtrerer dem og bygger site/index.html."""
import html
import json
import re
import sys
import time
from html.parser import HTMLParser
from pathlib import Path
from datetime import datetime
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import feedparser
import requests
import yaml

ROOT = Path(__file__).parent
OUT = ROOT / "site"

SESSION = requests.Session()
# Reddit kræver en beskrivende User-Agent; generiske bliver ofte afvist.
SESSION.headers["User-Agent"] = "personal-newsfeed/1.0 (privat RSS-læser)"
TIMEOUT = 20
# Kanal-ID'er gemmes, så YouTube-sider kun slås op første gang.
YT_CACHE_FILE = ROOT / ".cache" / "youtube_ids.json"
YT_CACHE = json.loads(YT_CACHE_FILE.read_text()) if YT_CACHE_FILE.exists() else {}


REDDIT_BASE = "https://www.reddit.com"
COMMON_FEED_PATHS = ["/feed", "/feed/", "/rss", "/rss.xml", "/feed.xml", "/atom.xml", "/index.xml"]


def get(url):
    """GET med op til to nye forsøg, hvis serveren svarer 429 (for mange forespørgsler)."""
    for attempt in range(3):
        r = SESSION.get(url, timeout=TIMEOUT)
        if r.status_code != 429 or attempt == 2:
            r.raise_for_status()
            return r
        wait = r.headers.get("Retry-After", "")
        time.sleep(min(int(wait), 30) if wait.isdigit() else 10 * (attempt + 1))


# Typiske adresser for "nye varer" på de mest udbredte webshop-platforme.
SHOP_FEED_PATHS = [
    "/collections/all.atom?sort_by=created-descending",  # Shopify
    "/?post_type=product&feed=rss2",                      # WooCommerce
    "/feed/?post_type=product",                           # WooCommerce, anden form
]


def first_feed(base, paths):
    """Returnerer den første adresse, der svarer med et feed med opslag, ellers None."""
    for path in paths:
        url = urljoin(base, path)
        try:
            r = SESSION.get(url, timeout=TIMEOUT)
            if r.ok and feedparser.parse(r.content).entries:
                return url
        except requests.RequestException:
            pass
    return None


class FeedLinkFinder(HTMLParser):
    """Finder <link rel="alternate" type="application/rss+xml"> på en forside."""

    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag != "link":
            return
        a = {k: (v or "") for k, v in attrs}
        if "alternate" in a.get("rel", "").lower() and a.get("type") in (
            "application/rss+xml",
            "application/atom+xml",
        ) and a.get("href"):
            self.links.append(a["href"])


def resolve_feed_url(src):
    if src.get("feed"):
        return src["feed"]
    if src.get("subreddit"):
        return f"{REDDIT_BASE}/r/{src['subreddit']}/{src.get('sort', 'hot')}/.rss"
    if src.get("youtube_channel_id"):
        return f"https://www.youtube.com/feeds/videos.xml?channel_id={src['youtube_channel_id']}"
    if src.get("youtube_handle"):
        handle = src["youtube_handle"].lstrip("@")
        if handle.lower() in YT_CACHE:
            return f"https://www.youtube.com/feeds/videos.xml?channel_id={YT_CACHE[handle.lower()]}"
        r = SESSION.get(f"https://www.youtube.com/@{handle}", timeout=TIMEOUT)
        r.raise_for_status()
        # Mest pålideligt først: kanalens eget RSS-link, så kanalens metadata.
        m = (re.search(r'feeds/videos\.xml\?channel_id=(UC[\w-]{22})', r.text)
             or re.search(r'"externalId":"(UC[\w-]{22})"', r.text)
             or re.search(r'"channelId":"(UC[\w-]{22})"', r.text))
        if not m:
            raise ValueError("kunne ikke finde kanalens ID; angiv youtube_channel_id i stedet")
        YT_CACHE[handle.lower()] = m.group(1)
        return f"https://www.youtube.com/feeds/videos.xml?channel_id={m.group(1)}"
    if src.get("shop"):
        found = first_feed(src["shop"], SHOP_FEED_PATHS)
        if found:
            return found
        src = {**src, "site": src["shop"]}  # prøv som almindelig side (fx butikkens blog)
    if src.get("site"):
        r = SESSION.get(src["site"], timeout=TIMEOUT)
        r.raise_for_status()
        finder = FeedLinkFinder()
        finder.feed(r.text)
        if finder.links:
            return urljoin(src["site"], finder.links[0])
        found = first_feed(src["site"], COMMON_FEED_PATHS)
        if found:
            return found
        raise ValueError("fandt intet RSS-feed på siden eller de almindelige adresser; angiv feed: i stedet")
    raise ValueError("kilden mangler feed, site, subreddit eller youtube_handle")


def strip_tags(text):
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def thumbnail(entry):
    for key in ("media_thumbnail", "media_content"):
        for m in entry.get(key) or []:
            if m.get("url"):
                return m["url"]
    for link in entry.get("links") or []:
        if link.get("type", "").startswith("image/") and link.get("href"):
            return link["href"]
    body = entry.get("summary", "") + "".join(
        c.get("value", "") for c in entry.get("content") or []
    )
    m = re.search(r'<img[^>]+src="([^"]+)"', body)
    return html.unescape(m.group(1)) if m else None


def block_pattern(words):
    parts = []
    for w in words or []:
        w = str(w).strip()
        if not w:
            continue
        parts.append(re.escape(w[:-1]) + r"\w*" if w.endswith("*") else re.escape(w))
    return re.compile(r"\b(?:" + "|".join(parts) + r")\b", re.IGNORECASE) if parts else None


def process_entries(entries, src, cfg, blocker, cutoff):
    is_reddit = bool(src.get("subreddit"))
    items, blocked = [], 0
    for e in entries[: cfg.get("max_per_source", 25)]:
        ts_struct = e.get("published_parsed") or e.get("updated_parsed")
        ts = time.mktime(ts_struct) if ts_struct else time.time()
        if ts < cutoff:
            continue
        title = strip_tags(e.get("title", ""))
        # Reddits RSS-tekst er bare "submitted by ... [link] [comments]", så den springes over.
        summary = "" if is_reddit else strip_tags(e.get("summary", ""))[:240]
        if blocker and blocker.search(f"{title} {summary}"):
            blocked += 1
            continue
        items.append({
            "title": title,
            "summary": summary,
            "link": e.get("link"),
            "image": thumbnail(e),
            "source": src["name"],
            "category": src.get("category", ""),
            "ts": int(ts),
        })
    return items, blocked


def fetch_smartweb(src, cfg, blocker, cutoff):
    """Webshops på Smartweb/ScanNet (fx Effektlageret) uden RSS: læser butikkens egen
    vareliste-adresse, sorteret efter nyeste."""
    base = src["smartweb"].rstrip("/")
    limit = cfg.get("max_per_source", 25)
    url = (f"{base}/json/products?currencyIso=DKK&field=new&filter=%7B%7D"
           f"&filterGenerate=false&id=&limit={limit}&orderBy=Age&page=1")
    print(f"     {src['name']} bruger {url}")
    products = get(url).json().get("products", [])
    if not products:
        raise ValueError("butikken returnerede ingen varer; adressen eller formatet er måske ændret")

    tz = ZoneInfo("Europe/Copenhagen")
    items, blocked = [], 0
    for p in products:
        try:
            ts = datetime.strptime(p["DateCreated"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz).timestamp()
        except (KeyError, TypeError, ValueError):
            ts = time.time()
        if ts < cutoff:
            continue
        title = strip_tags(" ".join(filter(None, [p.get("ProducerTitle"), p.get("Title")]))
                           if p.get("ProducerTitle") and p.get("ProducerTitle") not in (p.get("Title") or "")
                           else p.get("Title", ""))
        parts = [p.get("CategoryTitle")]
        price = (p.get("Prices") or [{}])[0].get("PriceMinWithVat")
        if price:
            parts.append(f"{price:,.0f} kr.".replace(",", "."))
        summary = ". ".join(x for x in parts if x)
        if blocker and blocker.search(f"{title} {summary}"):
            blocked += 1
            continue
        images = p.get("Images") or []
        items.append({
            "title": title,
            "summary": summary,
            "link": base + p["Handle"] if p.get("Handle") else base,
            "image": (base + images[0]) if images and images[0].startswith("/") else (images[0] if images else None),
            "source": src["name"],
            "category": src.get("category", ""),
            "ts": int(ts),
        })
    return items, blocked


def fetch_source(src, cfg, blocker, cutoff):
    if src.get("smartweb"):
        return fetch_smartweb(src, cfg, blocker, cutoff)
    url = resolve_feed_url(src)
    if src.get("site") or src.get("shop"):
        print(f"     {src['name']} bruger {url}")
    r = get(url)
    return process_entries(feedparser.parse(r.content).entries, src, cfg, blocker, cutoff)


def fetch_reddit_combined(srcs):
    """Henter alle subreddits i én forespørgsel (r/a+b+c), så Reddit ikke begrænser os,
    og fordeler opslagene på kilderne ud fra adressen."""
    names = "+".join(s["subreddit"] for s in srcs)
    r = get(f"{REDDIT_BASE}/r/{names}/hot/.rss?limit=100")
    groups = {s["subreddit"].lower(): [] for s in srcs}
    for e in feedparser.parse(r.content).entries:
        m = re.search(r"/r/([^/]+)/", e.get("link", ""))
        if m and m.group(1).lower() in groups:
            groups[m.group(1).lower()].append(e)
    return groups


def main():
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    blocker = block_pattern(cfg.get("block_words"))
    cutoff = time.time() - cfg.get("max_age_days", 3) * 86400

    all_items, problems, total_blocked = [], [], 0

    def record(src, items, blocked):
        nonlocal all_items, total_blocked
        all_items += items
        total_blocked += blocked
        print(f"OK   {src['name']}: {len(items)} opslag, {blocked} skjult")

    sources = cfg.get("sources", [])
    reddit = [s for s in sources if s.get("subreddit")]
    for src in sources:
        if src.get("subreddit"):
            continue
        try:
            record(src, *fetch_source(src, cfg, blocker, cutoff))
        except Exception as exc:  # én kilde må ikke vælte hele siden
            problems.append(src["name"])
            print(f"FEJL {src['name']}: {exc}", file=sys.stderr)

    if reddit:
        try:
            groups = fetch_reddit_combined(reddit)
            for src in reddit:
                entries = groups[src["subreddit"].lower()]
                if not entries:
                    print(f"NB   {src['name']}: ingen opslag i det samlede Reddit-feed")
                record(src, *process_entries(entries, src, cfg, blocker, cutoff))
        except Exception as exc:
            problems += [s["name"] for s in reddit]
            print(f"FEJL Reddit (alle subreddits): {exc}", file=sys.stderr)

    if not all_items and problems:
        sys.exit("Ingen kilder virkede. Se fejlene ovenfor.")

    YT_CACHE_FILE.parent.mkdir(exist_ok=True)
    YT_CACHE_FILE.write_text(json.dumps(YT_CACHE, indent=1))

    all_items.sort(key=lambda i: i["ts"], reverse=True)
    data = {
        "title": cfg.get("title", "Min feed"),
        "categories": cfg.get("categories", {}),
        "items": all_items,
        "problems": problems,
        "blocked": total_blocked,
        "built": int(time.time()),
    }
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    page = (ROOT / "template.html").read_text(encoding="utf-8")
    page = page.replace("__TITLE__", html.escape(data["title"])).replace("__DATA__", payload)

    OUT.mkdir(exist_ok=True)
    (OUT / "index.html").write_text(page, encoding="utf-8")
    (OUT / ".nojekyll").write_text("")
    print(f"Bygget: {len(all_items)} opslag, {total_blocked} skjult af filteret.")


if __name__ == "__main__":
    main()

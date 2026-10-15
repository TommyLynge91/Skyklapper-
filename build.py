#!/usr/bin/env python3
"""Henter alle kilder i config.yaml, filtrerer dem og bygger site/index.html."""
import html
import json
import re
import sys
import time
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin

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
        return f"https://www.reddit.com/r/{src['subreddit']}/{src.get('sort', 'hot')}/.rss"
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
    if src.get("site"):
        r = SESSION.get(src["site"], timeout=TIMEOUT)
        r.raise_for_status()
        finder = FeedLinkFinder()
        finder.feed(r.text)
        if not finder.links:
            raise ValueError("fandt intet RSS-feed på forsiden; angiv feed: i stedet")
        return urljoin(src["site"], finder.links[0])
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


def fetch_source(src, cfg, blocker, cutoff):
    url = resolve_feed_url(src)
    r = SESSION.get(url, timeout=TIMEOUT)
    r.raise_for_status()
    feed = feedparser.parse(r.content)
    is_reddit = bool(src.get("subreddit"))

    items, blocked = [], 0
    for e in feed.entries[: cfg.get("max_per_source", 25)]:
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


def main():
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    blocker = block_pattern(cfg.get("block_words"))
    cutoff = time.time() - cfg.get("max_age_days", 3) * 86400

    all_items, problems, total_blocked = [], [], 0
    for src in cfg.get("sources", []):
        try:
            items, blocked = fetch_source(src, cfg, blocker, cutoff)
            all_items += items
            total_blocked += blocked
            print(f"OK   {src['name']}: {len(items)} opslag, {blocked} skjult")
        except Exception as exc:  # én kilde må ikke vælte hele siden
            problems.append(src["name"])
            print(f"FEJL {src['name']}: {exc}", file=sys.stderr)
        if src.get("subreddit"):
            time.sleep(2)  # skån Reddit, så vi ikke bliver begrænset

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

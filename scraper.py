import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from xml.etree.ElementTree import Element, SubElement, tostring
import xml.dom.minidom

import requests
from bs4 import BeautifulSoup

# ── Configuration ─────────────────────────────────────────────────────────────

PAGES = [
    {
        "label": "MClimate Blog",
        "url": "https://mclimate.eu/blogs/blog",
        "feed_id": "mclimate-blog",
    },
]

OUTPUT_FILE = Path("feed.xml")
FEED_TITLE = "MClimate – Blog Monitor"
FEED_LINK = "https://mclimate.eu/blogs/blog"
FEED_DESCRIPTION = "Auto-generated feed tracking new posts on mclimate.eu/blogs/blog"
SEEN_FILE = Path(".seen_items.json")   # persisted between runs via git commit

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; feed-monitor-bot/1.0; "
        "+https://github.com/your-username/your-repo)"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# ── Helpers ───────────────────────────────────────────────────────────────────

def load_seen() -> dict:
    if SEEN_FILE.exists():
        return json.loads(SEEN_FILE.read_text())
    return {}


def save_seen(seen: dict) -> None:
    SEEN_FILE.write_text(json.dumps(seen, indent=2))


def stable_id(url: str) -> str:
    """Deterministic item ID from a URL (handles missing pub dates)."""
    return hashlib.md5(url.encode()).hexdigest()


def rfc822(dt: datetime | None) -> str:
    if dt is None:
        dt = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.strftime("%a, %d %b %Y %H:%M:%S +0000")


# ── Shopify blog scraper ───────────────────────────────────────────────────────

def scrape_shopify_blog(url: str) -> list[dict]:
    """
    Scrape a Shopify /blogs/<handle> listing page.

    Shopify blogs render article cards inside <article> tags with a
    canonical <a> pointing to /blogs/<handle>/<slug>.  We also try the
    JSON endpoint (?view=json) that some Shopify themes expose.
    """
    items = []

    # ── 1. Try the JSON endpoint first (fast, structured) ──────────────────
    try:
        json_url = url.rstrip("/") + ".json?limit=50"
        r = requests.get(json_url, headers=HEADERS, timeout=15)
        if r.status_code == 200 and r.headers.get("content-type", "").startswith("application/json"):
            data = r.json()
            articles = data.get("articles", [])
            for art in articles:
                pub = None
                if art.get("published_at"):
                    try:
                        pub = datetime.fromisoformat(art["published_at"].replace("Z", "+00:00"))
                    except ValueError:
                        pass
                handle = art.get("handle", "")
                slug_url = f"https://mclimate.eu/blogs/blog/{handle}" if handle else url
                items.append({
                    "title": art.get("title", "(no title)"),
                    "url": slug_url,
                    "summary": BeautifulSoup(art.get("summary_html") or art.get("body_html") or "", "html.parser").get_text()[:400],
                    "published": pub,
                    "author": art.get("author", ""),
                    "image": (art.get("image") or {}).get("src", ""),
                })
            if items:
                print(f"  ✓ JSON endpoint returned {len(items)} articles")
                return items
    except Exception as e:
        print(f"  ⚠ JSON endpoint failed ({e}); falling back to HTML scrape")

    # ── 2. HTML scrape ──────────────────────────────────────────────────────
    r = requests.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    # Shopify themes use <article> for blog cards; fall back to common patterns
    cards = (
        soup.select("article")
        or soup.select("[class*='blog-post']")
        or soup.select("[class*='article']")
        or soup.select("[class*='post-item']")
    )

    if not cards:
        # Last resort: harvest all /blogs/blog/<slug> hrefs
        seen_urls: set[str] = set()
        for a in soup.find_all("a", href=re.compile(r"/blogs/blog/[^/\"#?]+")):
            href = a["href"]
            if not href.startswith("http"):
                href = "https://mclimate.eu" + href
            if href in seen_urls:
                continue
            seen_urls.add(href)
            title = a.get_text(strip=True) or href
            if len(title) < 5:          # skip icon-only links
                continue
            items.append({"title": title, "url": href, "summary": "", "published": None, "author": "", "image": ""})
        print(f"  ✓ Fallback href harvest: {len(items)} links")
        return items

    base = "https://mclimate.eu"
    for card in cards:
        # Title
        h_tag = card.find(["h1", "h2", "h3", "h4"])
        title = h_tag.get_text(strip=True) if h_tag else ""

        # URL
        a_tag = card.find("a", href=re.compile(r"/blogs/blog/"))
        if not a_tag:
            a_tag = card.find("a", href=True)
        link = ""
        if a_tag:
            href = a_tag["href"]
            link = href if href.startswith("http") else base + href

        if not link:
            continue
        if not title:
            title = a_tag.get_text(strip=True) if a_tag else link

        # Date — Shopify themes often use <time datetime="...">
        pub = None
        time_tag = card.find("time")
        if time_tag:
            dt_str = time_tag.get("datetime") or time_tag.get_text(strip=True)
            for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d", "%B %d, %Y", "%b %d, %Y"):
                try:
                    pub = datetime.strptime(dt_str[:len(fmt)+4], fmt).replace(tzinfo=timezone.utc)
                    break
                except ValueError:
                    pass

        # Summary
        summary_tag = (
            card.find(class_=re.compile(r"summary|excerpt|body|content"))
            or card.find("p")
        )
        summary = summary_tag.get_text(" ", strip=True)[:400] if summary_tag else ""

        # Author
        author_tag = card.find(class_=re.compile(r"author|byline"))
        author = author_tag.get_text(strip=True) if author_tag else ""

        # Image
        img_tag = card.find("img")
        image = ""
        if img_tag:
            image = img_tag.get("src") or img_tag.get("data-src") or ""
            if image and not image.startswith("http"):
                image = base + image

        items.append({
            "title": title,
            "url": link,
            "summary": summary,
            "published": pub,
            "author": author,
            "image": image,
        })

    print(f"  ✓ HTML scrape: {len(items)} articles")
    return items


# ── RSS builder ───────────────────────────────────────────────────────────────

def build_rss(all_items: list[dict]) -> str:
    rss = Element("rss", version="2.0")
    rss.set("xmlns:atom", "http://www.w3.org/2005/Atom")
    channel = SubElement(rss, "channel")

    SubElement(channel, "title").text = FEED_TITLE
    SubElement(channel, "link").text = FEED_LINK
    SubElement(channel, "description").text = FEED_DESCRIPTION
    SubElement(channel, "language").text = "en"
    SubElement(channel, "lastBuildDate").text = rfc822(datetime.now(timezone.utc))

    atom_link = SubElement(channel, "atom:link")
    atom_link.set("href", "https://YOUR_USERNAME.github.io/YOUR_REPO/feed.xml")
    atom_link.set("rel", "self")
    atom_link.set("type", "application/rss+xml")

    for item in all_items:
        entry = SubElement(channel, "item")
        SubElement(entry, "title").text = item["title"]
        SubElement(entry, "link").text = item["url"]
        SubElement(entry, "guid", isPermaLink="true").text = item["url"]
        SubElement(entry, "pubDate").text = rfc822(item.get("published"))
        if item.get("author"):
            SubElement(entry, "author").text = item["author"]
        if item.get("summary"):
            SubElement(entry, "description").text = item["summary"]
        if item.get("image"):
            enc = SubElement(entry, "enclosure")
            enc.set("url", item["image"])
            enc.set("type", "image/jpeg")
            enc.set("length", "0")

    raw = tostring(rss, encoding="unicode", xml_declaration=False)
    pretty = xml.dom.minidom.parseString(
        '<?xml version="1.0" encoding="UTF-8"?>' + raw
    ).toprettyxml(indent="  ", encoding=None)
    # minidom adds its own declaration; replace to normalise
    return pretty.replace('<?xml version="1.0" ?>', '<?xml version="1.0" encoding="UTF-8"?>')


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    seen = load_seen()
    all_items: list[dict] = []
    new_count = 0

    for page in PAGES:
        print(f"\n→ Scraping: {page['url']}")
        try:
            items = scrape_shopify_blog(page["url"])
        except Exception as exc:
            print(f"  ✗ Error scraping {page['url']}: {exc}", file=sys.stderr)
            continue

        for item in items:
            item_id = stable_id(item["url"])
            if item_id not in seen:
                seen[item_id] = rfc822(item.get("published"))
                new_count += 1
                print(f"  🆕 New: {item['title']}")
            all_items.append(item)

    if not all_items:
        print("\n⚠ No items scraped — feed.xml not updated.")
        sys.exit(1)

    # Sort newest-first (items without dates fall to the bottom)
    all_items.sort(key=lambda x: x.get("published") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)

    OUTPUT_FILE.write_text(build_rss(all_items), encoding="utf-8")
    save_seen(seen)

    print(f"\n✓ feed.xml written ({len(all_items)} items, {new_count} new)")


if __name__ == "__main__":
    main()

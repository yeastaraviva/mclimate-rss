import requests
import hashlib
import os
from bs4 import BeautifulSoup
from feedgen.feed import FeedGenerator
from datetime import datetime, timezone

BASE_URL = "https://www.xovis.com"

PAGES = [
    {
        "name": "Events",
        "url": "https://www.xovis.com/events",
    },
    {
        "name": "Blog",
        "url": "https://www.xovis.com/blog",
    },
    {
        "name": "Press",
        "url": "https://www.xovis.com/press-media",
    },
]


def scrape_page(page: dict) -> list:
    print(f"Scraping {page['name']}...")
    try:
        res = requests.get(
            page["url"],
            timeout=15,
            headers={"User-Agent": "Mozilla/5.0"}
        )
        soup = BeautifulSoup(res.text, "html.parser")

        items = []
        # Every card on all three pages uses the same structure:
        # an <h4> title + a sibling/nearby <a href> with "Read more"
        for h4 in soup.select("h4"):
            title = h4.text.strip()
            if not title:
                continue

            # Find the closest "Read more" link in the same card
            card = h4.parent
            link_el = card.find("a", string=lambda t: t and "read more" in t.lower()) if card else None

            if not link_el:
                # fallback: any <a> in the parent container
                link_el = card.find("a") if card else None

            href = link_el["href"] if link_el and link_el.get("href") else page["url"]
            link = href if href.startswith("http") else BASE_URL + href

            # Category label is in a <p> or sibling text before the <h4>
            category_el = card.find("p") if card else None
            category = category_el.text.strip() if category_el else page["name"]

            items.append({
                "title": f"[{page['name']}] {title}",
                "link": link,
                "description": category,
                "id": hashlib.md5(link.encode()).hexdigest(),
            })

        print(f"  ✓ Found {len(items)} items")
        return items

    except Exception as e:
        print(f"  ✗ Error scraping {page['name']}: {e}")
        return []


def generate_feed(all_items: list):
    fg = FeedGenerator()
    fg.title("Xovis Updates — Events, Blog & Press")
    fg.link(href="https://www.xovis.com", rel="alternate")
    fg.description("Auto-generated RSS feed monitoring Xovis events, blog, and press pages")
    fg.language("en")

    for item in all_items:
        fe = fg.add_entry()
        fe.title(item["title"])
        fe.link(href=item["link"])
        fe.description(item["description"])
        fe.id(item["id"])
        fe.published(datetime.now(timezone.utc))

    fg.rss_file("feed.xml")
    print("\n✓ feed.xml generated successfully.")


if __name__ == "__main__":
    all_items = []
    for page in PAGES:
        all_items.extend(scrape_page(page))

    if all_items:
        generate_feed(all_items)
    else:
        print("No items found — feed not updated.")

"""index.html: the feeds to subscribe to."""

from __future__ import annotations

from html import escape

from .config import Digest, Feed, Rss
from .feed import domain, plural

FILTERS = (
    "include_categories",
    "exclude_categories",
    "include_sections",
    "exclude_sections",
    "exclude_authors",
    "include_titles",
    "exclude_titles",
    "include_links",
    "exclude_links",
)


def describe(feed: Feed | Digest | Rss) -> str:
    if isinstance(feed, Rss):
        hosts = list(dict.fromkeys(domain(s) or s for s in feed.sources))
        parts = [f"items from {', '.join(hosts)}"]
        if len(feed.sources) > 1:
            parts[0] += f" ({plural(len(feed.sources), 'feed')}, duplicates merged)"
        rules = sum(len(getattr(feed, key)) for key in FILTERS)
        if rules:
            parts.append(f"filtered by {plural(rules, 'rule')}")
        if feed.enrich:
            parts.append("with images and excerpts")
        parts.append(f"up to {feed.max_items} items")
        return ", ".join(parts)
    if isinstance(feed, Digest):
        return (
            f"the {feed.top} stories with the most points of each UTC day, "
            f"published at {feed.publish_hour:02d}:00 UTC the next day, last {feed.days} days"
        )
    if feed.ranked:
        parts = [f"stories tagged {feed.tags} in HN's current ranking"]
    elif feed.follows_front_page:
        parts = ["the current front page"]
    else:
        parts = [f"newest stories tagged {feed.tags}"]
    if feed.min_points:
        parts.append(f"at least {feed.min_points} points")
    if feed.min_comments:
        parts.append(f"at least {feed.min_comments} comments")
    parts.append(f"kept {feed.window_hours} hours, up to {feed.max_items} items")
    return ", ".join(parts)


def render_page(feeds: list[tuple[Feed | Digest | Rss, str]]) -> str:
    """feeds pairs each feed with its published URL."""
    rows = "\n".join(
        f'    <li><a href="{escape(url)}">{escape(feed.title)}</a><br>'
        f"<code>{escape(url)}</code><br><small>{escape(describe(feed))}</small></li>"
        for feed, url in feeds
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Feeds</title>
  <link rel="icon" href="favicon.svg" type="image/svg+xml">
  <link rel="apple-touch-icon" href="icon.png">
  <style>
    body {{ font-family: system-ui, sans-serif; max-width: 40rem; margin: 2rem auto; padding: 0 1rem; line-height: 1.5; }}
    li {{ margin-bottom: 1rem; }}
    code {{ user-select: all; }}
  </style>
</head>
<body>
  <h1>Feeds</h1>
  <p>Hacker News feeds, and other sites' feeds filtered and merged, whose items carry each article's image and excerpt.
  Rebuilt every 30 minutes. Source: <a href="https://github.com/mbologna/feed-refinery">mbologna/feed-refinery</a>.</p>
  <ul>
{rows}
  </ul>
</body>
</html>
"""

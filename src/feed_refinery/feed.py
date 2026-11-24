"""RSS 2.0 output. HN feeds are shaped like hnrss: the link is the article, <comments> is the HN thread."""

from __future__ import annotations

import datetime as dt
import xml.etree.ElementTree as ET  # nosec B405 # parses only this site's own published feeds
from email.utils import format_datetime, parsedate_to_datetime
from html import escape
from urllib.parse import urlsplit

from .config import Digest, Feed, Rss
from .preview import Preview
from .state import Entry, RssEntry

ATOM = "http://www.w3.org/2005/Atom"
DC = "http://purl.org/dc/elements/1.1/"
MEDIA = "http://search.yahoo.com/mrss/"
for prefix, uri in (("atom", ATOM), ("dc", DC), ("media", MEDIA)):
    ET.register_namespace(prefix, uri)


def domain(url: str | None) -> str | None:
    host = urlsplit(url).hostname if url else None
    return host.removeprefix("www.") if host else None


def plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


HOT = 4 * 3600  # hackernewsbot's 🔥: at most 4 hours old
COLD = 2 * 86400  # and its ❄️: at least 2 days old


def ago(seconds: int) -> str:
    if seconds < 60:
        return "just now"
    for unit, size in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if seconds >= size:
            return plural(seconds // size, unit) + " ago"
    raise AssertionError("unreachable")  # pragma: no cover


def age_line(created: int, now: int) -> str:
    """The story's age as on HN, with hackernewsbot's 🔥 (new) and ❄️ (old) markers."""
    elapsed = max(0, now - created)
    marker = "🔥 " if elapsed <= HOT else "❄️ " if elapsed >= COLD else ""
    return marker + ago(elapsed)


def item_html(entry: Entry, now: int) -> str:
    """The item body: image, source, excerpt, the submitter's text, the top comment, then links, counts and age."""
    story, preview = entry.story, entry.preview or Preview()
    parts = []
    if preview.image:
        parts.append(f'<p><img src="{escape(preview.image)}" alt=""></p>')
    source = preview.site_name or domain(story.url)
    if source:
        parts.append(f"<p><b>{escape(source)}</b></p>")
    if preview.description:
        parts.append(f"<p>{escape(preview.description)}</p>")
        if story.text:  # Show HN and the like: the page and the submitter's own words
            parts.append(f"<blockquote>{escape(story.text)}</blockquote>")
    elif story.text:
        parts.append(f"<p>{escape(story.text)}</p>")
    if entry.comment:
        c = entry.comment
        parts.append(f'<p>💬 <b>{escape(c.author)}</b>: {escape(c.text)} <a href="{escape(c.url)}">→</a></p>')
    stats = [f'<a href="{escape(story.url)}">Article</a>'] if story.url else []
    stats.append(f'<a href="{escape(story.comments_url)}">{plural(story.comments, "comment")}</a>')
    stats.append(plural(story.points, "point"))
    age = age_line(story.created, now)
    stats.append(f"{age} by {escape(story.author)}" if story.author else age)
    parts.append("<p>" + " · ".join(stats) + "</p>")
    return "\n".join(parts)


KINDS = ("Show HN", "Ask HN", "Launch HN", "Tell HN")


def categories(story) -> list[str]:
    """Reader-filterable tags: the post kind, from its title prefix, and the linked domain."""
    found = [k for k in KINDS if story.title.startswith(k + ":")]
    host = domain(story.url)
    return found + ([host] if host else [])


def utc(timestamp: int) -> dt.datetime:
    return dt.datetime.fromtimestamp(timestamp, dt.UTC)


def _channel(
    feed: Feed | Digest | Rss, description: str, feed_url: str, now: dt.datetime
) -> tuple[ET.Element, ET.Element]:
    """The <rss> and its <channel>. The channel's <link> and <image> are the site the feed is about, not this
    one's, so readers show that site's icon: Feedly takes a feed's icon from its <link>."""
    title = feed.title
    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = title
    ET.SubElement(channel, "link").text = feed.site_url
    ET.SubElement(channel, "description").text = description
    ET.SubElement(channel, f"{{{ATOM}}}link", {"href": feed_url, "rel": "self", "type": "application/rss+xml"})
    ET.SubElement(channel, "lastBuildDate").text = format_datetime(now)
    ET.SubElement(channel, "generator").text = "feed-refinery"
    ET.SubElement(channel, "ttl").text = "30"
    image = ET.SubElement(channel, "image")
    ET.SubElement(image, "url").text = feed.icon_url
    ET.SubElement(image, "title").text = title
    ET.SubElement(image, "link").text = feed.site_url
    return rss, channel


def _document(rss: ET.Element) -> bytes:
    ET.indent(rss)
    return ET.tostring(rss, encoding="utf-8", xml_declaration=True) + b"\n"


def digest_entry_html(rank: int, entry: Entry) -> str:
    """One story in a digest: a compact version of an item."""
    story, preview = entry.story, entry.preview or Preview()
    link = story.url or story.comments_url
    source = preview.site_name or domain(story.url)
    parts = [f'<h3>{rank}. <a href="{escape(link)}">{escape(story.title)}</a></h3>']
    if preview.image:
        parts.append(f'<p><img src="{escape(preview.image)}" alt=""></p>')
    text = preview.description or story.text
    if source or text:
        lead = f"<b>{escape(source)}</b>" + (" · " if text else "") if source else ""
        parts.append(f"<p>{lead}{escape(text or '')}</p>")
    comments = plural(story.comments, "comment")
    parts.append(
        f'<p>{plural(story.points, "point")} · <a href="{escape(story.comments_url)}">{comments}</a>'
        + (f" · by {escape(story.author)}" if story.author else "")
        + "</p>"
    )
    return "\n".join(parts)


def render_digest(digest: Digest, days: list[tuple[dt.date, list[Entry]]], feed_url: str, now: dt.datetime) -> bytes:
    """The digest document: one item per day, newest first, each listing that day's top stories."""
    description = f"The {digest.top} Hacker News stories with the most points each day (UTC)"
    rss, channel = _channel(digest, description, feed_url, now)
    for day, entries in days:
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = f"Hacker News digest: {day:%a %d %b %Y}"
        ET.SubElement(item, "link").text = f"https://news.ycombinator.com/front?day={day.isoformat()}"
        body = "\n".join(digest_entry_html(n, e) for n, e in enumerate(entries, 1))
        ET.SubElement(item, "description").text = body
        ET.SubElement(item, "guid", {"isPermaLink": "false"}).text = f"{digest.file}:{day.isoformat()}"
        published = dt.datetime.combine(day + dt.timedelta(days=1), dt.time(digest.publish_hour), dt.UTC)
        ET.SubElement(item, "pubDate").text = format_datetime(published)
        lead = next((e.preview.image for e in entries if e.preview and e.preview.image), None)
        if lead:
            ET.SubElement(item, f"{{{MEDIA}}}thumbnail", {"url": lead})
    return _document(rss)


def render_feed(feed: Feed, items: list[tuple[Entry, int]], feed_url: str, now: dt.datetime) -> bytes:
    """The feed document. items pairs each entry with when the feed first listed it, newest first.

    pubDate is that first-listed time, not the story's age: a story that
    crosses min_points a day after it was posted is news to this feed now, and
    a reader sorting by date should show it at the top.
    """
    description = f"{feed.title}, with each linked page's image and excerpt"
    rss, channel = _channel(feed, description, feed_url, now)
    for entry, first_seen in items:
        story = entry.story
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = story.title
        ET.SubElement(item, "link").text = story.url or story.comments_url
        ET.SubElement(item, "description").text = item_html(entry, int(now.timestamp()))
        ET.SubElement(item, "comments").text = story.comments_url
        ET.SubElement(item, "guid", {"isPermaLink": "false"}).text = story.comments_url
        ET.SubElement(item, "pubDate").text = format_datetime(utc(first_seen))
        for category in categories(story):
            ET.SubElement(item, "category").text = category
        if story.author:
            ET.SubElement(item, f"{{{DC}}}creator").text = story.author
        image = entry.preview.image if entry.preview else None
        if image:
            ET.SubElement(item, f"{{{MEDIA}}}content", {"url": image, "medium": "image"})
            ET.SubElement(item, f"{{{MEDIA}}}thumbnail", {"url": image})
    return _document(rss)


def rss_item_html(entry: RssEntry) -> str:
    """An RSS item's body: the image (the source's, else the page's) and the excerpt (likewise).

    The item's <link> already leads to the article, and readers build their
    preview text from the body, so a link to it shows only when there's no
    excerpt: then it's all the body has.
    """
    parts = []
    if entry.image:
        parts.append(f'<p><img src="{escape(entry.image)}" alt=""></p>')
    if entry.excerpt:
        parts.append(f"<p>{escape(entry.excerpt)}</p>")
    elif link := entry.item.link:
        parts.append(f'<p><a href="{escape(link)}">{escape(domain(link) or link)}</a></p>')
    return "\n".join(parts)


def distinct(categories: tuple[str, ...]) -> list[str]:
    """The categories without those that differ only in case from an earlier one ("Apple" and the tag "apple")."""
    seen: dict[str, str] = {}
    for category in categories:
        seen.setdefault(category.casefold(), category)
    return list(seen.values())


def render_rss_feed(feed: Rss, entries: list[RssEntry], feed_url: str, now: dt.datetime) -> bytes:
    """An RSS feed's document, entries newest first. Items keep the source's title, link, guid, date and categories."""
    description = f"{feed.title}, from {', '.join(feed.sources)}"
    if feed.enrich:
        description += ", with each article's image and excerpt"
    rss, channel = _channel(feed, description, feed_url, now)
    for entry in entries:
        source = entry.item
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = source.title
        if source.link:
            ET.SubElement(item, "link").text = source.link
        ET.SubElement(item, "description").text = rss_item_html(entry)
        permalink = "true" if source.permalink else "false"
        ET.SubElement(item, "guid", {"isPermaLink": permalink}).text = source.guid
        ET.SubElement(item, "pubDate").text = format_datetime(utc(source.published or entry.first_seen))
        for category in distinct(source.categories):
            ET.SubElement(item, "category").text = category
        if source.author:
            ET.SubElement(item, f"{{{DC}}}creator").text = source.author
        if entry.image:
            ET.SubElement(item, f"{{{MEDIA}}}content", {"url": entry.image, "medium": "image"})
            ET.SubElement(item, f"{{{MEDIA}}}thumbnail", {"url": entry.image})
    return _document(rss)


def last_build_date(document: bytes) -> dt.datetime:
    """A feed's lastBuildDate. Raises ValueError when the document has none."""
    try:
        text = ET.fromstring(document).findtext("channel/lastBuildDate")  # nosec B314 # this site's own feed
    except ET.ParseError as e:
        raise ValueError(f"not XML: {e}") from e
    if not text:
        raise ValueError("no lastBuildDate in the feed")
    try:
        return parsedate_to_datetime(text)
    except (TypeError, ValueError) as e:
        raise ValueError(f"unreadable lastBuildDate {text!r}") from e

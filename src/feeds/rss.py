"""Items from RSS and Atom feeds: fetched with conditional GETs, normalised, filtered and matched for dedup."""

from __future__ import annotations

import calendar
import http.client
import re
import time
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import NamedTuple
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import feedparser

from . import USER_AGENT
from .config import Rss
from .text import excerpt, plain_text, squash

TIMEOUT = 30
RETRY_DELAY = 2  # seconds before the one retry after a network error
MAX_BYTES = 10 * 1024 * 1024
ACCEPT = "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.9, */*;q=0.1"
TRACKING = {"fbclid", "gclid", "ref", "mc_cid", "mc_eid"}  # and every utm_* parameter
TITLE_LIMIT = 200  # for an item with no title, which gets the start of its text instead


@dataclass(frozen=True)
class Item:
    guid: str  # the source's id, or the link when it has none
    link: str | None
    title: str
    published: int | None  # Unix time, from the source; None when it gives no date
    categories: tuple[str, ...]
    author: str
    summary: str  # the source's description as a plain-text excerpt; empty when it has no text
    image: str | None  # from an enclosure, media:content or media:thumbnail, or the description's first <img>

    @property
    def permalink(self) -> bool:
        """Whether the guid is the article's URL, as RSS's isPermaLink means."""
        return self.guid == self.link and urlsplit(self.guid).scheme in ("http", "https")


class Fetched(NamedTuple):
    status: int  # 200, or 304 when the source is unchanged (items is then empty)
    items: list[Item]
    etag: str | None
    last_modified: str | None


def fetch(source: str, etag: str | None = None, last_modified: str | None = None) -> Fetched:
    """The source's items, with a conditional GET. Raises on HTTP and network errors and on a document that isn't a feed.

    A network error (not an HTTP status) is retried once.
    """
    headers = {"User-Agent": USER_AGENT, "Accept": ACCEPT}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    req = urllib.request.Request(source, headers=headers)
    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:  # nosec B310 # config allows only http(s)
                body = r.read(MAX_BYTES)
                content_type = r.headers.get("Content-Type", "")
                new_etag, new_modified = r.headers.get("ETag"), r.headers.get("Last-Modified")
                base = r.geturl() or source
            break
        except urllib.error.HTTPError as e:
            if e.code == 304:
                return Fetched(304, [], etag, last_modified)
            raise
        except urllib.error.URLError, OSError, http.client.HTTPException:
            if attempt == 2:
                raise
            time.sleep(RETRY_DELAY)
    return Fetched(200, parse(body, base, content_type), new_etag, new_modified)


def parse(body: bytes, base: str, content_type: str = "") -> list[Item]:
    """The items of an RSS or Atom document, in the source's order. Raises ValueError when it isn't a feed."""
    headers = {"content-type": content_type} if content_type else {}
    data = feedparser.parse(body, response_headers=headers)
    if not data.get("version") or (data.get("bozo") and not data.entries):
        reason = data.get("bozo_exception") or "not RSS or Atom"
        raise ValueError(f"not a feed: {reason}")
    items = []
    for entry in data.entries:
        item = _item(entry, base)
        if item:
            items.append(item)
    return items


def _web(url: str | None, base: str) -> str | None:
    if not url:
        return None
    url = urljoin(base, url.strip())
    return url if urlsplit(url).scheme in ("http", "https") else None


class _FirstImage(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.src: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "img" and self.src is None:
            self.src = dict(attrs).get("src") or None


def _first_image(html: str) -> str | None:
    parser = _FirstImage()
    parser.feed(html)
    parser.close()
    return parser.src


def _image(entry, html: str, base: str) -> str | None:
    candidates = [e.get("href") for e in entry.get("enclosures", []) if (e.get("type") or "").startswith("image/")]
    candidates += [
        m.get("url")
        for m in entry.get("media_content", [])
        if m.get("medium") == "image" or (m.get("type") or "").startswith("image/")
    ]
    candidates += [m.get("url") for m in entry.get("media_thumbnail", [])]
    candidates.append(_first_image(html) if html else None)
    return next((u for u in (_web(c, base) for c in candidates) if u), None)


def _item(entry, base: str) -> Item | None:
    link = _web(entry.get("link"), base)
    guid = (entry.get("id") or "").strip() or link
    if not guid:
        return None  # nothing to tell it apart by
    html = entry.get("summary") or ""  # feedparser fills it from <content> when the source has no summary
    summary = excerpt(plain_text(html)) if html else ""
    title = entry.get("title") or ""
    if entry.get("title_detail", {}).get("type") in ("text/html", "application/xhtml+xml"):
        title = plain_text(title)
    title = squash(title) or excerpt(summary, TITLE_LIMIT) or link or guid
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    categories: list[str] = []
    for tag in entry.get("tags", []):
        term = squash(tag.get("term") or "")
        if term and term not in categories:
            categories.append(term)
    return Item(
        guid=guid,
        link=link,
        title=title,
        published=calendar.timegm(parsed) if parsed else None,
        categories=tuple(categories),
        author=squash(entry.get("author") or ""),
        summary=summary,
        image=_image(entry, html, link or base),
    )


def keep(item: Item, feed: Rss) -> bool:
    """Whether the item passes the feed's filters.

    The includes come first: an item must match every include list the feed
    sets (any entry of each). The excludes then drop whatever matches any
    entry of any of them. Sections match only the first category: the
    section on most WordPress sites, whose other categories and free-form
    tags may reuse section names.
    """
    categories = {c.casefold() for c in item.categories}
    section = item.categories[0].casefold() if item.categories else None
    link = item.link or ""
    if feed.include_categories and not categories & feed.include_categories:
        return False
    if feed.include_sections and section not in feed.include_sections:
        return False
    if feed.include_titles and not any(p.search(item.title) for p in feed.include_titles):
        return False
    if feed.include_links and not any(p.search(link) for p in feed.include_links):
        return False
    if categories & feed.exclude_categories:
        return False
    if section in feed.exclude_sections:
        return False
    if any(p.search(item.title) for p in feed.exclude_titles):
        return False
    if any(p.search(link) for p in feed.exclude_links):
        return False
    return item.author.casefold() not in feed.exclude_authors


def _tracking(param: str) -> bool:
    param = param.lower()
    return param.startswith("utm_") or param in TRACKING


def canonical_link(url: str) -> str:
    """The link two copies of a story share: scheme and host lowercased; no fragment, trailing slash or
    tracking parameters; the other parameters sorted."""
    parts = urlsplit(url.strip())
    query = sorted((k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _tracking(k))
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), urlencode(query), ""))


PUNCTUATION = re.compile(r"[\W_]+")


def normal_title(title: str) -> str:
    """The title with case and Unicode forms folded, and runs of whitespace and punctuation collapsed."""
    return PUNCTUATION.sub(" ", unicodedata.normalize("NFKC", title).casefold()).strip()


def dedup_key(item: Item) -> str:
    """Where the state keeps an item: its canonical link, or its guid when it has no link."""
    return canonical_link(item.link) if item.link else "guid:" + item.guid

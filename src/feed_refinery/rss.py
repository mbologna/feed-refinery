"""Items from RSS and Atom feeds: fetched with conditional GETs, normalised, filtered and matched for dedup."""

from __future__ import annotations

import calendar
import datetime as dt
import http.client
import json
import re
import time
import unicodedata
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from html.parser import HTMLParser
from typing import NamedTuple
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import feedparser

from . import USER_AGENT
from .config import JsonFields, Rss
from .text import excerpt, plain_paragraphs, plain_text, squash

TIMEOUT = 30
RETRY_DELAY = 2  # seconds before the one retry after a network error
MAX_BYTES = 10 * 1024 * 1024
ACCEPT = "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.9, */*;q=0.1"
TRACKING = {"fbclid", "gclid", "ref", "mc_cid", "mc_eid"}  # and every utm_* parameter
TITLE_LIMIT = 200  # for an item with no title, which gets the start of its text instead
TEXT_LIMIT = 20_000  # of an item's text kept for exclude_text; never stored


@dataclass(frozen=True)
class Item:
    guid: str  # the source's id, or the link when it has none
    link: str | None
    title: str
    published: int | None  # Unix time, from the source; None when it gives no date
    categories: tuple[str, ...]
    author: str
    summary: str  # the source's description as a plain-text excerpt, paragraphs kept; empty when it has no text
    image: str | None  # from an enclosure, media:content or media:thumbnail, or the description's first <img>
    # The description's whole plain text, for exclude_text. Only freshly parsed
    # items have it: the state doesn't store it, and two copies of a story are
    # the same item whatever their text.
    text: str = field(default="", compare=False, repr=False)

    @property
    def permalink(self) -> bool:
        """Whether the guid is the article's URL, as RSS's isPermaLink means."""
        return self.guid == self.link and urlsplit(self.guid).scheme in ("http", "https")


class Fetched(NamedTuple):
    status: int  # 200, or 304 when the source is unchanged (items is then empty)
    items: list[Item]
    etag: str | None
    last_modified: str | None


def fetch(
    source: str, etag: str | None = None, last_modified: str | None = None, fields: JsonFields | None = None
) -> Fetched:
    """The source's items, with a conditional GET. Raises on HTTP and network errors and on a document that isn't a feed.

    With fields the source is a JSON document, read with parse_json.

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
    items = parse_json(body, base, fields) if fields else parse(body, base, content_type)
    return Fetched(200, items, new_etag, new_modified)


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


def _at(data: object, path: str) -> object:
    """The value at a dotted path ("data.posts", "media.0.url"), or None when any step is missing."""
    for step in path.split("."):
        if isinstance(data, dict):
            data = data.get(step)
        elif isinstance(data, list) and step.isdigit() and int(step) < len(data):
            data = data[int(step)]
        else:
            return None
    return data


def _json_date(value: object, fmt: str | None) -> int | None:
    """Unix time from a JSON date: a strptime pattern's string, else ISO 8601, or Unix seconds (milliseconds when
    larger than any second count of this century). A time without a zone is UTC; a date alone is its midnight."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return int(value / 1000 if value > 10**11 else value)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        moment = dt.datetime.strptime(value.strip(), fmt) if fmt else dt.datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.UTC)
    return int(moment.timestamp())


def labelled(items: list[Item], label: str | None) -> list[Item]:
    """The items with label in front of their titles, unless a title already starts with it."""
    if not label:
        return items
    folded = label.casefold()
    return [i if i.title.casefold().startswith(folded) else replace(i, title=f"{label} {i.title}") for i in items]


def parse_json(body: bytes, base: str, fields: JsonFields) -> list[Item]:
    """The items of a JSON document, as fields maps them, in the document's order.

    An item without a title or a link is skipped. Raises ValueError when the
    body isn't JSON or fields.items isn't a list, so the source keeps the
    items it had, as for a broken RSS feed.
    """
    try:
        data = json.loads(body)
    except ValueError as e:
        raise ValueError(f"not JSON: {e}") from e
    entries = _at(data, fields.items)
    if not isinstance(entries, list):
        raise ValueError(f"not a list at {fields.items!r}")
    items = []
    for entry in entries:
        title = _at(entry, fields.title)
        link = _at(entry, fields.link)
        if not isinstance(title, str) or not isinstance(link, str) or not (link := _web(link, base) or ""):
            continue
        title = squash(plain_text(title))
        if not title:
            continue
        html = _at(entry, fields.summary) if fields.summary else None
        prose = plain_paragraphs(html) if isinstance(html, str) else ""
        text = squash(prose)[:TEXT_LIMIT]
        author = _at(entry, fields.author) if fields.author else None
        image = _at(entry, fields.image) if fields.image else None
        items.append(
            Item(
                guid=link,
                link=link,
                title=title,
                published=_json_date(_at(entry, fields.date), fields.date_format) if fields.date else None,
                categories=(),
                author=squash(author) if isinstance(author, str) else "",
                summary=excerpt(prose),
                image=_web(image, link) if isinstance(image, str) else None,
                text=text,
            )
        )
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
    prose = plain_paragraphs(html)
    text = squash(prose)[:TEXT_LIMIT]
    title = entry.get("title") or ""
    if entry.get("title_detail", {}).get("type") in ("text/html", "application/xhtml+xml"):
        title = plain_text(title)
    title = squash(title) or excerpt(text, TITLE_LIMIT) or link or guid
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
        summary=excerpt(prose),
        image=_image(entry, html, link or base),
        text=text,
    )


class Drop(NamedTuple):
    """The filter that drops an item: the config key, and for an exclude the entry that matched."""

    key: str
    entry: str | None = None  # None for an include: the item matched none of the list


def verdict(item: Item, feed: Rss) -> Drop | None:
    """The filter that drops the item, or None when it passes them all.

    The includes come first: an item must match every include list the feed
    sets (any entry of each). The excludes then drop whatever matches any
    entry of any of them. Sections match only the first category: the
    section on most WordPress sites, whose other categories and free-form
    tags may reuse section names. exclude_text searches the title and the
    description's text; only freshly parsed items carry the text, so an item
    the state holds is judged on its title, as it passed with its text when
    first seen.
    """
    categories = [c.casefold() for c in item.categories]
    section = categories[0] if categories else None
    link = item.link or ""
    if feed.include_categories and not feed.include_categories.intersection(categories):
        return Drop("include_categories")
    if feed.include_sections and section not in feed.include_sections:
        return Drop("include_sections")
    if feed.include_titles and not any(p.search(item.title) for p in feed.include_titles):
        return Drop("include_titles")
    if feed.include_links and not any(p.search(link) for p in feed.include_links):
        return Drop("include_links")
    if found := next((c for c in categories if c in feed.exclude_categories), None):
        return Drop("exclude_categories", found)
    if section in feed.exclude_sections:
        return Drop("exclude_sections", section)
    if pattern := next((p for p in feed.exclude_titles if p.search(item.title)), None):
        return Drop("exclude_titles", pattern.pattern)
    if pattern := next((p for p in feed.exclude_links if p.search(link)), None):
        return Drop("exclude_links", pattern.pattern)
    if item.author.casefold() in feed.exclude_authors:
        return Drop("exclude_authors", item.author.casefold())
    if feed.exclude_text:
        text = item.title + "\n" + item.text
        if pattern := next((p for p in feed.exclude_text if p.search(text)), None):
            return Drop("exclude_text", pattern.pattern)
    return None


def keep(item: Item, feed: Rss) -> bool:
    """Whether the item passes the feed's filters (see verdict)."""
    return verdict(item, feed) is None


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


@dataclass(frozen=True)
class Choice:
    """What became of one item a source listed."""

    source: str
    item: Item
    drop: Drop | None  # the filter that dropped it; None when it passed
    key: str | None = None  # the story it belongs to, when it passed
    merged: bool = False  # True when it joined a story an earlier copy (or the state) already held


@dataclass
class Selection:
    """The stories a feed's sources list, after filtering and merging copies."""

    items: dict[str, Item] = field(default_factory=dict)  # dedup key -> the story's kept copy, every copy's categories
    origin: dict[str, str] = field(default_factory=dict)  # key -> the key it had in known, for a story known before
    moved: set[str] = field(default_factory=set)  # keys of stories whose link changed: their preview no longer applies
    sources: dict[str, list[str]] = field(default_factory=dict)  # key -> the sources listing a copy, in config order
    choices: list[Choice] = field(default_factory=list)  # every listed item, in the sources' order


def select(
    feed: Rss, listed: Sequence[tuple[str, Sequence[Item]]], known: Mapping[str, Item] | None = None
) -> Selection:
    """Filter the sources' items and merge the copies of each story into what known already holds.

    listed pairs each source that answered with its items, in the config's
    order; known maps dedup keys to the stories already held, oldest first.
    Two items are one story when their canonical links or their guids match,
    or, with dedupe = "link+title", their normalised titles. A known story
    keeps its copy; among new ones the earliest published wins, then the
    source and position order. Every copy adds its categories.
    """
    result = Selection(items=dict(known or {}), origin={k: k for k in known or {}})
    items = result.items
    order = {url: n for n, url in enumerate(feed.sources)}
    by_link = {canonical_link(i.link): k for k, i in items.items() if i.link}
    by_guid = {i.guid: k for k, i in items.items()}
    by_title: dict[str, str] = {}
    titles = feed.dedupe == "link+title"
    if titles:
        by_title = {t: k for k, i in items.items() if (t := normal_title(i.title))}
    choices = [Choice(url, item, verdict(item, feed)) for url, found in listed for item in found]
    candidates = [(n, c) for n, c in enumerate(choices) if c.drop is None]
    candidates.sort(
        key=lambda nc: (nc[1].item.published is None, nc[1].item.published or 0, order[nc[1].source], nc[0])
    )

    def rekey(old: str, new: str) -> None:
        items[new] = items.pop(old)
        for index in (by_link, by_guid, by_title):
            index.update({k: new for k, v in index.items() if v == old})
        if old in result.origin:
            result.origin[new] = result.origin.pop(old)
        if old in result.sources:
            result.sources[new] = result.sources.pop(old)
        if old in result.moved:
            result.moved.remove(old)
            result.moved.add(new)
        for m, earlier in enumerate(choices):
            if earlier.key == old:
                choices[m] = replace(earlier, key=new)

    for n, choice in candidates:
        item, url = choice.item, choice.source
        link = canonical_link(item.link) if item.link else None
        title = normal_title(item.title) if titles else ""
        key = (
            (by_link.get(link) if link else None)
            or by_guid.get(item.guid)
            or (by_title.get(title) if title else None)
            or (dedup_key(item) if dedup_key(item) in items else None)
        )
        merged = key is not None
        if key is None:
            key = dedup_key(item)
            items[key] = item
        else:
            kept = items[key]
            categories = kept.categories + tuple(c for c in item.categories if c not in kept.categories)
            if item.guid == kept.guid:  # the same item again, perhaps edited
                items[key] = replace(item, categories=categories)
                if item.link != kept.link:  # its preview no longer applies, and its key follows the link
                    if dedup_key(item) not in items:
                        rekey(key, dedup_key(item))
                        key = dedup_key(item)
                    result.moved.add(key)
            else:
                items[key] = replace(kept, categories=categories)
        sources = result.sources.setdefault(key, [])
        if url not in sources:
            sources.append(url)
            sources.sort(key=order.__getitem__)
        choices[n] = replace(choice, key=key, merged=merged)
        if link:
            by_link.setdefault(link, key)
        by_guid.setdefault(item.guid, key)
        if title:
            by_title.setdefault(title, key)
    result.choices = choices
    return result

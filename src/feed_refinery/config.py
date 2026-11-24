"""feeds.toml: which feeds to publish."""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from . import STATE_NAME

FILE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*\.xml$")


class ConfigError(ValueError):
    pass


HN_SITE = "https://news.ycombinator.com/"


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}/"


class _Site:
    """A feed's website, which readers show the icon of (Feedly takes it from the channel's <link>), and its icon."""

    site: str | None
    icon: str | None

    def default_site(self) -> str:
        return HN_SITE

    @property
    def site_url(self) -> str:
        return self.site or self.default_site()

    @property
    def icon_url(self) -> str:
        return self.icon or urljoin(self.site_url, "/favicon.ico")


DEDUPE = ("link", "link+title")


@dataclass(frozen=True)
class Feed(_Site):
    file: str
    title: str
    tags: str
    min_points: int = 0
    min_comments: int = 0
    window_hours: int = 48
    max_items: int = 150
    ranked: bool = False  # only stories in HN's current ranking (topstories)
    exclude_paywalled: bool = False  # hide stories whose page says it's paywalled
    exclude_titles: tuple[re.Pattern[str], ...] = ()  # regular expressions searched in each story's title
    site: str | None = None  # Hacker News when unset
    icon: str | None = None  # the site's /favicon.ico when unset

    @property
    def follows_front_page(self) -> bool:
        return "front_page" in self.tags.split(",")


@dataclass(frozen=True)
class Digest(_Site):
    """One item per UTC day with that day's top stories."""

    file: str
    title: str
    top: int = 20  # stories per day
    days: int = 7  # days kept
    publish_hour: int = 6  # UTC hour of the next day when a day's item appears
    exclude_titles: tuple[re.Pattern[str], ...] = ()  # stories never counted among a day's top
    exclude_paywalled: bool = False  # leave out the stories whose page says it's paywalled
    site: str | None = None  # Hacker News when unset
    icon: str | None = None


@dataclass(frozen=True)
class JsonFields:
    """Where a JSON source keeps its items and each item's fields: dotted paths, such as "data.posts"."""

    items: str  # the list of items, from the document's root
    title: str  # the rest from each item
    link: str
    date: str | None = None
    date_format: str | None = None  # a strptime pattern; ISO 8601 or Unix seconds when unset
    summary: str | None = None
    author: str | None = None
    image: str | None = None


@dataclass(frozen=True)
class Rss(_Site):
    """Items from one or more RSS or Atom feeds, filtered, merged and enriched."""

    file: str
    title: str
    sources: tuple[str, ...]
    # Matched case-insensitively against the whole category or author. A
    # section is the item's first category, as WordPress feeds list them.
    include_categories: frozenset[str] = frozenset()
    exclude_categories: frozenset[str] = frozenset()
    include_sections: frozenset[str] = frozenset()
    exclude_sections: frozenset[str] = frozenset()
    exclude_authors: frozenset[str] = frozenset()
    # Regular expressions, each searched anywhere in the title or link.
    include_titles: tuple[re.Pattern[str], ...] = ()
    exclude_titles: tuple[re.Pattern[str], ...] = ()
    include_links: tuple[re.Pattern[str], ...] = ()
    exclude_links: tuple[re.Pattern[str], ...] = ()
    # Searched in the title and the description's text when an item is first seen.
    exclude_text: tuple[re.Pattern[str], ...] = ()
    enrich: bool = True  # fetch each article's preview for what the source lacks
    max_items: int = 100
    dedupe: str = "link"  # or "link+title"
    # Hide items whose page declares isAccessibleForFree false; needs enrich.
    exclude_paywalled: bool = False
    # Hide items whose link was a story of the [[feed]] tables (see state.check_hacker_news).
    exclude_on_hacker_news: bool = False
    keep_days: int = 7  # how long an item no source lists any more stays
    json: JsonFields | None = None  # the sources are JSON documents, not RSS or Atom
    # (source URL, label) pairs: the label goes in front of the titles of that
    # source's items, for sources whose titles don't say what they are ("v2.15.3").
    source_titles: tuple[tuple[str, str], ...] = ()
    site: str | None = None  # the first source's site when unset
    icon: str | None = None  # the site's /favicon.ico when unset

    def default_site(self) -> str:
        return _origin(self.sources[0])

    def label(self, source: str) -> str | None:
        return dict(self.source_titles).get(source)

    @property
    def filters_key(self) -> str:
        """A digest of the options that decide which items pass: it changes when a filter does."""
        options = {
            name: sorted(p.pattern if isinstance(p, re.Pattern) else p for p in value)
            for name in FILTERS
            if (value := getattr(self, name))
        }
        return hashlib.sha256(json.dumps(options, sort_keys=True).encode()).hexdigest()[:16]


@dataclass(frozen=True)
class Config:
    feeds: list[Feed]
    digests: list[Digest]
    rss: list[Rss] = field(default_factory=list)

    @property
    def files(self) -> list[str]:
        return [f.file for f in self.feeds] + [d.file for d in self.digests] + [r.file for r in self.rss]

    @property
    def hacker_news_floor(self) -> int:
        """The points that put a story in a [[feed]] by score: the lowest min_points above 0, else 100."""
        return min((f.min_points for f in self.feeds if f.min_points > 0), default=100)


# The [[rss]] options rss.verdict reads.
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
    "exclude_text",
)
REQUIRED = ("file", "title", "tags")
FLAGS = ("ranked", "exclude_paywalled")
URLS = ("site", "icon")  # optional on every kind of table
KNOWN = {f.name for f in fields(Feed)}
NUMBERS = KNOWN - set(REQUIRED) - set(FLAGS) - {"exclude_titles"} - set(URLS)
DIGEST_KNOWN = {f.name for f in fields(Digest)}


def _check_file(where: str, file: object, used: list[str]) -> None:
    if not isinstance(file, str) or not FILE_RE.match(file) or file == STATE_NAME:
        raise ConfigError(f"{where}: file must be a lowercase name ending in .xml, not {file!r}")
    if file in used:
        raise ConfigError(f"{where}: {file} is already used by another feed")


def _parse_digest(where: str, table: dict, used: list[str]) -> Digest:
    unknown = set(table) - DIGEST_KNOWN
    if unknown:
        raise ConfigError(f"{where}: unknown keys: {', '.join(sorted(unknown))}")
    missing = [k for k in ("file", "title") if k not in table]
    if missing:
        raise ConfigError(f"{where}: missing {', '.join(missing)}")
    if not isinstance(table["title"], str) or not table["title"].strip():
        raise ConfigError(f"{where}: title must be a non-empty string")
    for key, low, high in (("top", 1, 100), ("days", 1, 60), ("publish_hour", 0, 23)):
        value = table.get(key)
        if value is not None and (not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high):
            raise ConfigError(f"{where}: {key} must be a whole number from {low} to {high}")
    if "exclude_paywalled" in table and not isinstance(table["exclude_paywalled"], bool):
        raise ConfigError(f"{where}: exclude_paywalled must be true or false")
    _check_file(where, table["file"], used)
    _check_urls(where, table)
    if "exclude_titles" in table:
        table = {**table, "exclude_titles": _patterns(where, "exclude_titles", table["exclude_titles"])}
    return Digest(**table)


RSS_KNOWN = {f.name for f in fields(Rss)} | {"source"}  # source: one URL, sources: several
RSS_WORDS = ("include_categories", "exclude_categories", "include_sections", "exclude_sections", "exclude_authors")
RSS_PATTERNS = ("include_titles", "exclude_titles", "include_links", "exclude_links", "exclude_text")
RSS_FLAGS = ("enrich", "exclude_paywalled", "exclude_on_hacker_news")
MAX_RSS_ITEMS = 1000
MAX_KEEP_DAYS = 365


def _web_url(value: object) -> bool:
    return isinstance(value, str) and value.startswith(("http://", "https://")) and len(value) > len("https://")


def _check_urls(where: str, table: dict) -> None:
    for key in URLS:
        if key in table and not _web_url(table[key]):
            raise ConfigError(f"{where}: {key} must be an http or https URL")


def _strings(where: str, key: str, value: object) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise ConfigError(f"{where}: {key} must be a list of non-empty strings")
    return value


def _patterns(where: str, key: str, value: object) -> tuple[re.Pattern[str], ...]:
    patterns = []
    for pattern in _strings(where, key, value):
        try:
            patterns.append(re.compile(pattern))
        except re.error as e:
            raise ConfigError(f"{where}: {key}: {pattern!r} is not a valid regular expression: {e}") from e
    return tuple(patterns)


def _parse_rss(where: str, table: dict, used: list[str]) -> Rss:
    unknown = set(table) - RSS_KNOWN
    if unknown:
        raise ConfigError(f"{where}: unknown keys: {', '.join(sorted(unknown))}")
    missing = [k for k in ("file", "title") if k not in table]
    if missing:
        raise ConfigError(f"{where}: missing {', '.join(missing)}")
    if not isinstance(table["title"], str) or not table["title"].strip():
        raise ConfigError(f"{where}: title must be a non-empty string")
    if ("source" in table) == ("sources" in table):
        raise ConfigError(f"{where}: needs either source (one URL) or sources (a list of URLs)")
    if "source" in table:
        sources = [table["source"]]
        if not _web_url(table["source"]):
            raise ConfigError(f"{where}: source must be an http or https URL")
    else:
        sources = table["sources"]
        if not isinstance(sources, list) or not sources or not all(_web_url(s) for s in sources):
            raise ConfigError(f"{where}: sources must be a non-empty list of http or https URLs")
        if len(set(sources)) < len(sources):
            raise ConfigError(f"{where}: sources lists a URL twice")
    options: dict = {}
    for key in RSS_WORDS:
        if key in table:
            options[key] = frozenset(v.strip().casefold() for v in _strings(where, key, table[key]))
    for key in RSS_PATTERNS:
        if key in table:
            options[key] = _patterns(where, key, table[key])
    for key in RSS_FLAGS:
        if key in table:
            if not isinstance(table[key], bool):
                raise ConfigError(f"{where}: {key} must be true or false")
            options[key] = table[key]
    if options.get("exclude_paywalled") and not options.get("enrich", True):
        raise ConfigError(f"{where}: exclude_paywalled needs enrich: the article pages say what's paywalled")
    for key, high in (("max_items", MAX_RSS_ITEMS), ("keep_days", MAX_KEEP_DAYS)):
        if key in table:
            value = table[key]
            if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= high:
                raise ConfigError(f"{where}: {key} must be a whole number from 1 to {high}")
            options[key] = value
    if "dedupe" in table:
        if table["dedupe"] not in DEDUPE:
            raise ConfigError(f"{where}: dedupe must be one of: {', '.join(repr(d) for d in DEDUPE)}")
        options["dedupe"] = table["dedupe"]
    if "json" in table:
        options["json"] = _parse_json_fields(where, table["json"])
    if "source_titles" in table:
        labels = table["source_titles"]
        if not isinstance(labels, dict) or not all(isinstance(v, str) and v.strip() for v in labels.values()):
            raise ConfigError(f'{where}: source_titles must be a table of source URL = "label"')
        stray = [url for url in labels if url not in sources]
        if stray:
            raise ConfigError(f"{where}: source_titles: {stray[0]} is not one of the sources")
        options["source_titles"] = tuple((url, label.strip()) for url, label in labels.items())
    _check_urls(where, table)
    for key in URLS:
        if key in table:
            options[key] = table[key]
    _check_file(where, table["file"], used)
    return Rss(file=table["file"], title=table["title"], sources=tuple(sources), **options)


JSON_KNOWN = {f.name for f in fields(JsonFields)}
JSON_REQUIRED = ("items", "title", "link")


def _parse_json_fields(where: str, value: object) -> JsonFields:
    if not isinstance(value, dict):
        raise ConfigError(f'{where}: json must be a table such as {{ items = "posts", title = "title", link = "url" }}')
    unknown = set(value) - JSON_KNOWN
    if unknown:
        raise ConfigError(f"{where}: json: unknown keys: {', '.join(sorted(unknown))}")
    missing = [k for k in JSON_REQUIRED if k not in value]
    if missing:
        raise ConfigError(f"{where}: json: missing {', '.join(missing)}")
    for key, path in value.items():
        if not isinstance(path, str) or not path.strip():
            raise ConfigError(f"{where}: json: {key} must be a non-empty string")
    if "date_format" in value and "%" not in value["date_format"]:
        raise ConfigError(f'{where}: json: date_format must be a strptime pattern such as "%B %d, %Y"')
    return JsonFields(**value)


def parse(text: str) -> Config:
    """The feeds a feeds.toml declares. Raises ConfigError on anything it doesn't expect."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"not valid TOML: {e}") from e
    unknown = set(data) - {"feed", "digest", "rss"}
    if unknown:
        raise ConfigError(f"unknown top-level keys: {', '.join(sorted(unknown))}")
    tables = data.get("feed", [])
    digest_tables = data.get("digest", [])
    rss_tables = data.get("rss", [])
    lists = (tables, digest_tables, rss_tables)
    if not all(isinstance(t, list) for t in lists) or not any(lists):
        raise ConfigError("no [[feed]], [[digest]] or [[rss]] tables")

    feeds: list[Feed] = []
    used: list[str] = []
    for n, table in enumerate(tables, 1):
        where = f"feed #{n}"
        unknown = set(table) - KNOWN
        if unknown:
            raise ConfigError(f"{where}: unknown keys: {', '.join(sorted(unknown))}")
        missing = [k for k in REQUIRED if k not in table]
        if missing:
            raise ConfigError(f"{where}: missing {', '.join(missing)}")
        for key in REQUIRED:
            if not isinstance(table[key], str) or not table[key].strip():
                raise ConfigError(f"{where}: {key} must be a non-empty string")
        for key in NUMBERS:
            value = table.get(key)
            # bool is an int subclass: reject it explicitly.
            if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
                raise ConfigError(f"{where}: {key} must be a whole number of at least 0")
        for key in FLAGS:
            if key in table and not isinstance(table[key], bool):
                raise ConfigError(f"{where}: {key} must be true or false")
        _check_urls(where, table)
        if "exclude_titles" in table:
            table = {**table, "exclude_titles": _patterns(where, "exclude_titles", table["exclude_titles"])}
        feed = Feed(**table)
        _check_file(where, feed.file, used)
        if feed.window_hours < 1 or feed.max_items < 1:
            raise ConfigError(f"{where}: window_hours and max_items must be at least 1")
        feeds.append(feed)
        used.append(feed.file)
    digests = []
    for n, table in enumerate(digest_tables, 1):
        digest = _parse_digest(f"digest #{n}", table, used)
        digests.append(digest)
        used.append(digest.file)
    rss = []
    for n, table in enumerate(rss_tables, 1):
        rss_feed = _parse_rss(f"rss #{n}", table, used)
        rss.append(rss_feed)
        used.append(rss_feed.file)
    return Config(feeds, digests, rss)


def load(path: Path) -> Config:
    return parse(path.read_text(encoding="utf-8"))

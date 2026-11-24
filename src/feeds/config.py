"""feeds.toml: which feeds to publish."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

from . import STATE_NAME

FILE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*\.xml$")


class ConfigError(ValueError):
    pass


DEDUPE = ("link", "link+title")


@dataclass(frozen=True)
class Feed:
    file: str
    title: str
    tags: str
    min_points: int = 0
    min_comments: int = 0
    window_hours: int = 48
    max_items: int = 150
    ranked: bool = False  # only stories in HN's current ranking (topstories)

    @property
    def follows_front_page(self) -> bool:
        return "front_page" in self.tags.split(",")


@dataclass(frozen=True)
class Digest:
    """One item per UTC day with that day's top stories."""

    file: str
    title: str
    top: int = 20  # stories per day
    days: int = 7  # days kept
    publish_hour: int = 6  # UTC hour of the next day when a day's item appears


@dataclass(frozen=True)
class Rss:
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
    enrich: bool = True  # fetch each article's preview for what the source lacks
    max_items: int = 100
    dedupe: str = "link"  # or "link+title"


@dataclass(frozen=True)
class Config:
    feeds: list[Feed]
    digests: list[Digest]
    rss: list[Rss] = field(default_factory=list)

    @property
    def files(self) -> list[str]:
        return [f.file for f in self.feeds] + [d.file for d in self.digests] + [r.file for r in self.rss]


REQUIRED = ("file", "title", "tags")
FLAGS = ("ranked",)
KNOWN = {f.name for f in fields(Feed)}
NUMBERS = KNOWN - set(REQUIRED) - set(FLAGS)
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
    _check_file(where, table["file"], used)
    return Digest(**table)


RSS_KNOWN = {f.name for f in fields(Rss)} | {"source"}  # source: one URL, sources: several
RSS_WORDS = ("include_categories", "exclude_categories", "include_sections", "exclude_sections", "exclude_authors")
RSS_PATTERNS = ("include_titles", "exclude_titles", "include_links", "exclude_links")
MAX_RSS_ITEMS = 1000


def _web_url(value: object) -> bool:
    return isinstance(value, str) and value.startswith(("http://", "https://")) and len(value) > len("https://")


def _strings(where: str, key: str, value: object) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise ConfigError(f"{where}: {key} must be a list of non-empty strings")
    return value


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
            patterns = []
            for pattern in _strings(where, key, table[key]):
                try:
                    patterns.append(re.compile(pattern))
                except re.error as e:
                    raise ConfigError(f"{where}: {key}: {pattern!r} is not a valid regular expression: {e}") from e
            options[key] = tuple(patterns)
    if "enrich" in table:
        if not isinstance(table["enrich"], bool):
            raise ConfigError(f"{where}: enrich must be true or false")
        options["enrich"] = table["enrich"]
    if "max_items" in table:
        value = table["max_items"]
        if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= MAX_RSS_ITEMS:
            raise ConfigError(f"{where}: max_items must be a whole number from 1 to {MAX_RSS_ITEMS}")
        options["max_items"] = value
    if "dedupe" in table:
        if table["dedupe"] not in DEDUPE:
            raise ConfigError(f"{where}: dedupe must be one of: {', '.join(repr(d) for d in DEDUPE)}")
        options["dedupe"] = table["dedupe"]
    _check_file(where, table["file"], used)
    return Rss(file=table["file"], title=table["title"], sources=tuple(sources), **options)


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

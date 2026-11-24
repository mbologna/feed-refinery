"""What the previous run published: each story or item with its preview, and when each feed first saw it.

The state travels with the site. Each run reads state.json from the published
site and deploys the updated copy next to the feeds, so it needs no commits
and no cache. Losing it only costs fetching the previews again.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
import urllib.error
import urllib.request
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import USER_AGENT
from .comments import Comment
from .config import Digest, Feed, Rss
from .preview import Preview
from .rss import Fetched, Item, keep, select
from .source import Story

VERSION = 2  # 2 added the rss section; a version 1 file loads with it empty
READABLE = (1, 2)
MAX_ATTEMPTS = 3  # preview fetches per story before giving up on it
COMMENT_INTERVAL = 2 * 3600  # how often a listed story's top comment is looked up again
RETRY_STATUSES = {408, 429}  # client errors worth another try; other 4xx won't change
RSS_KEEP = 7 * 86400  # how long an item no source lists any more stays in its feed


@dataclass
class Entry:
    story: Story
    preview: Preview | None = None  # None until a fetch succeeds
    attempts: int = 0
    comment: Comment | None = None  # the top comment when last looked up
    comment_checked: int = 0  # Unix time of that lookup; 0 = never

    @property
    def wants_preview(self) -> bool:
        return self.preview is None and self.attempts < MAX_ATTEMPTS


@dataclass
class Source:
    """One RSS source of a feed: its validators for the next conditional GET, and when it last answered."""

    etag: str | None = None
    last_modified: str | None = None
    last_ok: int | None = None  # Unix time of the last 200 or 304; None = never


@dataclass
class RssEntry:
    item: Item  # the kept copy, with the categories of every copy
    first_seen: int  # Unix time the feed first listed the story
    preview: Preview | None = None  # None until a fetch succeeds, or while the item needs none
    attempts: int = 0
    from_sources: list[str] = field(default_factory=list)  # the sources that list it, in the config's order

    @property
    def sort_key(self) -> tuple[int, int]:
        return (self.item.published or self.first_seen, self.first_seen)

    @property
    def image(self) -> str | None:
        return self.item.image or (self.preview.image if self.preview else None)

    @property
    def excerpt(self) -> str | None:
        return self.item.summary or (self.preview.description if self.preview else None)


@dataclass
class RssFeed:
    sources: dict[str, Source] = field(default_factory=dict)
    items: dict[str, RssEntry] = field(default_factory=dict)  # dedup key -> entry, newest first

    def last_ok(self, feed: Rss) -> int | None:
        """The oldest last_ok of the feed's sources: the feed is as fresh as its stalest source."""
        times = [self.sources[s].last_ok if s in self.sources else None for s in feed.sources]
        return None if None in times else min(t for t in times if t is not None)


@dataclass
class State:
    stories: dict[str, Entry] = field(default_factory=dict)
    # Feed file -> story id -> Unix time the feed first listed the story.
    feeds: dict[str, dict[str, int]] = field(default_factory=dict)
    # Digest file -> UTC day (YYYY-MM-DD) -> its top story ids, frozen once published.
    digests: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    # RSS feed file -> its sources and items.
    rss: dict[str, RssFeed] = field(default_factory=dict)

    def to_json(self) -> bytes:
        data = {
            "version": VERSION,
            "stories": {
                sid: {
                    "story": asdict(e.story),
                    "preview": asdict(e.preview) if e.preview else None,
                    "attempts": e.attempts,
                    "comment": asdict(e.comment) if e.comment else None,
                    "comment_checked": e.comment_checked,
                }
                for sid, e in self.stories.items()
            },
            "feeds": self.feeds,
            "digests": self.digests,
            "rss": {
                file: {
                    "sources": {url: asdict(s) for url, s in feed.sources.items()},
                    "items": {
                        key: {
                            "item": asdict(e.item),
                            "first_seen": e.first_seen,
                            "preview": asdict(e.preview) if e.preview else None,
                            "attempts": e.attempts,
                            "from_sources": e.from_sources,
                        }
                        for key, e in feed.items.items()
                    },
                }
                for file, feed in self.rss.items()
            },
        }
        return json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    @classmethod
    def from_json(cls, raw: bytes) -> State:
        """Raises ValueError on anything that isn't a state file of a version this code reads."""
        try:
            data = json.loads(raw)
            if data.get("version") not in READABLE:
                raise ValueError(f"state version {data.get('version')!r}, expected one of {READABLE}")
            stories = {
                sid: Entry(
                    story=Story(**e["story"]),
                    preview=Preview(**e["preview"]) if e["preview"] is not None else None,
                    attempts=int(e["attempts"]),
                    # Added after the first release: absent from older state files.
                    comment=Comment(**e["comment"]) if e.get("comment") else None,
                    comment_checked=int(e.get("comment_checked", 0)),
                )
                for sid, e in data["stories"].items()
            }
            feeds = {f: {sid: int(t) for sid, t in seen.items()} for f, seen in data["feeds"].items()}
            # Added with the digests: absent from older state files.
            digests = {
                f: {day: [str(sid) for sid in ids] for day, ids in days.items()}
                for f, days in data.get("digests", {}).items()
            }
            # Version 2: absent from version 1 files, which had no RSS feeds.
            rss = {file: _rss_feed(feed) for file, feed in data.get("rss", {}).items()}
        except (AttributeError, KeyError, TypeError) as e:
            raise ValueError(f"malformed state: {e!r}") from e
        return cls(stories, feeds, digests, rss)


def _rss_feed(data: dict) -> RssFeed:
    sources = {url: Source(**s) for url, s in data["sources"].items()}
    items = {}
    for key, e in data["items"].items():
        fields = e["item"]
        items[key] = RssEntry(
            item=Item(**{**fields, "categories": tuple(fields["categories"])}),
            first_seen=int(e["first_seen"]),
            preview=Preview(**e["preview"]) if e["preview"] is not None else None,
            attempts=int(e["attempts"]),
            from_sources=[str(s) for s in e["from_sources"]],
        )
    return RssFeed(sources, items)


def load(source: str) -> State:
    """The state at a URL or path; empty when source is empty, missing or unreadable."""
    if not source:
        return State()
    try:
        if source.startswith(("http://", "https://")):
            req = urllib.request.Request(source, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=30) as r:  # nosec B310 # http(s) only, checked above
                raw = r.read()
        else:
            raw = Path(source).read_bytes()
        return State.from_json(raw)
    except (OSError, ValueError) as e:
        print(f"previous state {source}: {e}; starting empty", file=sys.stderr)
        return State()


def merge_story(state: State, story: Story) -> None:
    """Add a story, or refresh the one the state has."""
    entry = state.stories.get(story.id)
    if entry is None:
        state.stories[story.id] = Entry(story)
        return
    if entry.story.url != story.url:  # the link was edited: its preview no longer applies
        entry.preview, entry.attempts = None, 0
    entry.story = story


def utc_day(timestamp: int) -> dt.date:
    return dt.datetime.fromtimestamp(timestamp, dt.UTC).date()


def publish_time(digest: Digest, day: dt.date) -> int:
    """When a digest publishes a day: publish_hour (UTC) on the next day."""
    moment = dt.datetime.combine(day + dt.timedelta(days=1), dt.time(digest.publish_hour), dt.UTC)
    return int(moment.timestamp())


def digest_days_due(state: State, digest: Digest, now: int) -> list[dt.date]:
    """The days within the digest's window that are due but not yet published, oldest first."""
    published = state.digests.get(digest.file, {})
    today = utc_day(now)
    days = [today - dt.timedelta(days=n) for n in range(digest.days, 0, -1)]
    return [d for d in days if d.isoformat() not in published and publish_time(digest, d) <= now]


def add_digest_day(state: State, digest: Digest, day: dt.date, stories: list[Story]) -> None:
    """Freeze the day's top stories by points, and add them to the state."""
    top = sorted(stories, key=lambda s: s.points, reverse=True)[: digest.top]
    for story in top:
        merge_story(state, story)
    state.digests.setdefault(digest.file, {})[day.isoformat()] = [s.id for s in top]


def update(
    state: State, feeds: list[Feed], results: dict[str, list[Story]], now: int, digests: Sequence[Digest] = ()
) -> None:
    """Merge this run's stories into the state, then drop what no feed or digest keeps."""
    for feed in feeds:
        cutoff = now - feed.window_hours * 3600
        # A feed with no history (new, or the state was lost) dates its first
        # batch by when each story was posted, so the batch doesn't all share
        # one timestamp. Readers match items by guid, so re-dating stories
        # they already have doesn't make them new again.
        fresh = feed.file not in state.feeds
        seen = state.feeds.setdefault(feed.file, {})
        for story in results[feed.file]:
            merge_story(state, story)
            seen.setdefault(story.id, min(now, story.created) if fresh else now)
        # A story the source still returns stays, however old: Algolia's
        # front_page tag can list stories posted days ago, and dropping one
        # while it is still listed would add it back on the next run. The
        # window times out only the stories that no longer match.
        current = {story.id for story in results[feed.file]}
        kept = [sid for sid, t in seen.items() if sid in state.stories and (sid in current or t >= cutoff)]
        # The cap keeps the listed stories first, for the same reason. A query
        # asks for at most max_items hits, so they always fit.
        kept.sort(key=lambda sid: (sid in current, seen[sid]), reverse=True)
        kept = kept[: feed.max_items]
        kept.sort(key=lambda sid: (seen[sid], state.stories[sid].story.created), reverse=True)
        state.feeds[feed.file] = {sid: seen[sid] for sid in kept}

    configured = {f.file for f in feeds}
    state.feeds = {f: seen for f, seen in state.feeds.items() if f in configured}
    today = utc_day(now)
    kept_days = {}
    for digest in digests:
        oldest = (today - dt.timedelta(days=digest.days)).isoformat()
        days = state.digests.get(digest.file, {})
        kept_days[digest.file] = {day: ids for day, ids in days.items() if day >= oldest}
    state.digests = kept_days
    listed = {sid for seen in state.feeds.values() for sid in seen}
    listed |= {sid for days in state.digests.values() for ids in days.values() for sid in ids}
    state.stories = {sid: e for sid, e in state.stories.items() if sid in listed}


def update_rss(
    state: State, feeds: Sequence[Rss], results: dict[str, dict[str, Fetched | Exception]], now: int
) -> None:
    """Merge this run's fetches into each RSS feed's items, then drop what no feed keeps.

    results maps each feed file to its sources' outcomes: a 200 replaces what
    the state knows the source lists, a 304 keeps it, and an exception keeps
    it too while the source's last_ok ages.
    """
    kept: dict[str, RssFeed] = {}
    for feed in feeds:
        current = state.rss.get(feed.file) or RssFeed()
        outcomes = results.get(feed.file, {})
        sources = {url: current.sources.get(url) or Source() for url in feed.sources}
        listed: list[tuple[str, list[Item]]] = []  # the sources that answered 200, in the config's order
        for url, source in sources.items():
            outcome = outcomes.get(url)
            if isinstance(outcome, Fetched):
                source.last_ok = now
                if outcome.status != 304:
                    source.etag, source.last_modified = outcome.etag, outcome.last_modified
                    listed.append((url, outcome.items))
        refreshed = {url for url, _ in listed}
        items = current.items
        for entry in items.values():
            entry.from_sources = [s for s in entry.from_sources if s in sources and s not in refreshed]
        _merge(feed, items, listed, now)
        cutoff = now - RSS_KEEP
        alive = [(k, e) for k, e in items.items() if keep(e.item, feed) and (e.from_sources or e.first_seen >= cutoff)]
        alive.sort(key=lambda ke: ke[1].sort_key, reverse=True)
        kept[feed.file] = RssFeed(sources, dict(alive[: feed.max_items]))
    state.rss = kept


def _merge(feed: Rss, items: dict[str, RssEntry], listed: list[tuple[str, list[Item]]], now: int) -> None:
    """Add the sources' items that pass the filters to items, one entry per story (see rss.select)."""
    chosen = select(feed, listed, {k: e.item for k, e in items.items()})
    order = {url: n for n, url in enumerate(feed.sources)}
    known = dict(items)
    items.clear()
    for key, item in chosen.items.items():
        entry = known[chosen.origin[key]] if key in chosen.origin else RssEntry(item, now)
        entry.item = item
        if key in chosen.moved:
            entry.preview, entry.attempts = None, 0
        if key in chosen.sources:
            entry.from_sources = sorted({*entry.from_sources, *chosen.sources[key]}, key=order.__getitem__)
        items[key] = entry


def _fetch_all(todo: dict[str, list], fetch, workers: int) -> tuple[int, list[tuple[str, str]]]:
    """Fetch each URL's preview once, for every entry (story or RSS item) that wants it."""
    fetched, failed = 0, []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fetch, url): url for url in todo}
        for future in as_completed(futures):
            url = futures[future]
            entries = todo[url]
            try:
                result = future.result()
            # Any error, not only network ones: one odd page must not break
            # every feed. The item keeps its title and links either way.
            except Exception as e:
                permanent = (
                    isinstance(e, urllib.error.HTTPError) and 400 <= e.code < 500 and e.code not in RETRY_STATUSES
                )
                for entry in entries:
                    entry.attempts = MAX_ATTEMPTS if permanent else entry.attempts + 1
                failed.append((url, f"{type(e).__name__}: {e}"))
                continue
            for entry in entries:
                entry.preview = result
            fetched += 1
    return fetched, failed


def fetch_previews(state: State, fetch, workers: int = 8) -> tuple[int, list[tuple[str, str]]]:
    """Fetch the stories' previews still missing. Returns how many succeeded and the (url, error) failures."""
    todo: dict[str, list[Entry]] = {}
    for entry in state.stories.values():
        if not entry.wants_preview:
            continue
        if entry.story.url:
            todo.setdefault(entry.story.url, []).append(entry)
        else:
            entry.preview = Preview()  # nothing to fetch
    return _fetch_all(todo, fetch, workers)


def fetch_rss_previews(
    state: State, feeds: Sequence[Rss], fetch, workers: int = 8
) -> tuple[int, list[tuple[str, str]]]:
    """Fetch the previews of the enriched feeds' items that lack an image or an excerpt, with the stories' retry rules."""
    todo: dict[str, list[RssEntry]] = {}
    for feed in feeds:
        if not feed.enrich:
            continue
        for entry in state.rss.get(feed.file, RssFeed()).items.values():
            if entry.preview is not None or entry.attempts >= MAX_ATTEMPTS:
                continue
            if entry.item.link and not (entry.item.image and entry.item.summary):
                todo.setdefault(entry.item.link, []).append(entry)
            else:
                entry.preview = Preview()  # nothing to fetch, or nothing missing
    return _fetch_all(todo, fetch, workers)


def fetch_comments(
    state: State, fetch, listed: set[str], now: int, workers: int = 8
) -> tuple[int, list[tuple[str, str]]]:
    """Look up the top comment of the listed stories that have comments, at most every COMMENT_INTERVAL.

    Only stories a feed still lists: the top comment of one that has dropped
    off changes little. A failed lookup is retried on the next run.
    """
    todo = [
        e
        for sid, e in state.stories.items()
        if sid in listed and e.story.comments and now - e.comment_checked >= COMMENT_INTERVAL
    ]
    fetched, failed = 0, []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fetch, e.story.id): e for e in todo}
        for future in as_completed(futures):
            entry = futures[future]
            try:
                entry.comment = future.result()
            except Exception as e:  # as in fetch_previews: one bad item must not break the feeds
                failed.append((entry.story.comments_url, f"{type(e).__name__}: {e}"))
                continue
            entry.comment_checked = now
            fetched += 1
    return fetched, failed

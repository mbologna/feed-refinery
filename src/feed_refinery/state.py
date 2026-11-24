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
from .rss import Fetched, Item, Selection, canonical_link, dedup_key, keep, select
from .source import Story

# 2 added the rss section; a version 1 file loads with it empty. 3 added the
# items' hn_points and hn_checked, the previews' paywalled and the feeds'
# filters; a version 2 file loads with them unknown. 4 added the items' and
# the previews' words; a version 3 file loads with them unknown.
VERSION = 4
READABLE = (1, 2, 3, 4)
MAX_ATTEMPTS = 3  # preview fetches per story before giving up on it
COMMENT_INTERVAL = 2 * 3600  # how often a listed story's top comment is looked up again
RETRY_STATUSES = {408, 429}  # client errors worth another try; other 4xx won't change
HN_RECHECK = 6 * 3600  # how often a new item's HN points are looked up again
HN_RECHECK_FOR = 2 * 86400  # for how long after an item first appears: an HN story's points settle in a day
TEASER = 500  # characters: a source's text shorter than this is a teaser, and the article's page may say more


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
    hn_points: int | None = None  # the most points an HN story with this link had; None until looked up
    hn_checked: int = 0  # Unix time of that lookup; 0 = never

    @property
    def sort_key(self) -> tuple[int, int]:
        return (self.item.published or self.first_seen, self.first_seen)

    @property
    def image(self) -> str | None:
        return self.item.image or (self.preview.image if self.preview else None)

    @property
    def words(self) -> int | None:
        return self.item.words or (self.preview.words if self.preview else None)

    @property
    def excerpt(self) -> str | None:
        """The longer of the source's text and the page's; the source's on a tie."""
        texts = [t for t in (self.item.summary, self.preview.description if self.preview else None) if t]
        return max(texts, key=len, default=None)


@dataclass
class RssFeed:
    sources: dict[str, Source] = field(default_factory=dict)
    items: dict[str, RssEntry] = field(default_factory=dict)  # dedup key -> entry, newest first
    # The feed's Rss.filters_key when it was built: a different one means a
    # changed filter, and the sources are fetched without validators so every
    # item they list is judged again.
    filters: str = ""

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
                            "item": {k: v for k, v in asdict(e.item).items() if k != "text"},
                            "first_seen": e.first_seen,
                            "preview": asdict(e.preview) if e.preview else None,
                            "attempts": e.attempts,
                            "from_sources": e.from_sources,
                            "hn_points": e.hn_points,
                            "hn_checked": e.hn_checked,
                        }
                        for key, e in feed.items.items()
                    },
                    "filters": feed.filters,
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
            # Version 3: absent from version 2 files.
            hn_points=None if e.get("hn_points") is None else int(e["hn_points"]),
            hn_checked=int(e.get("hn_checked", 0)),
        )
    return RssFeed(sources, items, str(data.get("filters", "")))  # filters: version 3


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
        kept = [
            sid
            for sid, t in seen.items()
            if sid in state.stories
            and (sid in current or t >= cutoff)
            # A pattern added later applies to the stories the feed already holds.
            and not any(p.search(state.stories[sid].story.title) for p in feed.exclude_titles)
        ]
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
        chosen = _merge(feed, items, listed, now)
        # A story a source lists again but whose fresh copy a filter drops
        # leaves now: the fresh copy has the text exclude_text searches, which
        # the stored one lacks, and a changed filter applies at once.
        passed = {c.key for c in chosen.choices if c.drop is None}
        dropped = {dedup_key(c.item) for c in chosen.choices if c.drop is not None} - passed
        cutoff = now - feed.keep_days * 86400
        alive = [
            (k, e)
            for k, e in items.items()
            if k not in dropped and keep(e.item, feed) and (e.from_sources or e.first_seen >= cutoff)
        ]
        alive.sort(key=lambda ke: ke[1].sort_key, reverse=True)
        kept[feed.file] = RssFeed(sources, dict(alive[: feed.max_items]), feed.filters_key)
    state.rss = kept


def _merge(feed: Rss, items: dict[str, RssEntry], listed: list[tuple[str, list[Item]]], now: int) -> Selection:
    """Add the sources' items that pass the filters to items, one entry per story (see rss.select). Returns the selection."""
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
    return chosen


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
    """Fetch the previews of the enriched feeds' items that lack an image or have only a teaser, with the stories' retry rules.

    With exclude_paywalled every item's page is fetched: it says whether the item is paywalled.
    """
    todo: dict[str, list[RssEntry]] = {}
    for feed in feeds:
        if not feed.enrich:
            continue
        for entry in state.rss.get(feed.file, RssFeed()).items.values():
            if entry.preview is not None or entry.attempts >= MAX_ATTEMPTS:
                continue
            if entry.item.link and (feed.exclude_paywalled or not entry.item.image or len(entry.item.summary) < TEASER):
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


def _hn_form(url: str) -> str:
    """The form an HN story's link and an item's link share: canonical, without the scheme or www."""
    return canonical_link(url).split("://", 1)[-1].removeprefix("www.")


def check_hacker_news(
    state: State, feeds: Sequence[Rss], lookup, floor: int, now: int, workers: int = 8
) -> tuple[int, list[tuple[str, str]]]:
    """Record which items of the exclude_on_hacker_news feeds were in the [[feed]] tables' stories.

    An item whose link is a story the state holds (the front page, best, top)
    gets floor points at once. The others are looked up with lookup(link,
    same), which returns the most points an HN story with that link had: when
    an item first appears, then every HN_RECHECK for HN_RECHECK_FOR, as a
    story's points still grow on its first day. A failed lookup is retried on
    the next run and leaves the item shown. Returns how many lookups succeeded
    and the (link, error) failures.
    """
    stories = {_hn_form(e.story.url) for e in state.stories.values() if e.story.url}
    todo: list[tuple[str, RssEntry]] = []
    for feed in feeds:
        if not feed.exclude_on_hacker_news:
            continue
        for entry in state.rss.get(feed.file, RssFeed()).items.values():
            link = entry.item.link
            if not link:
                continue
            if _hn_form(link) in stories:
                entry.hn_points = max(entry.hn_points or 0, floor)
                entry.hn_checked = now
            elif not entry.hn_checked or (
                now - entry.first_seen < HN_RECHECK_FOR and now - entry.hn_checked >= HN_RECHECK
            ):
                todo.append((link, entry))
    looked_up, failed = 0, []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(lookup, link, _hn_form): (link, entry) for link, entry in todo}
        for future in as_completed(futures):
            link, entry = futures[future]
            try:
                points = future.result()
            except Exception as e:  # as in fetch_previews: one bad lookup must not break the feeds
                failed.append((link, f"{type(e).__name__}: {e}"))
                continue
            entry.hn_points = max(entry.hn_points or 0, points)
            entry.hn_checked = now
            looked_up += 1
    return looked_up, failed


def held_back(entry: Entry | RssEntry) -> bool:
    """Whether exclude_paywalled hides a story or an item: its page says it's paywalled, or hasn't been read yet.

    Holding back an unread page means a paywalled story never shows for a run;
    after MAX_ATTEMPTS failed fetches it shows.
    """
    if entry.preview is None:
        return entry.attempts < MAX_ATTEMPTS
    return bool(entry.preview.paywalled)


def visible(feed: Rss, entry: RssEntry, floor: int) -> bool:
    """Whether a feed shows an item it holds.

    exclude_paywalled hides the items held_back. exclude_on_hacker_news hides
    an item an HN story with its link gave floor points or more.
    """
    if feed.exclude_paywalled and held_back(entry):
        return False
    return not (feed.exclude_on_hacker_news and (entry.hn_points or 0) >= floor)

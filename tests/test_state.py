import datetime as dt
import urllib.error

import pytest
from conftest import FakeResponse

from feeds.comments import Comment
from feeds.config import Digest, Feed, Rss
from feeds.preview import Preview
from feeds.rss import Fetched, Item
from feeds.source import Story
from feeds.state import (
    COMMENT_INTERVAL,
    MAX_ATTEMPTS,
    RSS_KEEP,
    Entry,
    RssEntry,
    RssFeed,
    Source,
    State,
    add_digest_day,
    digest_days_due,
    fetch_comments,
    fetch_previews,
    fetch_rss_previews,
    load,
    publish_time,
    update,
    update_rss,
)

HOUR = 3600
NOW = 1_800_000_000
FRONT = Feed("frontpage.xml", "Front", "front_page")
BEST = Feed("best.xml", "Best", "story", min_points=100)


def story(id, points=10, created=NOW - HOUR, url="", **kw):
    return Story(
        id=str(id),
        title=f"Story {id}",
        url=f"https://example.com/{id}" if url == "" else url,
        points=points,
        comments=1,
        created=created,
        author="a",
        text="",
        **kw,
    )


def test_a_new_feed_dates_its_first_batch_by_post_time():
    state = State()
    update(state, [FRONT], {"frontpage.xml": [story(1, created=NOW - 2 * HOUR), story(2, created=NOW - HOUR)]}, NOW)
    assert state.feeds["frontpage.xml"] == {"2": NOW - HOUR, "1": NOW - 2 * HOUR}


def test_first_batch_dates_are_never_in_the_future():
    state = State()
    update(state, [FRONT], {"frontpage.xml": [story(1, created=NOW + 60)]}, NOW)
    assert state.feeds["frontpage.xml"] == {"1": NOW}


def test_a_listed_story_older_than_the_window_stays_until_it_leaves():
    # Algolia's front_page tag can list stories posted days ago.
    state = State()
    feed = Feed("f.xml", "F", "front_page", window_hours=1)
    old = story(1, created=NOW - 50 * HOUR)
    update(state, [feed], {"f.xml": [old]}, NOW)
    update(state, [feed], {"f.xml": [old]}, NOW + HOUR)
    assert state.feeds["f.xml"] == {"1": NOW - 50 * HOUR}  # kept, and its date doesn't move
    update(state, [feed], {"f.xml": []}, NOW + 2 * HOUR)
    assert state.feeds["f.xml"] == {}


def test_later_stories_are_dated_when_the_feed_first_lists_them():
    state = State()
    update(state, [FRONT], {"frontpage.xml": [story(1)]}, NOW)
    update(state, [FRONT], {"frontpage.xml": [story(1), story(2, created=NOW - 5 * HOUR)]}, NOW + HOUR)
    assert state.feeds["frontpage.xml"] == {"2": NOW + HOUR, "1": NOW - HOUR}


def test_each_feed_keeps_its_own_first_seen_time():
    state = State()
    update(state, [FRONT, BEST], {"frontpage.xml": [story(1, points=40)], "best.xml": [story(9)]}, NOW)
    # One hour later story 1 crosses 100 points: news to best.xml now, not when it hit the front page.
    update(
        state, [FRONT, BEST], {"frontpage.xml": [story(1, points=120)], "best.xml": [story(1, points=120)]}, NOW + HOUR
    )
    assert state.feeds["frontpage.xml"]["1"] == NOW - HOUR
    assert state.feeds["best.xml"]["1"] == NOW + HOUR
    assert state.stories["1"].story.points == 120


def test_stories_leave_after_the_window_and_unlisted_stories_are_dropped():
    state = State()
    feed = Feed("f.xml", "F", "front_page", window_hours=2)
    update(state, [feed], {"f.xml": [story(1)]}, NOW)
    update(state, [feed], {"f.xml": [story(2)]}, NOW + HOUR)
    update(state, [feed], {"f.xml": []}, NOW + 3 * HOUR)
    assert list(state.feeds["f.xml"]) == ["2"]
    assert list(state.stories) == ["2"]


def test_max_items_keeps_listed_stories_before_newer_unlisted_ones():
    state = State()
    feed = Feed("f.xml", "F", "front_page", max_items=2)
    update(state, [feed], {"f.xml": [story(1, created=NOW - 3 * HOUR)]}, NOW)
    update(state, [feed], {"f.xml": [story(2)]}, NOW + HOUR)  # 1 left the list
    update(state, [feed], {"f.xml": [story(1, created=NOW - 3 * HOUR), story(3)]}, NOW + 2 * HOUR)  # 1 is back
    assert list(state.feeds["f.xml"]) == ["3", "1"]  # 2 is newer than 1 but no longer listed
    assert state.feeds["f.xml"]["1"] == NOW - 3 * HOUR


def test_max_items_keeps_the_newest():
    state = State()
    feed = Feed("f.xml", "F", "story", max_items=2)
    update(state, [feed], {"f.xml": [story(i, created=NOW - i * HOUR) for i in (1, 2, 3)]}, NOW)
    assert list(state.feeds["f.xml"]) == ["1", "2"]
    assert set(state.stories) == {"1", "2"}


def test_removed_feeds_are_forgotten():
    state = State()
    update(state, [FRONT, BEST], {"frontpage.xml": [story(1)], "best.xml": [story(2)]}, NOW)
    update(state, [FRONT], {"frontpage.xml": [story(1)]}, NOW + HOUR)
    assert list(state.feeds) == ["frontpage.xml"]
    assert list(state.stories) == ["1"]


def test_an_edited_link_drops_the_old_preview():
    state = State()
    update(state, [FRONT], {"frontpage.xml": [story(1)]}, NOW)
    state.stories["1"].preview = Preview(site_name="Old")
    update(state, [FRONT], {"frontpage.xml": [story(1, url="https://other.example/")]}, NOW + HOUR)
    assert state.stories["1"].preview is None
    update(state, [FRONT], {"frontpage.xml": [story(1, url="https://other.example/", points=99)]}, NOW + 2 * HOUR)
    assert state.stories["1"].story.points == 99


def test_round_trip():
    state = State(
        stories={
            "1": Entry(story(1), Preview("https://i.example/a.png", "desc", "Site"), 0),
            "2": Entry(story(2, url=None), None, 2),
            "3": Entry(story(3), Preview(), 0, Comment("30", "bob", "nice"), NOW),
        },
        feeds={"frontpage.xml": {"1": NOW, "2": NOW - HOUR}},
    )
    assert State.from_json(state.to_json()) == state


@pytest.mark.parametrize(
    "raw",
    [
        b"not json",
        b"[]",
        b'{"version": 99}',
        b'{"version": 1}',
        b'{"version": 2, "stories": {}, "feeds": {}, "rss": {"r.xml": {"sources": {}}}}',
        b'{"version": 2, "stories": {}, "feeds": {}, "rss": {"r.xml": {"sources": {"u": {"etag": 1, "x": 2}}, "items": {}}}}',
        b'{"version": 1, "stories": {"1": {"story": {}}}, "feeds": {}}',
    ],
)
def test_malformed_state_is_rejected(raw):
    with pytest.raises(ValueError):
        State.from_json(raw)


def test_load_from_a_path_url_or_nothing(tmp_path, web, capsys):
    good = State(feeds={"f.xml": {}})
    path = tmp_path / "state.json"
    path.write_bytes(good.to_json())
    assert load(str(path)) == good

    web.routes["https://site.example/state.json"] = lambda: FakeResponse(good.to_json())
    assert load("https://site.example/state.json") == good

    assert load("") == State()
    web.status("https://site.example/missing.json", 404)
    assert load("https://site.example/missing.json") == State()
    path.write_bytes(b"garbage")
    assert load(str(path)) == State()
    assert "starting empty" in capsys.readouterr().err


def test_fetch_previews_retries_transient_errors_and_gives_up_on_permanent_ones():
    state = State(
        stories={
            "ok": Entry(story("ok")),
            "flaky": Entry(story("flaky")),
            "blocked": Entry(story("blocked")),
            "ask": Entry(story("ask", url=None)),
            "done": Entry(story("done"), Preview(site_name="Done")),
            "spent": Entry(story("spent"), None, MAX_ATTEMPTS),
        }
    )

    def fetch(url):
        if url.endswith("/flaky"):
            raise TimeoutError("slow")
        if url.endswith("/blocked"):
            raise urllib.error.HTTPError(url, 403, "Forbidden", None, None)
        if url.endswith("/ok"):
            return Preview(site_name="OK")
        raise AssertionError(f"unexpected fetch of {url}")

    fetched, failed = fetch_previews(state, fetch, workers=2)
    assert fetched == 1
    assert sorted(u for u, _ in failed) == ["https://example.com/blocked", "https://example.com/flaky"]
    assert state.stories["ok"].preview == Preview(site_name="OK")
    assert state.stories["ask"].preview == Preview()
    assert state.stories["flaky"].attempts == 1 and state.stories["flaky"].wants_preview
    assert state.stories["blocked"].attempts == MAX_ATTEMPTS and not state.stories["blocked"].wants_preview


def test_rate_limits_are_retried():
    state = State(stories={"1": Entry(story(1))})

    def fetch(url):
        raise urllib.error.HTTPError(url, 429, "Too Many Requests", None, None)

    fetch_previews(state, fetch)
    assert state.stories["1"].attempts == 1


def test_state_files_from_before_top_comments_still_load():
    old = b'{"version":1,"stories":{"1":{"story":{"id":"1","title":"T","url":null,"points":1,"comments":0,"created":0,"author":"a","text":""},"preview":null,"attempts":0}},"feeds":{}}'
    entry = State.from_json(old).stories["1"]
    assert (entry.comment, entry.comment_checked) == (None, 0)


def test_fetch_comments_looks_up_listed_stories_with_comments_at_most_every_interval():
    state = State(
        stories={
            "new": Entry(story("new")),
            "recent": Entry(story("recent"), comment_checked=NOW - 60),
            "due": Entry(story("due"), comment=Comment("9", "x", "old"), comment_checked=NOW - COMMENT_INTERVAL),
            "quiet": Entry(Story("quiet", "T", None, 5, 0, NOW, "a", "")),
            "gone": Entry(story("gone")),
            "flaky": Entry(story("flaky")),
        }
    )
    asked = []

    def fetch(sid):
        asked.append(sid)
        if sid == "flaky":
            raise TimeoutError("slow")
        return None if sid == "due" else Comment("c" + sid, "bob", "hi")

    looked_up, failed = fetch_comments(state, fetch, {"new", "recent", "due", "quiet", "flaky"}, NOW, workers=2)
    assert sorted(asked) == ["due", "flaky", "new"]
    assert looked_up == 2
    assert failed == [("https://news.ycombinator.com/item?id=flaky", "TimeoutError: slow")]
    assert state.stories["new"].comment == Comment("cnew", "bob", "hi")
    assert state.stories["new"].comment_checked == NOW
    assert state.stories["due"].comment is None  # its comments were deleted
    assert state.stories["flaky"].comment_checked == 0  # retried next run


DIGEST = Digest("digest.xml", "Digest", top=2, days=3, publish_hour=6)


def at(day: str, hour: int = 0) -> int:
    return int(dt.datetime.combine(dt.date.fromisoformat(day), dt.time(hour), dt.UTC).timestamp())


def test_digest_days_become_due_at_the_publish_hour():
    state = State()
    assert digest_days_due(state, DIGEST, at("2026-11-19", 5)) == [dt.date(2026, 11, 16), dt.date(2026, 11, 17)]
    assert digest_days_due(state, DIGEST, at("2026-11-19", 6))[-1] == dt.date(2026, 11, 18)
    assert publish_time(DIGEST, dt.date(2026, 11, 18)) == at("2026-11-19", 6)


def test_published_days_are_frozen_and_old_ones_pruned():
    state = State()
    now = at("2026-11-19", 7)
    add_digest_day(state, DIGEST, dt.date(2026, 11, 18), [story(1, points=5), story(2, points=50), story(3, points=20)])
    assert state.digests["digest.xml"] == {"2026-11-18": ["2", "3"]}
    assert dt.date(2026, 11, 18) not in digest_days_due(state, DIGEST, now)
    state.digests["digest.xml"]["2026-11-10"] = ["9"]
    update(state, [], {}, now, [DIGEST])
    assert state.digests == {"digest.xml": {"2026-11-18": ["2", "3"]}}
    assert set(state.stories) == {"2", "3"}  # digest stories stay without any feed
    update(state, [], {}, now, [])  # the digest was removed from the config
    assert state.digests == {} and state.stories == {}


def test_digests_survive_a_round_trip_and_older_files_load():
    state = State(stories={"1": Entry(story(1))}, digests={"digest.xml": {"2026-11-18": ["1"]}})
    assert State.from_json(state.to_json()) == state
    old = b'{"version":1,"stories":{},"feeds":{}}'
    assert State.from_json(old).digests == {}


# RSS feeds

A, B, C = "https://a.example/feed", "https://b.example/feed", "https://c.example/feed"
NEWS = Rss("news.xml", "News", (A, B, C))


def item(n, link="", guid=None, published=NOW - HOUR, title=None, categories=(), **kw):
    link = f"https://news.example/{n}" if link == "" else link
    return Item(
        guid=guid or link or f"guid-{n}",
        link=link,
        title=title or f"Story {n}",
        published=published,
        categories=tuple(categories),
        author=kw.pop("author", ""),
        summary=kw.pop("summary", ""),
        image=kw.pop("image", None),
    )


def ok(*items, etag=None, modified=None):
    return Fetched(200, list(items), etag, modified)


def test_rss_items_are_merged_and_sources_remember_their_validators():
    state = State()
    update_rss(state, [NEWS], {"news.xml": {A: ok(item(1), item(2), etag='"a1"', modified="Mon"), B: ok(item(3))}}, NOW)
    feed = state.rss["news.xml"]
    assert feed.sources[A] == Source('"a1"', "Mon", NOW)
    assert feed.sources[B] == Source(None, None, NOW)
    assert feed.sources[C] == Source()  # never fetched
    assert feed.last_ok(NEWS) is None  # C has never answered
    assert list(feed.items) == ["https://news.example/1", "https://news.example/2", "https://news.example/3"]
    assert feed.items["https://news.example/3"].from_sources == [B]
    assert feed.items["https://news.example/1"].first_seen == NOW


def test_rss_items_are_ordered_by_the_sources_dates_and_capped():
    state = State()
    feed = Rss("r.xml", "R", (A,), max_items=2)
    found = [item(1, published=NOW - 3 * HOUR), item(2, published=None), item(3, published=NOW - HOUR)]
    update_rss(state, [feed], {"r.xml": {A: ok(*found)}}, NOW)
    # An item without a date sorts by when the feed first saw it.
    assert [e.item.title for e in state.rss["r.xml"].items.values()] == ["Story 2", "Story 3"]


def test_the_filters_apply_before_the_merge_and_again_to_kept_items():
    state = State()
    feed = Rss("r.xml", "R", (A,), exclude_categories=frozenset({"bits"}))
    update_rss(state, [feed], {"r.xml": {A: ok(item(1), item(2, categories=["Bits"]))}}, NOW)
    assert list(state.rss["r.xml"].items) == ["https://news.example/1"]
    # A new exclude drops a kept item at once, even when the source is unchanged.
    stricter = Rss("r.xml", "R", (A,), exclude_titles=(__import__("re").compile("Story 1"),))
    update_rss(state, [stricter], {"r.xml": {A: Fetched(304, [], None, None)}}, NOW + HOUR)
    assert state.rss["r.xml"].items == {}


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("https://news.example/a", "HTTPS://NEWS.example/a/"),
        ("https://news.example/a", "https://news.example/a#comments"),
        ("https://news.example/a", "https://news.example/a?utm_source=rss&utm_medium=feed"),
        ("https://news.example/a?x=1&y=2", "https://news.example/a?y=2&x=1&fbclid=abc"),
    ],
)
def test_copies_with_the_same_canonical_link_are_one_story(first, second):
    state = State()
    update_rss(state, [NEWS], {"news.xml": {A: ok(item(1, link=first)), B: ok(item(2, link=second))}}, NOW)
    (entry,) = state.rss["news.xml"].items.values()
    assert entry.item.link == first and entry.from_sources == [A, B]


def test_copies_with_the_same_guid_are_one_story():
    state = State()
    update_rss(
        state,
        [NEWS],
        {"news.xml": {A: ok(item(1, guid="tag:x,1")), B: ok(item(2, link="https://mirror.example/1", guid="tag:x,1"))}},
        NOW,
    )
    assert len(state.rss["news.xml"].items) == 1


def test_link_and_title_dedup():
    same_title = {A: ok(item(1, title="Kyiv’s bridges: why")), B: ok(item(2, title="KYIV S BRIDGES — WHY?"))}
    state = State()
    update_rss(state, [NEWS], {"news.xml": same_title}, NOW)
    assert len(state.rss["news.xml"].items) == 2  # "link": different links stay apart
    by_title = Rss("news.xml", "News", (A, B, C), dedupe="link+title")
    state = State()
    update_rss(state, [by_title], {"news.xml": same_title}, NOW)
    assert len(state.rss["news.xml"].items) == 1
    # A title that folds to nothing never matches another.
    state = State()
    update_rss(state, [by_title], {"news.xml": {A: ok(item(1, title="…")), B: ok(item(2, title="?!"))}}, NOW)
    assert len(state.rss["news.xml"].items) == 2


def test_the_kept_copy_is_the_earliest_published_then_the_source_order_and_categories_are_merged():
    state = State()
    a = item(1, link="https://news.example/s?utm_source=a", published=NOW - HOUR, categories=["World"])
    b = item(2, link="https://news.example/s", published=NOW - 2 * HOUR, categories=["Europe", "World"])
    c = item(3, link="https://news.example/s#x", published=NOW - 2 * HOUR, categories=["Politics"])
    update_rss(state, [NEWS], {"news.xml": {A: ok(a), B: ok(b), C: ok(c)}}, NOW)
    (entry,) = state.rss["news.xml"].items.values()
    # b and c share the earliest date; b's source comes first in the config.
    assert entry.item.guid == b.guid
    assert entry.item.categories == ("Europe", "World", "Politics")
    assert entry.from_sources == [A, B, C]


def test_the_first_seen_copy_stays_across_runs_with_a_stable_guid():
    state = State()
    update_rss(state, [NEWS], {"news.xml": {A: ok(item(1, link="https://news.example/s", published=NOW))}}, NOW)
    # An hour later another source carries an older-dated copy: the kept one stays.
    older = item(2, link="https://news.example/s/", published=NOW - 5 * HOUR, categories=["Extra"])
    update_rss(state, [NEWS], {"news.xml": {B: ok(older), A: Fetched(304, [], None, None)}}, NOW + HOUR)
    (entry,) = state.rss["news.xml"].items.values()
    assert entry.item.guid == "https://news.example/s" and entry.first_seen == NOW
    assert entry.item.categories == ("Extra",)
    assert entry.from_sources == [A, B]
    # It survives a round trip, and a third run still matches it.
    state = State.from_json(state.to_json())
    update_rss(
        state, [NEWS], {"news.xml": {A: ok(item(1, link="https://news.example/s", published=NOW))}}, NOW + 2 * HOUR
    )
    (entry,) = state.rss["news.xml"].items.values()
    assert entry.item.guid == "https://news.example/s" and entry.from_sources == [A, B]


def test_an_edited_item_is_refreshed_and_a_changed_link_drops_its_preview():
    state = State()
    update_rss(state, [NEWS], {"news.xml": {A: ok(item(1, guid="g1", title="Old"))}}, NOW)
    (entry,) = state.rss["news.xml"].items.values()
    entry.preview, entry.attempts = Preview(image="https://i.example/1.png"), 1
    update_rss(state, [NEWS], {"news.xml": {A: ok(item(1, guid="g1", title="New"))}}, NOW + HOUR)
    (entry,) = state.rss["news.xml"].items.values()
    assert entry.item.title == "New" and entry.preview is not None
    moved = item(1, link="https://news.example/moved", guid="g1", title="New")
    update_rss(state, [NEWS], {"news.xml": {A: ok(moved)}}, NOW + 2 * HOUR)
    (entry,) = state.rss["news.xml"].items.values()
    assert entry.item.link == "https://news.example/moved"
    assert (entry.preview, entry.attempts) == (None, 0)
    assert list(state.rss["news.xml"].items) == ["https://news.example/moved"]  # the key follows the link
    # Another copy of the moved item in the same run still matches it.
    again = item(2, link="https://mirror.example/x", guid="g1", title="New")
    update_rss(state, [NEWS], {"news.xml": {A: ok(item(1, guid="g1", title="New")), B: ok(again)}}, NOW + 2 * HOUR)
    assert len(state.rss["news.xml"].items) == 1
    update_rss(state, [NEWS], {"news.xml": {A: ok(moved)}}, NOW + 2 * HOUR)
    # The old link now belongs to no item: a new story there is its own entry.
    update_rss(state, [NEWS], {"news.xml": {A: ok(moved, item(9, link="https://news.example/1"))}}, NOW + 3 * HOUR)
    assert len(state.rss["news.xml"].items) == 2


def test_one_failing_source_keeps_its_items_while_the_others_refresh():
    state = State()
    update_rss(state, [NEWS], {"news.xml": {A: ok(item(1)), B: ok(item(2)), C: ok(item(3))}}, NOW)
    later = NOW + 4 * HOUR
    update_rss(
        state, [NEWS], {"news.xml": {A: ok(item(4)), B: TimeoutError("slow"), C: Fetched(304, [], None, None)}}, later
    )
    feed = state.rss["news.xml"]
    assert set(feed.items) == {f"https://news.example/{n}" for n in (1, 2, 3, 4)}
    assert feed.items["https://news.example/1"].from_sources == []  # A dropped it
    assert feed.items["https://news.example/2"].from_sources == [B]  # B failed: still listed
    assert feed.items["https://news.example/3"].from_sources == [C]  # C unchanged
    assert feed.sources[A].last_ok == later and feed.sources[C].last_ok == later
    assert feed.sources[B].last_ok == NOW
    assert feed.last_ok(NEWS) == NOW


def test_items_no_source_lists_leave_after_a_week():
    state = State()
    feed = Rss("r.xml", "R", (A,))
    update_rss(state, [feed], {"r.xml": {A: ok(item(1))}}, NOW)
    update_rss(state, [feed], {"r.xml": {A: ok(item(2))}}, NOW + RSS_KEEP)
    assert set(state.rss["r.xml"].items) == {"https://news.example/1", "https://news.example/2"}
    update_rss(state, [feed], {"r.xml": {A: ok(item(2))}}, NOW + RSS_KEEP + 1)
    assert set(state.rss["r.xml"].items) == {"https://news.example/2"}
    # One a failing source still lists stays however old.
    update_rss(state, [feed], {"r.xml": {A: ValueError("not a feed")}}, NOW + 3 * RSS_KEEP)
    assert set(state.rss["r.xml"].items) == {"https://news.example/2"}


def test_removed_sources_and_feeds_are_forgotten():
    state = State()
    update_rss(state, [NEWS], {"news.xml": {A: ok(item(1)), B: ok(item(2))}}, NOW)
    only_a = Rss("news.xml", "News", (A,))
    update_rss(state, [only_a], {"news.xml": {A: ok(item(1))}}, NOW + HOUR)
    feed = state.rss["news.xml"]
    assert list(feed.sources) == [A]
    assert feed.items["https://news.example/2"].from_sources == []  # stays its week
    update_rss(state, [], {}, NOW + 2 * HOUR)
    assert state.rss == {}


def test_items_without_a_link_are_kept_by_guid():
    state = State()
    update_rss(state, [NEWS], {"news.xml": {A: ok(item(1, link=None, guid="note-1"))}}, NOW)
    update_rss(state, [NEWS], {"news.xml": {A: ok(item(1, link=None, guid="note-1"))}}, NOW + HOUR)
    assert list(state.rss["news.xml"].items) == ["guid:note-1"]


def test_rss_round_trip():
    state = State(
        rss={
            "news.xml": RssFeed(
                {A: Source('"e"', "Mon", NOW), B: Source()},
                {
                    "https://news.example/1": RssEntry(
                        item(1, categories=["X", "Y"], author="Ada", summary="Sum", image="https://i.example/1.png"),
                        NOW,
                        Preview("https://i.example/og.png", "Desc", "Site"),
                        1,
                        [A, B],
                    ),
                    "guid:n": RssEntry(item(2, link=None, guid="n", published=None), NOW - HOUR),
                },
            )
        }
    )
    assert State.from_json(state.to_json()) == state


def test_version_1_state_loads_with_no_rss_feeds_and_keeps_the_rest():
    old = (
        b'{"version":1,"stories":{"1":{"story":{"id":"1","title":"T","url":"https://e.example/","points":5,'
        b'"comments":0,"created":0,"author":"a","text":""},"preview":{"image":"https://e.example/i.png",'
        b'"description":"D","site_name":null},"attempts":0,"comment":null,"comment_checked":7}},'
        b'"feeds":{"f.xml":{"1":100}},"digests":{"d.xml":{"2026-11-18":["1"]}}}'
    )
    state = State.from_json(old)
    assert state.rss == {}
    assert state.stories["1"].preview == Preview("https://e.example/i.png", "D", None)
    assert state.stories["1"].comment_checked == 7
    assert state.feeds == {"f.xml": {"1": 100}} and state.digests == {"d.xml": {"2026-11-18": ["1"]}}
    assert b'"version":2' in state.to_json()


def rss_state(*entries, file="news.xml"):
    return State(rss={file: RssFeed({}, {e.item.link or e.item.guid: e for e in entries})})


def test_rss_previews_fill_what_the_source_lacks_and_are_never_refetched():
    bare = RssEntry(item(1), NOW)
    no_text = RssEntry(item(2, image="https://i.example/2.png"), NOW)
    complete = RssEntry(item(3, image="https://i.example/3.png", summary="Text"), NOW)
    no_link = RssEntry(item(4, link=None, guid="n4"), NOW)
    state = rss_state(bare, no_text, complete, no_link)
    asked = []

    def fetch(url):
        asked.append(url)
        return Preview(image="https://og.example/i.png", description="OG")

    fetched, failed = fetch_rss_previews(state, [NEWS], fetch)
    assert (fetched, failed) == (2, [])
    assert sorted(asked) == ["https://news.example/1", "https://news.example/2"]
    assert bare.image == "https://og.example/i.png" and bare.excerpt == "OG"
    assert no_text.image == "https://i.example/2.png" and no_text.excerpt == "OG"  # the source's image first
    assert complete.preview == Preview() and complete.excerpt == "Text"
    assert no_link.preview == Preview() and no_link.image is None and no_link.excerpt is None

    asked.clear()
    assert fetch_rss_previews(state, [NEWS], fetch) == (0, [])
    assert asked == []


def test_rss_previews_follow_the_retry_rules():
    flaky = RssEntry(item(1), NOW)
    blocked = RssEntry(item(2), NOW)
    spent = RssEntry(item(3), NOW, attempts=MAX_ATTEMPTS)
    state = rss_state(flaky, blocked, spent)

    def fetch(url):
        if url.endswith("/1"):
            raise TimeoutError("slow")
        raise urllib.error.HTTPError(url, 404, "Not Found", None, None)

    for run in range(1, MAX_ATTEMPTS + 1):
        fetched, failed = fetch_rss_previews(state, [NEWS], fetch)
        assert fetched == 0
        assert [u for u, _ in failed] == (["https://news.example/1"] if run > 1 else sorted(u for u, _ in failed))
        assert flaky.attempts == run
    assert blocked.attempts == MAX_ATTEMPTS  # a 404 gives up at once
    assert fetch_rss_previews(state, [NEWS], fetch) == (0, [])  # the cap: no more tries
    assert spent.preview is None


def test_rss_previews_skip_feeds_without_enrich_and_share_one_fetch_per_link():
    plain = Rss("plain.xml", "P", (A,), enrich=False)
    shared = item(1)
    state = State(
        rss={
            "news.xml": RssFeed({}, {"k": RssEntry(shared, NOW)}),
            "other.xml": RssFeed({}, {"k": RssEntry(shared, NOW)}),
            "plain.xml": RssFeed({}, {"k": RssEntry(item(2), NOW)}),
        }
    )
    asked = []

    def fetch(url):
        asked.append(url)
        return Preview(description="OG")

    other = Rss("other.xml", "O", (A,))
    assert fetch_rss_previews(state, [NEWS, other, plain, Rss("gone.xml", "G", (A,))], fetch) == (1, [])
    assert asked == ["https://news.example/1"]
    assert state.rss["other.xml"].items["k"].excerpt == "OG"
    assert state.rss["plain.xml"].items["k"].preview is None

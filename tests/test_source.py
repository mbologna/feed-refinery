import datetime as dt
import urllib.error
from urllib.parse import parse_qs, urlsplit

import pytest
from conftest import hit

from feed_refinery.config import Feed
from feed_refinery.source import (
    API,
    FIREBASE,
    fetch_ranked_ids,
    fetch_stories,
    ids_query_urls,
    parse_hits,
    points_for_url,
    query_url,
    url_query_url,
)

NOW = dt.datetime(2026, 11, 19, 12, 0, tzinfo=dt.UTC)


def params(url):
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


def test_front_page_query():
    url = query_url(Feed("f.xml", "F", "front_page"), NOW)
    assert url.startswith(API + "search?")
    assert params(url) == {"tags": "front_page", "hitsPerPage": "150"}


def test_front_page_query_with_a_points_floor():
    url = query_url(Feed("f.xml", "F", "front_page", min_points=50), NOW)
    assert params(url)["numericFilters"] == "points>=50"


def test_points_feed_queries_newest_stories_in_its_window():
    feed = Feed("b.xml", "B", "story", min_points=100, min_comments=5, window_hours=24, max_items=2000)
    url = query_url(feed, NOW)
    assert url.startswith(API + "search_by_date?")
    cutoff = int(NOW.timestamp()) - 24 * 3600
    assert params(url) == {
        "tags": "story",
        "hitsPerPage": "1000",
        "numericFilters": f"points>=100,num_comments>=5,created_at_i>{cutoff}",
    }


def test_parse_hits_fills_defaults_and_skips_untitled_hits():
    stories = parse_hits(
        {
            "hits": [
                hit(1, url=None, points=None, comments=None, story_text="<p>Ask &amp; tell</p><p>more</p>"),
                {"objectID": "2", "title": ""},
                {"title": "no id"},
                hit(3),
            ]
        }
    )
    assert [s.id for s in stories] == ["1", "3"]
    ask = stories[0]
    assert ask.url is None
    assert (ask.points, ask.comments) == (0, 0)
    assert (ask.text, ask.words) == ("Ask & tell\n\nmore", 4)
    assert ask.comments_url == "https://news.ycombinator.com/item?id=1"
    assert stories[1].url == "https://example.com/3"
    assert (stories[1].text, stories[1].words) == ("", None)


def test_parse_hits_counts_the_words_of_the_whole_text_its_excerpt_cuts():
    (story,) = parse_hits({"hits": [hit(1, url=None, story_text="<p>" + "word " * 1000 + "</p>")]})
    assert len(story.text.split()) < 1000 and story.words == 1000


def test_fetch_stories(web):
    web.json(API, {"hits": [hit(7, title="Seven")]})
    (story,) = fetch_stories(API + "search?tags=front_page")
    assert story.title == "Seven"


def test_fetch_stories_raises_on_failure(web):
    web.status(API, 503)
    with pytest.raises(OSError):
        fetch_stories(API + "search?tags=front_page")


def test_ids_query_urls_chunk_the_ids():
    urls = ids_query_urls(list(range(1, 102)), "story", ["points>=150"])
    assert len(urls) == 2
    first, second = params(urls[0]), params(urls[1])
    assert first["tags"].startswith("story,(story_1,story_2,")
    assert first["tags"].endswith("story_100)")
    assert first["hitsPerPage"] == "100"
    assert first["numericFilters"] == "points>=150"
    assert second == {"tags": "story,(story_101)", "hitsPerPage": "1", "numericFilters": "points>=150"}
    assert "numericFilters" not in params(ids_query_urls([7], "story", [])[0])
    assert ids_query_urls([], "story", []) == []


def test_fetch_ranked_ids(web):
    web.json(FIREBASE + "topstories.json", [3, 1, "x", 2])
    assert fetch_ranked_ids() == [3, 1, 2]


def test_fetch_ranked_ids_rejects_a_non_list(web):
    web.json(FIREBASE + "topstories.json", {"error": "nope"})
    with pytest.raises(ValueError):
        fetch_ranked_ids()


def test_parse_hits_skips_job_posts():
    stories = parse_hits(
        {"hits": [hit(1, "Acme (YC S13) Is Hiring", _tags=["job", "front_page"]), hit(2, _tags=["story"]), hit(3)]}
    )
    assert [s.id for s in stories] == ["2", "3"]


def test_url_query_leaves_out_the_scheme_and_www():
    q = params(url_query_url("https://www.example.com/a?b=1"))
    assert q == {
        "query": "example.com/a?b=1",
        "restrictSearchableAttributes": "url",
        "tags": "story",
        "hitsPerPage": "20",
    }


def same(url):
    return url.split("://", 1)[-1].removeprefix("www.").rstrip("/")


def test_points_for_url_takes_the_best_matching_story(web):
    web.json(
        API + "search?",
        {
            "hits": [
                hit(1, url="https://example.com/a", points=40),
                hit(2, url="http://www.example.com/a/", points=180),  # the same link, another form
                hit(3, url="https://example.com/a/more", points=900),  # another page
                hit(4, url=None, points=999),
            ]
        },
    )
    assert points_for_url("https://example.com/a", same) == 180


def test_points_for_url_is_zero_when_no_story_links_there(web):
    web.json(API + "search?", {"hits": [hit(1, url="https://other.example/", points=500)]})
    assert points_for_url("https://example.com/a", same) == 0


def test_points_for_url_raises_on_failure(web):
    web.status(API + "search?", 503)
    with pytest.raises(urllib.error.HTTPError):
        points_for_url("https://example.com/a", same)

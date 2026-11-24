"""The editor's Python side (src/feeds/site/editor/bridge.py), as Pyodide runs it in the browser."""

import json
from pathlib import Path

import pytest

from feeds.site.editor import bridge

FIXTURES = Path(__file__).parent / "fixtures"
# A copy of feeds.toml as it was when the editor was written: the real file
# changes through the editor itself.
CONFIG = (FIXTURES / "feeds.toml").read_text(encoding="utf-8")
NEWS_SAMPLE = (FIXTURES / "newssite.xml").read_bytes()
SOURCE = "https://www.newssite.example/feed"
NOW = 1_795_500_000


def changed(old, new):
    """The lines removed and added, as a diff shows them."""
    lines = bridge.diff(old, new).splitlines()[2:]
    return [line for line in lines if line[:1] in "+-"]


def test_load_gives_every_table():
    loaded = bridge.load(CONFIG)
    assert loaded["error"] is None
    tables = loaded["tables"]
    assert [t["file"] for t in tables["feed"]] == ["frontpage.xml", "best.xml", "top.xml"]
    assert tables["digest"] == [{"file": "digest.xml", "title": "Hacker News: daily digest"}]
    newssite = tables["rss"][0]
    assert newssite["source"] == SOURCE
    assert newssite["exclude_links"] == [r"^https://www\.newssite\.example/(episodes|newsletter)/"]


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("[[feed]\n", "not valid TOML"),
        (
            '[[rss]]\nfile = "a.xml"\ntitle = "A"\nsource = "https://a.example/"\nexclude_titles = ["("]\n',
            "rss #1: exclude_titles",
        ),
        ('[[digest]]\nfile = "a.xml"\ntitle = "A"\n[[digest]]\nfile = "a.xml"\ntitle = "B"\n', "a.xml is already used"),
    ],
)
def test_load_and_check_report_the_builds_message(text, error):
    assert error in bridge.load(text)["error"]
    assert error in bridge.check(text)


def test_load_without_tables_of_a_kind():
    loaded = bridge.load('rss = 1\n[[digest]]\nfile = "d.xml"\ntitle = "D"\n')
    assert loaded["tables"]["rss"] == [] and loaded["tables"]["feed"] == []


def test_set_changes_one_line_and_keeps_the_comments():
    edits = [{"op": "set", "kind": "rss", "index": 0, "key": "exclude_categories", "value": ["peanuts", "Bits"]}]
    result = bridge.apply(CONFIG, edits)
    assert result["error"] is None
    assert changed(CONFIG, result["text"]) == [
        '-exclude_categories = ["peanuts", "foto del giorno", "celebrity"]',
        '+exclude_categories = ["peanuts", "Bits"]',
    ]
    assert result["diff"].startswith("--- a/feeds.toml\n+++ b/feeds.toml\n")
    assert "# /feed, not /feed/: the trailing slash often gets a cached 403." in result["text"]


def test_set_adds_a_key_after_the_last_one_not_after_the_next_tables_comment():
    edits = [
        {"op": "set", "kind": "rss", "index": 0, "key": "exclude_sections", "value": ["Sport"]},
        {"op": "set", "kind": "rss", "index": 0, "key": "include_links", "value": [r"^https://www\.newssite\.example/"]},
    ]
    text = bridge.apply(CONFIG, edits)["text"]
    assert "exclude_titles = ['(?i)^le prime pagine']\n" + 'exclude_sections = ["Sport"]\n' in text
    # A regular expression with a backslash is written as a literal string.
    assert "include_links = ['^https://www\\.newssite\\.example/']\n\n# Tech Site." in text


def test_set_keeps_a_multiline_array_and_its_indent():
    sources = ["https://feeds.techsite.example/index", "https://feeds.techsite.example/apple"]
    text = bridge.apply(CONFIG, [{"op": "set", "kind": "rss", "index": 1, "key": "sources", "value": sources}])["text"]
    assert 'sources = [\n  "https://feeds.techsite.example/index",\n  "https://feeds' in text
    assert "technology-lab" not in text


def test_set_writes_a_long_new_array_one_entry_per_line():
    long = [f"https://feeds.example.org/section-{n}" for n in range(4)]
    text = bridge.apply(CONFIG, [{"op": "set", "kind": "rss", "index": 0, "key": "sources", "value": long}])["text"]
    assert 'sources = [\n  "https://feeds.example.org/section-0",\n' in text


def test_set_the_same_value_or_none_changes_nothing():
    same = [{"op": "set", "kind": "feed", "index": 1, "key": "min_points", "value": 100}]
    assert bridge.apply(CONFIG, same) == {"text": CONFIG, "diff": "", "error": None}
    absent = [{"op": "set", "kind": "feed", "index": 1, "key": "max_items", "value": None}]
    assert bridge.apply(CONFIG, absent)["text"] == CONFIG


def test_set_none_removes_a_key():
    edits = [{"op": "set", "kind": "feed", "index": 2, "key": "ranked", "value": None}]
    assert changed(CONFIG, bridge.apply(CONFIG, edits)["text"]) == ["-ranked = true"]


def test_set_a_number_and_a_flag():
    edits = [
        {"op": "set", "kind": "feed", "index": 0, "key": "max_items", "value": 50},
        {"op": "set", "kind": "rss", "index": 0, "key": "enrich", "value": False},
    ]
    text = bridge.apply(CONFIG, edits)["text"]
    assert 'tags = "front_page"\nmax_items = 50\n' in text
    assert "exclude_titles = ['(?i)^le prime pagine']\nenrich = false\n" in text


def test_add_puts_a_table_after_the_last_of_its_kind():
    edits = [
        {"op": "add", "kind": "feed", "table": {"file": "show.xml", "title": "Show HN", "tags": "show_hn", "x": None}}
    ]
    result = bridge.apply(CONFIG, edits)
    assert result["error"] is None
    assert changed(CONFIG, result["text"]) == [
        "+",
        "+[[feed]]",
        '+file = "show.xml"',
        '+title = "Show HN"',
        '+tags = "show_hn"',
    ]
    # The digest's comment stays right above the digest.
    assert 'tags = "show_hn"\n\n# One item per UTC day' in result["text"]


def test_add_a_kind_the_file_lacks():
    text = '[[feed]]\nfile = "a.xml"\ntitle = "A"\ntags = "story"\n'
    edits = [{"op": "add", "kind": "digest", "table": {"file": "d.xml", "title": "D"}}]
    assert bridge.apply(text, edits)["text"] == text + '\n[[digest]]\nfile = "d.xml"\ntitle = "D"\n'


def test_delete_removes_a_table_with_its_own_comment():
    result = bridge.apply(CONFIG, [{"op": "delete", "kind": "rss", "index": 0}])
    assert result["error"] is None
    text = result["text"]
    assert "newssite" not in text and "News Site" not in text
    # The general [[rss]] comment and the next table's comment stay.
    assert "# guid, date and categories.\n\n# Tech Site. index lists every article" in text
    assert bridge.load(text)["tables"]["rss"][0]["file"] == "techsite.xml"


def test_delete_the_last_table_of_a_kind():
    result = bridge.apply(CONFIG, [{"op": "delete", "kind": "digest", "index": 0}])
    assert bridge.load(result["text"])["tables"]["digest"] == []
    assert "One item per UTC day" not in result["text"]
    assert "max_items = 250\n\n# Items from RSS or Atom feeds" in result["text"]
    assert result["error"] is None


def test_delete_the_first_table_keeps_the_files_header():
    result = bridge.apply(CONFIG, [{"op": "delete", "kind": "feed", "index": 0}])
    assert result["text"].startswith("# The published feeds:")
    assert (
        '#                 topstories.json), at any age (default false)\n\n[[feed]]\nfile = "best.xml"'
        in result["text"]
    )


def test_delete_the_last_table_in_the_file():
    result = bridge.apply(CONFIG, [{"op": "delete", "kind": "rss", "index": 1}])
    assert result["text"].endswith("exclude_titles = ['(?i)^le prime pagine']\n")


def test_rename_duplicate_and_invalid_edits():
    rename = [{"op": "set", "kind": "rss", "index": 0, "key": "file", "value": "techsite.xml"}]
    assert bridge.apply(CONFIG, rename)["error"] == "rss #2: techsite.xml is already used by another feed"
    with pytest.raises(ValueError, match="unknown kind"):
        bridge.apply(CONFIG, [{"op": "delete", "kind": "page", "index": 0}])
    with pytest.raises(ValueError, match="unknown edit"):
        bridge.apply(CONFIG, [{"op": "move", "kind": "rss", "index": 0}])


def newssite_table(**changes):
    table = bridge.load(CONFIG)["tables"]["rss"][0]
    return {**table, **changes}


def test_preview_runs_the_builds_filters_and_merge():
    bodies = {SOURCE: {"body": NEWS_SAMPLE, "content_type": "application/rss+xml", "url": SOURCE}}
    result = bridge.preview(newssite_table(), bodies, NOW)
    assert result["error"] is None
    assert result["sources"] == {SOURCE: {"items": 12, "error": None}}
    assert result["counts"] == {"listed": 12, "dropped": 5, "merged": 0, "stories": 7, "published": 7}
    first = result["items"][0]
    assert first["title"] == "Cosa dice il film di Chiara Ferragni del “pandorogate”"
    assert (first["drop"], first["merged"], first["key"]) == (None, False, first["link"].rstrip("/"))
    drops = {i["drop"]["key"] for i in result["items"] if i["drop"]}
    assert drops == {"exclude_categories", "exclude_links", "exclude_titles"}
    assert result["stories"][0]["sources"] == [SOURCE]
    sections = {f["name"]: (f["count"], f["passed"]) for f in result["facets"]["sections"]}
    assert sections["Bits"] == (1, 1) and sections["peanuts"] == (1, 0)
    # Excluding Bits drops one more item; max_items caps what's published.
    stricter = newssite_table(exclude_categories=["peanuts", "foto del giorno", "celebrity", "Bits"], max_items=5)
    assert bridge.preview(stricter, bodies, NOW)["counts"] == {
        "listed": 12,
        "dropped": 6,
        "merged": 0,
        "stories": 6,
        "published": 5,
    }


def test_preview_reports_failed_and_broken_sources():
    two = newssite_table(sources=[SOURCE, "https://b.example/feed", "https://c.example/feed"])
    del two["source"]
    bodies = {SOURCE: {"body": NEWS_SAMPLE}, "https://b.example/feed": {"error": "HTTP 403"}}
    result = bridge.preview(two, bodies, NOW)
    assert result["sources"]["https://b.example/feed"] == {"items": 0, "error": "HTTP 403"}
    assert result["sources"]["https://c.example/feed"] == {"items": 0, "error": "not fetched"}
    broken = bridge.preview(newssite_table(), {SOURCE: {"body": b"<html>no</html>"}}, NOW)
    assert broken["sources"][SOURCE]["error"].startswith("not a feed")
    assert broken["counts"]["listed"] == 0


def test_preview_refuses_a_table_that_wont_build():
    assert bridge.preview(newssite_table(max_items=0), {}, NOW) == {
        "error": "rss #1: max_items must be a whole number from 1 to 1000"
    }


def test_test_matches_regular_expressions_as_the_filters_do():
    assert bridge.test(["(?i)^le prime", "pagine$", "("], "Le prime pagine") == [
        {"pattern": "(?i)^le prime", "match": True, "error": None},
        {"pattern": "pagine$", "match": True, "error": None},
        {"pattern": "(", "match": False, "error": "missing ), unterminated subpattern at position 0"},
    ]


def test_call_is_json_in_json_out_and_reads_bodies_from_files(tmp_path):
    assert json.loads(bridge.call("check", json.dumps({"text": CONFIG}))) is None
    path = tmp_path / "body"
    path.write_bytes(NEWS_SAMPLE)
    payload = {"table": newssite_table(), "bodies": {SOURCE: {"path": str(path)}}, "now": NOW}
    assert json.loads(bridge.call("preview", json.dumps(payload)))["counts"]["published"] == 7


def test_set_on_an_empty_table():
    edits = [
        {"op": "set", "kind": "digest", "index": 0, "key": "file", "value": "d.xml"},
        {"op": "set", "kind": "digest", "index": 0, "key": "title", "value": "D"},
    ]
    assert bridge.apply("[[digest]]\n", edits) == {
        "text": '[[digest]]\nfile = "d.xml"\ntitle = "D"\n',
        "diff": bridge.diff("[[digest]]\n", '[[digest]]\nfile = "d.xml"\ntitle = "D"\n'),
        "error": None,
    }


JSON_MAP = {"items": "results", "title": "title", "link": "url", "date": "date", "date_format": "%B %d, %Y"}
BLOG = "https://blog.example/api/posts"


def test_set_writes_json_as_an_inline_table():
    edits = [{"op": "set", "kind": "rss", "index": 0, "key": "json", "value": {"items": "posts", "title": "t"}}]
    text = bridge.apply(CONFIG, edits)["text"]
    assert '\njson = {items = "posts", title = "t"}\n' in text
    added = [{"op": "add", "kind": "rss", "table": {"file": "b.xml", "title": "B", "source": BLOG, "json": JSON_MAP}}]
    result = bridge.apply(CONFIG, added)
    assert result["error"] is None
    assert (
        'json = {items = "results", title = "title", link = "url", date = "date", date_format = "%B %d, %Y"}'
        in (result["text"])
    )


def test_preview_reads_a_json_source():
    table = {"file": "b.xml", "title": "B", "source": BLOG, "json": JSON_MAP}
    body = json.dumps(
        {"results": [{"title": "Hello", "url": "/blog/hello/", "date": "November 23, 2026"}, {"title": "No link"}]}
    ).encode()
    result = bridge.preview(table, {BLOG: {"body": body, "content_type": "application/json", "url": BLOG}}, NOW)
    assert result["sources"] == {BLOG: {"items": 1, "error": None}}
    assert result["stories"] == [
        {
            "key": "https://blog.example/blog/hello",
            "title": "Hello",
            "link": "https://blog.example/blog/hello/",
            "sources": [BLOG],
        }
    ]
    broken = bridge.preview(table, {BLOG: {"body": b"<html>", "content_type": "text/html", "url": BLOG}}, NOW)
    assert broken["sources"][BLOG]["error"].startswith("not JSON")

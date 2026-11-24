import datetime as dt
import re
import xml.etree.ElementTree as ET
from dataclasses import replace

import pytest

from feed_refinery.comments import Comment
from feed_refinery.config import Digest, Feed, Rss
from feed_refinery.feed import (
    ATOM,
    DC,
    MEDIA,
    age_line,
    categories,
    domain,
    item_html,
    last_build_date,
    reading_time,
    render_digest,
    render_feed,
    render_rss_feed,
    rss_item_html,
)
from feed_refinery.page import describe, render_page
from feed_refinery.preview import Preview
from feed_refinery.rss import Item
from feed_refinery.source import Story
from feed_refinery.state import Entry, RssEntry

NOW = dt.datetime(2026, 11, 19, 12, 0, tzinfo=dt.UTC)
FEED = Feed("frontpage.xml", "Hacker News: front page", "front_page")


def story(**kw):
    base = {
        "id": "42",
        "title": 'Rust & "C" <compared>',
        "url": "https://www.example.com/post?a=1&b=2",
        "points": 150,
        "comments": 37,
        "created": 1_795_000_000,
        "author": "pg",
        "text": "",
    }
    return Story(**{**base, **kw})


def render(entries):
    doc = render_feed(FEED, entries, "https://site.example/frontpage.xml", NOW)
    return doc, ET.fromstring(doc)


def test_item_fields():
    entry = Entry(story(), Preview("https://cdn.example.com/i.png?x=1&y=2", "An excerpt <b>raw</b>", "Example"))
    doc, root = render([(entry, 1_795_001_000)])
    assert doc.startswith(b"<?xml version='1.0' encoding='utf-8'?>")
    channel = root.find("channel")
    assert channel.findtext("title") == "Hacker News: front page"
    assert channel.find(f"{{{ATOM}}}link").get("href") == "https://site.example/frontpage.xml"
    assert channel.findtext("lastBuildDate") == "Thu, 19 Nov 2026 12:00:00 +0000"
    item = channel.find("item")
    assert item.findtext("title") == 'Rust & "C" <compared>'
    assert item.findtext("link") == "https://www.example.com/post?a=1&b=2"
    assert item.findtext("comments") == "https://news.ycombinator.com/item?id=42"
    guid = item.find("guid")
    assert (guid.text, guid.get("isPermaLink")) == ("https://news.ycombinator.com/item?id=42", "false")
    assert item.findtext("pubDate") == "Wed, 18 Nov 2026 11:23:20 +0000"
    assert item.findtext(f"{{{DC}}}creator") == "pg"
    assert item.find(f"{{{MEDIA}}}thumbnail").get("url") == "https://cdn.example.com/i.png?x=1&y=2"
    assert item.find(f"{{{MEDIA}}}content").get("medium") == "image"


CREATED = 1_795_000_000  # story()'s post time
HOUR = 3600


def test_item_html_escapes_and_links():
    entry = Entry(story(), Preview("https://cdn.example.com/i.png?x=1&y=2", "An excerpt <b>raw</b>", "Example"))
    assert item_html(entry, CREATED + 5 * HOUR) == (
        '<p><img src="https://cdn.example.com/i.png?x=1&amp;y=2" alt=""></p>\n'
        "<p>An excerpt &lt;b&gt;raw&lt;/b&gt;</p>\n"
        '<p><a href="https://www.example.com/post?a=1&amp;b=2">Example</a> · '
        '<a href="https://news.ycombinator.com/item?id=42">37 comments</a> · 150 points · 5 hours ago by pg</p>'
    )


def test_item_without_preview_labels_the_article_link_with_the_domain():
    html = item_html(Entry(story(points=1, comments=1, author="")), CREATED + 30)
    assert "<img" not in html and "<b>" not in html
    assert html.startswith('<p><a href="https://www.example.com/post?a=1&amp;b=2">example.com</a> · ')
    assert "1 comment</a> · 1 point · 🔥 just now</p>" in html


@pytest.mark.parametrize(
    ("elapsed", "line"),
    [
        (-60, "🔥 just now"),  # a clock a little behind the post
        (59, "🔥 just now"),
        (60, "🔥 1 minute ago"),
        (HOUR, "🔥 1 hour ago"),
        (4 * HOUR, "🔥 4 hours ago"),
        (4 * HOUR + 1, "4 hours ago"),
        (47 * HOUR, "1 day ago"),
        (48 * HOUR, "❄️ 2 days ago"),
    ],
)
def test_age_line_uses_the_bots_markers(elapsed, line):
    assert age_line(CREATED, CREATED + elapsed) == line


def test_submitter_text_follows_the_page_excerpt():
    entry = Entry(story(text="I built this <alone>"), Preview(description="The page says"))
    html = item_html(entry, CREATED)
    assert "<p>The page says</p>\n<blockquote><p>I built this &lt;alone&gt;</p></blockquote>" in html


def test_submitter_text_stands_in_for_a_missing_excerpt():
    html = item_html(Entry(story(text="Only mine"), Preview()), CREATED)
    assert "<p>Only mine</p>" in html
    assert "<blockquote>" not in html


def test_ask_hn_item_links_the_thread_and_shows_its_text():
    entry = Entry(story(url=None, text="What do you use?"), Preview())
    _, root = render([(entry, 1_795_001_000)])
    item = root.find("channel/item")
    assert item.findtext("link") == "https://news.ycombinator.com/item?id=42"
    assert item.find(f"{{{MEDIA}}}content") is None
    html = item.findtext("description")
    assert "Article" not in html
    assert "<p>What do you use?</p>" in html


def test_items_keep_the_given_order():
    entries = [(Entry(story(id=str(i))), 1_795_000_000 - i) for i in range(3)]
    _, root = render(entries)
    assert [i.findtext("guid")[-1] for i in root.iter("item")] == ["0", "1", "2"]


def test_domain():
    assert domain("https://www.github.com/x") == "github.com"
    assert domain(None) is None
    assert domain("not a url") is None


def test_last_build_date_round_trip():
    doc, _ = render([])
    assert last_build_date(doc) == NOW


@pytest.mark.parametrize(
    ("doc", "message"),
    [
        (b"<html>", "not XML"),
        (b"<rss><channel></channel></rss>", "no lastBuildDate"),
        (b"<rss><channel><lastBuildDate>yesterday</lastBuildDate></channel></rss>", "unreadable lastBuildDate"),
    ],
)
def test_last_build_date_errors(doc, message):
    with pytest.raises(ValueError, match=message):
        last_build_date(doc)


def test_page_lists_feeds():
    best = Feed("best.xml", "Best <100+>", "story", min_points=100, min_comments=3)
    page = render_page([(FEED, "https://site.example/frontpage.xml"), (best, "https://site.example/best.xml")])
    assert '<a href="https://site.example/best.xml">Best &lt;100+&gt;</a>' in page
    assert "<code>https://site.example/frontpage.xml</code>" in page
    assert describe(FEED) == "the current front page, kept 48 hours, up to 150 items"
    assert (
        describe(best)
        == "newest stories tagged story, at least 100 points, at least 3 comments, kept 48 hours, up to 150 items"
    )


def test_top_comment_line():
    entry = Entry(story(), Preview(), comment=Comment("7", "bob <b>", "Nice & clean"))
    html = item_html(entry, CREATED)
    assert (
        '<p>💬 <b>bob &lt;b&gt;</b>: Nice &amp; clean <a href="https://news.ycombinator.com/item?id=7">→</a></p>'
        in html
    )
    assert html.index("💬") < html.index("example.com")


def test_item_keeps_the_paragraphs_of_the_excerpt_the_text_and_the_comment():
    entry = Entry(
        story(text="Mine.\n\nAll mine."),
        Preview(description="The page.\n\nMore of it."),
        comment=Comment("7", "bob", "First.\n\nSecond."),
    )
    html = item_html(entry, CREATED)
    assert "<p>The page.</p>\n<p>More of it.</p>\n<blockquote><p>Mine.</p>\n<p>All mine.</p></blockquote>" in html
    assert '<p>💬 <b>bob</b>: First.</p>\n<p>Second. <a href="https://news.ycombinator.com/item?id=7">→</a></p>' in html


@pytest.mark.parametrize(
    ("words", "preview", "line"),
    [
        (None, None, None),
        (199, None, None),  # too few words to tell: a page its scripts fill in, or only the lede
        (200, None, "1 min read"),
        (2300, Preview(), "10 min read"),
        (2300, Preview(paywalled=True), None),  # the count covers only what the paywall lets through
    ],
)
def test_reading_time(words, preview, line):
    assert reading_time(words, preview) == line


def test_item_shows_the_reading_time_after_the_article_link():
    entry = Entry(story(), Preview(description="Text", site_name="Example", words=1150))
    html = item_html(entry, CREATED)
    assert '">Example</a> · 5 min read · <a href="https://news.ycombinator.com/item?id=42">37 comments</a>' in html
    assert html.startswith("<p>Text</p>")  # what readers show in a list


def test_categories_and_channel_image():
    entry = Entry(story(title="Show HN: A thing"), Preview())
    _, root = render([(entry, CREATED)])
    channel = root.find("channel")
    # Hacker News's site and icon, so readers show its icon rather than one shared by every feed.
    assert channel.findtext("link") == "https://news.ycombinator.com/"
    assert channel.findtext("image/url") == "https://news.ycombinator.com/favicon.ico"
    assert channel.findtext("image/link") == "https://news.ycombinator.com/"
    assert [c.text for c in channel.find("item").findall("category")] == ["Show HN", "example.com"]


def test_categories_of_a_text_post():
    assert categories(story(title="Ask HN: Why?", url=None)) == ["Ask HN"]
    assert categories(story(title="Showing HN things")) == ["example.com"]


def test_page_links_the_icons():
    page = render_page([(FEED, "https://site.example/frontpage.xml")])
    assert '<link rel="icon" href="favicon.svg" type="image/svg+xml">' in page
    assert '<link rel="apple-touch-icon" href="icon.png">' in page


def test_digest_document():
    digest = Digest("digest.xml", "Daily", top=2, publish_hour=6)
    day = dt.date(2026, 11, 18)
    first = Entry(story(id="1", title="One & only"), Preview("https://i.example/1.png", "Desc", "Site"))
    second = Entry(story(id="2", url=None, text="Ask text", author="", points=1, comments=1), None)
    doc = render_digest(digest, [(day, [first, second])], "https://site.example/digest.xml", NOW)
    item = ET.fromstring(doc).find("channel/item")
    assert item.findtext("title") == "Hacker News digest: Wed 18 Nov 2026"
    assert item.findtext("link") == "https://news.ycombinator.com/front?day=2026-11-18"
    assert item.findtext("guid") == "digest.xml:2026-11-18"
    assert item.findtext("pubDate") == "Thu, 19 Nov 2026 06:00:00 +0000"
    assert item.find(f"{{{MEDIA}}}thumbnail").get("url") == "https://i.example/1.png"
    html = item.findtext("description")
    assert '<h3>1. <a href="https://www.example.com/post?a=1&amp;b=2">One &amp; only</a></h3>' in html
    assert "<p><b>Site</b> · Desc</p>" in html
    assert '<h3>2. <a href="https://news.ycombinator.com/item?id=2">' in html
    assert "<p>Ask text</p>" in html
    assert "1 point · " in html and "1 comment</a></p>" in html


def test_digest_shows_the_first_paragraph_of_each_story_cut_short():
    long = "Word " * 200
    entry = Entry(story(), Preview(description=f"First paragraph.\n\n{long}", site_name="Site"))
    doc = render_digest(Digest("d.xml", "D"), [(dt.date(2026, 11, 18), [entry])], "u", NOW)
    html = ET.fromstring(doc).find("channel/item").findtext("description")
    assert "<p><b>Site</b> · First paragraph.</p>" in html and "Word" not in html
    entry = Entry(story(), Preview(description=long.strip()))
    doc = render_digest(Digest("d.xml", "D"), [(dt.date(2026, 11, 18), [entry])], "u", NOW)
    html = ET.fromstring(doc).find("channel/item").findtext("description")
    assert "Word Word…</p>" in html and html.count("Word") < 110


def test_digest_entry_without_text_or_image():
    doc = render_digest(Digest("d.xml", "D"), [(dt.date(2026, 11, 18), [Entry(story(), Preview())])], "u", NOW)
    item = ET.fromstring(doc).find("channel/item")
    assert item.find(f"{{{MEDIA}}}thumbnail") is None
    assert "<p><b>example.com</b></p>" in item.findtext("description")


def test_page_describes_a_digest():
    assert describe(Digest("d.xml", "D")) == (
        "the 20 stories with the most points of each UTC day, published at 06:00 UTC the next day, last 7 days"
    )


NEWS_SAMPLE = Rss("newssite.xml", "News Site: articles", ("https://www.newssite.example/feed",))
LINK = "https://www.newssite.example/2026/11/23/chiara-ferragni-pandorogate-documentario/"


def rss_item(**kw):
    base = {
        "guid": LINK,
        "link": LINK,
        "title": "Cosa dice il film del “pandorogate” & <altro>",
        "published": 1_795_460_484,
        "categories": ("Cultura", "TV"),
        "author": "Redazione",
        "summary": "Non aveva mai parlato delle sue responsabilità",
        "image": "https://static.example/chiara.png?w=680&h=453",
    }
    return Item(**{**base, **kw})


def render_rss(entries, feed=NEWS_SAMPLE):
    doc = render_rss_feed(feed, entries, "https://site.example/newssite.xml", NOW)
    return doc, ET.fromstring(doc)


def test_rss_items_keep_the_sources_fields():
    doc, root = render_rss([RssEntry(rss_item(), 1_795_469_000)])
    assert doc.startswith(b"<?xml version='1.0' encoding='utf-8'?>")
    channel = root.find("channel")
    assert channel.findtext("title") == "News Site: articles"
    assert channel.findtext("description") == (
        "News Site: articles, from https://www.newssite.example/feed, with each article's image and excerpt"
    )
    assert channel.find(f"{{{ATOM}}}link").get("href") == "https://site.example/newssite.xml"
    assert (
        channel.findtext("link") == channel.findtext("image/link") == "https://www.newssite.example/"
    )  # the source's site
    assert channel.findtext("image/url") == "https://www.newssite.example/favicon.ico"
    item = channel.find("item")
    assert item.findtext("title") == "Cosa dice il film del “pandorogate” & <altro>"
    assert item.findtext("link") == LINK
    guid = item.find("guid")
    assert (guid.text, guid.get("isPermaLink")) == (LINK, "true")
    assert item.findtext("pubDate") == "Mon, 23 Nov 2026 19:01:24 +0000"  # the source's date, not first seen
    assert [c.text for c in item.findall("category")] == ["Cultura", "TV"]
    assert item.findtext(f"{{{DC}}}creator") == "Redazione"
    assert item.find(f"{{{MEDIA}}}content").get("url") == "https://static.example/chiara.png?w=680&h=453"
    assert item.find(f"{{{MEDIA}}}thumbnail").get("url") == "https://static.example/chiara.png?w=680&h=453"
    assert item.findtext("comments") is None


def test_rss_items_drop_categories_that_differ_only_in_case():
    entry = RssEntry(rss_item(categories=("AI", "Apple", "Culture", "apple", "APPLE", "opinion")), 0)
    _, root = render_rss([entry])
    assert [c.text for c in root.findall("channel/item/category")] == ["AI", "Apple", "Culture", "opinion"]


def test_rss_item_html():
    # No link to the article: the item's <link> is one, and readers would show it in the preview text.
    assert rss_item_html(RssEntry(rss_item(summary="Uno <b>due</b> & tre"), 0)) == (
        '<p><img src="https://static.example/chiara.png?w=680&amp;h=453" alt=""></p>\n'
        "<p>Uno &lt;b&gt;due&lt;/b&gt; &amp; tre</p>"
    )


def test_rss_item_keeps_the_excerpts_paragraphs():
    html = rss_item_html(RssEntry(rss_item(summary="Uno.\n\nDue & tre.", image=None), 0))
    assert html == "<p>Uno.</p>\n<p>Due &amp; tre.</p>"


def test_rss_item_ends_with_the_site_and_facts_when_they_say_something():
    with_facts = RssEntry(rss_item(words=920), 0, Preview(site_name="News Site"), hn_points=312)
    assert rss_item_html(with_facts).endswith(
        f'<p><a href="{LINK}">News Site</a> · 4 min read · 312 points on Hacker News</p>'
    )
    # After an excerpt too long for a reader's preview text, the link alone.
    long = RssEntry(rss_item(summary="Parola " * 80, image=None), 0)
    assert rss_item_html(long).endswith(f'</p>\n<p><a href="{LINK}">newssite.example</a></p>')
    # After a short one, with no facts, nothing: the line would end the preview text.
    assert "<a " not in rss_item_html(RssEntry(rss_item(), 0))


def test_rss_item_without_text_links_to_the_article():
    assert (
        rss_item_html(RssEntry(rss_item(summary="", image=None), 0)) == f'<p><a href="{LINK}">newssite.example</a></p>'
    )


def test_rss_items_fall_back_on_the_preview():
    entry = RssEntry(
        rss_item(summary="", image=None), 0, Preview("https://og.example/i.png", "From the page", "News Site")
    )
    html = rss_item_html(entry)
    assert '<img src="https://og.example/i.png"' in html and "<p>From the page</p>" in html
    # The source's own image and text come first.
    entry = RssEntry(rss_item(), 0, Preview("https://og.example/i.png", "From the page"))
    html = rss_item_html(entry)
    assert "og.example" not in html and "From the page" not in html


def test_rss_item_without_link_date_image_or_text():
    entry = RssEntry(
        rss_item(guid="tag:x,1", link=None, published=None, image=None, summary="", author=""), 1_795_001_000
    )
    _, root = render_rss([entry], Rss("n.xml", "N", ("https://a.example/", "https://b.example/"), enrich=False))
    assert root.findtext("channel/description") == "N, from https://a.example/, https://b.example/"
    item = root.find("channel/item")
    assert item.find("link") is None
    assert (item.findtext("guid"), item.find("guid").get("isPermaLink")) == ("tag:x,1", "false")
    assert item.findtext("pubDate") == "Wed, 18 Nov 2026 11:23:20 +0000"  # when the feed first saw it
    assert item.findtext("description") == ""
    assert item.find(f"{{{MEDIA}}}content") is None and item.find(f"{{{DC}}}creator") is None


def test_a_guid_that_isnt_the_link_is_not_a_permalink():
    _, root = render_rss([RssEntry(rss_item(guid="https://www.newssite.example/?p=1"), 0)])
    assert root.find("channel/item/guid").get("isPermaLink") == "false"


def test_page_describes_rss_feeds():
    filtered = Rss(
        "newssite.xml",
        "News Site",
        ("https://www.newssite.example/feed",),
        exclude_categories=frozenset({"bits", "peanuts"}),
        exclude_titles=(re.compile("x"),),
    )
    assert (
        describe(filtered)
        == "items from newssite.example, filtered by 3 rules, with images and excerpts, up to 100 items"
    )
    merged = Rss(
        "n.xml",
        "N",
        ("https://www.newssite.example/feed", "https://www.newssite.example/mondo/feed", "https://example.org/rss"),
        include_links=(re.compile("y"),),
        enrich=False,
        max_items=50,
    )
    assert (
        describe(merged)
        == "items from newssite.example, example.org (3 feeds, duplicates merged), filtered by 1 rule, up to 50 items"
    )
    page = render_page([(filtered, "https://site.example/newssite.xml")])
    assert '<a href="https://site.example/newssite.xml">News Site</a>' in page


def test_site_and_icon_can_be_set():
    feed = replace(NEWS_SAMPLE, site="https://www.example.org/", icon="https://www.example.org/assets/img/favicon.png")
    channel = render_rss([], feed)[1].find("channel")
    assert channel.findtext("link") == channel.findtext("image/link") == "https://www.example.org/"
    assert channel.findtext("image/url") == "https://www.example.org/assets/img/favicon.png"
    digest = ET.fromstring(render_digest(Digest("d.xml", "D", site="https://links.example.org/"), [], "u", NOW))
    assert digest.findtext("channel/image/url") == "https://links.example.org/favicon.ico"

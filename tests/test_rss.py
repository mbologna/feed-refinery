import json
import re
import urllib.error
from dataclasses import replace
from pathlib import Path

import pytest

from feed_refinery import USER_AGENT, rss
from feed_refinery.config import JsonFields, Rss
from feed_refinery.rss import (
    Drop,
    Fetched,
    Item,
    canonical_link,
    dedup_key,
    fetch,
    keep,
    labelled,
    normal_title,
    parse,
    parse_json,
    select,
    verdict,
)
from feed_refinery.text import EXCERPT_LIMIT

FIXTURES = Path(__file__).parent / "fixtures"
NEWS_SAMPLE = (FIXTURES / "newssite.xml").read_bytes()
ATOM = (FIXTURES / "atom.xml").read_bytes()
BROKEN = (FIXTURES / "broken.xml").read_bytes()
TECH_SAMPLE = (FIXTURES / "techsite.xml").read_bytes()
SOURCE = "https://www.newssite.example/feed"
NEWS_SITE = Rss(
    "newssite.xml",
    "News Site: articles",
    (SOURCE,),
    exclude_categories=frozenset({"peanuts", "foto del giorno", "celebrity"}),
    exclude_links=(re.compile(r"^https://www\.newssite\.example/(episodes|newsletter)/"),),
    exclude_titles=(re.compile(r"(?i)^le prime pagine"),),
)


def item(**kw):
    base = {
        "guid": "https://news.example/a",
        "link": "https://news.example/a",
        "title": "A story",
        "published": 1_795_000_000,
        "categories": (),
        "author": "",
        "summary": "",
        "image": None,
    }
    return Item(**{**base, **kw})


def test_parse_news_site():
    items = parse(NEWS_SAMPLE, SOURCE, "application/rss+xml")
    assert len(items) == 12
    first = items[0]
    assert first.title == "Cosa dice il film di Chiara Ferragni del “pandorogate”"
    assert first.link == "https://www.newssite.example/2026/11/23/chiara-ferragni-pandorogate-documentario/"
    assert first.guid == first.link and first.permalink
    assert first.published == 1_795_460_484  # Mon, 23 Nov 2026 19:01:24 +0000
    assert first.categories == ("Cultura",)
    assert first.summary.startswith("Non aveva mai parlato delle sue responsabilità")
    assert first.image == (
        "https://static-prod.cdn.newssite.example/wp-content/uploads/2026/11/23/680x453/1795460433-chiara.png"
    )
    assert first.author == ""
    assert items[2].categories == ("Mondo", "Tecnologia")
    # An image and no text in the CDATA description.
    assert items[1].summary == "" and items[1].image
    # An empty CDATA description: neither.
    episode = items[3]
    assert episode.link.startswith("https://www.newssite.example/episodes/")
    assert (episode.summary, episode.image, episode.categories) == ("", None, ())


def test_news_site_filters():
    kept = [i.title for i in parse(NEWS_SAMPLE, SOURCE) if keep(i, NEWS_SITE)]
    assert kept == [
        "Cosa dice il film di Chiara Ferragni del “pandorogate”",
        "Un uomo senzatetto è stato trovato morto nel cortile della Sapienza a Roma, dove si era accampato da giorni",
        "Jensen Huang è di nuovo l’amministratore delegato di Nvidia",
        "Da leggere per provare a capire l’ex Ilva di Taranto",
        "La locandina di “Artificial” di Guadagnino si è fatta notare",
        "Come funzioneranno i visti agevolati per giovani tra Regno Unito e Unione Europea",
        "Perché i ponti di Kyiv sono così importanti",
    ]


def test_parse_atom():
    release, meetup, note = parse(ATOM, "https://blog.example.org/feed.atom")
    assert release.guid == "tag:blog.example.org,2026:post-2" and not release.permalink
    assert release.link == "https://blog.example.org/posts/release-2/?utm_source=feed#top"  # relative, resolved
    assert release.title == "Release 2.0 is out"  # type="html": the markup goes
    assert release.published == 1_795_339_800  # <published>, not <updated>
    assert release.categories == ("releases", "software")
    assert release.author == "Ada"
    assert release.summary == "What changed in 2.0, and why."
    assert release.image == "https://cdn.example.org/release-2.jpg"  # the image enclosure
    assert meetup.published == 1_795_197_600  # only <updated>
    assert meetup.summary == "We met, we talked."  # <content> stands in for a missing summary
    assert meetup.image == "https://cdn.example.org/meetup.jpg"  # media:thumbnail
    assert note.link is None and note.guid == "tag:blog.example.org,2026:note-7"
    assert note.title == "A short note with no title and no link."
    assert note.published is None


def test_parse_media_content_and_relative_images():
    doc = b"""<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/"
      xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel><title>T</title>
    <item><title>Media</title><link>https://s.example/1</link>
      <media:content url="https://s.example/video.mp4" type="video/mp4"/>
      <media:content url="https://s.example/1.jpg" medium="image"/></item>
    <item><title>Relative</title><link>https://s.example/2</link>
      <description><![CDATA[<p><img src="/2.png"> Text &amp; more</p>]]></description></item>
    <item><title>Data image</title><guid isPermaLink="false">2-guid</guid>
      <description><![CDATA[<img src="data:image/png;base64,AAAA">]]></description>
      <category>A</category><category> A </category><category>B</category></item>
    <item><description>No title, no link, no guid</description></item>
    <item><title>Full text</title><link>https://s.example/3</link>
      <content:encoded><![CDATA[<p>Only the full text.</p>]]></content:encoded></item>
    </channel></rss>"""
    media, relative, data, full = parse(doc, "https://s.example/feed")
    assert full.summary == "Only the full text."
    assert media.image == "https://s.example/1.jpg"
    assert relative.image == "https://s.example/2.png" and relative.summary == "Text & more"
    assert data.image is None and data.link is None and data.guid == "2-guid"
    assert data.categories == ("A", "B")


def test_parse_rejects_what_isnt_a_feed():
    with pytest.raises(ValueError, match="not a feed"):
        parse(BROKEN, SOURCE, "text/html")
    with pytest.raises(ValueError, match="not a feed"):
        parse(b'<?xml version="1.0"?><rss version="2.0"><channel><title>cut off', SOURCE)


def test_an_empty_feed_has_no_items():
    assert parse(b'<rss version="2.0"><channel><title>T</title></channel></rss>', SOURCE) == []


def test_fetch_sends_validators_and_returns_new_ones(web):
    web.feed(SOURCE, NEWS_SAMPLE, {"ETag": '"v2"', "Last-Modified": "Mon, 23 Nov 2026 21:08:25 GMT"})
    result = fetch(SOURCE, '"v1"', "Mon, 23 Nov 2026 20:00:00 GMT")
    assert (result.status, len(result.items), result.etag, result.last_modified) == (
        200,
        12,
        '"v2"',
        "Mon, 23 Nov 2026 21:08:25 GMT",
    )
    (req,) = web.sent
    assert req.get_header("User-agent") == USER_AGENT
    assert req.get_header("If-none-match") == '"v1"'
    assert req.get_header("If-modified-since") == "Mon, 23 Nov 2026 20:00:00 GMT"


def test_fetch_without_validators_asks_unconditionally(web):
    web.feed(SOURCE, ATOM, content_type="application/atom+xml")
    result = fetch(SOURCE)
    assert result.status == 200 and result.etag is None and len(result.items) == 3
    assert web.sent[0].get_header("If-none-match") is None


def test_fetch_304_keeps_the_validators(web):
    web.status(SOURCE, 304)
    assert fetch(SOURCE, '"v1"', "then") == Fetched(304, [], '"v1"', "then")


def test_fetch_raises_on_http_errors_without_retrying(web):
    web.status(SOURCE, 404)
    with pytest.raises(urllib.error.HTTPError):
        fetch(SOURCE)
    assert len(web.requests) == 1


def test_fetch_retries_a_network_error_once(web, monkeypatch):
    monkeypatch.setattr(rss, "RETRY_DELAY", 0)
    answers = [TimeoutError("slow")]

    def flaky():
        if answers:
            raise answers.pop()
        return web.routes["ok"]()

    web.feed("ok", NEWS_SAMPLE)
    web.routes[SOURCE] = flaky
    assert fetch(SOURCE).status == 200
    assert len(web.requests) == 2

    web.error(SOURCE, urllib.error.URLError("down"))
    with pytest.raises(urllib.error.URLError):
        fetch(SOURCE)
    assert len(web.requests) == 4


def test_fetch_raises_on_a_broken_feed(web):
    web.feed(SOURCE, BROKEN, content_type="text/html")
    with pytest.raises(ValueError, match="not a feed"):
        fetch(SOURCE)


def feed(**kw):
    return Rss("f.xml", "F", (SOURCE,), **kw)


@pytest.mark.parametrize(
    ("options", "fields", "kept"),
    [
        ({}, {}, True),
        ({"include_categories": frozenset({"world"})}, {"categories": ("World",)}, True),
        ({"include_categories": frozenset({"world"})}, {"categories": ("Sport",)}, False),
        ({"include_categories": frozenset({"world"})}, {"categories": ("World news",)}, False),  # exact only
        ({"exclude_categories": frozenset({"bits"})}, {"categories": ("BITS", "Mondo")}, False),
        ({"exclude_categories": frozenset({"bits"})}, {"categories": ("Bitstream",)}, True),
        ({"include_titles": (re.compile("^News"),)}, {"title": "News: it rained"}, True),
        ({"include_titles": (re.compile("^News"),)}, {"title": "Old News"}, False),
        ({"exclude_titles": (re.compile("(?i)sponsored"),)}, {"title": "A Sponsored post"}, False),
        ({"exclude_titles": (re.compile("sponsored"),)}, {"title": "A Sponsored post"}, True),  # case counts
        ({"include_links": (re.compile("/articles/"),)}, {"link": "https://news.example/articles/1"}, True),
        ({"include_links": (re.compile("/articles/"),)}, {"link": None}, False),
        ({"exclude_links": (re.compile("/video/"),)}, {"link": "https://news.example/video/1"}, False),
        ({"exclude_links": (re.compile("/video/"),)}, {"link": None}, True),
        ({"include_sections": frozenset({"world"})}, {"categories": ("World", "Sport")}, True),
        ({"include_sections": frozenset({"world"})}, {"categories": ("Sport", "World")}, False),  # first only
        ({"include_sections": frozenset({"world"})}, {"categories": ()}, False),
        ({"exclude_sections": frozenset({"sport"})}, {"categories": ("SPORT", "World")}, False),
        (
            {"exclude_sections": frozenset({"sport"})},
            {"categories": ("World", "Sport")},
            True,
        ),  # a tag, not the section
        ({"exclude_sections": frozenset({"sport"})}, {"categories": ("Sports",)}, True),  # exact only
        ({"exclude_sections": frozenset({"sport"})}, {"categories": ()}, True),
        ({"exclude_authors": frozenset({"bot"})}, {"author": "Bot"}, False),
        ({"exclude_authors": frozenset({"bot"})}, {"author": "Robot"}, True),
    ],
)
def test_each_filter(options, fields, kept):
    assert keep(item(**fields), feed(**options)) is kept


def test_every_include_list_must_match_then_any_exclude_drops():
    both = feed(include_categories=frozenset({"world"}), include_titles=(re.compile("Kyiv"),))
    assert keep(item(categories=("World",), title="Kyiv bridges"), both)
    assert not keep(item(categories=("World",), title="Paris"), both)
    assert not keep(item(categories=("Sport",), title="Kyiv derby"), both)
    # An item the includes keep is still dropped by an exclude.
    mixed = feed(include_categories=frozenset({"world"}), exclude_categories=frozenset({"bits"}))
    assert keep(item(categories=("World",)), mixed)
    assert not keep(item(categories=("World", "Bits")), mixed)


def test_sections_include_first_then_exclude():
    both = feed(include_sections=frozenset({"features", "ai"}), exclude_sections=frozenset({"ai"}))
    assert keep(item(categories=("Features", "AI")), both)
    assert not keep(item(categories=("AI", "Features")), both)
    assert not keep(item(categories=("Tech", "Features")), both)


TECH_FEED = Rss(
    "techsite.xml",
    "Tech Site",
    ("https://feeds.techsite.example/index",),
    exclude_sections=frozenset({"policy", "space", "gaming", "cars", "culture"}),
)


def test_ars_sections_keep_cross_tagged_articles():
    items = parse(TECH_SAMPLE, "https://feeds.techsite.example/index")
    kept = {i.title: i.categories for i in items if keep(i, TECH_FEED)}
    assert kept == {
        # Features, tagged Gaming
        "Grand Theft Auto VI: The Tech Site review": (
            "Features",
            "Gaming",
            "grand theft auto",
            "review",
            "rockstar",
        ),
        # AI, tagged Cars
        "Tesla gave up the EV crown to chase autonomy. It still has to deliver": (
            "AI",
            "Cars",
            "autonomy",
            "Elon Musk",
            "Tesla",
        ),
        # AI, tagged Culture
        "The new human-crafted Apple Music brand matters in an AI world": (
            "AI",
            "Apple",
            "Culture",
            "apple",
            "opinion",
        ),
        # AI, tagged Policy
        "States sue SpaceX to stop Grok from generating abusive images": ("AI", "Policy", "Elon Musk", "grok"),
        "Trump halts Iowa detonation, says new governor to blame if it blows accidentally": (
            "Health",
            "army",
            "detonation",
            "Iowa",
        ),
        "COP31 ends in failure as countries split over fossil fuels": (
            "Science",
            "climate change",
            "COP31",
            "syndication",
        ),
    }
    dropped = {i.categories[0] for i in items if not keep(i, TECH_FEED)}
    assert dropped == {"Policy", "Space", "Gaming", "Cars", "Culture"}
    # Excluding the same names as categories would drop the first four.
    by_category = Rss("a.xml", "A", TECH_FEED.sources, exclude_categories=TECH_FEED.exclude_sections)
    assert [i.title for i in items if keep(i, by_category)] == [
        "Trump halts Iowa detonation, says new governor to blame if it blows accidentally",
        "COP31 ends in failure as countries split over fossil fuels",
    ]


@pytest.mark.parametrize(
    ("url", "canonical"),
    [
        ("HTTPS://News.Example/Path/", "https://news.example/Path"),  # host lowercased, path case kept
        ("https://www.news.example/a/", "https://www.news.example/a"),  # www. kept
        ("https://news.example/a#comments", "https://news.example/a"),
        ("https://news.example/", "https://news.example"),
        (
            "https://news.example/a?utm_source=x&UTM_Medium=y&fbclid=1&gclid=2&ref=hn&mc_cid=3&mc_eid=4",
            "https://news.example/a",
        ),
        ("https://news.example/a?b=2&a=1&utm_campaign=z", "https://news.example/a?a=1&b=2"),
        ("https://news.example/a?id=7&referrer=x", "https://news.example/a?id=7&referrer=x"),
        (" https://news.example/a ", "https://news.example/a"),
    ],
)
def test_canonical_link(url, canonical):
    assert canonical_link(url) == canonical


def test_normal_title_folds_case_unicode_and_punctuation():
    assert normal_title("Kyiv’s  bridges — why they MATTER!") == normal_title("kyiv s bridges: why they matter")
    assert normal_title("ﬁnal ＣＡＬＬ") == "final call"  # NFKC
    assert normal_title("Straße") == normal_title("STRASSE")  # casefold
    assert normal_title("?!") == ""


def test_dedup_key():
    assert dedup_key(item(link="https://News.example/a/?utm_source=x")) == "https://news.example/a"
    assert dedup_key(item(link=None, guid="tag:x,2026:1")) == "guid:tag:x,2026:1"


@pytest.mark.parametrize(
    ("options", "fields", "drop"),
    [
        ({}, {}, None),
        ({"include_categories": frozenset({"world"})}, {"categories": ("Sport",)}, Drop("include_categories")),
        ({"include_sections": frozenset({"world"})}, {"categories": ("Sport", "World")}, Drop("include_sections")),
        ({"include_titles": (re.compile("^News"),)}, {"title": "Old News"}, Drop("include_titles")),
        ({"include_links": (re.compile("/articles/"),)}, {"link": None}, Drop("include_links")),
        (
            {"exclude_categories": frozenset({"bits"})},
            {"categories": ("Mondo", "BITS")},
            Drop("exclude_categories", "bits"),
        ),
        ({"exclude_sections": frozenset({"sport"})}, {"categories": ("Sport",)}, Drop("exclude_sections", "sport")),
        (
            {"exclude_titles": (re.compile("(?i)sponsored"),)},
            {"title": "Sponsored"},
            Drop("exclude_titles", "(?i)sponsored"),
        ),
        (
            {"exclude_links": (re.compile("/video/"),)},
            {"link": "https://n.example/video/1"},
            Drop("exclude_links", "/video/"),
        ),
        ({"exclude_authors": frozenset({"bot"})}, {"author": "Bot"}, Drop("exclude_authors", "bot")),
    ],
)
def test_verdict_names_the_filter_that_drops(options, fields, drop):
    assert verdict(item(**fields), feed(**options)) == drop


def test_verdict_reports_the_first_filter_in_order():
    both = feed(include_titles=(re.compile("Kyiv"),), exclude_categories=frozenset({"bits"}))
    assert verdict(item(title="Paris", categories=("Bits",)), both) == Drop("include_titles")
    assert verdict(item(title="Kyiv", categories=("Bits",)), both) == Drop("exclude_categories", "bits")


A, B = "https://a.example/feed", "https://b.example/feed"


def test_select_filters_merges_copies_and_reports_every_item():
    two = Rss("t.xml", "T", (A, B), exclude_categories=frozenset({"bits"}))
    first = item(guid="a1", link="https://news.example/x", published=200, categories=("World",))
    copy = item(guid="b1", link="https://NEWS.example/x/?utm_source=b", published=100, categories=("Europe",))
    dropped = item(guid="a2", link="https://news.example/y", categories=("Bits",))
    chosen = select(two, [(A, [first, dropped]), (B, [copy])])
    # The earliest copy is kept, with the categories of both.
    assert list(chosen.items) == ["https://news.example/x"]
    kept = chosen.items["https://news.example/x"]
    assert kept.guid == "b1" and kept.categories == ("Europe", "World")
    assert chosen.sources == {"https://news.example/x": [A, B]}
    assert chosen.origin == {} and chosen.moved == set()
    assert [(c.source, c.item.guid, c.drop, c.key, c.merged) for c in chosen.choices] == [
        (A, "a1", None, "https://news.example/x", True),
        (A, "a2", Drop("exclude_categories", "bits"), None, False),
        (B, "b1", None, "https://news.example/x", False),
    ]


def test_select_keeps_known_stories_and_follows_a_moved_link():
    known = {"https://news.example/old": item(guid="g", link="https://news.example/old")}
    moved = item(guid="g", link="https://news.example/new", title="Edited")
    chosen = select(feed(), [(SOURCE, [moved])], known)
    assert list(chosen.items) == ["https://news.example/new"]
    assert chosen.items["https://news.example/new"].title == "Edited"
    assert chosen.origin == {"https://news.example/new": "https://news.example/old"}
    assert chosen.moved == {"https://news.example/new"}
    assert chosen.sources == {"https://news.example/new": [SOURCE]}
    assert chosen.choices[0].key == "https://news.example/new" and chosen.choices[0].merged


def test_select_renames_the_keys_of_earlier_copies_when_a_link_moves():
    two = Rss("t.xml", "T", (A, B))
    original = item(guid="g", link="https://news.example/old", published=100)
    edited = item(guid="g", link="https://news.example/new", published=200)
    chosen = select(two, [(A, [original]), (B, [edited])])
    assert list(chosen.items) == ["https://news.example/new"]
    assert chosen.moved == {"https://news.example/new"}
    assert chosen.sources == {"https://news.example/new": [A, B]}
    assert [c.key for c in chosen.choices] == ["https://news.example/new", "https://news.example/new"]
    # Moved twice in one run: the key follows, and stays marked as moved.
    again = item(guid="g", link="https://news.example/newer", published=300)
    chosen = select(Rss("t.xml", "T", (A, B, SOURCE)), [(A, [original]), (B, [edited]), (SOURCE, [again])])
    assert list(chosen.items) == ["https://news.example/newer"] and chosen.moved == {"https://news.example/newer"}


def test_select_merges_by_title_with_link_title_dedupe():
    titled = Rss("t.xml", "T", (A, B), dedupe="link+title")
    one = item(guid="1", link="https://a.example/1", title="Kyiv’s bridges", published=1)
    two = item(guid="2", link="https://b.example/2", title="kyiv s bridges", published=2)
    assert list(select(titled, [(A, [one]), (B, [two])]).items) == ["https://a.example/1"]
    assert len(select(Rss("t.xml", "T", (A, B)), [(A, [one]), (B, [two])]).items) == 2


def test_parse_keeps_the_whole_text_and_the_excerpt_cuts_it():
    long = "word " * 600
    body = (
        '<rss version="2.0"><channel><title>S</title><item><title>T</title><link>https://n.example/1</link>'
        f"<description>&lt;p&gt;{long}sponsored by Acme&lt;/p&gt;</description></item></channel></rss>"
    ).encode()
    (parsed,) = parse(body, "https://n.example/feed")
    assert parsed.text.endswith("sponsored by Acme") and len(parsed.text) > EXCERPT_LIMIT
    assert len(parsed.summary) <= EXCERPT_LIMIT and "Acme" not in parsed.summary
    assert parsed == replace(parsed, text="")  # the text tells no two copies apart


SPONSORED = feed(exclude_text=(re.compile(r"(?i)\bsponsored by acme\b"),))


def test_exclude_text_searches_the_title_and_the_text():
    assert verdict(item(text="A study, sponsored by Acme, found…"), SPONSORED) == Drop(
        "exclude_text", r"(?i)\bsponsored by acme\b"
    )
    assert verdict(item(title="Sponsored by Acme: a study", text="body"), SPONSORED) is not None
    assert verdict(item(text="Acme sponsored nothing here"), SPONSORED) is None


def test_exclude_text_judges_items_without_text_on_their_title():
    # Items from state.json carry no text: they passed exclude_text with it when first seen.
    assert keep(item(title="A study of digital sovereignty"), SPONSORED)
    assert not keep(item(title="A study sponsored by Acme"), SPONSORED)


BLOG_API = "https://blog.example/api/posts?language=en"
FIELDS = JsonFields(
    items="data.results",
    title="title",
    link="url",
    date="date",
    date_format="%B %d, %Y",
    summary="excerpt",
    author="author.name",
    image="media.0.src",
)


def blog(*posts):
    return json.dumps({"data": {"results": list(posts)}}).encode()


def test_parse_json_maps_the_fields():
    (post,) = parse_json(
        blog(
            {
                "title": "SLES 16.1 <b>is here</b>",
                "url": "/blog/sles-16-1/",
                "date": "November 10, 2026",
                "excerpt": "<p>More choice</p> for the enterprise",
                "author": {"name": " Ada  Lovelace "},
                "media": [{"src": "/img/a.png"}],
            }
        ),
        BLOG_API,
        FIELDS,
    )
    assert post.title == "SLES 16.1 is here"
    assert post.link == post.guid == "https://blog.example/blog/sles-16-1/"
    assert post.published == 1_794_268_800  # 2026-11-10 00:00 UTC
    assert post.text == "More choice for the enterprise" and post.summary == "More choice\n\nfor the enterprise"
    assert post.author == "Ada Lovelace" and post.image == "https://blog.example/img/a.png"
    assert post.categories == ()


@pytest.mark.parametrize(
    ("value", "fmt", "expected"),
    [
        ("2026-11-10T08:30:00+01:00", None, 1_794_295_800),  # 07:30 UTC
        ("2026-11-10", None, 1_794_268_800),
        (1_794_268_800, None, 1_794_268_800),
        (1_794_268_800_000, None, 1_794_268_800),  # milliseconds
        ("10/11/2026", "%d/%m/%Y", 1_794_268_800),
        ("tomorrow", None, None),
        ("November 10, 2026", "%d/%m/%Y", None),
        (True, None, None),
        (None, None, None),
    ],
)
def test_json_dates(value, fmt, expected):
    fields = JsonFields(items="data.results", title="t", link="u", date="d", date_format=fmt)
    (post,) = parse_json(blog({"t": "T", "u": "https://blog.example/1", "d": value}), BLOG_API, fields)
    assert post.published == expected


def test_parse_json_skips_items_without_a_title_or_a_link():
    posts = [
        {"title": "No link"},
        {"url": "https://blog.example/no-title"},
        {"title": "  ", "url": "https://blog.example/blank"},
        {"title": "Bad link", "url": "javascript:alert(1)"},
        {"title": 3, "url": "https://blog.example/number"},
        "not an object",
        {"title": "Kept", "url": "https://blog.example/kept"},
    ]
    minimal = JsonFields(items="data.results", title="title", link="url")
    assert [p.title for p in parse_json(blog(*posts), BLOG_API, minimal)] == ["Kept"]


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (b"<html>", "not JSON"),
        (b'{"data": {"results": {"a": 1}}}', "not a list at 'data.results'"),
        (b"[]", "not a list"),
    ],
)
def test_parse_json_rejects_what_isnt_a_list_of_items(body, message):
    with pytest.raises(ValueError, match=message):
        parse_json(body, BLOG_API, FIELDS)


def test_fetch_reads_a_json_source(web):
    web.json(BLOG_API, {"data": {"results": [{"title": "T", "url": "/t"}]}})
    fetched = fetch(BLOG_API, fields=JsonFields(items="data.results", title="title", link="url"))
    assert [i.link for i in fetched.items] == ["https://blog.example/t"]


def test_labelled_puts_the_label_in_front_unless_the_title_has_it():
    items = [item(title="v2.15.3"), item(title="Longhorn v1.12.2"), item(title="longhorn v1.12.3")]
    assert [i.title for i in labelled(items, "Longhorn")] == [
        "Longhorn v2.15.3",
        "Longhorn v1.12.2",
        "longhorn v1.12.3",
    ]
    assert labelled(items, None) is items

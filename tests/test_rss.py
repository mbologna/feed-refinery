import re
import urllib.error
from pathlib import Path

import pytest

from feeds import USER_AGENT, rss
from feeds.config import Rss
from feeds.rss import Fetched, Item, canonical_link, dedup_key, fetch, keep, normal_title, parse

FIXTURES = Path(__file__).parent / "fixtures"
NEWS_SAMPLE = (FIXTURES / "newssite.xml").read_bytes()
ATOM = (FIXTURES / "atom.xml").read_bytes()
BROKEN = (FIXTURES / "broken.xml").read_bytes()
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

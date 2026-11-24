import re
from pathlib import Path

import pytest

from feeds.config import ConfigError, Digest, Feed, Rss, load, parse

REPO_CONFIG = Path(__file__).parent.parent / "feeds.toml"


def test_the_repo_config_is_valid():
    conf = load(REPO_CONFIG)
    feeds = conf.feeds
    assert conf.digests == [Digest("digest.xml", "Hacker News: daily digest")]
    assert conf.files == ["frontpage.xml", "best.xml", "top.xml", "digest.xml", "newssite.xml", "techsite.xml"]
    assert [f.file for f in feeds] == ["frontpage.xml", "best.xml", "top.xml"]
    assert feeds[0].follows_front_page
    assert not feeds[1].follows_front_page
    assert feeds[1].min_points == 100
    assert feeds[2].ranked and feeds[2].min_points == 150
    newssite, tech = conf.rss
    assert newssite.sources == ("https://www.newssite.example/feed",)
    assert newssite.exclude_categories == {"peanuts", "foto del giorno", "celebrity"}
    assert newssite.exclude_links[0].search("https://www.newssite.example/episodes/x/")
    assert not newssite.exclude_links[0].search("https://www.newssite.example/flashes/x/")
    assert newssite.exclude_titles[0].search("Le prime pagine di oggi")
    assert newssite.enrich and newssite.max_items == 100 and newssite.dedupe == "link"
    assert tech.sources[0] == "https://feeds.techsite.example/index"
    assert len(tech.sources) == 5 and all(s.startswith("https://feeds.techsite.example/") for s in tech.sources)
    assert tech.exclude_sections == {"policy", "space", "gaming", "cars", "culture"}
    assert not tech.exclude_categories and not tech.include_sections
    assert tech.enrich and tech.dedupe == "link"


def test_defaults_apply():
    (feed,) = parse('[[feed]]\nfile = "a.xml"\ntitle = "A"\ntags = "story"\n').feeds
    assert feed == Feed("a.xml", "A", "story", min_points=0, min_comments=0, window_hours=48, max_items=150)
    assert not feed.ranked


def test_front_page_among_several_tags():
    (feed,) = parse('[[feed]]\nfile = "a.xml"\ntitle = "A"\ntags = "front_page,show_hn"\n').feeds
    assert feed.follows_front_page


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("this is = = not toml", "not valid TOML"),
        ("", r"no \[\[feed\]\], \[\[digest\]\] or \[\[rss\]\] tables"),
        ('feed = "x"', r"no \[\[feed\]\], \[\[digest\]\] or \[\[rss\]\] tables"),
        ('other = 1\n[[feed]]\nfile = "a.xml"\ntitle = "A"\ntags = "story"', "unknown top-level keys: other"),
        ('[[feed]]\nfile = "a.xml"\ntitle = "A"\ntags = "story"\nmin_point = 5', "feed #1: unknown keys: min_point"),
        ('[[feed]]\nfile = "a.xml"\ntags = "story"', "feed #1: missing title"),
        ('[[feed]]\nfile = "a.xml"\ntitle = " "\ntags = "story"', "title must be a non-empty string"),
        ('[[feed]]\nfile = "a.xml"\ntitle = "A"\ntags = "story"\nmin_points = -1', "min_points must be a whole number"),
        (
            '[[feed]]\nfile = "a.xml"\ntitle = "A"\ntags = "story"\nmin_points = "100"',
            "min_points must be a whole number",
        ),
        ('[[feed]]\nfile = "a.xml"\ntitle = "A"\ntags = "story"\nmax_items = true', "max_items must be a whole number"),
        ('[[feed]]\nfile = "a.xml"\ntitle = "A"\ntags = "story"\nwindow_hours = 0', "must be at least 1"),
        ('[[feed]]\nfile = "a.xml"\ntitle = "A"\ntags = "story"\nranked = 1', "ranked must be true or false"),
        ('[[feed]]\nfile = "../a.xml"\ntitle = "A"\ntags = "story"', "file must be a lowercase name ending in .xml"),
        ('[[feed]]\nfile = "index.html"\ntitle = "A"\ntags = "story"', "file must be a lowercase name ending in .xml"),
        (
            '[[feed]]\nfile = "a.xml"\ntitle = "A"\ntags = "story"\n[[feed]]\nfile = "a.xml"\ntitle = "B"\ntags = "ask_hn"',
            "feed #2: a.xml is already used",
        ),
    ],
)
def test_bad_configs_fail_with_a_message(text, message):
    with pytest.raises(ConfigError, match=message):
        parse(text)


def test_digest_defaults_and_digest_only_config():
    conf = parse('[[digest]]\nfile = "d.xml"\ntitle = "D"\n')
    assert conf.feeds == []
    assert conf.digests == [Digest("d.xml", "D", top=20, days=7, publish_hour=6)]


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ('[[digest]]\nfile = "d.xml"', "digest #1: missing title"),
        ('[[digest]]\nfile = "d.xml"\ntitle = ""', "digest #1: title must be a non-empty string"),
        ('[[digest]]\nfile = "d.xml"\ntitle = "D"\ntags = "story"', "digest #1: unknown keys: tags"),
        ('[[digest]]\nfile = "d.xml"\ntitle = "D"\ntop = 0', "top must be a whole number from 1 to 100"),
        (
            '[[digest]]\nfile = "d.xml"\ntitle = "D"\npublish_hour = 24',
            "publish_hour must be a whole number from 0 to 23",
        ),
        ('[[digest]]\nfile = "d.xml"\ntitle = "D"\ndays = true', "days must be a whole number"),
        ('[[digest]]\nfile = "D.XML"\ntitle = "D"', "file must be a lowercase name"),
        (
            '[[feed]]\nfile = "a.xml"\ntitle = "A"\ntags = "story"\n[[digest]]\nfile = "a.xml"\ntitle = "D"',
            "digest #1: a.xml is already used",
        ),
    ],
)
def test_bad_digests_fail_with_a_message(text, message):
    with pytest.raises(ConfigError, match=message):
        parse(text)


RSS = '[[rss]]\nfile = "r.xml"\ntitle = "R"\n'


def test_rss_defaults_and_rss_only_config():
    conf = parse(RSS + 'source = "https://a.example/feed"\n')
    assert conf.feeds == [] and conf.digests == []
    assert conf.rss == [Rss("r.xml", "R", ("https://a.example/feed",))]
    assert conf.files == ["r.xml"]
    (feed,) = conf.rss
    assert feed.enrich and feed.max_items == 100 and feed.dedupe == "link"
    assert not feed.include_categories and not feed.exclude_titles


def test_rss_with_every_key():
    (feed,) = parse(
        RSS
        + """sources = ["https://a.example/feed", "https://b.example/rss"]
include_categories = ["World", " Science "]
exclude_categories = ["Bits"]
include_sections = ["Features", " AI"]
exclude_sections = ["Gaming"]
exclude_authors = ["Bot"]
include_titles = ["^News"]
exclude_titles = ["(?i)sponsored"]
include_links = ["/articles/"]
exclude_links = ["/video/"]
enrich = false
max_items = 20
dedupe = "link+title"
"""
    ).rss
    assert feed.sources == ("https://a.example/feed", "https://b.example/rss")
    assert feed.include_categories == {"world", "science"}  # stripped and case-folded
    assert feed.exclude_categories == {"bits"} and feed.exclude_authors == {"bot"}
    assert feed.include_sections == {"features", "ai"} and feed.exclude_sections == {"gaming"}
    assert feed.include_titles == (re.compile("^News"),)
    assert feed.exclude_titles[0].search("A SPONSORED post")
    assert feed.include_links == (re.compile("/articles/"),) and feed.exclude_links == (re.compile("/video/"),)
    assert not feed.enrich and feed.max_items == 20 and feed.dedupe == "link+title"


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (RSS, "rss #1: needs either source"),
        (RSS + 'source = "https://a.example/"\nsources = ["https://b.example/"]', "needs either source"),
        ('[[rss]]\nfile = "r.xml"\nsource = "https://a.example/"', "rss #1: missing title"),
        ('[[rss]]\nfile = "r.xml"\ntitle = 1\nsource = "https://a.example/"', "title must be a non-empty string"),
        (RSS + 'source = "https://a.example/"\ntags = "x"', "rss #1: unknown keys: tags"),
        (RSS + 'source = "ftp://a.example/"', "source must be an http or https URL"),
        (RSS + 'source = "https://"', "source must be an http or https URL"),
        (RSS + "sources = []", "sources must be a non-empty list"),
        (RSS + 'sources = "https://a.example/"', "sources must be a non-empty list"),
        (RSS + 'sources = ["https://a.example/", "file:///etc/passwd"]', "sources must be a non-empty list"),
        (RSS + 'sources = ["https://a.example/", "https://a.example/"]', "sources lists a URL twice"),
        (RSS + 'source = "https://a.example/"\nexclude_categories = "Bits"', "exclude_categories must be a list"),
        (RSS + 'source = "https://a.example/"\ninclude_categories = [""]', "include_categories must be a list"),
        (RSS + 'source = "https://a.example/"\nexclude_authors = [1]', "exclude_authors must be a list"),
        (RSS + 'source = "https://a.example/"\ninclude_sections = "AI"', "include_sections must be a list"),
        (RSS + 'source = "https://a.example/"\nexclude_sections = [" "]', "exclude_sections must be a list"),
        (RSS + 'source = "https://a.example/"\nexclude_section = ["AI"]', "unknown keys: exclude_section"),
        (RSS + 'source = "https://a.example/"\nexclude_titles = ["(unclosed"]', "exclude_titles: '\\(unclosed'"),
        (RSS + 'source = "https://a.example/"\ninclude_links = ["[a-"]', "include_links: .* is not a valid regular"),
        (RSS + 'source = "https://a.example/"\nenrich = "yes"', "enrich must be true or false"),
        (RSS + 'source = "https://a.example/"\nmax_items = 0', "max_items must be a whole number from 1 to 1000"),
        (RSS + 'source = "https://a.example/"\nmax_items = true', "max_items must be a whole number"),
        (RSS + 'source = "https://a.example/"\nmax_items = 1001', "max_items must be a whole number"),
        (RSS + 'source = "https://a.example/"\ndedupe = "title"', "dedupe must be one of: 'link', 'link\\+title'"),
        ('[[rss]]\nfile = "R.xml"\ntitle = "R"\nsource = "https://a.example/"', "file must be a lowercase name"),
        ('[[rss]]\nfile = "state.json"\ntitle = "R"\nsource = "https://a.example/"', "file must be a lowercase"),
        (
            '[[digest]]\nfile = "r.xml"\ntitle = "D"\n' + RSS + 'source = "https://a.example/"',
            "rss #1: r.xml is already used",
        ),
        (
            '[[feed]]\nfile = "r.xml"\ntitle = "A"\ntags = "story"\n' + RSS + 'source = "https://a.example/"',
            "rss #1: r.xml is already used",
        ),
        (RSS + 'source = "https://a.example/"\n' + RSS + 'source = "https://b.example/"', "rss #2: r.xml is already"),
        ("rss = 1", r"no \[\[feed\]\], \[\[digest\]\] or \[\[rss\]\] tables"),
    ],
)
def test_bad_rss_tables_fail_with_a_message(text, message):
    with pytest.raises(ConfigError, match=message):
        parse(text)

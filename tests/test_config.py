import re
from pathlib import Path

import pytest

from feed_refinery.config import ConfigError, Digest, Feed, JsonFields, Rss, load, parse

EXAMPLE_CONFIG = Path(__file__).parent.parent / "examples" / "feeds.toml"


def test_the_example_config_is_valid():
    # The real feeds.toml is private (mbologna/feeds-config); the example
    # shows every kind of table. The parsing tests below use their own tables.
    conf = load(EXAMPLE_CONFIG)
    assert conf.files and conf.feeds and conf.digests and conf.rss


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
exclude_text = ["(?i)sponsored by"]
enrich = false
exclude_on_hacker_news = true
max_items = 20
keep_days = 30
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
    assert feed.exclude_text == (re.compile("(?i)sponsored by"),)
    assert feed.exclude_on_hacker_news and not feed.exclude_paywalled and feed.keep_days == 30


def test_rss_paywall_flag_and_defaults():
    (feed,) = parse(RSS + 'source = "https://a.example/"\nexclude_paywalled = true\n').rss
    assert feed.exclude_paywalled and feed.enrich
    (plain,) = parse(RSS + 'source = "https://a.example/"\n').rss
    assert not plain.exclude_paywalled and not plain.exclude_on_hacker_news and plain.keep_days == 7
    assert plain.exclude_text == ()


def test_hn_tables_take_exclude_titles():
    conf = parse(
        '[[feed]]\nfile = "a.xml"\ntitle = "A"\ntags = "story"\nexclude_titles = ["^Launch HN:"]\n'
        '[[digest]]\nfile = "d.xml"\ntitle = "D"\nexclude_titles = ["(?i)^launch hn"]\n'
    )
    assert conf.feeds[0].exclude_titles == (re.compile("^Launch HN:"),)
    assert conf.digests[0].exclude_titles[0].search("LAUNCH HN: Acme")
    assert parse('[[feed]]\nfile = "a.xml"\ntitle = "A"\ntags = "story"\n').feeds[0].exclude_titles == ()


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ('[[feed]]\nfile = "a.xml"\ntitle = "A"\ntags = "story"\nexclude_titles = "x"', "feed #1: exclude_titles must"),
        ('[[feed]]\nfile = "a.xml"\ntitle = "A"\ntags = "story"\nexclude_titles = ["("]', "feed #1: exclude_titles:"),
        ('[[digest]]\nfile = "d.xml"\ntitle = "D"\nexclude_titles = ["("]', "digest #1: exclude_titles:"),
    ],
)
def test_bad_hn_exclude_titles_fail_with_a_message(text, message):
    with pytest.raises(ConfigError, match=message):
        parse(text)


@pytest.mark.parametrize(
    ("tables", "floor"),
    [
        ('[[feed]]\nfile = "a.xml"\ntitle = "A"\ntags = "front_page"\n', 100),
        (
            '[[feed]]\nfile = "a.xml"\ntitle = "A"\ntags = "story"\nmin_points = 150\n'
            '[[feed]]\nfile = "b.xml"\ntitle = "B"\ntags = "story"\nmin_points = 80\n',
            80,
        ),
        (RSS + 'source = "https://a.example/"\n', 100),
    ],
)
def test_hacker_news_floor_is_the_lowest_points_threshold(tables, floor):
    assert parse(tables).hacker_news_floor == floor


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
        (RSS + 'source = "https://a.example/"\nexclude_text = ["(x"]', "exclude_text: .* is not a valid regular"),
        (RSS + 'source = "https://a.example/"\nexclude_paywalled = 1', "exclude_paywalled must be true or false"),
        (RSS + 'source = "https://a.example/"\nexclude_on_hacker_news = "y"', "exclude_on_hacker_news must be true"),
        (RSS + 'source = "https://a.example/"\nexclude_paywalled = true\nenrich = false', "exclude_paywalled needs"),
        (RSS + 'source = "https://a.example/"\nkeep_days = 0', "keep_days must be a whole number from 1 to 365"),
        (RSS + 'source = "https://a.example/"\nkeep_days = 366', "keep_days must be a whole number"),
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


def test_filters_key_follows_the_filters_only():
    base = parse(RSS + 'source = "https://a.example/"\nexclude_titles = ["^A", "^B"]\n').rss[0]
    same = parse(RSS + 'source = "https://a.example/"\nexclude_titles = ["^B", "^A"]\nmax_items = 5\n').rss[0]
    other = parse(RSS + 'source = "https://a.example/"\nexclude_titles = ["^A"]\n').rss[0]
    text = parse(RSS + 'source = "https://a.example/"\nexclude_titles = ["^A", "^B"]\nexclude_text = ["x"]\n').rss[0]
    assert base.filters_key == same.filters_key  # order and non-filter options don't count
    assert len({base.filters_key, other.filters_key, text.filters_key}) == 3


def test_rss_json_source():
    (feed,) = parse(
        RSS + 'source = "https://a.example/api"\njson = { items = "data.posts", title = "t", link = "u", date = "d" }\n'
    ).rss
    assert feed.json == JsonFields(items="data.posts", title="t", link="u", date="d")
    assert parse(RSS + 'source = "https://a.example/"\n').rss[0].json is None


@pytest.mark.parametrize(
    ("json_value", "message"),
    [
        ('"posts"', "json must be a table"),
        ('{ items = "p", title = "t" }', "json: missing link"),
        ('{ items = "p", title = "t", link = "u", body = "b" }', "json: unknown keys: body"),
        ('{ items = "p", title = "t", link = "" }', "json: link must be a non-empty string"),
        ('{ items = "p", title = "t", link = "u", date_format = "YYYY" }', "json: date_format must be a strptime"),
    ],
)
def test_bad_json_sources_fail_with_a_message(json_value, message):
    with pytest.raises(ConfigError, match=message):
        parse(RSS + f'source = "https://a.example/api"\njson = {json_value}\n')


def test_rss_source_titles():
    (feed,) = parse(
        RSS
        + 'sources = ["https://a.example/r.atom", "https://b.example/r.atom"]\n'
        + '[rss.source_titles]\n"https://a.example/r.atom" = " Alpha "\n'
    ).rss
    assert feed.source_titles == (("https://a.example/r.atom", "Alpha"),)
    assert feed.label("https://a.example/r.atom") == "Alpha" and feed.label("https://b.example/r.atom") is None


@pytest.mark.parametrize(
    ("titles", "message"),
    [
        ('source_titles = "Alpha"', "source_titles must be a table"),
        ('source_titles = { "https://a.example/r.atom" = "" }', "source_titles must be a table"),
        ('source_titles = { "https://c.example/r.atom" = "C" }', "https://c.example/r.atom is not one of the sources"),
    ],
)
def test_bad_source_titles_fail_with_a_message(titles, message):
    with pytest.raises(ConfigError, match=message):
        parse(RSS + 'source = "https://a.example/r.atom"\n' + titles + "\n")


def test_site_and_icon_defaults_and_overrides():
    conf = parse(
        '[[feed]]\nfile = "a.xml"\ntitle = "A"\ntags = "story"\n'
        '[[digest]]\nfile = "d.xml"\ntitle = "D"\nsite = "https://links.example.org/"\n'
        + RSS
        + 'source = "https://feeds.example.com/x/rss?y=1"\n'
        + '[[rss]]\nfile = "s.xml"\ntitle = "S"\nsource = "https://a.example/feed"\n'
        + 'site = "https://www.example.org/"\nicon = "https://www.example.org/favicon.png"\n'
    )
    (feed,), (digest,), (plain, labelled) = conf.feeds, conf.digests, conf.rss
    assert (feed.site_url, feed.icon_url) == (
        "https://news.ycombinator.com/",
        "https://news.ycombinator.com/favicon.ico",
    )
    assert (digest.site_url, digest.icon_url) == ("https://links.example.org/", "https://links.example.org/favicon.ico")
    assert (plain.site_url, plain.icon_url) == ("https://feeds.example.com/", "https://feeds.example.com/favicon.ico")
    assert (labelled.site_url, labelled.icon_url) == ("https://www.example.org/", "https://www.example.org/favicon.png")


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (
            '[[feed]]\nfile = "a.xml"\ntitle = "A"\ntags = "story"\nsite = "news.ycombinator.com"',
            "feed #1: site must be",
        ),
        ('[[digest]]\nfile = "d.xml"\ntitle = "D"\nicon = 1', "digest #1: icon must be an http or https URL"),
        (RSS + 'source = "https://a.example/"\nicon = "file:///x.png"', "rss #1: icon must be an http or https URL"),
    ],
)
def test_bad_site_or_icon_fails_with_a_message(text, message):
    with pytest.raises(ConfigError, match=message):
        parse(text)

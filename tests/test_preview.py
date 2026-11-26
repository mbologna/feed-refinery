import json
from pathlib import Path

import pytest
from conftest import FakeResponse

from feed_refinery.preview import MAX_BYTES, Preview, fetch_preview, parse_preview
from feed_refinery.text import EXCERPT_LIMIT, excerpt

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_open_graph_tags():
    p = parse_preview(fixture("og.html"), "https://example.com/blog/post")
    assert p.image == "https://example.com/images/cover.png?a=1&b=2"
    assert p.description == 'An article about "things" & stuff. Second line.'
    assert p.site_name == "Example Blog"


def test_twitter_fallback_skips_non_web_images():
    p = parse_preview(fixture("twitter.html"), "https://example.com/")
    assert p.image == "https://cdn.example.com/card.jpg"
    assert p.description == "Card description"
    assert p.site_name is None


def test_plain_description_fallback():
    p = parse_preview('<head><meta name="description" content="Plain"></head>', "https://example.com/")
    assert p == Preview(description="Plain", words=0)


def test_page_without_tags():
    assert parse_preview("<html><body>nothing</body></html>", "https://example.com/") == Preview(words=0)


def test_long_description_is_cut_at_a_word():
    long = "word " * 600
    p = parse_preview(f'<meta property="og:description" content="{long}">', "https://example.com/")
    assert p.description.endswith("word…")
    assert len(p.description) <= EXCERPT_LIMIT


def test_excerpt_without_spaces():
    assert excerpt("x" * 400, 10) == "x" * 9 + "…"


def test_fetch_resolves_against_the_final_url(web):
    web.html("https://short.example/a", fixture("og.html"), final_url="https://example.com/blog/post")
    p = fetch_preview("https://short.example/a")
    assert p.image == "https://example.com/images/cover.png?a=1&b=2"
    assert web.requests == ["https://short.example/a"]


def test_fetch_skips_non_html(web):
    web.html("https://example.com/paper.pdf", "%PDF-1.7", content_type="application/pdf")
    assert fetch_preview("https://example.com/paper.pdf") == Preview()


def test_fetch_never_opens_a_non_web_link(web):
    assert fetch_preview("file:///etc/passwd") == Preview()
    assert web.requests == []


def test_fetch_decodes_the_declared_charset(web):
    body = '<meta property="og:description" content="caffè">'
    web.routes["https://example.com/latin"] = lambda: FakeResponse(
        body.encode("latin-1"), "text/html; charset=iso-8859-1", "https://example.com/latin"
    )
    assert fetch_preview("https://example.com/latin").description == "caffè"


def test_fetch_survives_an_unknown_charset(web):
    web.html("https://example.com/odd", '<meta property="og:site_name" content="Odd">', "text/html; charset=x-nope")
    assert fetch_preview("https://example.com/odd").site_name == "Odd"


def test_fetch_reads_at_most_max_bytes(web):
    head = '<meta property="og:site_name" content="Big">'
    web.html("https://example.com/big", head + "x" * (MAX_BYTES * 2))
    assert fetch_preview("https://example.com/big").site_name == "Big"


def test_fetch_raises_on_http_errors(web):
    web.status("https://example.com/gone", 404)
    with pytest.raises(OSError):
        fetch_preview("https://example.com/gone")


LONG = "This paragraph is long enough to stand in for a page description, being well past the minimum."


def test_first_paragraph_inside_article_stands_in_for_a_description():
    html = f"""<html><head><title>No description</title></head><body>
    <nav><p>{LONG} (navigation)</p></nav>
    <p>Short byline.</p>
    <p>{LONG} (outside the article)</p>
    <article><p>Too short.</p><p>{LONG} <a href="/x">(the article)</a><br>next line</p></article>
    </body></html>"""
    assert parse_preview(html, "https://example.com/").description == f"{LONG} (the article) next line"


def test_paragraphs_anywhere_without_an_article():
    html = f"<body><header><p>{LONG} (header)</p></header><p>{LONG} (one)<p>{LONG} (two)</body>"
    assert parse_preview(html, "https://example.com/").description == f"{LONG} (one)\n\n{LONG} (two)"


def test_opening_paragraphs_skip_short_ones_and_captions():
    html = f"""<body><article><p>Too short to open with.</p><p>{LONG} (first)</p>
    <figure><img src="/x.png"><figcaption><p>{LONG} (caption)</p></figcaption></figure>
    <p>Advertisement</p><p>A short paragraph that still counts.</p></article></body>"""
    assert parse_preview(html, "https://example.com/").description == (
        f"{LONG} (first)\n\nA short paragraph that still counts."
    )


def test_opening_paragraphs_stop_at_the_excerpt_limit():
    html = "<body><main>" + f"<p>{LONG}</p>" * 100 + "</main></body>"
    description = parse_preview(html, "https://example.com/").description
    assert description.endswith(LONG) and len(description) <= EXCERPT_LIMIT


def test_unclosed_paragraph_at_the_end_counts():
    assert parse_preview(f"<body><main><p>{LONG}", "https://example.com/").description == LONG


def test_script_text_is_never_a_paragraph():
    html = f"<body><p><script>var x = '{LONG}';</script></p></body>"
    assert parse_preview(html, "https://example.com/").description is None


def test_words_count_the_articles_paragraphs():
    words = len(LONG.split())
    html = f"<body><p>{LONG} (outside)</p><article><p>{LONG}</p>" + "<p>Two more words.</p>" * 50 + "</article></body>"
    assert parse_preview(html, "https://example.com/").words == words + 150  # every paragraph, past the excerpt's
    html = f"<body><p>{LONG}</p><p>Short.</p><nav><p>{LONG}</p></nav></body>"
    assert parse_preview(html, "https://example.com/").words == words + 1  # anywhere, without an article
    assert parse_preview('<meta name="description" content="D">', "https://example.com/").words == 0  # counted


def test_later_articles_are_teasers_not_the_article():
    # A news site's recommended stories: after the article, a card for each, each an <article>.
    card = f"<article><a href='/other'><h2>Other</h2><p>{LONG} (a teaser)</p></a></article>"
    html = f"<body><main><article><p>{LONG} (the article)</p></article></main><div>{card * 3}</div></body>"
    preview = parse_preview(html, "https://example.com/")
    assert preview.description == f"{LONG} (the article)"
    assert preview.words == len(LONG.split()) + 2


def test_a_first_article_without_paragraphs_is_passed_over():
    html = f"<body><article><p>A card.</p></article><article><p>{LONG}</p></article></body>"
    preview = parse_preview(html, "https://example.com/")
    assert (preview.description, preview.words) == (LONG, len(LONG.split()))


def test_a_longer_opening_beats_the_meta_description():
    html = f'<head><meta name="description" content="Meta"></head><body><article><p>{LONG}</p></article></body>'
    assert parse_preview(html, "https://example.com/").description == LONG


def test_a_longer_meta_description_beats_the_opening():
    html = f'<head><meta name="description" content="{LONG} (meta)"></head><body><p>{LONG}</p></body>'
    assert parse_preview(html, "https://example.com/").description == f"{LONG} (meta)"


@pytest.mark.parametrize(
    ("img", "image"),
    [
        ('<img src="/pic.jpg">', "https://example.com/pic.jpg"),
        ('<figure><img src="/hero.jpg"><figcaption>Hero</figcaption></figure>', "https://example.com/hero.jpg"),
        ('<img src="/pic.jpg" width="1200" height="800px">', "https://example.com/pic.jpg"),
        ('<img src="data:image/gif;base64,R0lG" data-src="/lazy.jpg">', "https://example.com/lazy.jpg"),
        ('<img src="/icon.png" width="32" height="32">', None),
        ('<img src="javascript:alert(1)">', None),
    ],
)
def test_the_articles_first_image_stands_in_for_a_missing_og_image(img, image):
    assert parse_preview(f"<body><article>{img}</article></body>", "https://example.com/a").image == image


def test_images_outside_the_article_text_are_not_used():
    html = '<body><img src="/logo.png"><article><aside><img src="/ad.png"></aside></article></body>'
    assert parse_preview(html, "https://example.com/").image is None


def test_og_image_beats_the_articles_image():
    html = '<head><meta property="og:image" content="/og.png"></head><body><main><img src="/in.png"></main></body>'
    assert parse_preview(html, "https://example.com/").image == "https://example.com/og.png"


def ld(*blocks: str) -> str:
    scripts = "".join(f'<script type="application/ld+json">{b}</script>' for b in blocks)
    return f"<html><head>{scripts}</head><body><p>{'x' * 100}</p></body></html>"


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        (ld('{"@type": "NewsArticle", "isAccessibleForFree": true}'), False),
        (ld('{"@type": "NewsArticle", "isAccessibleForFree": false}'), True),
        (ld('{"@type": "NewsArticle", "isAccessibleForFree": "False"}'), True),  # a string, as many sites write it
        (ld('{"@type": "NewsArticle", "isAccessibleForFree": "true"}'), False),
        (ld('{"@type": "NewsArticle", "isAccessibleForFree": "maybe"}'), None),
        (ld('{"@type": "NewsArticle"}'), None),
        (ld("not json {"), None),
        (ld('{"@graph": [{"@type": "WebPage"}, {"hasPart": {"isAccessibleForFree": false}}]}'), True),
        (ld('[{"isAccessibleForFree": true}]', '{"isAccessibleForFree": false}'), True),  # any false wins
        (ld("not json {", '{"isAccessibleForFree": true}'), False),
        ("<html><head></head><body><p>no structured data</p></body></html>", None),
        ('<script type="text/javascript">{"isAccessibleForFree": false}</script>', None),  # not JSON-LD
        ('<body><script type="Application/LD+JSON">{"isAccessibleForFree": false}</script></body>', True),
    ],
)
def test_paywalled_from_json_ld(html, expected):
    assert parse_preview(html, "https://news.example/a").paywalled is expected


def test_json_ld_is_never_a_description():
    preview = parse_preview(ld('{"description": "' + "y" * 120 + '"}'), "https://news.example/a")
    assert preview.description == "x" * 100


ARTICLE = """<html><head><style>p { color: red }</style></head><body>
<nav><p>A navigation paragraph long enough to be counted as one, but not the article's.</p></nav>
<main><article>
<header><h1>Title</h1><button>Follow</button></header>
<p class="lede" style="x" onclick="evil()">The article's opening paragraph, long enough to start it, with
<a href="/more" onclick="evil()">a <em>relative</em> link</a>, <a href="javascript:evil()">a script link</a>
and <span>a span</span>.</p>
<img src="/wide.jpg" width="1200"><img src="/wide.jpg" width="1200"><img src="/icon.png" width="16">
<h2>Picks <svg><title>icon</title></svg></h2>
<ul><li><strong>One</strong>.<br>The first pick.</li><li>The second pick.<button>Share</button></li></ul>
<p>An unclosed paragraph<blockquote><p>A quote.</p></blockquote>
<p> </p><script>evil()</script><aside><p>A newsletter box inside the article, long enough to be counted.</p></aside>
</article></main></body></html>"""


def test_full_text_copies_the_article_with_its_safe_tags_only():
    p = parse_preview(ARTICLE, "https://example.com/post", full_text=True)
    assert p.content == (
        "<p>The article's opening paragraph, long enough to start it, with "
        '<a href="https://example.com/more">a <em>relative</em> link</a>, a script link and a span.</p>'
        '<p><img src="https://example.com/wide.jpg" alt=""></p>'
        "<h2>Picks </h2>"
        "<ul><li><strong>One</strong>.<br>The first pick.</li><li>The second pick.</li></ul>"
        "<p>An unclosed paragraph</p><blockquote><p>A quote.</p></blockquote>"
    )
    assert parse_preview(ARTICLE, "https://example.com/post").content is None  # only asked for


def test_full_text_needs_an_article_or_main_with_paragraphs():
    page = "<body><p>A paragraph long enough to be the opening of a page with no article element at all.</p></body>"
    assert parse_preview(page, "https://example.com/", full_text=True).content == ""


OPENING = "<p>A paragraph long enough to be an article's opening one, which takes 80 characters or more.</p>"


def test_full_text_starts_with_the_article_holding_paragraphs():
    page = f"<body><article><p>Short card.</p></article><article>{OPENING}</article></body>"
    assert parse_preview(page, "https://example.com/", full_text=True).content == OPENING


def article_ld(body):
    return f'<script type="application/ld+json">{json.dumps({"@type": "NewsArticle", "articleBody": body})}</script>'


def test_full_text_keeps_only_the_blocks_the_article_body_holds():
    opening = "Hi, friends! Welcome to the newsletter, your guide to the <em>best</em> stuff in the world."
    middle = "The picks of the week follow, and every one of them is worth a look this weekend."
    body = (
        "Hi, friends! Welcome to the newsletter, your guide to the best stuff in the world.\n"
        "Picks\n"
        "* One. The first pick.\n"
        "* Two. The second&nbsp;pick.\n"
        "The picks of the week follow, and every one of them is worth a look this weekend.\n"
        "See you next week!\n"
        "8"
    )
    page = (
        f"<head>{article_ld(body)}</head><body><article>"
        "<p>A standfirst that the article body leaves out, long enough to be counted.</p>"
        "<p>Picks</p>"  # short and before the first long block it holds: the article's own heading
        f"<p>{opening}</p><h2>Picks</h2>"
        "<ul><li>One. The first pick.</li><li>Two. The second pick.</li></ul>"
        '<p>An ad between the blocks.</p><p><img src="https://i.example/a.jpg"></p>'
        f"<p>{middle}</p><p>See you next week!</p>"
        '<p><img src="https://i.example/related.jpg"></p><p>8</p><h2>Top Stories</h2>'
        "</article></body>"
    )
    assert parse_preview(page, "https://example.com/", full_text=True).content == (
        f"<p>Picks</p><p>{opening}</p><h2>Picks</h2>"
        "<ul><li>One. The first pick.</li><li>Two. The second pick.</li></ul>"
        '<p><img src="https://i.example/a.jpg" alt=""></p>'
        f"<p>{middle}</p><p>See you next week!</p>"
    )


def test_full_text_keeps_everything_when_no_block_is_in_the_article_body():
    page = f"<head>{article_ld('Something else entirely.')}</head><body><article>{OPENING}</article></body>"
    assert parse_preview(page, "https://example.com/", full_text=True).content == OPENING


def test_full_text_keeps_preformatted_whitespace():
    page = f"<article>{OPENING}<pre>a  b\n c</pre></article>"
    assert parse_preview(page, "https://example.com/", full_text=True).content.endswith("<pre>a  b\n c</pre>")


def test_fetch_with_full_text_reads_more_of_the_page(web):
    filler = "<p>" + "x" * MAX_BYTES + "</p>"
    tail = "<p>The article's paragraph, past the bytes a preview reads, long enough to be its opening.</p>"
    web.html("https://example.com/long", f"<body>{filler}<article>{tail}</article></body>")
    assert fetch_preview("https://example.com/long", full_text=True).content == tail
    assert fetch_preview("https://example.com/long").content is None


def test_fetch_with_full_text_marks_a_page_without_html_as_read(web):
    web.html("https://example.com/paper.pdf", "%PDF-1.7", content_type="application/pdf")
    assert fetch_preview("https://example.com/paper.pdf", full_text=True) == Preview(content="")
    assert fetch_preview("file:///etc/passwd", full_text=True) == Preview(content="")

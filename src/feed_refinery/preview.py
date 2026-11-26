"""Link previews: the image, description and site name a page declares in its <head>, or its opening paragraphs; and,
for full_text feeds, the article itself."""

from __future__ import annotations

import json
import re
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from html import escape, unescape
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from . import USER_AGENT
from .text import EXCERPT_LIMIT, PARAGRAPH, excerpt, pixels, squash

MAX_BYTES = 512 * 1024  # the <head> comes first; no need for the whole page
FULL_MAX_BYTES = 4 * 1024 * 1024  # but the article may end further on
HTML_TYPES = {"text/html", "application/xhtml+xml"}
IMAGE_KEYS = ("og:image", "og:image:url", "og:image:secure_url", "twitter:image", "twitter:image:src")
DESCRIPTION_KEYS = ("og:description", "twitter:description", "description")


@dataclass(frozen=True)
class Preview:
    image: str | None = None
    description: str | None = None
    site_name: str | None = None
    # From the page's JSON-LD isAccessibleForFree: True when it says false
    # anywhere, False when it says true, None when it says nothing.
    paywalled: bool | None = None
    # In the article's paragraphs, for its reading time. None when they weren't
    # counted: a preview fetched before counting began, or one with nothing fetched.
    words: int | None = None
    # The article's HTML, for a full_text feed: "" when the page has none, None
    # when it wasn't asked for.
    content: str | None = None


PARAGRAPH_MIN = 80  # the first paragraph kept: shorter ones are bylines, captions and the like
LATER_MIN = 30  # the next ones: shorter ones are "Advertisement" and the like
IMAGE_MIN = 200  # pixels: an <img> declared narrower or shorter is an icon, not the article's picture
SKIPPED = {"script", "style", "noscript", "nav", "header", "footer", "aside", "form", "figcaption"}
CONTENT = {"article", "main"}
JSON_LD = "application/ld+json"
# What full_text keeps of the article: these tags, with no attributes but a
# link's href and an image's src, and the text inside the blocks. Buttons and
# icons inside the article are its sharing and follow widgets.
ARTICLE_BLOCKS = {"p", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li", "blockquote", "pre"}
ARTICLE_INLINE = {"a", "strong", "b", "em", "i", "code"}
CHROME = {"button", "svg"}
EMPTY = re.compile(rf"<({'|'.join(sorted(ARTICLE_BLOCKS | ARTICLE_INLINE))})(?: [^>]*)?>\s*</\1>")
SPACES = re.compile(r"\s+")
TAG = re.compile(r"<[^>]*>")
MARKER = re.compile(r"^[ \t]*(?:[*•-]|\d+[.)])[ \t]+", re.M)  # a list item's bullet or number in articleBody
ANCHOR = 40  # characters but spaces: a block this long found in articleBody is the article's, not a word that happens to be there


class _PageParser(HTMLParser):
    """Collects <meta property|name=... content=...> up to the end of <head>, and the opening paragraphs.

    The paragraphs are the page's opening: those inside the first <article>
    or <main> that has any, else those anywhere, outside navigation, headers,
    footers, captions and scripts, from the first of at least PARAGRAPH_MIN
    characters, up to EXCERPT_LIMIT characters in all. A later <article> or
    <main> is the page's related links, teasers or comments, not the article.
    The first <img> inside <article> or <main> stands in for a missing og:image.

    With full_text it also copies that <article> or <main>, the one whose
    paragraphs make the excerpt, as HTML made of ARTICLE_BLOCKS and
    ARTICLE_INLINE only.
    """

    def __init__(self, base_url: str = "", full_text: bool = False) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.full_text = full_text
        self.blocks: list[str] = []  # the article's top-level blocks, as HTML
        self.block: list[str] = []  # the open block's HTML so far
        self.open_tags: list[str] = []  # the tags open in it
        self.chrome = 0  # depth inside CHROME elements
        self.meta: dict[str, str] = {}
        self.in_head = True
        self.skip = 0  # depth inside SKIPPED elements
        self.content = 0  # depth inside CONTENT elements
        self.buffer: list[str] | None = None  # the open <p>'s text
        self.paragraphs: list[str] = []
        self.content_paragraphs: list[str] = []  # those inside CONTENT elements
        self.words = 0  # in every paragraph
        self.content_words = 0  # in those inside CONTENT elements
        self.content_done = False  # a CONTENT element with paragraphs has closed: later ones aren't the article
        self.content_image: str | None = None  # the first <img> inside CONTENT elements, as written
        self.json_ld: list[str] = []  # the text of each <script type="application/ld+json">
        self.ld_buffer: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "body":
            self.in_head = False
        elif tag == "meta" and self.in_head:
            a = {k.lower(): v for k, v in attrs if v is not None}
            key = (a.get("property") or a.get("name") or "").strip().lower()
            content = (a.get("content") or "").strip()
            if key and content:
                self.meta.setdefault(key, content)
        if tag == "script" and any(k == "type" and (v or "").strip().lower() == JSON_LD for k, v in attrs):
            self.ld_buffer = []
        if tag in SKIPPED:
            self.skip += 1
        elif tag in CONTENT:
            self.content += 1
        elif tag == "p":
            self._close_paragraph()  # an unclosed <p> ends where the next begins
            if not self.skip:
                self.buffer = []
        elif tag == "br" and self.buffer is not None:
            self.buffer.append(" ")
        elif tag == "img" and self.content and not self.skip and self.content_image is None:
            self.content_image = _image(attrs)
        if self.full_text:
            self._copy_start(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if self.full_text:
            self._copy_end(tag)
        if tag == "script" and self.ld_buffer is not None:
            self.json_ld.append("".join(self.ld_buffer))
            self.ld_buffer = None
        if tag == "head":
            self.in_head = False
        elif tag in SKIPPED:
            self.skip = max(0, self.skip - 1)
        elif tag in CONTENT:
            self._close_paragraph()
            self.content = max(0, self.content - 1)
            if not self.content:
                if self.content_paragraphs:
                    self.content_done = True
                else:  # only short text, a card or a byline: the article is further on
                    self.content_words = 0
                    self.blocks = []
        elif tag == "p":
            self._close_paragraph()

    def handle_data(self, data: str) -> None:
        if self.ld_buffer is not None:
            self.ld_buffer.append(data)
        if self.buffer is not None and not self.skip:
            self.buffer.append(data)
        if self.full_text and self.open_tags and self._copying:
            self.block.append(escape(data if "pre" in self.open_tags else SPACES.sub(" ", data), quote=False))

    @property
    def _copying(self) -> bool:
        """Whether the parser is inside the article, outside the elements it leaves out."""
        return bool(self.content) and not self.content_done and not self.skip and not self.chrome

    def _copy_start(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in CHROME:
            self.chrome += 1
        if not self._copying:
            return
        if tag in ARTICLE_BLOCKS:
            if "p" in self.open_tags:  # a block ends an unclosed <p>, as in a browser
                self._close_to("p")
            self._open(tag)
        elif tag in ARTICLE_INLINE and self.open_tags:  # text outside the blocks is left out
            if tag != "a":
                self._open(tag)
            elif href := _web(self.base_url, dict(attrs).get("href")):
                self._open("a", f' href="{escape(href)}"')
        elif tag == "br" and self.open_tags:
            self.block.append("<br>")
        elif tag == "img" and (src := _web(self.base_url, _image(attrs))):
            img = f'<img src="{escape(src)}" alt="">'
            if self.open_tags:
                self.block.append(img)
            else:
                self.blocks.append(f"<p>{img}</p>")

    def _copy_end(self, tag: str) -> None:
        if tag in CHROME:
            self.chrome = max(0, self.chrome - 1)
        if tag in CONTENT:
            self._close_to(None)
        elif tag in self.open_tags:
            self._close_to(tag)

    def _open(self, tag: str, attrs: str = "") -> None:
        self.block.append(f"<{tag}{attrs}>")
        self.open_tags.append(tag)

    def _close_to(self, tag: str | None) -> None:
        """Close the open tags up to the innermost tag, or all of them when tag is None."""
        while self.open_tags:
            last = self.open_tags.pop()
            self.block.append(f"</{last}>")
            if not self.open_tags:
                self.blocks.append("".join(self.block))
                self.block = []
            if last == tag:
                return

    def article_html(self) -> str:
        """The article's blocks, without empty elements and repeats; "" when the page has no <article> or <main> with
        paragraphs.

        A page's JSON-LD articleBody is the article's text alone: when there is
        one, the article runs from the first to the last block of at least
        ANCHOR characters whose text it holds, with the blocks it holds next to
        them, and keeps the blocks in between whose text it holds, and the
        images. That leaves out the standfirst, disclosures, related stories
        and ads around the article.
        """
        if not self.content_paragraphs:
            return ""
        blocks = []
        for block in self.blocks:
            html, previous = block, None
            while html != previous:  # removing an empty element may empty its parent
                html, previous = EMPTY.sub("", html), html
            # Pages repeat an image or a line for wide and narrow screens, one of them hidden.
            if html and html not in blocks:
                blocks.append(html)
        if body := "".join(_text(MARKER.sub("", b)) for b in _find(self.json_ld, "articleBody") if isinstance(b, str)):
            texts = [_text(TAG.sub("", b)) for b in blocks]
            found = [bool(t) and t in body for t in texts]
            anchors = [i for i, t in enumerate(texts) if found[i] and len(t) >= ANCHOR]
            if anchors:
                first, last = anchors[0], anchors[-1]
                # A short opening or closing line next to them ("See you next week!") is the article's too.
                while first > 0 and found[first - 1]:
                    first -= 1
                while last < len(blocks) - 1 and found[last + 1]:
                    last += 1
                blocks = [blocks[i] for i in range(first, last + 1) if found[i] or not texts[i]]
        return "".join(blocks)

    def _close_paragraph(self) -> None:
        if self.buffer is None:
            return
        text = squash("".join(self.buffer))
        self.buffer = None
        words = len(text.split())
        self.words += words
        _keep(self.paragraphs, text)
        if self.content and not self.content_done:
            self.content_words += words
            _keep(self.content_paragraphs, text)

    def close(self) -> None:
        super().close()
        self._close_paragraph()
        self._close_to(None)


def _image(attrs: list[tuple[str, str | None]]) -> str | None:
    """An <img>'s source, as written; None for one declared smaller than IMAGE_MIN, an icon."""
    a = {k: v for k, v in attrs if v is not None}
    sizes = [s for s in (pixels(a.get("width")), pixels(a.get("height"))) if s is not None]
    # Lazy-loading pages put a placeholder in src and the image in data-src.
    src = next((s for s in (a.get("src"), a.get("data-src")) if s and not s.startswith("data:")), None)
    return src if src and all(s >= IMAGE_MIN for s in sizes) else None


def _web(base_url: str, url: str | None) -> str | None:
    """url resolved against base_url when that gives an http or https URL; else None (javascript:, mailto: and the like)."""
    if not url:
        return None
    resolved = urljoin(base_url, url.strip())
    return resolved if urlsplit(resolved).scheme in ("http", "https") else None


def _keep(paragraphs: list[str], text: str) -> None:
    if len(text) >= (LATER_MIN if paragraphs else PARAGRAPH_MIN) and sum(map(len, paragraphs)) < EXCERPT_LIMIT:
        paragraphs.append(text)


def _values(data: object, name: str) -> Iterator[object]:
    """Every value of the key name in a JSON-LD document, at any depth (@graph, hasPart, …)."""
    if isinstance(data, dict):
        for key, value in data.items():
            if key == name:
                yield value
            else:
                yield from _values(value, name)
    elif isinstance(data, list):
        for value in data:
            yield from _values(value, name)


def _find(blocks: list[str], name: str) -> Iterator[object]:
    """Every value of the key name in JSON-LD blocks. A block that isn't JSON is skipped."""
    for block in blocks:
        try:
            data = json.loads(block)
        except ValueError:
            continue
        yield from _values(data, name)


def paywalled(blocks: list[str]) -> bool | None:
    """Whether JSON-LD blocks mark the page as paywalled; None when none says."""
    flags: list[bool] = []
    for value in _find(blocks, "isAccessibleForFree"):
        if isinstance(value, bool):
            flags.append(value)
        elif isinstance(value, str) and value.strip().lower() in ("true", "false"):
            flags.append(value.strip().lower() == "true")
    if not flags:
        return None
    return not all(flags)


def _text(text: str) -> str:
    """Text without its entities and whitespace, to compare an article's blocks with its articleBody: pages leave
    entities (&nbsp;) in articleBody, and drop or add spaces around their markup ("<em>Title</em>for")."""
    return "".join(unescape(text).split())


def _first(meta: dict[str, str], keys: tuple[str, ...]) -> str | None:
    return next((meta[k] for k in keys if k in meta), None)


def parse_preview(html: str, base_url: str, full_text: bool = False) -> Preview:
    parser = _PageParser(base_url, full_text)
    parser.feed(html)
    parser.close()
    meta = parser.meta

    # The first image that resolves to a web URL: skip data: and javascript: ones.
    declared = [meta[k] for k in IMAGE_KEYS if k in meta] + [parser.content_image or ""]
    image = next((u for u in (_web(base_url, u) for u in declared) if u), None)
    # The article's opening when it says more than the page's own summary.
    opening = PARAGRAPH.join(parser.content_paragraphs or parser.paragraphs)
    summary = _first(meta, DESCRIPTION_KEYS) or ""
    description = opening if len(opening) > len(summary) else summary
    site_name = meta.get("og:site_name")
    return Preview(
        image=image,
        description=excerpt(description) if description else None,
        site_name=excerpt(site_name, 80) if site_name else None,
        paywalled=paywalled(parser.json_ld),
        words=parser.content_words if parser.content_paragraphs else parser.words,
        content=parser.article_html() if full_text else None,
    )


def fetch_preview(url: str, timeout: float = 10, full_text: bool = False) -> Preview:
    """The page's preview, with its article when full_text. A response that isn't HTML (a PDF, an image) has none.

    Raises on network and HTTP errors, so the caller can retry on a later run.
    """
    # A story's link comes from whoever submitted it: never open file: or
    # another local scheme, which would publish the runner's files.
    nothing = Preview(content="" if full_text else None)
    if urlsplit(url).scheme not in ("http", "https"):
        return nothing
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1",
            "Accept-Language": "en",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:  # nosec B310 # http(s) only, checked above
        if r.headers.get_content_type() not in HTML_TYPES:
            return nothing
        body = r.read(FULL_MAX_BYTES if full_text else MAX_BYTES)
        charset = r.headers.get_content_charset() or "utf-8"
        final_url = r.geturl()
    try:
        html = body.decode(charset, errors="replace")
    except LookupError:  # a charset Python doesn't know
        html = body.decode("utf-8", errors="replace")
    return parse_preview(html, final_url, full_text)

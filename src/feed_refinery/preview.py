"""Link previews: the image, description and site name a page declares in its <head>, or its opening paragraphs."""

from __future__ import annotations

import json
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from . import USER_AGENT
from .text import EXCERPT_LIMIT, PARAGRAPH, excerpt, pixels, squash

MAX_BYTES = 512 * 1024  # the <head> comes first; no need for the whole page
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


PARAGRAPH_MIN = 80  # the first paragraph kept: shorter ones are bylines, captions and the like
LATER_MIN = 30  # the next ones: shorter ones are "Advertisement" and the like
IMAGE_MIN = 200  # pixels: an <img> declared narrower or shorter is an icon, not the article's picture
SKIPPED = {"script", "style", "noscript", "nav", "header", "footer", "aside", "form", "figcaption"}
CONTENT = {"article", "main"}
JSON_LD = "application/ld+json"


class _PageParser(HTMLParser):
    """Collects <meta property|name=... content=...> up to the end of <head>, and the opening paragraphs.

    The paragraphs are the page's opening: those inside the first <article>
    or <main> that has any, else those anywhere, outside navigation, headers,
    footers, captions and scripts, from the first of at least PARAGRAPH_MIN
    characters, up to EXCERPT_LIMIT characters in all. A later <article> or
    <main> is the page's related links, teasers or comments, not the article.
    The first <img> inside <article> or <main> stands in for a missing og:image.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
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
            a = {k: v for k, v in attrs if v is not None}
            sizes = [s for s in (pixels(a.get("width")), pixels(a.get("height"))) if s is not None]
            # Lazy-loading pages put a placeholder in src and the image in data-src.
            src = next((s for s in (a.get("src"), a.get("data-src")) if s and not s.startswith("data:")), None)
            if src and all(s >= IMAGE_MIN for s in sizes):
                self.content_image = src

    def handle_endtag(self, tag: str) -> None:
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
        elif tag == "p":
            self._close_paragraph()

    def handle_data(self, data: str) -> None:
        if self.ld_buffer is not None:
            self.ld_buffer.append(data)
        if self.buffer is not None and not self.skip:
            self.buffer.append(data)

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


def _keep(paragraphs: list[str], text: str) -> None:
    if len(text) >= (LATER_MIN if paragraphs else PARAGRAPH_MIN) and sum(map(len, paragraphs)) < EXCERPT_LIMIT:
        paragraphs.append(text)


def _free_flags(data: object) -> Iterator[bool]:
    """Every isAccessibleForFree in a JSON-LD document, at any depth (@graph, hasPart, …)."""
    if isinstance(data, dict):
        for key, value in data.items():
            if key == "isAccessibleForFree":
                if isinstance(value, bool):
                    yield value
                elif isinstance(value, str) and value.strip().lower() in ("true", "false"):
                    yield value.strip().lower() == "true"
            else:
                yield from _free_flags(value)
    elif isinstance(data, list):
        for value in data:
            yield from _free_flags(value)


def paywalled(blocks: list[str]) -> bool | None:
    """Whether JSON-LD blocks mark the page as paywalled; None when none says. A block that isn't JSON is skipped."""
    flags: list[bool] = []
    for block in blocks:
        try:
            flags.extend(_free_flags(json.loads(block)))
        except ValueError:
            continue
    if not flags:
        return None
    return not all(flags)


def _first(meta: dict[str, str], keys: tuple[str, ...]) -> str | None:
    return next((meta[k] for k in keys if k in meta), None)


def parse_preview(html: str, base_url: str) -> Preview:
    parser = _PageParser()
    parser.feed(html)
    parser.close()
    meta = parser.meta

    # The first image that resolves to a web URL: skip data: and javascript: ones.
    declared = [meta[k] for k in IMAGE_KEYS if k in meta] + [parser.content_image or ""]
    candidates = (urljoin(base_url, u) for u in declared if u)
    image = next((u for u in candidates if urlsplit(u).scheme in ("http", "https")), None)
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
    )


def fetch_preview(url: str, timeout: float = 10) -> Preview:
    """The page's preview. A response that isn't HTML (a PDF, an image) has none.

    Raises on network and HTTP errors, so the caller can retry on a later run.
    """
    # A story's link comes from whoever submitted it: never open file: or
    # another local scheme, which would publish the runner's files.
    if urlsplit(url).scheme not in ("http", "https"):
        return Preview()
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
            return Preview()
        body = r.read(MAX_BYTES)
        charset = r.headers.get_content_charset() or "utf-8"
        final_url = r.geturl()
    try:
        html = body.decode(charset, errors="replace")
    except LookupError:  # a charset Python doesn't know
        html = body.decode("utf-8", errors="replace")
    return parse_preview(html, final_url)

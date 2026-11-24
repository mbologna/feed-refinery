"""Plain text out of HTML snippets, trimmed for an excerpt."""

from __future__ import annotations

import re
from html.parser import HTMLParser

EXCERPT_LIMIT = 2000
PARAGRAPH = "\n\n"  # between an excerpt's paragraphs
BLANK_LINE = re.compile(r"\n\s*\n")
# A sentence's last character, with any closing quotes or brackets, before a space.
SENTENCE_END = re.compile(r"[.!?…][\"'”’)\]]*(?=\s)")
# Tags that start or end a paragraph: their text doesn't run into the next.
BLOCKS = frozenset(
    "address article aside blockquote br dd div dl dt figcaption figure footer h1 h2 h3 h4 h5 h6 header hr li main"
    " ol p pre section table tr ul".split()
)


class _TextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in BLOCKS:
            self.parts.append(PARAGRAPH)

    def handle_endtag(self, tag: str) -> None:
        if tag in BLOCKS:
            self.parts.append(PARAGRAPH)


def plain_paragraphs(html: str) -> str:
    """The text of an HTML snippet, its paragraphs apart by a blank line and whitespace within each collapsed."""
    parser = _TextParser()
    parser.feed(html)
    parser.close()
    return _paragraphs("".join(parser.parts))


def plain_text(html: str) -> str:
    """The text of an HTML snippet, whitespace collapsed."""
    return squash(plain_paragraphs(html))


def squash(text: str) -> str:
    return " ".join(text.split())


def _paragraphs(text: str) -> str:
    return PARAGRAPH.join(p for p in map(squash, BLANK_LINE.split(text)) if p)


def excerpt(text: str, limit: int = EXCERPT_LIMIT) -> str:
    """text cut to at most limit characters. Paragraphs apart by a blank line stay apart; other whitespace collapses.

    A cut text ends with its last whole paragraph, else sentence, when one ends
    in the second half of the limit; otherwise at a word, with an ellipsis.
    """
    text = _paragraphs(text)
    if len(text) <= limit:
        return text
    end = text.rfind(PARAGRAPH, 0, limit + len(PARAGRAPH))
    if end < limit // 2:
        end = max((m.end() for m in SENTENCE_END.finditer(text, 0, limit + 1)), default=-1)
    if end >= limit // 2:
        return text[:end]
    cut = text[: limit - 1]
    if re.search(r"\s", cut):
        cut = re.sub(r"\s+\S*$", "", cut)
    return cut.rstrip(" ,;:.-\n") + "…"

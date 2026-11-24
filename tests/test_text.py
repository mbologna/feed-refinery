import pytest

from feed_refinery.text import excerpt, plain_paragraphs, plain_text


@pytest.mark.parametrize(
    ("html", "text"),
    [
        ("<p>One</p><p>Two</p>", "One\n\nTwo"),
        ("One<br>Two<br/>Three", "One\n\nTwo\n\nThree"),
        ("<ul><li>a</li><li>b</li></ul>", "a\n\nb"),
        ("<div><div>Nested</div> <p>  spaced   out </p></div>", "Nested\n\nspaced out"),
        ("Plain text,\nwrapped.\n\n  Next paragraph.", "Plain text, wrapped.\n\nNext paragraph."),
        ("<p>Ask HN text<p>with unclosed paragraphs", "Ask HN text\n\nwith unclosed paragraphs"),
        ("<p></p><p> </p>", ""),
    ],
)
def test_plain_paragraphs(html, text):
    assert plain_paragraphs(html) == text


def test_plain_text_keeps_list_items_apart():
    assert plain_text("<ul><li>Apples</li><li>pears</li></ul>") == "Apples pears"


def test_excerpt_keeps_the_paragraphs_that_fit():
    assert excerpt("First paragraph.\n\nSecond   one.") == "First paragraph.\n\nSecond one."


def test_excerpt_cuts_after_the_last_whole_paragraph():
    text = "A first paragraph that ends here\n\nand a second that runs past the limit"
    assert excerpt(text, 50) == "A first paragraph that ends here"


def test_excerpt_cuts_after_the_last_whole_sentence():
    text = "The first sentence ends. The second “ends too.” The third does not end before the limit"
    assert excerpt(text, 60) == "The first sentence ends. The second “ends too.”"


def test_excerpt_cuts_at_a_word_when_no_sentence_ends_late_enough():
    text = "Short. " + "word " * 40
    assert excerpt(text, 60) == "Short. " + " ".join(["word"] * 10) + "…"

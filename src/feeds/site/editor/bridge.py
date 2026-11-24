"""The editor's Python side, run by Pyodide in the browser.

The same validation, filters and merging as a build: load() and apply()
check feeds.toml with config.parse, preview() runs a feed's sources through
rss.parse and rss.select. apply() edits with tomlkit, which keeps the
file's comments and layout.
"""

from __future__ import annotations

import difflib
import json
import re
import time
import tomllib
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

import tomlkit
from tomlkit.container import Container
from tomlkit.items import AoT, Array, Comment, Item, Table, Whitespace

from feeds import config, rss

KINDS = ("feed", "digest", "rss")


def check(text: str) -> str | None:
    """The build's own message for what's wrong with a feeds.toml, or None when it builds."""
    try:
        config.parse(text)
    except config.ConfigError as e:
        return str(e)
    return None


def load(text: str) -> dict[str, Any]:
    """Every table of a feeds.toml as plain data, by kind, and the build's validation error, if any."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        return {"tables": {kind: [] for kind in KINDS}, "error": f"not valid TOML: {e}"}
    tables = {kind: data.get(kind, []) if isinstance(data.get(kind), list) else [] for kind in KINDS}
    return {"tables": tables, "error": check(text)}


# Edits. A table's own comment is the block of comment lines right above its
# header; tomlkit keeps it at the end of the table before, with the blank
# lines between tables. These helpers keep each block with its table.


def _trailing(body: list[tuple[Any, Item]]) -> int:
    """Where a table's trailing comments and blank lines start: after its last key."""
    n = len(body)
    while n and body[n - 1][0] is None and isinstance(body[n - 1][1], (Comment, Whitespace)):
        n -= 1
    return n


def _heading(body: list[tuple[Any, Item]]) -> int:
    """Where the comment block right above the next table starts, at the end of this table's body."""
    n = len(body)
    while n > _trailing(body) and isinstance(body[n - 1][1], Comment):
        n -= 1
    return n


def _tables(doc: tomlkit.TOMLDocument) -> list[Table]:
    """Every [[feed]], [[digest]] and [[rss]] table, in the file's order."""
    return [t for _, item in doc.body if isinstance(item, AoT) for t in item.body]


WIDTH = 100  # a longer one-line array is written one entry per line


def _string(value: str, literal: bool = False) -> Item:
    """A string, literal (single-quoted) when it has a backslash, as regular expressions do, or when asked."""
    fits = "'" not in value and "\n" not in value
    return tomlkit.string(value, literal=fits and (literal or "\\" in value))


def _value(value: Any, old: Item | None = None) -> Item:
    """A TOML value in the file's style: entries the old array had keep their quoting, new ones follow it; one
    entry per line when the old array was so or one line would be too long, indented like the old one."""
    if isinstance(value, str):
        return _string(value)
    if isinstance(value, dict):  # json = { items = "…", … }, on one line as the file writes it
        table = tomlkit.inline_table()
        for key, entry in value.items():
            table.append(key, _value(entry))
        return table
    if not isinstance(value, list):
        return tomlkit.item(value)
    before = old if isinstance(old, Array) else []
    quoted = {entry.unwrap(): entry.as_string() for entry in before if isinstance(entry, Item)}
    literal = any(text.startswith("'") for text in quoted.values())
    entries = [quoted.get(v) or (_string(v, literal) if isinstance(v, str) else _value(v)).as_string() for v in value]
    line = "[" + ", ".join(entries) + "]"
    layout = old.as_string() if isinstance(old, Array) else ""
    if entries and ("\n" in layout or len(line) > WIDTH):
        indent = "  "
        if "\n" in layout:
            first = layout.split("\n", 1)[1]
            indent = first[: len(first) - len(first.lstrip(" \t"))] or indent
        line = "[\n" + "".join(f"{indent}{e},\n" for e in entries) + "]"
    return tomlkit.parse(f"v = {line}\n")["v"]


def _set(table: Table, key: str, value: Any) -> None:
    """Set, change or (value None) remove one key, leaving the other lines alone."""
    container: Container = table.value
    if value is None:
        if key in container:
            container.remove(key)
        return
    if key in container:
        old = container.item(key)
        if old.unwrap() != value:
            container[key] = _value(value, old)
        return
    keys = [k for k, _ in container.body[: _trailing(container.body)] if k is not None]
    if keys:
        container._insert_after(keys[-1].key, key, _value(value))
    else:
        container.append(key, _value(value))


def _add(doc: tomlkit.TOMLDocument, kind: str, values: dict[str, Any]) -> None:
    """Append a table after the last one of its kind, or at the end of the file."""
    table = tomlkit.table(is_super_table=False)
    for key, value in values.items():
        if value is not None:
            table.append(key, _value(value))
    if kind not in doc:
        if doc.body and not doc.as_string().endswith("\n\n"):
            doc.add(tomlkit.nl())
        doc.append(kind, tomlkit.aot())
    aot: AoT = doc[kind]
    last = aot.body[-1] if aot.body else None
    aot.append(table)
    if last is not None:
        # What followed the last table (the next kind's comments) now follows this one.
        body = last.value.body
        cut = _trailing(body)
        moved, body[cut:] = body[cut:], []
        body.append((None, Whitespace("\n")))
        table.value.body.extend(moved)


def _delete(doc: tomlkit.TOMLDocument, kind: str, index: int) -> None:
    """Remove a table with its own comment, keeping the comments that belong to the tables after it."""
    tables = _tables(doc)
    aot: AoT = doc[kind]
    target = aot.body[index]
    position = next(n for n, t in enumerate(tables) if t is target)
    body = target.value.body
    after = body[_trailing(body) :]
    if position:
        before = tables[position - 1].value.body
        del before[_heading(before) :]
        while len(before) > _trailing(before) and isinstance(before[-1][1], Whitespace):
            before.pop()
        last = position == len(tables) - 1
        before.extend(after if after or last else [(None, Whitespace("\n"))])
    del aot[index]
    if not aot.body:
        doc.remove(kind)


def apply(text: str, edits: list[dict[str, Any]]) -> dict[str, Any]:
    """Apply the editor's edits to a feeds.toml, in order. Returns the new text, its diff and validation error.

    Each edit is one of:
      {"op": "set", "kind": "rss", "index": 0, "key": "max_items", "value": 50}  (value None removes the key)
      {"op": "add", "kind": "rss", "table": {...}}
      {"op": "delete", "kind": "rss", "index": 1}
    An index counts the tables of that kind after the edits before it.
    """
    doc = tomlkit.parse(text)
    for edit in edits:
        op, kind = edit["op"], edit["kind"]
        if kind not in KINDS:
            raise ValueError(f"unknown kind {kind!r}")
        if op == "set":
            _set(doc[kind][edit["index"]], edit["key"], edit["value"])
        elif op == "add":
            _add(doc, kind, edit["table"])
        elif op == "delete":
            _delete(doc, kind, edit["index"])
        else:
            raise ValueError(f"unknown edit {op!r}")
    new = tomlkit.dumps(doc)
    return {"text": new, "diff": diff(text, new), "error": check(new)}


def diff(old: str, new: str) -> str:
    """A unified diff of feeds.toml, as git shows it."""
    lines = difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True), "a/feeds.toml", "b/feeds.toml"
    )
    return "".join(lines)


# Preview.


def _rss_feed(table: dict[str, Any]) -> config.Rss:
    """The table as the build reads it. Raises ConfigError with the build's message."""
    (feed,) = config.parse(tomlkit.dumps({"rss": [table]})).rss
    return feed


def _facets(items: list[dict[str, Any]], field: str) -> list[dict[str, Any]]:
    """Counts per category (or section), case folded, with how many such items pass; most common first."""
    names: dict[str, str] = {}
    total: Counter[str] = Counter()
    passed: Counter[str] = Counter()
    for item in items:
        values = item["categories"] if field == "categories" else item["categories"][:1]
        for folded in dict.fromkeys(v.casefold() for v in values):
            total[folded] += 1
            passed[folded] += item["drop"] is None
        for value in values:
            names.setdefault(value.casefold(), value)
    ranked = sorted(total, key=lambda f: (-total[f], names[f].casefold()))
    return [{"name": names[f], "count": total[f], "passed": passed[f]} for f in ranked]


def preview(table: dict[str, Any], bodies: dict[str, dict[str, Any]], now: int | None = None) -> dict[str, Any]:
    """What a [[rss]] table would publish from its sources' current documents.

    bodies maps each source URL to {"body": bytes, "content_type": str, "url": final URL} or {"error": message}.
    Returns every item with its verdict (the filter that drops it), the story it belongs to and whether it
    merged into an earlier copy, the published stories newest first within max_items, the counts, and
    category and section facets.
    """
    try:
        feed = _rss_feed(table)
    except config.ConfigError as e:
        return {"error": str(e)}
    now = int(time.time()) if now is None else now
    sources: dict[str, dict[str, Any]] = {}
    listed: list[tuple[str, list[rss.Item]]] = []
    for url in feed.sources:
        got = bodies.get(url) or {"error": "not fetched"}
        if "error" in got:
            sources[url] = {"items": 0, "error": got["error"]}
            continue
        try:
            base = got.get("url") or url
            if feed.json:
                found = rss.parse_json(got["body"], base, feed.json)
            else:
                found = rss.parse(got["body"], base, got.get("content_type") or "")
        except ValueError as e:
            sources[url] = {"items": 0, "error": str(e)}
            continue
        sources[url] = {"items": len(found), "error": None}
        listed.append((url, found))
    chosen = rss.select(feed, listed)
    stories = sorted(chosen.items, key=lambda k: chosen.items[k].published or now, reverse=True)
    published = stories[: feed.max_items]
    items = [
        {
            "source": c.source,
            "title": c.item.title,
            "link": c.item.link,
            "published": c.item.published,
            "categories": list(c.item.categories),
            "author": c.item.author,
            "drop": c.drop._asdict() if c.drop else None,
            "key": c.key,
            "merged": c.merged,
        }
        for c in chosen.choices
    ]
    return {
        "error": None,
        "sources": sources,
        "items": items,
        "stories": [
            {"key": k, "title": chosen.items[k].title, "link": chosen.items[k].link, "sources": chosen.sources[k]}
            for k in published
        ],
        "counts": {
            "listed": len(items),
            "dropped": sum(1 for i in items if i["drop"]),
            "merged": sum(1 for i in items if i["merged"]),
            "stories": len(stories),
            "published": len(published),
        },
        "facets": {"categories": _facets(items, "categories"), "sections": _facets(items, "sections")},
    }


def test(patterns: list[str], text: str) -> list[dict[str, Any]]:
    """Whether each regular expression matches text, as the filters search it, or why it doesn't compile."""
    results = []
    for pattern in patterns:
        try:
            results.append({"pattern": pattern, "match": re.search(pattern, text) is not None, "error": None})
        except re.error as e:
            results.append({"pattern": pattern, "match": False, "error": str(e)})
    return results


def call(name: str, payload: str) -> str:
    """The worker's entry point: JSON in, JSON out. A body given as {"path": ...} is read from Pyodide's file system."""
    args = json.loads(payload)
    if name == "preview":
        for got in args["bodies"].values():
            if "path" in got:
                got["body"] = Path(got.pop("path")).read_bytes()
    functions: dict[str, Callable[..., Any]] = {
        "load": load,
        "apply": apply,
        "preview": preview,
        "check": check,
        "test": test,
    }
    return json.dumps(functions[name](**args))

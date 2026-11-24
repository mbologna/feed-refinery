"""Command line: build the feeds, publish them to Workers KV, check the published ones' age, or check the feed list."""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import importlib.resources
import io
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

from . import HEALTH_NAME, PAGE_URL, STATE_NAME, USER_AGENT, comments, config, kv, preview, rss, source, state
from .feed import last_build_date, plural, render_digest, render_feed, render_rss_feed
from .page import render_page

IMAGE_RATE_FLOOR = 0.3  # below this share of items with an image, something is blocking or broken
RSS_MAX_AGE = 3 * 3600  # an RSS feed is not ok while one of its sources last answered longer ago than this


def feed_url(page_url: str, file: str) -> str:
    return page_url.rstrip("/") + "/" + file


def fetch_rss(feeds: list[config.Rss], current: state.State) -> dict[str, dict[str, rss.Fetched | Exception]]:
    """Every RSS feed's sources, with conditional GETs unless the feed's filters changed.

    A source two feeds share is fetched once per validator pair.
    """
    shared: dict[tuple[str, str | None, str | None, config.JsonFields | None], rss.Fetched | Exception] = {}
    results: dict[str, dict[str, rss.Fetched | Exception]] = {}
    for feed in feeds:
        known = current.rss.get(feed.file) or state.RssFeed()
        outcomes = results[feed.file] = {}
        # A changed filter: every source answers in full, so every item is judged again.
        changed = known.filters != feed.filters_key
        for url in feed.sources:
            source_state = known.sources.get(url) or state.Source()
            etag, modified = (None, None) if changed else (source_state.etag, source_state.last_modified)
            key = (url, etag, modified, feed.json)
            if key not in shared:
                try:
                    shared[key] = rss.fetch(url, etag, modified, feed.json)
                # Any error: the source keeps the items it had while the others refresh.
                except Exception as e:
                    shared[key] = e
            outcome = shared[key]
            if isinstance(outcome, rss.Fetched) and feed.label(url):
                outcome = outcome._replace(items=rss.labelled(outcome.items, feed.label(url)))
            outcomes[url] = outcome
            if isinstance(outcome, Exception):
                print(f"::warning::{feed.file}: {url}: {type(outcome).__name__}: {outcome}")
            elif outcome.status == 304:
                print(f"{feed.file}: {url}: unchanged")
            else:
                passed = sum(1 for item in outcome.items if rss.keep(item, feed))
                print(f"{feed.file}: {url}: {len(outcome.items)} items, {passed} pass the filters")
    return results


def wanted(stories: list[source.Story], exclude_titles: tuple) -> list[source.Story]:
    """The stories whose title matches none of exclude_titles."""
    return [s for s in stories if not any(p.search(s.title) for p in exclude_titles)]


def build(config_path: Path, output_dir: Path, page_url: str, previous: str, now: dt.datetime) -> None:
    """Write every configured feed, state.json and index.html into output_dir."""
    conf = config.load(config_path)
    feeds = conf.feeds
    current = state.load(previous)

    # Feeds with the same query share one request.
    responses: dict[str, list[source.Story]] = {}

    def stories(url: str) -> list[source.Story]:
        if url not in responses:
            responses[url] = source.fetch_stories(url)
        return responses[url]

    ranked = source.fetch_ranked_ids() if any(f.ranked for f in feeds) else []
    results = {}
    for feed in feeds:
        if feed.ranked:
            # In rank order, at most max_items: state.update relies on a
            # feed's results fitting in it.
            urls = source.ids_query_urls(ranked, feed.tags, source.count_filters(feed))
            rank = {str(sid): n for n, sid in enumerate(ranked)}
            matched = sorted((s for url in urls for s in stories(url)), key=lambda s: rank.get(s.id, len(rank)))
            results[feed.file] = wanted(matched, feed.exclude_titles)[: feed.max_items]
        else:
            results[feed.file] = wanted(stories(source.query_url(feed, now)), feed.exclude_titles)

    # Stories a feed still holds but the API no longer lists for it (off the
    # front page, unranked): refresh their counts, 100 per request.
    listed = {s.id for found in results.values() for s in found}
    unlisted = sorted({sid for seen in current.feeds.values() for sid in seen} - listed)
    for url in source.ids_query_urls(unlisted, "story", []):
        for story in stories(url):
            if story.id in current.stories:
                state.merge_story(current, story)
    print(f"counts: refreshed {len(unlisted)} stories no longer listed")

    stamp = int(now.timestamp())
    for digest in conf.digests:
        for day in state.digest_days_due(current, digest, stamp):
            start = int(dt.datetime.combine(day, dt.time(), dt.UTC).timestamp())
            day_stories = wanted(stories(source.day_query_url(start, start + 86400)), digest.exclude_titles)
            state.add_digest_day(current, digest, day, day_stories)
            print(f"{digest.file}: published {day.isoformat()}")
    state.update(current, feeds, results, stamp, conf.digests)
    fetched, failed = state.fetch_previews(current, preview.fetch_preview)
    print(f"previews: fetched {fetched} new, {len(failed)} failed")
    for url, error in failed:
        print(f"  {url}: {error}")
    fetched, failed = state.fetch_comments(current, comments.fetch_top_comment, listed, stamp)
    print(f"top comments: looked up {fetched}, {len(failed)} failed")
    for url, error in failed:
        print(f"  {url}: {error}")
    rss_results = fetch_rss(conf.rss, current)
    state.update_rss(current, conf.rss, rss_results, stamp)
    if conf.rss:
        fetched, failed = state.fetch_rss_previews(current, conf.rss, preview.fetch_preview)
        print(f"rss previews: fetched {fetched} new, {len(failed)} failed")
        for url, error in failed:
            print(f"  {url}: {error}")
    floor = conf.hacker_news_floor
    if any(f.exclude_on_hacker_news for f in conf.rss):
        looked_up, failed = state.check_hacker_news(current, conf.rss, source.points_for_url, floor, stamp)
        print(f"hacker news: looked up {looked_up} links, {len(failed)} failed")
        for url, error in failed:
            print(f"  {url}: {error}")

    output_dir.mkdir(parents=True, exist_ok=True)
    health: dict = {"built": stamp, "feeds": {}, "digests": {}, "rss": {}}
    for feed in feeds:
        seen = current.feeds[feed.file]
        items = [(current.stories[sid], t) for sid, t in seen.items()]
        if feed.exclude_paywalled:
            items = [(e, t) for e, t in items if not state.held_back(e)]
        document = render_feed(feed, items, feed_url(page_url, feed.file), now)
        (output_dir / feed.file).write_bytes(document)
        images = sum(1 for e, _ in items if e.preview and e.preview.image)
        excerpts = sum(1 for e, _ in items if (e.preview and e.preview.description) or e.story.text)
        print(f"{feed.file}: {len(items)} items, {images} with an image, {excerpts} with an excerpt")
        health["feeds"][feed.file] = {"items": len(items), "images": images, "excerpts": excerpts}
        if len(items) >= 10 and images < IMAGE_RATE_FLOOR * len(items):
            # A GitHub Actions annotation: shows on the run's summary page.
            print(f"::warning::{feed.file}: only {images} of {len(items)} items have an image")
    for digest in conf.digests:
        days = sorted(current.digests.get(digest.file, {}).items(), reverse=True)
        entries: list[tuple[dt.date, list[state.Entry]]] = []
        for iso, ids in days:
            day_entries = [current.stories[sid] for sid in ids if sid in current.stories]
            if digest.exclude_paywalled:
                day_entries = [e for e in day_entries if not state.held_back(e)]
            entries.append((dt.date.fromisoformat(iso), day_entries))
        document = render_digest(digest, entries, feed_url(page_url, digest.file), now)
        (output_dir / digest.file).write_bytes(document)
        print(f"{digest.file}: {len(entries)} days")
        health["digests"][digest.file] = {"days": len(entries)}
    for rss_feed in conf.rss:
        known = current.rss[rss_feed.file]
        rss_entries = [e for e in known.items.values() if state.visible(rss_feed, e, floor)]
        document = render_rss_feed(rss_feed, rss_entries, feed_url(page_url, rss_feed.file), now)
        (output_dir / rss_feed.file).write_bytes(document)
        images = sum(1 for e in rss_entries if e.image)
        excerpts = sum(1 for e in rss_entries if e.excerpt)
        last_ok = known.last_ok(rss_feed)
        failing = [url for url, outcome in rss_results[rss_feed.file].items() if isinstance(outcome, Exception)]
        print(f"{rss_feed.file}: {len(rss_entries)} items, {images} with an image, {excerpts} with an excerpt")
        health["rss"][rss_feed.file] = {
            "items": len(rss_entries),
            "images": images,
            "excerpts": excerpts,
            "ok": last_ok is not None and stamp - last_ok <= RSS_MAX_AGE,
            "last_ok": last_ok,
            "sources_failed": failing,
        }
        if rss_feed.enrich and len(rss_entries) >= 10 and images < IMAGE_RATE_FLOOR * len(rss_entries):
            print(f"::warning::{rss_feed.file}: only {images} of {len(rss_entries)} items have an image")
    # For outside monitoring: when the build ran and how the feeds look.
    (output_dir / HEALTH_NAME).write_text(json.dumps(health, indent=2) + "\n", encoding="utf-8")
    (output_dir / STATE_NAME).write_bytes(current.to_json())
    # The icons: the feeds' <image> and the index page's favicon. The editor,
    # in site/editor/, is published by CI with the package's wheel.
    for asset in importlib.resources.files("feed_refinery").joinpath("site").iterdir():
        if asset.is_file():
            (output_dir / asset.name).write_bytes(asset.read_bytes())
    published: list[config.Feed | config.Digest | config.Rss] = [*feeds, *conf.digests, *conf.rss]
    page = render_page([(f, feed_url(page_url, f.file)) for f in published])
    (output_dir / "index.html").write_text(page, encoding="utf-8")


def check(config_path: Path, page_url: str, max_age: dt.timedelta, now: dt.datetime) -> bool:
    """Whether every configured feed is published and was built within max_age."""
    ok = True
    for file in config.load(config_path).files:
        url = feed_url(page_url, file)
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:  # nosec B310 # the operator's own page URL
                built = last_build_date(r.read())
        except (urllib.error.URLError, OSError, ValueError) as e:
            print(f"{url}: {e}", file=sys.stderr)
            ok = False
            continue
        hours = (now - built).total_seconds() / 3600
        message = (
            f"{url}: built {built:%Y-%m-%d %H:%M} UTC, {hours:.1f} hours ago (limit {max_age.total_seconds() / 3600:g})"
        )
        fresh = now - built <= max_age
        print(message, file=sys.stdout if fresh else sys.stderr)
        ok = ok and fresh
    return ok


def check_config(config_path: Path) -> None:
    """Print what a feeds.toml publishes. Raises ConfigError, with the build's message, when it wouldn't build."""
    conf = config.load(config_path)
    counts = [
        plural(len(conf.feeds), "HN feed"),
        plural(len(conf.digests), "digest"),
        plural(len(conf.rss), "RSS feed"),
    ]
    print(f"{config_path}: ok, {', '.join(counts)}")
    for file in conf.files:
        print(f"  {file}")


def quiet_build(config_path: Path, output_dir: Path, page_url: str, previous: str, now: dt.datetime) -> int:
    """build() with its log kept back: only totals, for a public CI log of a private feeds.toml.

    The log names feeds, sources and article URLs, and so would an error
    message: an error prints only its kind. Run the build locally for the
    details.
    """
    log = io.StringIO()
    try:
        with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
            build(config_path, output_dir, page_url, previous, now)
    except config.ConfigError:
        print("feeds.toml is invalid: run check-config on it", file=sys.stderr)
        return 2
    except Exception as e:  # anything else: its message may name a feed or a source
        print(f"build failed: {type(e).__name__}", file=sys.stderr)
        return 1
    health = json.loads((output_dir / HEALTH_NAME).read_text(encoding="utf-8"))
    items = sum(f.get("items", 0) for kind in ("feeds", "rss") for f in health.get(kind, {}).values())
    stale = sum(1 for f in health.get("rss", {}).values() if not f.get("ok"))
    counts = [
        plural(len(health.get("feeds", {})), "HN feed"),
        plural(len(health.get("digests", {})), "digest"),
        plural(len(health.get("rss", {})), "RSS feed"),
    ]
    print(f"built {', '.join(counts)}: {plural(items, 'item')}, {stale} stale")
    warnings = log.getvalue().count("::warning::")
    if warnings:
        print(f"::warning::{plural(warnings, 'warning')}; the details stay out of this log")
    return 0


def publish(output_dir: Path, config_path: Path, now: dt.datetime) -> int:
    """Write the build to Workers KV (credentials from the environment), and print counts only."""
    files = config.load(config_path).files
    try:
        done = kv.publish(kv.Kv.from_env(), output_dir, files, now)
    except kv.KvError as e:
        print(f"publish failed: {e}", file=sys.stderr)
        return 1
    print(
        f"kv: {done.written} written, {done.unchanged} unchanged, {done.deleted} deleted; "
        f"{done.writes_today} of {kv.DAILY_WRITES} writes today"
    )
    if done.guard:
        print(f"::warning::write guard on: {plural(done.deferred, 'count-only refresh')} skipped until 00:00 UTC")
    return 0


def kv_get(key: str, path: Path) -> int:
    """Copy a key's value to path; nothing, and success, when KV lacks it."""
    try:
        value = kv.Kv.from_env().get(key)
    except kv.KvError as e:
        print(f"kv-get failed: {e}", file=sys.stderr)
        return 1
    if value is None:
        print(f"{key}: not in KV")
        return 0
    path.write_bytes(value)
    print(f"{key}: {len(value)} bytes")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="feed-refinery", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    b = sub.add_parser("build", help="write the feeds, state.json and index.html")
    b.add_argument("--config", type=Path, default=Path("feeds.toml"))
    b.add_argument("--output-dir", type=Path, default=Path("public"))
    b.add_argument("--page-url", default=PAGE_URL, help="where the site is published")
    b.add_argument(
        "--previous-state",
        help="URL or path of the previous state.json; an empty string starts fresh "
        "(default: state.json under --page-url)",
    )
    b.add_argument("--quiet", action="store_true", help="print totals only, for a public log: no feed, source or URL")

    pub = sub.add_parser("kv-publish", help="write a build to Workers KV, only what changed (env: CLOUDFLARE_*)")
    pub.add_argument("output_dir", type=Path)
    pub.add_argument("--config", type=Path, default=Path("feeds.toml"))

    get = sub.add_parser("kv-get", help="copy a Workers KV key to a file, such as the previous state.json")
    get.add_argument("key")
    get.add_argument("path", type=Path)

    c = sub.add_parser("check", help="fail when a published feed is missing or older than --max-age-hours")
    c.add_argument("--config", type=Path, default=Path("feeds.toml"))
    c.add_argument("--page-url", default=PAGE_URL)
    c.add_argument("--max-age-hours", type=float, default=3)

    v = sub.add_parser("check-config", help="fail when the feed list wouldn't build, with the build's message")
    v.add_argument("config", type=Path, nargs="?", default=Path("feeds.toml"))

    args = parser.parse_args(argv)
    now = dt.datetime.now(dt.UTC).replace(microsecond=0)

    try:
        if args.command == "build":
            # The Pages CDN caches state.json for 10 minutes and ignores query
            # strings, so a run that starts within 10 minutes of a deploy reads
            # the state before it. That costs refetching the previews and top
            # comments of the stories the newer run added, nothing more.
            default = feed_url(args.page_url, STATE_NAME)
            previous = default if args.previous_state is None else args.previous_state
            if args.quiet:
                return quiet_build(args.config, args.output_dir, args.page_url, previous, now)
            build(args.config, args.output_dir, args.page_url, previous, now)
            return 0
        if args.command == "kv-publish":
            return publish(args.output_dir, args.config, now)
        if args.command == "kv-get":
            return kv_get(args.key, args.path)
        if args.command == "check-config":
            check_config(args.config)
            return 0
        return 0 if check(args.config, args.page_url, dt.timedelta(hours=args.max_age_hours), now) else 1
    except config.ConfigError as e:
        print(f"{args.config}: {e}", file=sys.stderr)
        return 2
    except FileNotFoundError as e:
        print(f"{args.config}: {e.strerror}", file=sys.stderr)
        return 2

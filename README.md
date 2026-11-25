# feed-refinery

[![CI](https://github.com/mbologna/feed-refinery/actions/workflows/ci.yml/badge.svg)](https://github.com/mbologna/feed-refinery/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

RSS feeds whose items carry each article's image and excerpt, from two kinds of source:

- **Hacker News:** the front page, stories with 100+ points, ranked stories and a daily digest, each item with the linked page's image and excerpt, the top comment and the article and comments links. [hnrss](https://github.com/hnrss/hnrss) gives you only the links.
- **Any RSS or Atom feed:** one site's feed, or several merged into one with duplicates removed, filtered by category, title, link and author, with the article's image and excerpt filled in where the source lacks them.

It's an engine: it builds whatever feeds a `feeds.toml` lists, and [`examples/feeds.toml`](examples/feeds.toml) shows every kind. This repository's workflow builds a private list (its `feeds.toml` lives in a private repository, since it shows what someone reads) every 30 minutes, writes the feeds to Cloudflare Workers KV, and a Worker serves them at `https://feeds.michelebologna.net/<file>.xml?token=…`: a missing or wrong token is a 404, so nobody can tell which feeds exist. `https://feeds.michelebologna.net/health` answers 200 `{"status":"ok",…}`, or 503 with the problems counted, never named. [Run it yourself](#run-it-yourself), with a public or a private list.

## Hacker News items

Each item of an HN feed has:

- the title, linking to the article (Ask HN and other text posts link to the thread);
- the page's `og:image` (falling back to `twitter:image`, then to the first `<img>` in its `<article>` or `<main>` that isn't declared smaller than 200 pixels), also as `media:thumbnail` for list views;
- the page's excerpt: its opening paragraphs (inside the first `<article>` or `<main>` that has any, when it has one: a later one holds related stories, teasers or comments; from the first of at least 80 characters, without captions), or its description when that's longer, cut to about 2,000 characters;
- the submitter's own text, for Show HN and other posts that have some, quoted below the page's description (or in its place);
- the article's link, labelled with its site name (or domain), and its reading time, as `Example · 9 min read`: after the excerpt, where one decides whether to read on. A text post gets the reading time alone, when its text is longer than the excerpt;
- the top comment, as HN ranks it, cut to about 2,000 characters, with a link to it;
- a last line with a link to the HN thread with the comment count, the points, and the story's age and author, as `37 comments · 150 points · 🔥 2 hours ago by pg`. As in the Telegram bot, 🔥 marks a story at most 4 hours old and ❄️ one at least 2 days old;
- `<comments>` set to the HN thread, as in hnrss;
- `<category>` tags with the post kind (Show HN, Ask HN, Launch HN, Tell HN) and the linked domain, for reader filters and rules.

Each feed carries an icon (`<image>`), so readers show one next to its name.

The reading time counts the words of the article's paragraphs at 230 a minute, or of the whole of the post's own text for Ask HN and other text posts. It's left out when the excerpt shown holds at least 90% of those words, as it's then the whole post less its captions and bylines; under 200 words (usually a page its scripts fill in); and for a page that says it's paywalled, where only the lede is there to count. Pages read before word counting began, or before the count stopped at the first `<article>` or `<main>`, are read again, at most 100 a build, so older items get a right one too; a page that fails then keeps its preview without one.

The counts are a snapshot from when the item was built. A page that blocks bots (`403`), times out or isn't HTML gets an item without the image and excerpt.

Job posts ("Acme (YC S13) Is Hiring") are ads and never become items.

## RSS items

An item of an RSS feed keeps the source's title, link, guid, `pubDate` and categories, and the author as `dc:creator`. Its description has:

- the image: the source's (an image enclosure, `media:content`, the first `<img>` in its description, then in its full content, that isn't declared smaller than 100 pixels, or `media:thumbnail`, in that order: thumbnails are often small crops), else the article page's, as for HN items, also as `media:thumbnail`;
- the excerpt: the source's text as plain text, which is its full content (`content:encoded`, Atom `<content>`) when that's longer than its description, or the page's excerpt, whichever is longer, cut to about 2,000 characters.

Excerpts, here and in HN items, keep their paragraphs. One that's too long ends with its last whole paragraph or sentence when that keeps at least half of it, else at a word, with an ellipsis.

A line after the excerpt gives the reading time and the story's points on HN, when known. The reading time comes from the source's full content (`content:encoded`, Atom `<content>`) when that's longer than its excerpt, else from the article page, as for HN items: content the excerpt holds may be the teaser again, which some sources repeat there, so the larger count wins; a description alone says nothing of the article's length. As in HN items, it's left out when the excerpt is the whole post. The title already links to the article, so that line links to it, labelled with its site name (or domain), only when the item has neither image nor excerpt.

The article page is fetched once per item, as for HN stories, unless the source has the image, more than a teaser (500 characters of text) and the whole article (full content longer than its excerpt), which gives the reading time: in practice, for every item of a source without full content. Without `enrich`, only items with full content get a reading time. With `exclude_paywalled` every item's page is fetched, as it says whether the item is free.

## Reading in NetNewsWire

NetNewsWire, and Feedly when it syncs it, takes an item's body from its description and shows its first 300 characters, as plain text, in the timeline. Items start with the excerpt, so those characters are the article's opening; the reading time follows, and the counts come last. The image doubles as `media:content` and `media:thumbnail`, which give the timeline its thumbnail and Feedly its visual, and the article view doesn't repeat it.

## Feeds

A `feeds.toml` lists the feeds; [`examples/feeds.toml`](examples/feeds.toml) has one of each kind, with every key described. Each `[[feed]]` table publishes an HN feed:

- `file`: the published file name, ending in `.xml`.
- `title`: the feed's title.
- `tags`: [Algolia tags](https://hn.algolia.com/api) such as `front_page`, `story`, `show_hn` or `ask_hn`. A feed with `front_page` follows the current front page. Any other feed takes the newest matching stories created within its window.
- `min_points`, `min_comments`: only stories with at least that many (default 0).
- `window_hours`: how long a story stays in the feed after it first appeared there (default 48). A story the API still returns for the feed stays however old it is: Algolia's `front_page` tag sometimes lists stories posted days ago.
- `max_items`: the most items the feed holds (default 150).
- `ranked`: `true` keeps only stories in HN's current ranking, the ~500 ids of [`topstories.json`](https://github.com/HackerNews/API#new-top-and-best-stories), at any age (default `false`). This is the list [hackernewsbot](https://github.com/phil-r/hackernewsbot) reads; with `min_points = 150`, `top.xml` holds the stories it posts. The bot posts at most one story per 10 minutes; a feed doesn't need that limit.
- `exclude_titles`: [regular expressions](https://docs.python.org/3/library/re.html#regular-expression-syntax); a story whose title matches one never enters the feed, such as `^Launch HN:`, YC companies announcing a product.
- `exclude_paywalled`: `true` hides the stories whose page's [JSON-LD](https://developers.google.com/search/docs/appearance/structured-data/paywalled-content) says `"isAccessibleForFree": false` (default `false`). Every story's page is read for its preview anyway; a story is held back until its page has been read, and one whose page fails three times shows. A page without the marker counts as free.
- `site`: the website the feed is about (default `https://news.ycombinator.com/`). It is the channel's `<link>`, which Feedly takes a feed's icon from, so each feed shows its source's icon rather than one shared by every feed here.
- `icon`: the channel's `<image>`, for readers that show it (default the site's `/favicon.ico`).

Each `[[digest]]` table publishes one item per UTC day, listing that day's top stories by points, each with its image, the first paragraph of its excerpt (up to about 500 characters), then its reading time and counts:

- `file`, `title`: as for a feed.
- `top`: stories per day (default 20).
- `days`: days kept (default 7).
- `publish_hour`: the UTC hour of the next day when a day appears (default 6), by when late stories have had time to score. A day's list is frozen once published.
- `exclude_titles`: as for a feed; a matching story never counts among a day's top.
- `exclude_paywalled`: as for a feed; a paywalled story is left out of its day's list, which then has fewer than `top` stories.

In an HN feed, an item's `pubDate` is when that feed first listed the story, not when it was posted. A story that crosses 100 points a day after it was posted is new to `best.xml` then, and a reader sorting by date shows it at the top. A new feed's first batch is the exception: it carries the stories' post times.

Each `[[rss]]` table publishes items from other feeds:

- `file`, `title`: as for an HN feed.
- `source`: the URL of an RSS or Atom feed; or `sources`, a list of them, merged into one feed. A table has one or the other.
- `json`: for a site with no feed, whose page loads its posts from a JSON API: the sources are JSON documents, and this inline table says where the items are. `items` is the dotted path to the list (`"results"`, `"data.posts"`); `title` and `link`, and optionally `date`, `summary`, `author` and `image`, are paths within each item. `date_format` is a [strptime pattern](https://docs.python.org/3/library/datetime.html#format-codes) for the date; without one, ISO 8601 or Unix seconds. Links resolve against the source's URL, a date without a time is its midnight UTC, and an item without a title or a link is skipped. Filters, merging and enrichment work as for RSS.
- `include_categories`, `exclude_categories`: categories, matched against the whole category, ignoring case.
- `include_sections`, `exclude_sections`: sections, matched like categories but only against the item's first category, for sites that list the section first and then free-form tags reusing section names. Check the site first: a feed that lists categories alphabetically has no section first, and the URL path (`exclude_links`) is the filter to use.
- `include_titles`, `exclude_titles`, `include_links`, `exclude_links`: [regular expressions](https://docs.python.org/3/library/re.html#regular-expression-syntax), each searched anywhere in the item's title or link. Start one with `(?i)` to ignore case.
- `exclude_authors`: authors, matched against the whole name, ignoring case.
- `exclude_text`: regular expressions searched in the item's title and the source's whole description, as plain text, when the item first appears. For example, a vendor's studies "sponsored by" it whose titles don't say so.
- `enrich`: `false` skips fetching article pages, so items carry only what the source gives (default `true`). With `true`, an item whose source lacks an image or has only a teaser gets the page's image and opening.
- `exclude_paywalled`: `true` fetches every item's page and hides the items whose [JSON-LD](https://developers.google.com/search/docs/appearance/structured-data/paywalled-content) says `"isAccessibleForFree": false`, anywhere in the page's structured data (default `false`; needs `enrich`). An item is held back until its page has been read, so a paywalled one never shows for a build; one whose page fails three times shows. A page without the marker counts as free. News sites mark their subscriber-only newsletters this way, so one made free gets in by itself.
- `exclude_on_hacker_news`: `true` hides the items whose link was a story in the HN feeds: one the front page, best or top feeds hold, or one HN gave at least the lowest `min_points` of the `[[feed]]` tables (100 when none sets one), looked up in Algolia by URL. The lookup runs when an item appears and every 6 hours for its first 2 days, while a story's points still grow; a failed one leaves the item shown and is retried on the next build (default `false`).
- `site`, `icon`: as for an HN feed; `site` defaults to the first source's site (its scheme and host). Set it when that host isn't the site readers know (a `feeds.` host, `github.com`), and `icon` when the site's `/favicon.ico` is missing or wrong.
- `source_titles`: a `[rss.source_titles]` table of source URL = label. The label goes in front of the titles of that source's items, unless a title already starts with it, before the filters see them: a GitHub release feed titles its items with a bare version (`v2.15.3`), so a feed merging several projects needs `Alpha v2.15.3`. The editor has no field for it: edit it in the file.
- `max_items`: the most items the feed holds, newest first by the source's dates (default 100).
- `keep_days`: how long an item stays once no source lists it (default 7), below.
- `dedupe`: `"link"` (default) or `"link+title"`, below.

The filters apply in two steps. The includes come first: an item must match every include list the table sets, any entry of each. The excludes then drop an item that matches any entry of any exclude list. Changing a filter applies to the items the feed already holds on the next build.

A news site's feed without its comic strip, its podcasts, its ticketed events and its paywalled newsletters:

```toml
[[rss]]
file = "news.xml"
title = "News: articles"
source = "https://news.example.org/feed"
exclude_categories = ["comics", "podcast"]
exclude_links = ['^https://news\.example\.org/(episodes|events)/']
exclude_paywalled = true
```

Several sources merged into one feed, a story both carry appearing once, without webinars or sponsored posts:

```toml
[[rss]]
file = "engineering.xml"
title = "Engineering blogs"
sources = [
  "https://blog.example.com/feed.xml",
  "https://engineering.example.net/atom.xml",
]
dedupe = "link+title"
exclude_titles = ['(?i)\bwebinar\b']
exclude_text = ['(?i)\bsponsored by\b']
```

`dedupe = "link+title"` also merges stories published under different URLs. Check a site's categories before filtering on them: some feeds list them alphabetically, so the first isn't the section, and the URL path (`exclude_links`) is the better filter.

A site with no feed, whose page loads its posts from a JSON API:

```toml
[[rss]]
file = "blog.xml"
title = "A blog without a feed"
source = "https://www.example.com/api/posts?lang=en"
json = { items = "results", title = "title", link = "url", date = "date", date_format = "%B %d, %Y", summary = "excerpt" }
```

An item stays while a source lists it, and for `keep_days` (7 by default) after it first appeared once none does, within `max_items`. A feed whose source lists only a few days and whose filter keeps few items needs a longer `keep_days` not to go empty in a quiet week. A source that fails (an HTTP error, a timeout, a page that isn't a feed) keeps its items while the others refresh.

### Duplicates

A story that two sources carry, or one source under two URLs, appears once. Two items are the same story when their canonical links match, or their guids do. The canonical link is the item's link with:

- the scheme and host lowercased (`www.` kept);
- no fragment and no trailing slash;
- no tracking parameters: `utm_*`, `fbclid`, `gclid`, `ref`, `mc_cid`, `mc_eid`;
- the other query parameters sorted.

With `dedupe = "link+title"`, items with the same normalised title also count as one: case and Unicode forms folded, runs of whitespace and punctuation collapsed. This suits sites that publish one story under different URLs.

The copy the feed first saw stays, so its guid holds across builds. Among copies new in the same build, the one with the earliest date wins, then the one from the source listed first. The kept item carries the categories of every copy, so a section feed's tags aren't lost. The published item lists a category once, in its first spelling: "Apple" and the tag "apple" become "Apple".

A push to `main` republishes the feeds. An unknown key, a wrong type, a regular expression that doesn't compile or a duplicate `file` fails the build.

## Editor

<https://feeds.michelebologna.net/editor/> edits the private `feeds.toml` in the browser: every `[[feed]]`, `[[digest]]` and `[[rss]]` table, with each feed's item count, images, last good fetch and subscription URL. You can add, duplicate, delete and rename feeds, and change any key. The editor checks every change with the build's own code and messages, so a regular expression that doesn't compile or a duplicate `file` is refused before anything is saved.

For an `[[rss]]` table it previews the filters on the live sources: each item with the rule that drops it or the story it merges into, the counts before and after the change, and the sections and categories with their counts. Clicking a section or category adds it to `exclude_sections` or `exclude_categories`. A Hacker News table shows the items published now. `exclude_paywalled` and `exclude_on_hacker_news` are checked at build time only: the preview fetches no article pages.

**Sign in** with a [fine-grained token](https://github.com/settings/personal-access-tokens/new) limited to the private repository that holds `feeds.toml` (`mbologna/feeds-config` here), with these permissions:

- Contents: read and write;
- Pull requests: read and write.

The token stays in the browser's local storage for `feeds.michelebologna.net`; **Sign out** removes it.

**Saving** shows the diff of `feeds.toml` and a commit message, `chore(config): …`, which you can edit; its first line must fit in 72 characters. The editor then:

1. creates a branch `editor/…` from the commit it loaded, commits `feeds.toml` to it and opens a pull request;
2. follows it: the config repository's workflow checks the file with `feed-refinery check-config`, merges the pull request (a private repository on GitHub's free plan has no auto-merge) and dispatches this repository's build;
3. waits for the build's `health.json` to show a build after the merge, a minute or two.

The feeds Worker serves the editor's three endpoints on the same host, each with the token as `Authorization: Bearer <token>`, and answers only a token that can push to the config repository:

- `GET /editor/fetch?url=<source>`, a relay for the preview: most feeds don't allow a browser to fetch them from another site. Without it the editor says "Preview relay unavailable", and editing and saving still work;
- `GET /editor/health`: the build's `health.json`, private, plus each feed's subscription URL;
- `GET /editor/feed?file=<file>`: a published feed.

The Worker also sends a Content-Security-Policy for `/editor/*`; the browser test runs the editor under the same policy (`CSP` in `tests/test_editor_browser.py`).

The same Python code as the build runs in the browser: [Pyodide](https://pyodide.org/), pinned and loaded from jsDelivr with Subresource Integrity hashes, runs in a Web Worker the feed-refinery wheel and the locked versions of feedparser and tomlkit (`uv.lock`'s `pyodide` group), which CI publishes to GitHub Pages with the editor, alone, as `feed_refinery.whl` and `requirements.txt`. `src/feed_refinery/site/editor/bridge.py` loads, edits and previews: it edits with [tomlkit](https://github.com/python-poetry/tomlkit), which keeps the file's comments and layout, and filters and merges with `rss.select`, the function the build uses.

## How it's published

The [CI workflow](.github/workflows/ci.yml) runs the tests on every push and pull request, checks `examples/feeds.toml` with `feed-refinery check-config`, and tests the editor in Chromium. On `main` it then builds the feeds, writes them to Workers KV, and deploys the editor to GitHub Pages:

- on every push;
- every 30 minutes (at :07 and :37), dispatched by a Cron Trigger on the Worker that serves the feeds: GitHub's own schedule ran hours late, or not at all. Those runs skip the tests;
- when the private `feeds.toml` changes: its repository's workflow dispatches the build;
- every Monday at 06:00 UTC, on GitHub's own schedule;
- on a manual run.

This repository and its Actions logs are public, and the feed list isn't, so:

- the build checks out the private `feeds.toml` with a read-only deploy key (secret `FEEDS_CONFIG_DEPLOY_KEY`);
- `feed-refinery build --quiet` prints totals only: no feed, source or URL, even in an error;
- the feeds, `state.json` and `health.json` go to Workers KV (`feed-refinery kv-publish`), never to Pages or an artifact. The editor is the only thing on Pages.

A build:

1. Reads the previous run's `state.json` from KV (`feed-refinery kv-get`). It holds every listed story with its preview, and when each feed first listed it.
2. Asks the [Algolia HN API](https://hn.algolia.com/api), the one hnrss uses, for each feed's stories. Feeds that share a query share one request. A ranked feed first reads `topstories.json` from the [HN API](https://github.com/HackerNews/API), then asks Algolia for those ids, 100 per request.
3. Then refreshes the counts of the stories a feed still holds but the API no longer lists for it (off the front page, out of the ranking), 100 per request.
4. Fetches the previews still missing, eight pages at a time: the first 512 KB of each, with a 10-second timeout. It retries a failed page on the next two runs, except for permanent errors such as `403` and `404`.
5. Looks up the top comment of every listed story that has comments, at most every 2 hours per story: the first live reply in the story's `kids` from the HN API, which lists them in HN's order.
6. Fetches every RSS source with a conditional GET (`If-None-Match`, `If-Modified-Since`), a 30-second timeout and one retry after a network error; a source that two feeds share is fetched once. A `304` keeps what the source listed before, unless the feed's filters changed since the last build: then every source is fetched in full, so the items the feed holds are judged again. It reads the feed with [feedparser](https://github.com/kurtmckee/feedparser), filters and merges the items, then fetches the previews they lack, with the same retry rules as step 4.
7. Writes each feed, `state.json` and `health.json`, then publishes them to KV.

**Publishing to KV** writes only what changed. Each key's metadata holds the hash of its document without `lastBuildDate`, so one list request tells which feeds changed. The HN feeds change on every build (their items show points, comments and age), the RSS feeds only when their items do, and `state.json` and `health.json` are written every time: about 330 writes a day for 17 feeds. A feed no longer in `feeds.toml` is deleted from KV.

The free plan allows 1,000 writes a day (reset at 00:00 UTC) for the whole account, shared with any other Worker that writes to KV, so each run spends within a budget. It takes the account's writes so far (from the GraphQL Analytics API, with `CLOUDFLARE_ANALYTICS_TOKEN`) and sets aside the other writers' writes until midnight, at the rate they wrote so far, two writes for every later run, and a margin of 50. Within what's left it writes every changed feed while that's no more than this run's share of the day's rest (`mode` `all`). Past that it writes the feeds with a new item first, then refreshes that only change counts within the share (`items`). With no room for feeds it writes only `state.json` and `health.json` (`frozen`), and with not even that, nothing (`none`). A feed not written keeps serving its last version. A write refused for the daily limit stops the run, which fails. `health.json` records the account's writes, the other writers' rate and the mode under `kv`. `kv.guard` is true outside `all`, and `/health` reports it. Without the analytics token the budget counts only the build's own writes, which is right for a namespace in an account of its own.

`health.json` is for outside monitoring: the build time (`built`, Unix seconds) and, per feed, its item count and how many items have an image and an excerpt. Each RSS feed, under `rss`, also has `last_ok` (Unix seconds), the oldest of its sources' last good fetches, `sources_failed`, the sources that failed in this build, and `ok`, false while any source last answered more than 3 hours ago. When fewer than 30% of a feed's items have an image, the build also leaves a warning (counted, in a quiet build) on the run's summary page: a sign the runner is being blocked or the page parser broke.

The generated files are never committed. Losing `state.json` only means fetching the previews again.

After publishing, the workflow checks `https://feeds.michelebologna.net/health`, which fails when the last build is over 3 hours old or a feed is stale. If the dispatches stop, that check can't notice (it runs in the same workflow); the outside check of `/health` does, and alerts.

## Run it yourself

The script needs Python 3.15 or later (uv, below, downloads it if the machine has none), [feedparser](https://github.com/kurtmckee/feedparser), and network access to the HN APIs, the RSS sources and the linked pages. It reads the feed list from `feeds.toml` in the working directory, so start from a copy of the example. With [uv](https://docs.astral.sh/uv/), which installs feedparser too:

```sh
curl -o feeds.toml https://raw.githubusercontent.com/mbologna/feed-refinery/main/examples/feeds.toml
uvx --from git+https://github.com/mbologna/feed-refinery feed-refinery build \
  --output-dir public --page-url https://feeds.example.com/ --previous-state public/state.json
```

From a clone, `uv run feed-refinery build` takes the same options. The `build` options:

- `--config`: the feed list (default `feeds.toml`);
- `--output-dir`: where to write the feeds, `index.html`, `health.json`, `state.json` and the icons (default `public`);
- `--page-url`: the URL the folder is served from, used for the links in the feeds and on the index page (default: this site's address);
- `--previous-state`: the previous run's `state.json`, as a path or URL. Without the option the build fetches `state.json` from `--page-url`; an empty string starts fresh;
- `--quiet`: print totals only, with no feed, source or URL even in an error, for a public log of a private list.

Keep the output directory between runs and point `--previous-state` at the `state.json` inside it: the build reads it before writing the new one. The digest's frozen days, the fetched previews, the time each feed first listed a story and each RSS source's validators all live there. A missing or unreadable file starts an empty state, as on the first run, so every preview is fetched again.

Serve the folder with any static web server, such as nginx, Caddy or `python3 -m http.server`. The published site rebuilds every 30 minutes, at :07 and :37, from the directory that holds `feeds.toml`:

```cron
# Every 30 minutes, at :07 and :37
7,37 * * * *  cd /srv/feeds && uvx --from git+https://github.com/mbologna/feed-refinery feed-refinery build --output-dir /var/www/feeds --page-url https://feeds.example.com/ --previous-state /var/www/feeds/state.json
```

Give cron the full path to `uvx` if its `PATH` does not include it. `feed-refinery check --page-url https://feeds.example.com/`, run from the same directory, exits with status 1 when a configured feed is missing or was built more than 3 hours ago (`--max-age-hours`). `feed-refinery check-config feeds.toml` exits with status 2 and the build's message when the file wouldn't build.

### With a private list, on Workers KV

As this repository runs it, for free: the list in a private repository, the build in a public one's Actions (free minutes), the feeds in Workers KV behind a Worker that checks a token.

- **The list:** a private repository with `feeds.toml` and a workflow that runs `feed-refinery check-config` on every change. The build reads it with a read-only deploy key: its private half is the secret `FEEDS_CONFIG_DEPLOY_KEY` here.
- **KV:** a namespace, and a Cloudflare API token that can only edit Workers KV. Here they're the repository variables `CLOUDFLARE_ACCOUNT_ID` and `CLOUDFLARE_KV_NAMESPACE` and the secret `CLOUDFLARE_KV_TOKEN`; `feed-refinery kv-get` and `feed-refinery kv-publish` read the same names from the environment. When other Workers in the account write to KV too, add the secret `CLOUDFLARE_ANALYTICS_TOKEN`, a token that can only read the account's analytics, so the write budget counts their writes.
- **The build:** `feed-refinery kv-get state.json previous.json`, then `feed-refinery build --quiet --config config/feeds.toml --previous-state previous.json`, then `feed-refinery kv-publish public --config config/feeds.toml`.
- **Serving:** a Worker bound to the namespace serves `<file>.xml` with a token check, and computes `/health` from `health.json`. This one is in [mbologna/infra](https://codeberg.org/mbologna/infra) (`terraform/dns-michelebologna.net/feeds-worker.js`).

## Development

```sh
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run pre-commit run --all-files  # ruff, mypy, bandit and file checks; `uv run pre-commit install` runs them on each commit

# Build into public/ against the live APIs, starting from no state
uv run feed-refinery build --output-dir public --previous-state ''

# Check the published feeds' age
uv run feed-refinery check --max-age-hours 3

# Check the example feeds.toml
uv run feed-refinery check-config examples/feeds.toml

# The editor's browser test: Chromium, Pyodide from jsDelivr, packages from PyPI
uv run --group editor playwright install chromium
uv run --group editor pytest tests/test_editor_browser.py
```

`uv run pytest` skips the browser test unless the `editor` dependency group is installed.

The tests never touch the network, apart from the editor's browser test: they swap `urlopen` for a table of canned responses. The RSS fixtures under `tests/fixtures/` are cut from News Site's live feed, plus items modelled on Tech Site's, an Atom sample and a page that isn't a feed. `tests/fixtures/feeds.toml` is a sample config for the editor's tests; `examples/feeds.toml` is only checked to build.

Dependencies resolve only to releases at least 7 days old (`exclude-newer` in `pyproject.toml`). The script itself needs only feedparser and the standard library.

## Why not an existing tool

For Hacker News:

- [hnrss](https://github.com/hnrss/hnrss) (last changed in 2024) filters by points and comments, but has no `og:image` or excerpt, no top comment, no feed of ranked stories with 150+ points, no `pubDate` from when a feed first listed a story, and no daily digest.
- [RSSHub](https://docs.rsshub.app/)'s Hacker News route also filters by points and comments, and has the same gaps.
- [Huginn](https://github.com/huginn/huginn), a server that runs automation agents, could put parts of this together only with custom JavaScript agents.

For filtering other feeds:

- [FreshRSS](https://freshrss.org/) filters by category, title, URL and regular expression and can republish a saved query as RSS, but it is a server to run, with a database and a public route, and adds no images or excerpts.
- [RSS-Bridge](https://github.com/RSS-Bridge/rss-bridge)'s FilterBridge has no category filter, and the public instances belong to others.
- [Miniflux](https://miniflux.app/) filters but can't republish a feed.
- [Feedly](https://feedly.com/)'s mute filters need a Pro+ plan and match keywords only, not categories or links.
- None of them hides the items of one feed that another already carried (an aggregator's stories HN had), or reads a page's paywall marker.
- For a site with no feed, RSS-Bridge needs a bridge written for that site and a server to run it; the JSON API the site's own page reads needs only a field map.

Actions and Pages cost nothing for a public repository, and the HN feeds already had the preview fetching and the state that RSS items need. Checked 2026-11-24.

## Versioning

`main` is what runs: every push to it, and every 30-minute dispatch, deploys the feeds to GitHub Pages. Releases follow SemVer as a changelog: the Monday run's [auto-release](https://github.com/mbologna/auto-release) tags `vX.Y.Z` from the [Conventional Commit](https://www.conventionalcommits.org/) types merged since the last tag (`feat` minor; `fix`, `perf`, `revert` patch; a breaking change major), or cuts a maintenance release when only dependency updates landed; the 30-minute dispatches never release. The tag builds and deploys nothing; its [GitHub Release](https://github.com/mbologna/feed-refinery/releases) lists the changes. See the [versioning standard](https://github.com/mbologna/.github/blob/main/VERSIONING.md).

## License

MIT, see [LICENSE](LICENSE).

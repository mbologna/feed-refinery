# AGENTS.md

Notes for coding agents working in this repository. [README.md](README.md) covers what it does and how to use it.

## Layout

`feeds` (`src/feeds/`) builds two kinds of feed from a `feeds.toml` ([`examples/feeds.toml`](examples/feeds.toml) shows every key):

- Hacker News: `[[feed]]` and `[[digest]]` tables; `source.py` asks the Algolia and Firebase APIs, `comments.py` finds the top comment.
- Other RSS or Atom feeds: `[[rss]]` tables; `rss.py` fetches (conditional GET), parses with feedparser, filters and computes the dedup keys.

`config.py` validates every table. `rss.select` filters a feed's items (`rss.verdict` names the rule that drops one) and merges the copies of each story; `state.py` merges each run's results into `state.json`, which the build keeps in Workers KV with the feeds, and fetches previews (`preview.py`) with one set of retry rules for both kinds. `feed.py` renders the documents, `page.py` the index, `cli.py` runs a build and writes `health.json`; `kv.py` publishes a build to Workers KV, writing only what changed.

The web editor lives in `src/feeds/site/editor/`: plain HTML, CSS and JavaScript, no build step. `worker.js` runs Pyodide with the feeds wheel and `bridge.py`, which validates with `config.parse`, edits with tomlkit and previews with `rss.parse` and `rss.select`, so the browser runs the build's code. CI publishes the folder at `/editor/` with the wheel and `requirements.txt`; `feeds build` copies only the files directly in `site/`.

## Constraints

- `state.json` is versioned (`VERSION` in `state.py`). A change to its shape bumps the version and keeps `from_json` reading the older versions, so a deploy never drops the published state.
- Tests never touch the network: `tests/conftest.py` swaps `urlopen` for canned responses. RSS fixtures live in `tests/fixtures/`.
- Coverage must stay at or above `fail_under` in `pyproject.toml`.
- **Never commit a personal feed list.** The real `feeds.toml` is private (mbologna/feeds-config: it shows what someone reads); this repository has only `examples/feeds.toml` and `tests/fixtures/feeds.toml`, with example or sample sites. The same goes for README examples, test data, commit messages and pull request text.
- The build's Actions log is public: in CI it runs `feeds build --quiet`, which prints totals only, and nothing of the feeds goes to Pages or an artifact (`kv-publish` writes them to KV). Don't add output that names a feed, a source or an article URL to the quiet path or to `kv-publish`.
- Tests use `tests/fixtures/feeds.toml`; `examples/feeds.toml` is only checked to build.
- The browser test (`tests/test_editor_browser.py`) needs the `editor` dependency group and the network; the python job skips it, the `editor` CI job runs it.
- The Pyodide version and its Subresource Integrity hashes are pinned in `worker.js`; a new version means new hashes for every file listed there.
- The editor's HTML, CSS and JavaScript are formatted with `npx prettier --print-width 120`.
- The feeds Worker serves `/editor/fetch` and sends the editor's Content-Security-Policy; `CSP` in the browser test is a copy of it. An editor change that needs another origin, or inline code, changes both.
- The repository is public: test data and examples use public sites only.

## Versioning

SemVer with changelog-only releases: `vX.Y.Z` tags, each with a GitHub Release listing the changes, per the [versioning standard](https://github.com/mbologna/.github/blob/main/VERSIONING.md).

- A tag builds and deploys nothing: `main` is what runs, built and published to Workers KV on every push and every 30-minute dispatch.
- Never create, move or delete tags, and never edit a version number to cut a release: auto-release tags from the commit types (`feat` minor; `fix`, `perf`, `revert` patch; `!` or a `BREAKING CHANGE:` footer major).
- Write Conventional Commit subjects; they decide the next version.
- `version` in `pyproject.toml` is a placeholder and stays `0.0.0`. Nothing reads it: tags are the version.

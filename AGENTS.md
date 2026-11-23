# AGENTS.md

Notes for coding agents working in this repository. [README.md](README.md) covers what it does and how to use it.

## Layout

`feeds` (`src/feeds/`) builds two kinds of feed from [`feeds.toml`](feeds.toml):

- Hacker News: `[[feed]]` and `[[digest]]` tables; `source.py` asks the Algolia and Firebase APIs, `comments.py` finds the top comment.
- Other RSS or Atom feeds: `[[rss]]` tables; `rss.py` fetches (conditional GET), parses with feedparser, filters and computes the dedup keys.

`config.py` validates every table. `state.py` merges each run's results into `state.json`, which travels with the published site, and fetches previews (`preview.py`) with one set of retry rules for both kinds. `feed.py` renders the documents, `page.py` the index, `cli.py` runs a build and writes `health.json`.

## Constraints

- `state.json` is versioned (`VERSION` in `state.py`). A change to its shape bumps the version and keeps `from_json` reading the older versions, so a deploy never drops the published state.
- Tests never touch the network: `tests/conftest.py` swaps `urlopen` for canned responses. RSS fixtures live in `tests/fixtures/`.
- Coverage must stay at or above `fail_under` in `pyproject.toml`.
- The repository is public: test data and examples use public sites only.

## Versioning

SemVer with changelog-only releases: `vX.Y.Z` tags, each with a GitHub Release listing the changes, per the [versioning standard](https://github.com/mbologna/.github/blob/main/VERSIONING.md).

- A tag builds and deploys nothing: `main` is what runs, deployed to GitHub Pages on every push and every 30-minute dispatch.
- Never create, move or delete tags, and never edit a version number to cut a release: auto-release tags from the commit types (`feat` minor; `fix`, `perf`, `revert` patch; `!` or a `BREAKING CHANGE:` footer major).
- Write Conventional Commit subjects; they decide the next version.
- `version` in `pyproject.toml` is a placeholder and stays `0.0.0`. Nothing reads it: tags are the version.

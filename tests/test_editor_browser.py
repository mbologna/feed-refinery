"""The editor in a real browser: Pyodide, the feeds wheel and bridge.py, with GitHub and the relay mocked.

Skipped unless the editor dependency group is installed. Run it with:

    uv run --group editor playwright install chromium
    uv run --group editor pytest tests/test_editor_browser.py

It loads Pyodide from jsDelivr and the packages from PyPI, so it needs the
network. FEEDS_WHEEL names a wheel already built; otherwise it builds one.
The browser test is not in the coverage total: it covers the JavaScript.
"""

from __future__ import annotations

import base64
import functools
import json
import os
import shutil
import subprocess
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

sync_api = pytest.importorskip("playwright.sync_api")

ROOT = Path(__file__).parent.parent
FIXTURES = Path(__file__).parent / "fixtures"
CONFIG = (FIXTURES / "feeds.toml").read_text(encoding="utf-8")
NEWS_SAMPLE = (FIXTURES / "newssite.xml").read_bytes()
SOURCE = "https://www.newssite.example/feed"
REPO = "https://api.github.com/repos/mbologna/feeds"
BOOT = 180_000  # ms: Pyodide and the packages come from the network


# The Content-Security-Policy the feeds Worker sends for /editor/*: scripts
# from this site and the pinned Pyodide (blob: for the integrity-checked
# modules the worker imports), WebAssembly, and connections to this site,
# GitHub's API, jsDelivr and PyPI (micropip).
CSP = (
    "default-src 'none'; script-src 'self' blob: https://cdn.jsdelivr.net 'wasm-unsafe-eval'; worker-src 'self'; "
    "connect-src 'self' https://api.github.com https://cdn.jsdelivr.net https://pypi.org https://files.pythonhosted.org; "
    "style-src 'self'; img-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)


class Quiet(SimpleHTTPRequestHandler):
    def end_headers(self) -> None:
        self.send_header("Content-Security-Policy", CSP)
        super().end_headers()

    def log_message(self, *args) -> None:
        pass


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    """The editor and its wheel, served as the site publishes them, under /editor/."""
    root = tmp_path_factory.mktemp("site")
    shutil.copytree(ROOT / "src/feeds/site/editor", root / "editor")
    wheel = os.environ.get("FEEDS_WHEEL")
    if not wheel:
        dist = tmp_path_factory.mktemp("dist")
        subprocess.run(["uv", "build", "--wheel", "--quiet", "-o", str(dist)], cwd=ROOT, check=True)
        wheel = str(next(dist.glob("feeds-*.whl")))
    shutil.copy(wheel, root / "editor/feeds.whl")
    # As the CI build job publishes it.
    export = ["uv", "export", "--frozen", "--only-group", "pyodide", "--no-hashes", "--no-annotate", "--no-header"]
    pins = subprocess.run(export, cwd=ROOT, check=True, capture_output=True, text=True).stdout
    (root / "editor/requirements.txt").write_text(pins)
    server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(root)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


class GitHub:
    """The REST and GraphQL answers the editor needs, and the requests it made."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict | None]] = []
        self.auth: dict[str, str | None] = {}  # path -> the Authorization header it came with

    def answer(self, route) -> None:
        request = route.request
        path = urlsplit(request.url).path
        body = json.loads(request.post_data) if request.post_data else None
        self.requests.append((request.method, request.url, body))
        self.auth[path] = request.headers.get("authorization")
        answers = {
            ("GET", "/repos/mbologna/feeds"): {"permissions": {"push": True}},
            ("GET", "/user"): {"login": "tester"},
            ("GET", "/repos/mbologna/feeds/branches/main"): {"commit": {"sha": "c0ffee"}},
            ("GET", "/repos/mbologna/feeds/contents/feeds.toml"): {
                "sha": "blob1",
                "content": base64.encodebytes(CONFIG.encode()).decode(),
            },
            ("POST", "/repos/mbologna/feeds/git/refs"): {"ref": "refs/heads/x"},
            ("PUT", "/repos/mbologna/feeds/contents/feeds.toml"): {"commit": {"sha": "abcdef1234"}},
            ("POST", "/repos/mbologna/feeds/pulls"): {
                "number": 42,
                "node_id": "PR_node",
                "html_url": "https://github.com/mbologna/feeds/pull/42",
            },
            ("POST", "/graphql"): {"data": {"enablePullRequestAutoMerge": {"clientMutationId": None}}},
            ("GET", "/repos/mbologna/feeds/pulls/42"): {
                "merged": True,
                "state": "closed",
                "merged_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 60)),
                "merge_commit_sha": "merge1",
                "head": {"sha": "abcdef1234"},
            },
            ("GET", "/repos/mbologna/feeds/actions/runs"): {
                "workflow_runs": [{"id": 7, "name": "CI", "status": "in_progress", "conclusion": None}]
            },
            ("GET", "/repos/mbologna/feeds/actions/runs/7/jobs"): {
                "jobs": [{"name": "Deploy to GitHub Pages", "status": "completed", "conclusion": "success"}]
            },
        }
        data = answers.get((request.method, path))
        if data is None:
            route.fulfill(status=404, json={"message": "Not Found"})
        else:
            route.fulfill(status=201 if request.method in ("POST", "PUT") else 200, json=data)

    def sent(self, method: str, suffix: str) -> dict | None:
        return next(body for m, url, body in self.requests if m == method and urlsplit(url).path.endswith(suffix))


def health(route) -> None:
    """health.json from a build that ran just now, after the mocked merge."""
    built = int(time.time())
    newssite = {"items": 96, "images": 95, "excerpts": 95, "ok": True, "last_ok": built - 600, "sources_failed": []}
    route.fulfill(json={"built": built, "feeds": {}, "digests": {}, "rss": {"newssite.xml": newssite}})


def test_edit_news_sites_filters_preview_and_save(site):
    github = GitHub()
    relay = {"up": False, "requests": []}

    def fetch(route) -> None:
        request = route.request
        relay["requests"].append((request.headers.get("authorization"), parse_qs(urlsplit(request.url).query)["url"]))
        if not relay["up"]:
            route.fulfill(status=404, body="Not Found")
        else:
            route.fulfill(status=200, body=NEWS_SAMPLE, headers={"Content-Type": "application/rss+xml; charset=UTF-8"})

    with sync_api.sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context()
        context.add_init_script("localStorage.setItem('feeds-editor-token', 'test-token')")
        context.route("https://api.github.com/**", github.answer)
        context.route("**/editor/fetch?*", fetch)
        context.route("**/health.json", health)
        page = context.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        # A CSP violation shows up here; the relay's 404 below is expected.
        page.on("console", lambda m: m.type == "error" and "404" not in m.text and errors.append(m.text))
        page.goto(f"{site}/editor/")
        expect = sync_api.expect
        expect(page.locator("#runtime")).to_have_text("Python ready", timeout=BOOT)
        expect(page.locator("#who")).to_have_text("Signed in as tester")

        page.get_by_role("button", name="News Site: articles").click()
        expect(page.locator(".feed-button[aria-current=true]")).to_contain_text("96 items")

        # No relay yet: the editor says so, and editing still works.
        page.get_by_role("button", name="Fetch the sources").click()
        expect(page.locator("#preview-result")).to_contain_text("Preview relay unavailable")
        assert relay["requests"] == [("Bearer test-token", [SOURCE])]

        relay["up"] = True
        page.get_by_role("button", name="Fetch the sources").click()
        expect(page.locator("[data-count=published]")).to_have_text("7")
        expect(page.locator("[data-count=dropped]")).to_have_text("5")

        # Click-to-exclude: the Bits category.
        page.get_by_role("button", name="Exclude category Bits: 1 item, 1 pass").click()
        expect(page.locator("[data-count=published]")).to_have_text("6")
        expect(page.locator(".counts")).to_contain_text("6 published (was 7)")
        expect(page.locator("tr", has_text="Un uomo senzatetto")).to_contain_text("dropped: exclude_categories bits")
        expect(page.get_by_role("list", name="Exclude categories")).to_contain_text("Bits")

        # And one more by hand: a title pattern.
        entry = page.get_by_role("textbox", name="Exclude titles: new entry")
        entry.fill("^Perché")
        entry.press("Enter")
        expect(page.locator("[data-count=published]")).to_have_text("5")

        expect(page.locator("#review")).to_be_enabled()
        page.locator("#review").click()
        diff = page.locator("#diff")
        expect(diff).to_contain_text('-exclude_categories = ["peanuts", "foto del giorno", "celebrity"]')
        expect(diff).to_contain_text('+exclude_categories = ["peanuts", "foto del giorno", "celebrity", "Bits"]')
        expect(diff).to_contain_text("+exclude_titles = ['(?i)^le prime pagine', '^Perché']")
        message = page.locator("#message")
        expect(message).to_have_value("chore(config): change newssite.xml exclude_categories and exclude_titles")
        expect(page.locator("#message-check")).to_have_text("First line: 70 of 72 characters.")

        # The header rule.
        message.fill("chore(config): Exclude Bits and the explainer on why the bridges of Kyiv matter, from News Site.")
        expect(page.locator("#message-check")).to_contain_text("the limit is 72")
        expect(page.locator("#message-check")).to_contain_text("lowercase word")
        expect(page.locator("#submit")).to_be_disabled()
        message.fill("chore(config): drop Bits and the Kyiv bridges from newssite.xml")
        expect(page.locator("#submit")).to_be_enabled()

        page.locator("#submit").click()
        expect(page.locator("#steps")).to_contain_text(
            "Live: the published feeds use the new feeds.toml", timeout=30_000
        )
        assert not errors

        branch = github.sent("POST", "/git/refs")
        assert branch["sha"] == "c0ffee" and branch["ref"].startswith(
            "refs/heads/editor/drop-bits-and-the-kyiv-bridges-from-"
        )
        put = github.sent("PUT", "/contents/feeds.toml")
        assert (
            put["sha"] == "blob1"
            and put["message"] == "chore(config): drop Bits and the Kyiv bridges from newssite.xml\n"
        )
        saved = base64.b64decode(put["content"]).decode()
        assert saved == CONFIG.replace(
            '"celebrity"]\nexclude_links', '"celebrity", "Bits"]\nexclude_links'
        ).replace("'(?i)^le prime pagine']", "'(?i)^le prime pagine', '^Perché']")
        pull = github.sent("POST", "/pulls")
        assert pull["title"] == "chore(config): drop Bits and the Kyiv bridges from newssite.xml"
        assert pull["base"] == "main" and pull["head"] == branch["ref"].removeprefix("refs/heads/")
        merge = github.sent("POST", "/graphql")
        assert "mergeMethod: MERGE" in merge["query"] and merge["variables"] == {"id": "PR_node"}
        # The token goes to the calls that need it; the public reads while polling go without.
        assert github.auth["/repos/mbologna/feeds/pulls"] == "Bearer test-token"
        assert github.auth["/repos/mbologna/feeds/actions/runs"] is None
        browser.close()

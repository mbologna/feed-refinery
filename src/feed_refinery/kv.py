"""Publishing to Cloudflare Workers KV: the feeds, state.json and health.json, writing only what changed.

The free plan allows 1,000 writes a day (reset at 00:00 UTC). A build writes
a feed only when its document changed, ignoring <lastBuildDate>, which
changes every run; each key's metadata holds that hash, so one list request
tells what changed. state.json and health.json are written on every run.
health.json counts the day's writes; past GUARD_WRITES, a feed is written
only when its items changed (a new guid), not for a count or an age in an
item, and health.json says so, which the feeds Worker reports on /health.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path

from . import HEALTH_NAME, STATE_NAME, USER_AGENT

API = "https://api.cloudflare.com/client/v4"
DAILY_WRITES = 1000  # the free plan's
GUARD_WRITES = 900
LAST_BUILD = re.compile(rb"<lastBuildDate>[^<]*</lastBuildDate>")
GUID = re.compile(rb"<guid[^>]*>([^<]*)</guid>")
TIMEOUT = 30


class KvError(RuntimeError):
    pass


def content_hash(document: bytes) -> str:
    """The document's hash without its lastBuildDate: equal when only the build time changed."""
    return hashlib.sha256(LAST_BUILD.sub(b"", document)).hexdigest()


def items_hash(document: bytes) -> str:
    """A hash of the document's item guids: equal when no item came or went."""
    return hashlib.sha256(b"\n".join(GUID.findall(document))).hexdigest()


@dataclass
class Kv:
    """One namespace, through the REST API. Errors say what failed and the HTTP status, never a key's content."""

    account: str
    namespace: str
    token: str

    @classmethod
    def from_env(cls) -> Kv:
        names = ("CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_KV_NAMESPACE", "CLOUDFLARE_KV_TOKEN")
        missing = [n for n in names if not os.environ.get(n)]
        if missing:
            raise KvError(f"set {', '.join(missing)}")
        return cls(*(os.environ[n] for n in names))

    @property
    def base(self) -> str:
        return f"{API}/accounts/{self.account}/storage/kv/namespaces/{self.namespace}"

    def _call(self, what: str, method: str, url: str, body: bytes | None = None, content_type: str = "") -> bytes:
        headers = {"Authorization": f"Bearer {self.token}", "User-Agent": USER_AGENT}
        if content_type:
            headers["Content-Type"] = content_type
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:  # nosec B310 # the Cloudflare API, https
                return r.read()
        except urllib.error.HTTPError as e:
            raise KvError(f"KV {what}: HTTP {e.code}") from None
        except (urllib.error.URLError, OSError) as e:
            raise KvError(f"KV {what}: {type(e).__name__}") from None

    def _value_url(self, key: str) -> str:
        return f"{self.base}/values/{urllib.parse.quote(key, safe='')}"

    def get(self, key: str) -> bytes | None:
        """A key's value, None when the namespace lacks it."""
        try:
            return self._call("get", "GET", self._value_url(key))
        except KvError as e:
            if str(e).endswith("HTTP 404"):
                return None
            raise

    def keys(self) -> dict[str, dict]:
        """Every key with its metadata ({} when none)."""
        found: dict[str, dict] = {}
        cursor = ""
        while True:
            query = urllib.parse.urlencode({"limit": 1000, **({"cursor": cursor} if cursor else {})})
            data = json.loads(self._call("list", "GET", f"{self.base}/keys?{query}"))
            for entry in data.get("result", []):
                found[entry["name"]] = entry.get("metadata") or {}
            cursor = (data.get("result_info") or {}).get("cursor") or ""
            if not cursor:
                return found

    def put(self, key: str, value: bytes, metadata: dict) -> None:
        """Write a value with its metadata (a multipart form, as the API takes both)."""
        boundary = uuid.uuid4().hex
        parts = [
            (b'Content-Disposition: form-data; name="value"\r\n\r\n', value),
            (b'Content-Disposition: form-data; name="metadata"\r\n\r\n', json.dumps(metadata).encode()),
        ]
        body = b"".join(b"--" + boundary.encode() + b"\r\n" + head + data + b"\r\n" for head, data in parts)
        body += b"--" + boundary.encode() + b"--\r\n"
        self._call("put", "PUT", self._value_url(key), body, f"multipart/form-data; boundary={boundary}")

    def delete(self, key: str) -> None:
        self._call("delete", "DELETE", self._value_url(key))


@dataclass
class Published:
    written: int
    unchanged: int
    deferred: int  # feeds whose only change waited because of the write guard
    deleted: int
    writes_today: int
    guard: bool


def publish(kv: Kv, directory: Path, files: list[str], now: dt.datetime) -> Published:
    """Write the build in directory to KV: changed feeds, then state.json, then health.json (with the day's counts).

    files are the configured feeds; a .xml key KV holds that isn't one of
    them is deleted. Feeds go first and health.json last, so a failure
    midway leaves the old health.json, whose age /health then reports.
    """
    stored = kv.keys()
    previous = json.loads(kv.get(HEALTH_NAME) or b"{}")
    today = now.date().isoformat()
    counts = previous.get("kv") or {}
    writes_today = counts.get("writes", 0) if counts.get("day") == today else 0
    guard = writes_today >= GUARD_WRITES
    written = unchanged = deferred = 0
    for file in files:
        path = directory / file
        if not path.exists():
            continue
        document = path.read_bytes()
        meta = {"sha256": content_hash(document), "items": items_hash(document)}
        old = stored.get(file, {})
        if old.get("sha256") == meta["sha256"]:
            unchanged += 1
            continue
        if guard and old.get("items") == meta["items"]:
            deferred += 1
            continue
        kv.put(file, document, meta)
        written += 1
    deleted = 0
    for key in stored:
        if key.endswith(".xml") and key not in files:
            kv.delete(key)
            deleted += 1
    state = (directory / STATE_NAME).read_bytes()
    kv.put(STATE_NAME, state, {"sha256": content_hash(state)})
    writes_today += written + 2  # with state.json and health.json
    health = json.loads((directory / HEALTH_NAME).read_text(encoding="utf-8"))
    health["kv"] = {"day": today, "writes": writes_today, "limit": DAILY_WRITES, "guard": guard}
    kv.put(HEALTH_NAME, (json.dumps(health, indent=2) + "\n").encode(), {})
    return Published(written + 2, unchanged, deferred, deleted, writes_today, guard)

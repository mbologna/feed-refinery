"""Publishing to Cloudflare Workers KV: the feeds, state.json and health.json, writing only what changed.

The free plan allows 1,000 writes a day (reset at 00:00 UTC) for the whole
account, shared with every other Worker that writes to KV. A build writes a
feed only when its document changed, ignoring <lastBuildDate>, which changes
every run; each key's metadata holds that hash, so one list request tells
what changed. state.json and health.json are written on every run.

Each run spends within a budget: what the account has left today (from the
GraphQL Analytics API, with CLOUDFLARE_ANALYTICS_TOKEN; else from the
build's own count) less a reserve for the other writers until midnight, at
the rate they wrote so far, for state.json and health.json in every later
run, and a margin. Within it a run writes every changed feed while that's
no more than its share of the day's rest (mode "all"); else feeds with a new
item first, then count-only refreshes within the share ("items"); with no
budget left, only state.json and health.json ("frozen"); and with not even
that, nothing ("none"). A feed not written keeps serving its last version.
health.json says which, and the feeds Worker reports a mode other than "all"
on /health.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
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
DAILY_WRITES = 1000  # the free plan's, for the whole account
RUNS_PER_HOUR = 2  # the build's dispatches, at :07 and :37
MARGIN = 50  # writes kept spare: a push's build, a manual run, the analytics' lag
LAST_BUILD = re.compile(rb"<lastBuildDate>[^<]*</lastBuildDate>")
GUID = re.compile(rb"<guid[^>]*>([^<]*)</guid>")
TIMEOUT = 30
ACCOUNT_WRITES = """query($account: String!, $day: Date!) { viewer { accounts(filter: {accountTag: $account}) {
  kvOperationsAdaptiveGroups(limit: 1, filter: {date: $day, actionType: "write"}) { sum { requests } } } } }"""


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
    analytics_token: str | None = None  # Account Analytics Read, for the account's writes today

    @classmethod
    def from_env(cls) -> Kv:
        names = ("CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_KV_NAMESPACE", "CLOUDFLARE_KV_TOKEN")
        missing = [n for n in names if not os.environ.get(n)]
        if missing:
            raise KvError(f"set {', '.join(missing)}")
        account, namespace, token = (os.environ[n] for n in names)
        return cls(account, namespace, token, os.environ.get("CLOUDFLARE_ANALYTICS_TOKEN") or None)

    @property
    def base(self) -> str:
        return f"{API}/accounts/{self.account}/storage/kv/namespaces/{self.namespace}"

    def _call(
        self,
        what: str,
        method: str,
        url: str,
        body: bytes | None = None,
        content_type: str = "",
        token: str | None = None,
    ) -> bytes:
        headers = {"Authorization": f"Bearer {token or self.token}", "User-Agent": USER_AGENT}
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

    def account_writes(self, day: str) -> int | None:
        """The account's KV writes on day (UTC, a few minutes behind); None without an analytics token."""
        if not self.analytics_token:
            return None
        query = {"query": ACCOUNT_WRITES, "variables": {"account": self.account, "day": day}}
        raw = self._call(
            "analytics", "POST", f"{API}/graphql", json.dumps(query).encode(), "application/json", self.analytics_token
        )
        try:
            data = json.loads(raw)
            if data.get("errors"):
                raise KvError("KV analytics: the API answered with errors")
            groups = data["data"]["viewer"]["accounts"][0]["kvOperationsAdaptiveGroups"]
            return int(groups[0]["sum"]["requests"]) if groups else 0  # no row before the day's first write
        except ValueError, LookupError, TypeError, AttributeError:
            raise KvError("KV analytics: unexpected answer") from None


@dataclass(frozen=True)
class Budget:
    """What one run may write."""

    spare: int  # writes left for this run and the later ones today, past the other writers' reserve and the margin
    feeds: int  # of which feeds, once every run today has state.json and health.json
    share: int  # this run's share of those, for when not every changed feed fits
    others_per_hour: float  # the other writers' rate today, kept for the next run


def plan_writes(account: int, own: int, others_per_hour: float, now: dt.datetime) -> Budget:
    """This run's budget, from the account's writes today, the build's own, and the other writers' last known rate.

    The rate is measured anew once an hour of the day has gone (before that,
    the previous run's carries over midnight), so it follows the other
    writers as they change.
    """
    midnight = dt.datetime.combine(now.date() + dt.timedelta(days=1), dt.time(), dt.UTC)
    hours_left = (midnight - now).total_seconds() / 3600
    hours_gone = 24 - hours_left
    if hours_gone >= 1:
        others_per_hour = max(0, account - own) / hours_gone
    runs = math.floor(hours_left * RUNS_PER_HOUR) + 1  # this one and the later ones
    spare = DAILY_WRITES - account - math.ceil(others_per_hour * hours_left) - MARGIN
    feeds = max(0, spare - 2 * runs)
    return Budget(spare, feeds, feeds // runs, round(others_per_hour, 1))


@dataclass
class Published:
    written: int
    unchanged: int
    deferred: int  # changed feeds left for a later run, for the budget
    deleted: int
    writes_today: int  # the build's own
    account: int  # the account's, this run's included; the build's own without analytics
    mode: str  # "all", "items", "frozen" or "none"
    scope: str  # "account" when the count is the account's, "build" when it's only the build's
    analytics_error: str | None = None  # why the account's count is unknown, when it should be
    quota: bool = False  # a write hit the daily limit, and publishing stopped there

    @property
    def guard(self) -> bool:
        return self.mode != "all"


def publish(kv: Kv, directory: Path, files: list[str], now: dt.datetime) -> Published:
    """Write the build in directory to KV, within the day's budget: changed feeds, then state.json, then health.json.

    files are the configured feeds; a .xml key KV holds that isn't one of
    them is deleted (deletes have their own daily limit). Feeds go first and
    health.json last, so a failure midway leaves the old health.json, whose
    age /health then reports. A write refused for the daily limit (HTTP 429)
    stops publishing: what's written stays, and every feed keeps serving.
    """
    stored = kv.keys()
    previous = json.loads(kv.get(HEALTH_NAME) or b"{}")
    today = now.date().isoformat()
    counts = previous.get("kv") or {}
    same_day = counts.get("day") == today
    writes_today = counts.get("writes", 0) if same_day else 0
    others_per_hour = float(counts.get("others_per_hour", 0))
    analytics_error = None
    try:
        measured = kv.account_writes(today)
    except KvError as e:
        measured, analytics_error = None, str(e)
    if measured is not None:
        account, scope = max(measured, writes_today), "account"
    else:
        # Without the account's count: the build's own, and the other writers at their last known rate.
        hours_gone = (now - dt.datetime.combine(now.date(), dt.time(), dt.UTC)).total_seconds() / 3600
        account = writes_today + math.ceil(others_per_hour * hours_gone)
        scope = "build" if analytics_error is None else "estimate"
    budget = plan_writes(account, writes_today, others_per_hour, now)

    new_items, counts_only, unchanged = [], [], 0
    for file in files:
        path = directory / file
        if not path.exists():
            continue
        document = path.read_bytes()
        meta = {"sha256": content_hash(document), "items": items_hash(document)}
        old = stored.get(file, {})
        if old.get("sha256") == meta["sha256"]:
            unchanged += 1
        elif old.get("items") == meta["items"]:
            counts_only.append((file, document, meta))
        else:
            new_items.append((file, document, meta))
    changed = len(new_items) + len(counts_only)
    if budget.spare < 2:
        mode, chosen = "none", []
    elif changed <= budget.share:
        mode, chosen = "all", new_items + counts_only
    elif budget.feeds == 0:
        mode, chosen = "frozen", []
    else:
        mode = "items"
        chosen = new_items[: budget.feeds]
        chosen += counts_only[: max(0, budget.share - len(chosen))]

    def done(written: int, deleted: int, quota: bool = False) -> Published:
        return Published(
            written, unchanged, changed - len(chosen), deleted, writes_today + written, account + written,
            mode, scope, analytics_error, quota,
        )  # fmt: skip

    if mode == "none":
        return done(0, 0)
    written = deleted = 0
    try:
        for file, document, meta in chosen:
            kv.put(file, document, meta)
            written += 1
        for key in stored:
            if key.endswith(".xml") and key not in files:
                kv.delete(key)
                deleted += 1
        kv.put(STATE_NAME, (state := (directory / STATE_NAME).read_bytes()), {"sha256": content_hash(state)})
        written += 1
        health = json.loads((directory / HEALTH_NAME).read_text(encoding="utf-8"))
        health["kv"] = {
            "day": today,
            "writes": writes_today + written + 1,  # with health.json itself
            "account": account + written + 1,
            "scope": scope,
            "others_per_hour": budget.others_per_hour,
            "spare": budget.spare,
            "mode": mode,
            "limit": DAILY_WRITES,
            "guard": mode != "all",
        }
        kv.put(HEALTH_NAME, (json.dumps(health, indent=2) + "\n").encode(), {})
        written += 1
    except KvError as e:
        if not str(e).endswith("HTTP 429"):
            raise
        return done(written, deleted, quota=True)
    return done(written, deleted)

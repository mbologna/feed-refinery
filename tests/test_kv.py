import datetime as dt
import json
import urllib.error

import pytest
from conftest import FakeResponse

from feed_refinery import kv
from feed_refinery.kv import Kv, KvError, content_hash, items_hash, publish

ACCOUNT, NAMESPACE = "acct", "ns1"
BASE = f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT}/storage/kv/namespaces/{NAMESPACE}"
NOW = dt.datetime(2026, 11, 24, 18, 0, tzinfo=dt.UTC)


def doc(*guids: str, built: str = "Tue, 24 Nov 2026 18:00:00 +0000") -> bytes:
    items = "".join(f"<item><guid>{g}</guid><title>{g}</title></item>" for g in guids)
    return f"<rss><channel><lastBuildDate>{built}</lastBuildDate>{items}</channel></rss>".encode()


def store() -> Kv:
    return Kv(ACCOUNT, NAMESPACE, "kv-token")


def test_hashes_ignore_the_build_time_and_follow_the_items():
    assert content_hash(doc("a", built="Mon")) == content_hash(doc("a", built="Tue"))
    assert content_hash(doc("a")) != content_hash(doc("a", "b"))
    assert items_hash(doc("a", built="Mon")) == items_hash(doc("a"))
    assert items_hash(doc("a")) != items_hash(doc("b"))


def test_credentials_come_from_the_environment(monkeypatch):
    for name in ("CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_KV_NAMESPACE", "CLOUDFLARE_KV_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(KvError, match="set CLOUDFLARE_ACCOUNT_ID, CLOUDFLARE_KV_NAMESPACE, CLOUDFLARE_KV_TOKEN"):
        Kv.from_env()
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", ACCOUNT)
    monkeypatch.setenv("CLOUDFLARE_KV_NAMESPACE", NAMESPACE)
    monkeypatch.setenv("CLOUDFLARE_KV_TOKEN", "t")
    assert Kv.from_env() == Kv(ACCOUNT, NAMESPACE, "t")


def test_get_and_missing_keys(web):
    web.html(f"{BASE}/values/state.json", "{}", "application/octet-stream")
    assert store().get("state.json") == b"{}"
    assert web.sent[-1].get_header("Authorization") == "Bearer kv-token"
    web.status(f"{BASE}/values/gone.xml", 404)
    assert store().get("gone.xml") is None


def test_errors_name_the_operation_and_status_only(web):
    web.status(f"{BASE}/values/state.json", 500)
    with pytest.raises(KvError, match=r"^KV get: HTTP 500$"):
        store().get("state.json")
    web.error(f"{BASE}/keys", urllib.error.URLError("dns"))
    with pytest.raises(KvError, match=r"^KV list: URLError$"):
        store().keys()


def test_keys_follow_the_cursor(web):
    first = {"result": [{"name": "a.xml", "metadata": {"sha256": "1"}}], "result_info": {"cursor": "c1"}}
    second = {"result": [{"name": "health.json"}], "result_info": {"cursor": ""}}
    web.routes[f"{BASE}/keys?limit=1000"] = lambda: FakeResponse(json.dumps(first).encode(), "application/json")
    web.routes[f"{BASE}/keys?limit=1000&cursor=c1"] = lambda: FakeResponse(json.dumps(second).encode())
    assert store().keys() == {"a.xml": {"sha256": "1"}, "health.json": {}}


def test_put_sends_the_value_and_metadata_as_a_form(web):
    web.html(f"{BASE}/values/top.xml", '{"success": true}', "application/json")
    store().put("top.xml", b"<rss/>", {"sha256": "abc"})
    sent = web.sent[-1]
    assert sent.get_method() == "PUT"
    boundary = sent.get_header("Content-type").split("boundary=")[1]
    assert sent.data.startswith(f"--{boundary}\r\n".encode())
    assert b'name="value"\r\n\r\n<rss/>\r\n' in sent.data
    assert b'name="metadata"\r\n\r\n{"sha256": "abc"}\r\n' in sent.data


class FakeKv:
    """A namespace in memory, with the calls made."""

    def __init__(self, values: dict[str, tuple[bytes, dict]]):
        self.values = dict(values)
        self.calls: list[tuple[str, str]] = []

    def keys(self):
        return {k: m for k, (_, m) in self.values.items()}

    def get(self, key):
        return self.values.get(key, (None, {}))[0]

    def put(self, key, value, metadata):
        self.calls.append(("put", key))
        self.values[key] = (value, metadata)

    def delete(self, key):
        self.calls.append(("delete", key))
        del self.values[key]


def build_dir(tmp_path, feeds: dict[str, bytes]):
    for name, body in feeds.items():
        (tmp_path / name).write_bytes(body)
    (tmp_path / "state.json").write_text('{"version": 3}')
    (tmp_path / "health.json").write_text('{"built": 1}')
    return tmp_path


def meta(document: bytes) -> dict:
    return {"sha256": content_hash(document), "items": items_hash(document)}


def test_publish_writes_only_what_changed_and_deletes_removed_feeds(tmp_path):
    same, old, new = doc("a"), doc("b"), doc("b", "c")
    namespace = FakeKv(
        {"same.xml": (doc("a", built="Mon"), meta(same)), "old.xml": (old, meta(old)), "gone.xml": (old, {})}
    )
    directory = build_dir(tmp_path, {"same.xml": same, "old.xml": new, "fresh.xml": doc("z")})
    done = publish(namespace, directory, ["same.xml", "old.xml", "fresh.xml", "unbuilt.xml"], NOW)
    assert (done.written, done.unchanged, done.deleted, done.deferred, done.guard) == (4, 1, 1, 0, False)
    assert namespace.calls == [
        ("put", "old.xml"),
        ("put", "fresh.xml"),
        ("delete", "gone.xml"),
        ("put", "state.json"),
        ("put", "health.json"),  # last: a failure before it leaves the old one, whose age /health reports
    ]
    assert namespace.values["old.xml"] == (new, meta(new))
    health = json.loads(namespace.values["health.json"][0])
    assert health["built"] == 1
    assert health["kv"] == {"day": "2026-11-24", "writes": 4, "limit": 1000, "guard": False}


def test_the_days_writes_add_up_and_reset_at_midnight(tmp_path):
    previous = {"kv": {"day": "2026-11-24", "writes": 100}}
    namespace = FakeKv({"health.json": (json.dumps(previous).encode(), {})})
    directory = build_dir(tmp_path, {"a.xml": doc("a")})
    assert publish(namespace, directory, ["a.xml"], NOW).writes_today == 103
    yesterday = {"kv": {"day": "2026-11-23", "writes": 950}}
    namespace = FakeKv({"health.json": (json.dumps(yesterday).encode(), {})})
    assert publish(namespace, directory, ["a.xml"], NOW).writes_today == 3


def test_past_the_guard_only_feeds_with_new_items_are_written(tmp_path):
    counts_only, more = doc("a"), doc("a", "b")
    previous = {"kv": {"day": "2026-11-24", "writes": kv.GUARD_WRITES}}
    stored_counts = doc("a").replace(b"<title>a", b"<title>a (10 points)")
    namespace = FakeKv(
        {
            "health.json": (json.dumps(previous).encode(), {}),
            "counts.xml": (stored_counts, meta(stored_counts)),
            "more.xml": (doc("a"), meta(doc("a"))),
        }
    )
    directory = build_dir(tmp_path, {"counts.xml": counts_only, "more.xml": more})
    done = publish(namespace, directory, ["counts.xml", "more.xml"], NOW)
    assert (done.written, done.deferred, done.guard) == (3, 1, True)
    assert ("put", "counts.xml") not in namespace.calls and ("put", "more.xml") in namespace.calls
    assert json.loads(namespace.values["health.json"][0])["kv"]["guard"] is True

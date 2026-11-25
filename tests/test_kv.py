import datetime as dt
import json
import urllib.error

import pytest
from conftest import FakeResponse

from feed_refinery.kv import Budget, Kv, KvError, content_hash, items_hash, plan_writes, publish

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
    monkeypatch.delenv("CLOUDFLARE_ANALYTICS_TOKEN", raising=False)
    assert Kv.from_env() == Kv(ACCOUNT, NAMESPACE, "t")
    monkeypatch.setenv("CLOUDFLARE_ANALYTICS_TOKEN", "a")
    assert Kv.from_env() == Kv(ACCOUNT, NAMESPACE, "t", "a")


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


GRAPHQL = "https://api.cloudflare.com/client/v4/graphql"


def analytics(requests: int | None) -> dict:
    groups = [] if requests is None else [{"sum": {"requests": requests}}]
    return {"data": {"viewer": {"accounts": [{"kvOperationsAdaptiveGroups": groups}]}}, "errors": None}


def test_the_accounts_writes_come_from_the_analytics_api(web):
    web.json(GRAPHQL, analytics(512))
    namespace = Kv(ACCOUNT, NAMESPACE, "kv-token", "analytics-token")
    assert namespace.account_writes("2026-11-24") == 512
    sent = web.sent[-1]
    assert sent.get_method() == "POST" and sent.get_header("Authorization") == "Bearer analytics-token"
    body = json.loads(sent.data)
    assert body["variables"] == {"account": ACCOUNT, "day": "2026-11-24"}
    assert 'actionType: "write"' in body["query"]
    web.json(GRAPHQL, analytics(None))
    assert namespace.account_writes("2026-11-24") == 0  # no row before the day's first write
    assert store().account_writes("2026-11-24") is None  # no analytics token


@pytest.mark.parametrize(
    ("answer", "message"),
    [
        (None, r"^KV analytics: HTTP 403$"),
        ({"data": None, "errors": [{"message": "not authorized for analytics-token"}]}, "answered with errors$"),
        ({"data": {"viewer": {"accounts": []}}}, "unexpected answer$"),
    ],
)
def test_analytics_errors_never_show_the_token(web, answer, message):
    if answer is None:
        web.status(GRAPHQL, 403)
    else:
        web.json(GRAPHQL, answer)
    with pytest.raises(KvError, match=message) as caught:
        Kv(ACCOUNT, NAMESPACE, "kv-token", "analytics-token").account_writes("2026-11-24")
    assert "analytics-token" not in str(caught.value)


def at(hour: int, minute: int = 0) -> dt.datetime:
    return dt.datetime(2026, 11, 24, hour, minute, tzinfo=dt.UTC)


def test_the_budget_reserves_the_other_writers_and_every_later_run():
    # 06:00: the others wrote 50 in 6 hours, so 150 more until midnight; 37 runs need 74 for their state and health.
    assert plan_writes(100, 50, 0, at(6)) == Budget(spare=700, feeds=626, share=16, others_per_hour=8.3)


def test_in_the_days_first_hour_the_other_writers_rate_carries_over():
    # 00:30: 12 an hour from the day before, 282 until midnight; 48 runs.
    assert plan_writes(10, 4, 12.0, at(0, 30)) == Budget(spare=658, feeds=562, share=11, others_per_hour=12.0)
    assert plan_writes(10, 4, 12.0, at(1)).others_per_hour == 6.0  # then today's own


class FakeKv:
    """A namespace in memory, with the calls made, and the account's writes today."""

    def __init__(self, values: dict[str, tuple[bytes, dict]], account: int | None = None, quota_after: int = 0):
        self.values = dict(values)
        self.calls: list[tuple[str, str]] = []
        self.account = account
        self.quota_after = quota_after  # puts before the daily limit answers 429; 0 = never

    def account_writes(self, day):
        if isinstance(self.account, Exception):
            raise self.account
        return self.account

    def keys(self):
        return {k: m for k, (_, m) in self.values.items()}

    def get(self, key):
        return self.values.get(key, (None, {}))[0]

    def put(self, key, value, metadata):
        if self.quota_after and sum(c[0] == "put" for c in self.calls) >= self.quota_after:
            raise KvError("KV put: HTTP 429")
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
    assert (done.written, done.unchanged, done.deleted, done.deferred, done.mode) == (4, 1, 1, 0, "all")
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
    assert health["kv"] == {
        "day": "2026-11-24",
        "writes": 4,
        "account": 4,
        "scope": "build",  # no analytics: the build's own count
        "others_per_hour": 0.0,
        "spare": 950,
        "mode": "all",
        "limit": 1000,
        "guard": False,
    }


def test_the_days_writes_add_up_and_reset_at_midnight(tmp_path):
    previous = {"kv": {"day": "2026-11-24", "writes": 100}}
    namespace = FakeKv({"health.json": (json.dumps(previous).encode(), {})})
    directory = build_dir(tmp_path, {"a.xml": doc("a")})
    assert publish(namespace, directory, ["a.xml"], NOW).writes_today == 103
    yesterday = {"kv": {"day": "2026-11-23", "writes": 950}}
    namespace = FakeKv({"health.json": (json.dumps(yesterday).encode(), {})})
    assert publish(namespace, directory, ["a.xml"], NOW).writes_today == 3


def test_the_budget_counts_the_accounts_writes(tmp_path):
    previous = {"kv": {"day": "2026-11-24", "writes": 100}}
    namespace = FakeKv({"health.json": (json.dumps(previous).encode(), {})}, account=400)
    done = publish(namespace, build_dir(tmp_path, {"a.xml": doc("a")}), ["a.xml"], NOW)
    assert (done.scope, done.account, done.writes_today) == ("account", 403, 103)
    kv_health = json.loads(namespace.values["health.json"][0])["kv"]
    assert (kv_health["account"], kv_health["others_per_hour"]) == (403, 16.7)  # 300 in 18 hours
    # The analytics lag: never below the build's own count.
    namespace = FakeKv({"health.json": (json.dumps(previous).encode(), {})}, account=90)
    assert publish(namespace, build_dir(tmp_path, {"a.xml": doc("a")}), ["a.xml"], NOW).account == 103


def test_without_the_accounts_count_the_others_last_rate_stands_in(tmp_path):
    previous = {"kv": {"day": "2026-11-24", "writes": 100, "others_per_hour": 10.0}}
    namespace = FakeKv({"health.json": (json.dumps(previous).encode(), {})}, account=KvError("KV analytics: HTTP 500"))
    done = publish(namespace, build_dir(tmp_path, {"a.xml": doc("a")}), ["a.xml"], NOW)
    assert (done.scope, done.account, done.analytics_error) == ("estimate", 283, "KV analytics: HTTP 500")  # 100 + 180


def three_kinds(tmp_path, account, quota_after=0):
    """Two feeds with a new item and two with only new counts, at 18:00 with the account at account writes."""
    counts = doc("a").replace(b"<title>a", b"<title>a (10 points)")
    previous = {"kv": {"day": "2026-11-24", "writes": 500}}
    namespace = FakeKv(
        {
            "health.json": (json.dumps(previous).encode(), {}),
            "c1.xml": (counts, meta(counts)),
            "c2.xml": (counts, meta(counts)),
        },
        account=account,
        quota_after=quota_after,
    )
    files = ["c1.xml", "n1.xml", "c2.xml", "n2.xml"]
    directory = build_dir(tmp_path, {"c1.xml": doc("a"), "c2.xml": doc("a"), "n1.xml": doc("b"), "n2.xml": doc("c")})
    return namespace, publish(namespace, directory, files, NOW)


def test_when_not_every_change_fits_new_items_go_first(tmp_path):
    # 780 written, 280 of them by others in 18 hours: 94 more by midnight, 76 spare, 50 for feeds, 3 for this run.
    namespace, done = three_kinds(tmp_path, 780)
    assert (done.mode, done.written, done.deferred, done.guard) == ("items", 5, 1, True)
    puts = [k for op, k in namespace.calls if op == "put"]
    assert puts == ["n1.xml", "n2.xml", "c1.xml", "state.json", "health.json"]
    assert json.loads(namespace.values["health.json"][0])["kv"]["guard"] is True


def test_with_no_room_for_feeds_only_the_state_is_written(tmp_path):
    namespace, done = three_kinds(tmp_path, 820)  # 23 spare: less than the day's runs need for their state
    assert (done.mode, done.written, done.deferred) == ("frozen", 2, 4)
    assert [k for op, k in namespace.calls if op == "put"] == ["state.json", "health.json"]


def test_with_no_room_at_all_nothing_is_written(tmp_path):
    namespace, done = three_kinds(tmp_path, 850)
    assert (done.mode, done.written, done.deferred) == ("none", 0, 4)
    assert namespace.calls == []


def test_the_daily_limit_stops_publishing_and_keeps_what_was_written(tmp_path):
    namespace, done = three_kinds(tmp_path, 0, quota_after=2)
    assert (done.quota, done.written) == (True, 2)
    assert [k for op, k in namespace.calls if op == "put"] == ["n1.xml", "n2.xml"]  # new items first, always
    assert b'"writes": 500' in namespace.values["health.json"][0]  # the old health.json


def test_other_write_errors_still_fail_the_publish(tmp_path):
    class Failing(FakeKv):
        def put(self, key, value, metadata):
            raise KvError("KV put: HTTP 500")

    with pytest.raises(KvError, match="HTTP 500$"):
        publish(Failing({}), build_dir(tmp_path, {"a.xml": doc("a")}), ["a.xml"], NOW)

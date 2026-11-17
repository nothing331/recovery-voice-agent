import json

from app import calls, db
from app.latency import classify_service, compute_stats, percentile
from tests.test_api import client, call  # noqa: F401


def test_percentile():
    assert percentile([], 50) == 0
    assert percentile([5], 95) == 5
    v = [100, 200, 300, 400]
    assert percentile(v, 50) == 200 and percentile(v, 95) == 400


def test_compute_stats_empty_and_values():
    e = compute_stats([], {})
    assert e["response_ms"] == {"avg": 0, "p50": 0, "p95": 0, "max": 0, "count": 0}
    assert e["services"]["stt"] == {"avg_ttfb_ms": 0, "count": 0}
    s = compute_stats([800, 1200], {"llm": [300, 500]})
    assert s["turns_ms"] == [800, 1200] and s["response_ms"]["avg"] == 1000 and s["response_ms"]["max"] == 1200
    assert s["services"]["llm"] == {"avg_ttfb_ms": 400, "count": 2}


def test_classify():
    assert classify_service("DeepgramSTTService#0") == "stt"
    assert classify_service("DeepgramTTSService#0") == "tts"
    assert classify_service("OpenRouterLLMService#0") == "llm"


def test_migration_adds_column():
    conn = db.connect(":memory:")
    conn.execute("CREATE TABLE calls(id INTEGER PRIMARY KEY, customer_id TEXT)")
    db.init_schema(conn)
    assert "latency_json" in {r[1] for r in conn.execute("PRAGMA table_info(calls)")}


def test_api_latency_fields(client):  # noqa: F811
    cid = call(client).json()["call_id"]
    calls.finalize_call(client.conn, cid, [], compute_stats([800, 1200], {"tts": [100]}))
    row = next(c for c in client.get("/api/calls").json() if c["id"] == cid)
    assert row["avg_latency_ms"] == 1000 and "latency_json" not in row
    d = client.get(f"/api/calls/{cid}").json()
    assert d["latency"]["response_ms"]["count"] == 2 and "latency_json" not in d


def test_api_latency_null(client):  # noqa: F811
    cid = call(client).json()["call_id"]
    assert client.get("/api/calls").json()[0]["avg_latency_ms"] is None
    assert client.get(f"/api/calls/{cid}").json()["latency"] is None

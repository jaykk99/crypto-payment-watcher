"""Audit tests, 2026-11-25: bugs found by the hourly repo audit.

Every test here fails on the pre-fix code (verified via git stash)
and passes on the fix. Offline only: fakes and a loopback stub server.
"""
import os
import sys
import threading
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from paywatch import MemoryStore, SQLiteStore, Watcher, load_plans  # noqa: E402
from paywatch.core import Order  # noqa: E402
from paywatch.providers import (  # noqa: E402
    FailoverChainFetcher, FetchResult, PriceQuote, ProviderError, _get_json,
)
from paywatch.watcher import WatcherError  # noqa: E402

PAYER = "bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq"
RECEIVE = "receive-test-address"
PLANS = [{"id": "starter", "name": "Starter", "price_usd": 19}]


class FakePrice:
    def rate_info(self, asset):
        return PriceQuote(50000.0, "fake", False, 0)


class MutableChain:
    """A chain fetcher whose tx list the test can change between calls."""

    name = "mutable"

    def __init__(self):
        self.txs = []
        self.calls = 0

    def fetch_address(self, address):
        self.calls += 1
        return FetchResult(list(self.txs), 100, self.name)

    def tip_height(self):
        return 100

    def health(self):
        return self.name


def qualifying_tx(txid="tx1", value=38000):
    return {"txid": txid,
            "vout": [{"value": value, "scriptpubkey_address": RECEIVE}],
            "vin": [{"prevout": {"scriptpubkey_address": PAYER}}],
            "status": {"confirmed": True, "block_height": 99}}


def make_watcher(chain, store=None, rate=None):
    return Watcher(
        plans=PLANS, store=store or MemoryStore(),
        chain_fetchers={"btc": chain},
        rate_fetcher=rate or FakePrice(),
        address_pools={"btc": [RECEIVE]})


# ------------------------------------------------- 1. failover cache
# A long-running host reuses one Watcher (and one FailoverChainFetcher)
# across sweeps. Before the fix the fetcher cached the first fetch per
# address forever, so a payment arriving after the first check was
# never seen and the invoice stayed pending forever.

def test_failover_fetcher_refetches_every_call():
    chain = MutableChain()
    failover = FailoverChainFetcher("btc", [chain])
    assert failover.fetch_address(RECEIVE).txs == []
    chain.txs = [qualifying_tx()]
    result = failover.fetch_address(RECEIVE)
    assert len(result.txs) == 1
    assert chain.calls == 2


def test_watcher_unlocks_payment_arriving_after_first_check():
    # End to end through the production fetcher stack: the watcher
    # builds a FailoverChainFetcher, which is the layer that cached.
    chain = MutableChain()
    watcher = Watcher(
        plans=PLANS, store=MemoryStore(),
        chain_fetchers={"btc": FailoverChainFetcher("btc", [chain])},
        rate_fetcher=FakePrice(),
        address_pools={"btc": [RECEIVE]})
    order = watcher.create_order("starter", PAYER, asset="btc")
    assert watcher.check_order(order.id)["action"] == "pending"
    chain.txs = [qualifying_tx()]
    assert watcher.check_order(order.id)["action"] == "unlocked"
    assert watcher.get_order(order.id).status == "paid"


def test_failover_tip_height_probes_providers():
    chain = MutableChain()
    failover = FailoverChainFetcher("btc", [chain])
    assert failover.tip_height() == 100

    class Down:
        name = "down"

        def tip_height(self):
            raise ProviderError("down", "unreachable", "boom")

    assert FailoverChainFetcher("btc", [Down()]).tip_height() is None


# ------------------------------------------------- 2. paid_late txid
# Before the fix a late payment was recorded as paid_late WITHOUT its
# txid, so the same transaction could then unlock a second order.

def test_paid_late_claims_txid_and_second_order_cannot_unlock():
    chain = MutableChain()
    watcher = make_watcher(chain)
    t0 = datetime.now(timezone.utc)
    first = watcher.create_order("starter", PAYER, asset="btc", now=t0)
    chain.txs = [qualifying_tx("latetx")]
    res = watcher.check_order(first.id, now=t0 + timedelta(minutes=61))
    assert res["action"] == "paid_late"
    assert watcher.get_order(first.id).txid == "latetx"
    second = watcher.create_order("starter", PAYER, asset="btc",
                                  now=t0 + timedelta(minutes=62))
    res = watcher.check_order(second.id, now=t0 + timedelta(minutes=62))
    assert res["action"] == "txid_reused"
    assert watcher.get_order(second.id).status != "paid"


def test_paid_late_clash_with_paid_order_is_txid_reused(tmp_path):
    # Same scenario on SQLiteStore: before the fix the paid_late save
    # hit the UNIQUE txid index and raised IntegrityError instead.
    chain = MutableChain()
    store = SQLiteStore(str(tmp_path / "paywatch.db"))
    watcher = make_watcher(chain, store=store)
    t0 = datetime.now(timezone.utc)
    chain.txs = [qualifying_tx("sharedtx")]
    first = watcher.create_order("starter", PAYER, asset="btc", now=t0)
    assert watcher.check_order(first.id, now=t0)["action"] == "unlocked"
    second = watcher.create_order("starter", PAYER, asset="btc", now=t0)
    res = watcher.check_order(second.id, now=t0 + timedelta(minutes=61))
    assert res["action"] == "txid_reused"
    assert watcher.get_order(second.id).status != "paid"


# ------------------------------------------------- 3. order validation
# POST /orders with a missing plan_id or payer_address used to raise a
# raw KeyError and come back as a 500.

def test_api_order_body_validation_returns_400():
    from fastapi.testclient import TestClient

    from paywatch.api import create_app

    watcher = make_watcher(MutableChain())
    client = TestClient(create_app(watcher), raise_server_exceptions=False)
    for body, want in ({}, 400), ({"plan_id": "starter"}, 400), (
            {"payer_address": PAYER}, 400), ({"plan_id": 7, "payer_address": PAYER}, 400), (
            {"plan_id": "starter", "payer_address": " "}, 400):
        response = client.post("/orders", json=body)
        assert response.status_code == want, (body, response.text)
        assert response.json()["code"] == "INVALID_ORDER"
    response = client.post("/orders", json={
        "plan_id": "starter", "payer_address": PAYER})
    assert response.status_code == 200


# ------------------------------------------------- 4. plan loading
# Malformed plan entries used to raise raw KeyError / TypeError out of
# load_plans instead of the ValueError the callers expect.

def test_load_plans_malformed_entries_raise_value_error():
    for bad in ([{"id": "x"}],
                ["not-a-dict"],
                [{"id": "x", "price_usd": "abc"}],
                [{"id": "x", "price_usd": -5}],
                [{"id": "x", "price_usd": float("nan")}]):
        try:
            load_plans(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"load_plans({bad!r}) did not raise ValueError")


# ------------------------------------------------- 5. store threads
# Both hosts serve from threads. Before the fix the SQLiteStore's
# shared connection raised InterfaceError / OperationalError under
# concurrent use.

def _hammer_store(store):
    errors = []

    def worker(n):
        try:
            for i in range(30):
                order = Order(
                    id=f"w{n}-{i}", plan_id="starter", price_usd=19,
                    payer_address="p", receive_address=RECEIVE,
                    expected_sats=1000,
                    created_at=datetime.now(timezone.utc))
                store.save(order)
                assert store.get(f"w{n}-{i}") is not None
                store.active_counts("btc")
                store.get_counter("pool_rotation:btc")
                store.set_counter("pool_rotation:btc", i)
        except Exception as exc:  # noqa: BLE001 - collected, asserted below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors[:3]


def test_sqlite_store_concurrent_use(tmp_path):
    _hammer_store(SQLiteStore(str(tmp_path / "paywatch.db")))


def test_memory_store_concurrent_use():
    _hammer_store(MemoryStore())


# ------------------------------------------------- 6. response cap
# _get_json used to read a provider body unboundedly; a 30 MB junk
# body was buffered whole.

def test_provider_response_size_cap():
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    big = b'{"data": "' + b"x" * (11 * 1024 * 1024) + b'"}'

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = big if self.path == "/big" else b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        assert _get_json("stub", base + "/small") == {"ok": True}
        try:
            _get_json("stub", base + "/big")
        except ProviderError as exc:
            assert exc.kind == "bad_response"
            assert "size limit" in str(exc)
        else:
            raise AssertionError("oversized body was not rejected")
    finally:
        server.shutdown()
        server.server_close()


# ------------------------------------------------- 7. sweep counts
# A txid_reused outcome used to fall through the sweep counters, so
# the sweep reported 2 checked / 1 unlocked and silently dropped the
# second order's outcome.

def test_sweep_counts_txid_reused():
    chain = MutableChain()
    watcher = make_watcher(chain)
    first = watcher.create_order("starter", PAYER, asset="btc")
    second = watcher.create_order("starter", PAYER, asset="btc")
    chain.txs = [qualifying_tx("sharedtx")]
    result = watcher.sweep()
    assert result["checked"] == 2
    assert result["unlocked"] == 1
    assert result["txid_reused"] == 1
    assert watcher.get_order(first.id).status == "paid"
    assert watcher.get_order(second.id).status == "underpaid"


# ------------------------------------------------- 8. checkout page
# The buyer page used to collapse paid_late into the paid view, so a
# late payer saw "Payment confirmed. You're all set." while nothing
# was unlocked.

def test_checkout_page_paid_late_is_not_paid():
    from pathlib import Path

    html = Path(__file__).parent.parent.joinpath(
        "src", "paywatch", "static", "checkout.html").read_text()
    assert 'id="late"' in html
    assert 'o.status === "paid_late" ? "paid_late"' in html
    assert "Payment received late" in html
    assert 'var status = isPaid(o.status) ? "paid" : o.status;' not in html


# ------------------------------------------------- 9. demo server
# The stdlib demo dropped the connection (traceback, no response) on
# a bad order body, a garbage Content-Length, or invalid JSON.

def test_demo_server_rejects_bad_bodies(monkeypatch):
    import http.client
    import importlib.util

    root = os.path.join(os.path.dirname(__file__), "..")
    monkeypatch.setenv("PAYWATCH_PLANS_JSON",
                       '[{"id": "starter", "name": "Starter", "price_usd": 19}]')
    monkeypatch.setenv("PAYWATCH_ADDRESSES", '{"btc": ["recv1"]}')
    spec = importlib.util.spec_from_file_location(
        "demo_server", os.path.join(root, "examples", "server.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    server = module.ThreadingHTTPServer(("127.0.0.1", 0), module.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        port = server.server_address[1]

        def post(path, body, headers=None):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            conn.request("POST", path, body=body,
                         headers=headers or {"Content-Type": "application/json"})
            resp = conn.getresponse()
            return resp.status, resp.read()

        status, _ = post("/orders", b"{}")
        assert status == 400, status
        status, _ = post("/orders", b"{not json")
        assert status == 400, status
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("POST", "/orders", body=b"{}",
                     headers={"Content-Length": "abc"})
        assert conn.getresponse().status == 400
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("POST", "/orders", body=b"",
                     headers={"Content-Length": str(2 * 1024 * 1024)})
        assert conn.getresponse().status == 413
    finally:
        server.shutdown()
        server.server_close()

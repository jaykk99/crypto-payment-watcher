"""Unit tests for crypto-payment-watcher. No network, no secrets.

Fake addresses below are plain strings for the matcher (it does not
validate), except the address-validation tests, which use the public
BIP-173 test vector bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq and
obviously-invalid strings. No real receive/payer address appears here.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from paywatch import (  # noqa: E402
    Watcher, MemoryStore, SQLiteStore, btc_to_sats, threshold_sats,
    usd_to_sats, valid_btc_address, match_payment,
)
from paywatch.watcher import WatcherError  # noqa: E402

os.environ["PAYWATCH_BTC_ADDRESS"] = "receive-test-address"

# Public BIP-173 test vector, used as the fake payer in tests only.
PAYER = "bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq"
RECEIVE = "receive-test-address"
PLANS = [{"id": "starter", "name": "Starter", "price_usd": 19},
         {"id": "pro", "name": "Pro", "price_usd": 49}]


def tx(txid, to_receive, from_payer=True, conf_height=None, tip=100):
    return {
        "txid": txid,
        "vout": [{"value": to_receive, "scriptpubkey_address": RECEIVE}],
        "vin": [{"prevout": {"scriptpubkey_address": PAYER if from_payer else "someone-else"}}],
        "status": {"confirmed": conf_height is not None,
                   "block_height": conf_height},
    }


class FakeFetcher:
    def __init__(self, txs, tip=100):
        self.txs, self.tip = txs, tip

    def address_txs(self, _addr):
        return self.txs

    def tip_height(self):
        return self.tip


def make_watcher(txs=None, tip=100):
    fetcher = FakeFetcher(txs or [], tip)
    return Watcher(plans=PLANS, store=MemoryStore(), fetcher=fetcher)


def make_order(watcher, expected_sats=100_000):
    # Inject a fixed rate via create_order's rate param path: build the
    # order directly through the watcher with a rate that pins cleanly.
    # $19 at $19,000/BTC = 0.001 BTC = 100,000 sats.
    order = watcher.create_order("starter", PAYER, rate_usd=19000.0)
    assert order.expected_sats == expected_sats, order.expected_sats
    return order


# ------------------------------------------------------------ amounts

def test_pinning_and_tolerance():
    assert usd_to_sats(19, 19000.0) == 100_000
    assert btc_to_sats("0.001") == 100_000
    assert threshold_sats(100_000) == 99_000
    assert threshold_sats(101) == 100  # 101 - 101//100 = 100


def test_address_validation():
    assert valid_btc_address("bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq")
    assert not valid_btc_address("bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdr")
    assert not valid_btc_address("")
    assert not valid_btc_address(None)
    assert not valid_btc_address("not-an-address")


# ------------------------------------------------------------ matching

def test_matcher_requires_payer_binding():
    res = match_payment([tx("a", 100_000, from_payer=False)],
                        PAYER, RECEIVE, 100_000, tip_height=100)
    assert res["match"] is None
    assert res["best_seen_sats"] == 0  # wrong-payer tx does not count


def test_matcher_wrong_recipient_never_matches():
    wrong = {"txid": "b", "vout": [{"value": 100_000,
             "scriptpubkey_address": "other-address"}],
             "vin": [{"prevout": {"scriptpubkey_address": PAYER}}],
             "status": {"confirmed": True, "block_height": 99}}
    res = match_payment([wrong], PAYER, RECEIVE, 100_000, tip_height=100)
    assert res["match"] is None


def test_matcher_change_output_does_not_count():
    change = {"txid": "c",
              "vout": [{"value": 50_000, "scriptpubkey_address": RECEIVE},
                       {"value": 900_000, "scriptpubkey_address": PAYER}],
              "vin": [{"prevout": {"scriptpubkey_address": PAYER}}],
              "status": {"confirmed": True, "block_height": 99}}
    res = match_payment([change], PAYER, RECEIVE, 100_000, tip_height=100)
    assert res["match"] is None
    assert res["best_seen_sats"] == 50_000


def test_confirmation_counting():
    res = match_payment([tx("d", 100_000, conf_height=99)],
                        PAYER, RECEIVE, 100_000, tip_height=100)
    assert res["match"]["confirmations"] == 2  # 100 - 99 + 1
    res = match_payment([tx("e", 100_000, conf_height=100)],
                        PAYER, RECEIVE, 100_000, tip_height=100)
    assert res["match"]["confirmations"] == 1
    res = match_payment([tx("f", 100_000, conf_height=None)],
                        PAYER, RECEIVE, 100_000, tip_height=100)
    assert res["match"]["confirmations"] == 0


# ------------------------------------------------------------ watcher

def test_unlock_at_two_confirmations():
    w = make_watcher()
    order = make_order(w)
    w.fetcher.txs = [tx("tx1", 100_000, conf_height=99)]
    res = w.check_order(order.id)
    assert res["action"] == "unlocked"
    assert w.get_order(order.id).status == "paid"


def test_confirming_below_two_confirmations_no_unlock():
    w = make_watcher()
    order = make_order(w)
    w.fetcher.txs = [tx("tx1", 100_000, conf_height=100)]
    res = w.check_order(order.id)
    assert res["action"] == "confirming"
    assert w.get_order(order.id).status == "confirming"


def test_underpayment_no_unlock_with_shortfall():
    w = make_watcher()
    order = make_order(w)
    w.fetcher.txs = [tx("tx1", 98_999, conf_height=99)]
    res = w.check_order(order.id)
    assert res["action"] == "underpaid"
    assert w.get_order(order.id).shortfall_sats == 99_000 - 98_999


def test_tolerance_boundary_qualifies():
    w = make_watcher()
    order = make_order(w)
    w.fetcher.txs = [tx("tx1", 99_000, conf_height=99)]
    assert w.check_order(order.id)["action"] == "unlocked"


def test_overpayment_unlocks_tier_only():
    w = make_watcher()
    order = make_order(w)
    w.fetcher.txs = [tx("tx1", 200_000, conf_height=99)]
    res = w.check_order(order.id)
    assert res["action"] == "unlocked"
    assert w.get_order(order.id).plan_id == "starter"  # not upgraded
    assert w.get_order(order.id).tx_received_sats == 200_000


def test_txid_reuse_blocked():
    w = make_watcher()
    o1 = make_order(w)
    w.fetcher.txs = [tx("shared", 100_000, conf_height=99)]
    assert w.check_order(o1.id)["action"] == "unlocked"
    o2 = make_order(w)
    res = w.check_order(o2.id)
    assert res["action"] == "txid_reused"
    assert w.get_order(o2.id).status != "paid"


def test_expiry_paid_late_no_unlock():
    w = make_watcher()
    order = make_order(w)
    late = order.created_at + timedelta(minutes=61)
    w.fetcher.txs = [tx("tx1", 100_000, conf_height=99)]
    res = w.check_order(order.id, now=late)
    assert res["action"] == "paid_late"
    assert w.get_order(order.id).status == "paid_late"
    assert "after invoice expiry" in (w.get_order(order.id).late_payment_note or "")


def test_expired_with_nothing_seen():
    w = make_watcher()
    order = make_order(w)
    late = order.created_at + timedelta(minutes=61)
    assert w.check_order(order.id, now=late)["action"] == "expired"


def test_manual_approve_fallback():
    w = make_watcher()
    order = make_order(w)
    res = w.approve_order(order.id)
    assert res["action"] == "manually_approved"
    assert w.get_order(order.id).status == "manually_approved"
    assert w.check_order(order.id)["action"] == "already_paid"


def test_checkout_closed_without_receive_address():
    old = os.environ.pop("PAYWATCH_BTC_ADDRESS", None)
    try:
        w = make_watcher()
        try:
            w.create_order("starter", PAYER, rate_usd=19000.0)
            raise AssertionError("should have raised")
        except WatcherError as exc:
            assert exc.status_code == 503
    finally:
        if old:
            os.environ["PAYWATCH_BTC_ADDRESS"] = old


def test_unknown_plan_and_bad_payer_rejected():
    w = make_watcher()
    try:
        w.create_order("nope", PAYER, rate_usd=19000.0)
        raise AssertionError("should have raised")
    except WatcherError as exc:
        assert exc.status_code == 404
    try:
        w.create_order("starter", "not-an-address", rate_usd=19000.0)
        raise AssertionError("should have raised")
    except WatcherError:
        pass


def test_sqlite_store_roundtrip_and_txid_claim(tmp_path):
    store = SQLiteStore(str(tmp_path / "watch.db"))
    w = Watcher(plans=PLANS, store=store,
                fetcher=FakeFetcher([tx("tx9", 100_000, conf_height=99)]))
    order = w.create_order("starter", PAYER, rate_usd=19000.0)
    assert w.check_order(order.id)["action"] == "unlocked"
    reopened = SQLiteStore(str(tmp_path / "watch.db"))
    assert reopened.get(order.id).status == "paid"
    assert reopened.txid_claimed_by("tx9", "other-order") == order.id


def test_on_unlock_callback_receives_order_and_tx():
    seen = []
    w = Watcher(plans=PLANS, store=MemoryStore(),
                fetcher=FakeFetcher([tx("tx1", 100_000, conf_height=99)]),
                on_unlock=lambda o, t: seen.append((o.plan_id, t["txid"])))
    order = w.create_order("starter", PAYER, rate_usd=19000.0)
    w.check_order(order.id)
    assert seen == [("starter", "tx1")]

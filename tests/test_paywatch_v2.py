"""Offline tests for pools, assets, failover, and availability.

All addresses and hashes here are clearly fake placeholders. Solana
examples are deterministic base58 encodings of sequential test bytes,
not a wallet used by any project.
"""
import os
import sqlite3
import sys
import time
from datetime import timedelta

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from paywatch import (  # noqa: E402
    AssetSpec, MemoryStore, SQLiteStore, Watcher, get_asset,
    load_address_pools, register_asset, supported_assets,
    valid_eth_address, valid_sol_address,
)
from paywatch.assets import ASSET_REGISTRY  # noqa: E402
from paywatch.providers import (  # noqa: E402
    FailoverChainFetcher, PriceFetcher, PriceQuote, ProviderError,
    SolanaRPCFetcher,
)
from paywatch.watcher import AssetUnavailableError, WatcherError  # noqa: E402

PLANS = [{"id": "starter", "name": "Starter", "price_usd": 19},
         {"id": "pro", "name": "Pro", "price_usd": 49}]
BTC_PAYER = "bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq"
EVM_PAYER = "0x1111111111111111111111111111111111111111"
EVM_OTHER = "0x3333333333333333333333333333333333333333"
EVM_RECEIVE = "0x2222222222222222222222222222222222222222"
SOL_PAYER = "4wBqpZM9xaSheZzJSMawUKKwhdpChKbZ5eu5ky4Vigw"
SOL_RECEIVE = "3ELeRTTg5W5hAYaEFznzFV1jknNFkjHqS8ytwvQEQP1Z"
SOL_OTHER = "5Pk716N113awdSaUDZEPZVi9Zs6hJmG5KCJtp5qQK3LB"
ENV_KEYS = [
    "PAYWATCH_ADDRESSES", "PAYWATCH_BTC_ADDRESS", "PAYWATCH_BTC_ADDRESSES",
    "PAYWATCH_ETH_ADDRESSES", "PAYWATCH_BNB_ADDRESSES", "PAYWATCH_SOL_ADDRESSES",
    "PAYWATCH_BTC_CONFIRMATIONS", "PAYWATCH_ETH_CONFIRMATIONS",
    "PAYWATCH_BNB_CONFIRMATIONS", "PAYWATCH_SOL_CONFIRMATIONS",
]


@pytest.fixture(autouse=True)
def clean_paywatch_env(monkeypatch):
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    yield


class FakeChain:
    def __init__(self, txs=None, tip=100, name="fake-chain", error=None):
        self.txs = list(txs or [])
        self.tip = tip
        self.name = name
        self.error = error
        self.address_calls = []

    def address_txs(self, address):
        self.address_calls.append(address)
        if self.error:
            raise self.error
        return self.txs

    def tip_height(self):
        if self.error:
            raise self.error
        return self.tip


class FakePrice:
    def __init__(self, rate=19000.0):
        self.rate = rate

    def rate_info(self, asset):
        return PriceQuote(self.rate, "fake-price", False, time.time())


def evm_tx(txid, value, payer=EVM_PAYER, receive=EVM_RECEIVE, confirmations=12):
    return {"txid": txid, "from_address": payer, "to_address": receive,
            "value_wei": value, "value_amount": value,
            "confirmations": confirmations}


# --------------------------------------------------------------- pools

def test_address_pool_config_formats_and_shared_evm_address():
    pools = load_address_pools({
        "PAYWATCH_ADDRESSES": (
            '{"btc": ["btc-a", "btc-b"], '
            '"eth": ["0x2222222222222222222222222222222222222222"]}'
        ),
        "PAYWATCH_BNB_ADDRESSES": EVM_RECEIVE,
        "PAYWATCH_BTC_ADDRESSES": "btc-c,btc-a",
    })
    assert pools["btc"] == ["btc-a", "btc-b", "btc-c"]
    # The same multi-chain EVM address in two asset pools is valid.
    assert pools["eth"] == [EVM_RECEIVE]
    assert pools["bnb"] == [EVM_RECEIVE]


def test_legacy_single_btc_address_is_one_address_pool():
    pools = load_address_pools({"PAYWATCH_BTC_ADDRESS": "legacy-btc"})
    assert pools == {"btc": ["legacy-btc"]}


def test_pool_assignment_least_active_and_rotation():
    chain = FakeChain()
    watcher = Watcher(
        plans=PLANS, store=MemoryStore(), fetcher=chain,
        rate_fetcher=FakePrice(),
        address_pools={"btc": ["pool-a", "pool-b"]})
    assigned = [
        watcher.create_order("starter", BTC_PAYER, rate_usd=19000.0)
        .receive_address for _ in range(4)
    ]
    assert assigned[0] != assigned[1]
    assert set(assigned) == {"pool-a", "pool-b"}
    assert assigned.count("pool-a") == 2
    assert assigned.count("pool-b") == 2


def test_empty_asset_pool_is_closed_not_an_error(monkeypatch):
    watcher = Watcher(plans=PLANS, store=MemoryStore(), fetcher=FakeChain(),
                      rate_fetcher=FakePrice(), address_pools={"btc": []})
    status = watcher.asset_status("btc")
    assert status["available"] is False
    assert status["code"] == "CHECKOUT_CLOSED"
    with pytest.raises(WatcherError) as caught:
        watcher.create_order("starter", BTC_PAYER, rate_usd=19000.0)
    assert caught.value.status_code == 503
    assert caught.value.code == "CHECKOUT_CLOSED"


# ----------------------------------------------------------------- ETH

def test_eth_invoice_match_confirmations_and_public_amounts():
    chain = FakeChain()
    watcher = Watcher(plans=PLANS, store=MemoryStore(), fetcher=chain,
                      rate_fetcher=FakePrice(1900.0),
                      address_pools={"eth": [EVM_RECEIVE]})
    order = watcher.create_order("starter", EVM_PAYER, asset="eth")
    assert order.asset == "eth"
    assert order.expected_amount == 10_000_000_000_000_000
    assert order.expected_wei == order.expected_amount
    assert order.required_confirmations == 12
    assert order.public()["expected_wei"] == order.expected_amount

    chain.txs = [evm_tx("0xabc", order.expected_amount, confirmations=11)]
    assert watcher.check_order(order.id)["action"] == "confirming"
    chain.txs = [evm_tx("0xabc", order.expected_amount, confirmations=12)]
    assert watcher.check_order(order.id)["action"] == "unlocked"
    assert watcher.get_order(order.id).txid == "0xabc"


def test_eth_payer_binding_is_case_insensitive_and_rejects_mismatch():
    chain = FakeChain()
    watcher = Watcher(plans=PLANS, store=MemoryStore(), fetcher=chain,
                      rate_fetcher=FakePrice(1900.0),
                      address_pools={"eth": [EVM_RECEIVE]})
    order = watcher.create_order(
        "starter", "0xAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", asset="eth")
    chain.txs = [evm_tx(
        "0xgood", order.expected_amount,
        payer="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")]
    assert watcher.check_order(order.id)["action"] == "unlocked"

    order2 = watcher.create_order(
        "starter", EVM_PAYER, asset="eth", rate_usd=1900.0)
    chain.txs = [evm_tx("0xbad", order2.expected_amount, payer=EVM_OTHER)]
    result = watcher.check_order(order2.id)
    assert result["action"] == "pending"
    assert watcher.get_order(order2.id).seen_amount == 0


def test_eth_confirmation_default_is_env_overridable(monkeypatch):
    monkeypatch.setenv("PAYWATCH_ETH_CONFIRMATIONS", "3")
    assert get_asset("eth").required_confirmations() == 3


def test_evm_address_validation_is_shared():
    assert valid_eth_address(EVM_RECEIVE)
    assert get_asset("eth").validate_address(EVM_RECEIVE)
    assert get_asset("bnb").validate_address(EVM_RECEIVE)
    assert not valid_eth_address("0x1234")


# ------------------------------------------------------- BNB and registry

def test_bnb_is_pure_evm_registry_entry_and_matches_payment():
    spec = get_asset("bnb")
    assert spec.asset_family == "evm"
    assert spec.transaction_family == "account"
    assert spec.price_id == "binancecoin"
    assert spec.default_confirmations == 15
    chain = FakeChain()
    watcher = Watcher(
        plans=PLANS, store=MemoryStore(), chain_fetchers={"bnb": chain},
        rate_fetcher=FakePrice(600.0),
        address_pools={"bnb": [EVM_RECEIVE], "eth": [EVM_RECEIVE]})
    order = watcher.create_order("starter", EVM_PAYER, asset="bnb",
                                 rate_usd=600.0)
    assert order.required_confirmations == 15
    chain.txs = [evm_tx("0xbnb", order.expected_amount, confirmations=15)]
    assert watcher.check_order(order.id)["action"] == "unlocked"


def test_new_evm_asset_registers_without_watcher_changes():
    key = "testevm"
    spec = AssetSpec(
        key=key, symbol="TEVM", display_name="Test EVM", price_id="test",
        unit_name="wei", decimals=18, default_confirmations=2,
        transaction_family="account",
        address_validator=valid_eth_address,
        provider_names=("mock-evm",), asset_family="evm")
    register_asset(spec)
    try:
        chain = FakeChain()
        watcher = Watcher(
            plans=PLANS, store=MemoryStore(), chain_fetchers={key: chain},
            rate_fetcher=FakePrice(1900.0), address_pools={key: [EVM_RECEIVE]})
        order = watcher.create_order("starter", EVM_PAYER, asset=key)
        chain.txs = [evm_tx("0xtest", order.expected_amount, confirmations=2)]
        assert watcher.check_order(order.id)["action"] == "unlocked"
    finally:
        ASSET_REGISTRY.pop(key, None)


# -------------------------------------------------------------- Solana

def sol_transport(*, finalized=True, payer=SOL_PAYER, amount=190_000_000):
    signature = "5FakeSignatureForOfflinePaywatchTests111111111111"

    def transport(_endpoint, payload):
        method = payload["method"]
        if method == "getSignaturesForAddress":
            return {"result": [{
                "signature": signature, "err": None,
                "confirmationStatus": "finalized" if finalized else "confirmed",
            }]}
        if method == "getTransaction":
            return {"result": {
                "slot": 123,
                "meta": {"err": None, "preBalances": [500_000_000, 0],
                         "postBalances": [500_000_000 - amount, amount]},
                "transaction": {"message": {
                    "accountKeys": [{"pubkey": payer}, {"pubkey": SOL_RECEIVE}],
                    "instructions": [{
                        "parsed": {"type": "transfer", "info": {
                            "source": payer, "destination": SOL_RECEIVE,
                            "lamports": amount}},
                        "program": "system",
                    }],
                }},
            }}
        if method == "getBlockHeight":
            return {"result": 123}
        raise AssertionError(method)

    return transport


def test_solana_invoice_match_and_finalized_gate():
    assert valid_sol_address(SOL_PAYER)
    assert valid_sol_address(SOL_RECEIVE)
    fetcher = SolanaRPCFetcher(
        endpoint="https://rpc.invalid", name="mock-solana",
        transport=sol_transport(finalized=True))
    watcher = Watcher(plans=PLANS, store=MemoryStore(),
                      chain_fetchers={"sol": fetcher},
                      rate_fetcher=FakePrice(100.0),
                      address_pools={"sol": [SOL_RECEIVE]})
    order = watcher.create_order("starter", SOL_PAYER, asset="sol",
                                 rate_usd=100.0)
    assert order.expected_amount == 190_000_000
    assert order.public()["expected_lamports"] == 190_000_000
    assert watcher.check_order(order.id)["action"] == "unlocked"


def test_solana_unfinalized_payment_stays_pending():
    fetcher = SolanaRPCFetcher(
        endpoint="https://rpc.invalid", name="mock-solana",
        transport=sol_transport(finalized=False))
    watcher = Watcher(plans=PLANS, store=MemoryStore(),
                      chain_fetchers={"sol": fetcher},
                      rate_fetcher=FakePrice(100.0),
                      address_pools={"sol": [SOL_RECEIVE]})
    order = watcher.create_order("starter", SOL_PAYER, asset="sol",
                                 rate_usd=100.0)
    result = watcher.check_order(order.id)
    assert result["action"] == "pending"
    assert watcher.get_order(order.id).status == "pending"


def test_solana_payer_mismatch_rejected():
    fetcher = SolanaRPCFetcher(
        endpoint="https://rpc.invalid", name="mock-solana",
        transport=sol_transport(payer=SOL_OTHER))
    watcher = Watcher(plans=PLANS, store=MemoryStore(),
                      chain_fetchers={"sol": fetcher},
                      rate_fetcher=FakePrice(100.0),
                      address_pools={"sol": [SOL_RECEIVE]})
    order = watcher.create_order("starter", SOL_PAYER, asset="sol",
                                 rate_usd=100.0)
    assert watcher.check_order(order.id)["action"] == "pending"
    assert watcher.get_order(order.id).seen_amount == 0


def test_solana_rpc_failover_and_error_kinds():
    class DownRPC(SolanaRPCFetcher):
        def __init__(self):
            super().__init__(endpoint="https://rpc.invalid", name="sol-down",
                             transport=self._down)

        @staticmethod
        def _down(_endpoint, _payload):
            raise ProviderError("sol-down", "rate_limited", "HTTP 429", 429)

    good = SolanaRPCFetcher(endpoint="https://rpc.invalid", name="sol-good",
                            transport=sol_transport())
    fetcher = FailoverChainFetcher("sol", [DownRPC(), good])
    result = fetcher.fetch_address(SOL_RECEIVE)
    assert result.provider == "sol-good"
    assert result.errors[0]["kind"] == "rate_limited"
    assert result.txs[0]["value_lamports"] == 190_000_000


# ------------------------------------------------------------- failover

class BrokenChain:
    name = "broken"

    def address_txs(self, _address):
        raise ProviderError("broken", "rate_limited", "HTTP 429", 429)

    def tip_height(self):
        raise ProviderError("broken", "rate_limited", "HTTP 429", 429)


class GoodChain:
    name = "good"

    def address_txs(self, _address):
        return [{"txid": "fallback-tx"}]

    def tip_height(self):
        return 100


def test_btc_chain_provider_failover():
    fetcher = FailoverChainFetcher("btc", [BrokenChain(), GoodChain()])
    result = fetcher.fetch_address("pool-address")
    assert result.provider == "good"
    assert result.txs == [{"txid": "fallback-tx"}]
    assert result.errors[0]["kind"] == "rate_limited"


class PriceDown:
    name = "price-down"

    def get_price(self, _spec):
        raise ProviderError("price-down", "unreachable", "offline")


class PriceGood:
    name = "price-good"

    def get_price(self, _spec):
        return 123.0


def test_price_failover_and_stale_cache_marking():
    fetcher = PriceFetcher([PriceDown(), PriceGood()])
    quote = fetcher.rate_info("btc", now=1000.0)
    assert quote.rate_usd == 123.0
    assert quote.provider == "price-good"
    assert quote.stale is False

    class LaterDown:
        name = "later-down"

        def get_price(self, _spec):
            raise ProviderError("later-down", "unreachable", "offline")

    fetcher.providers = [LaterDown()]
    stale = fetcher.rate_info("btc", now=1061.0)
    assert stale.rate_usd == 123.0
    assert stale.stale is True


def test_unavailable_btc_returns_busy_code_while_eth_still_creates():
    down = ProviderError("btc-down", "rate_limited", "HTTP 429", 429)
    watcher = Watcher(
        plans=PLANS, store=MemoryStore(),
        chain_fetchers={"btc": FakeChain(error=down), "eth": FakeChain()},
        rate_fetcher=FakePrice(1900.0),
        address_pools={"btc": ["btc-pool"], "eth": [EVM_RECEIVE]})
    with pytest.raises(AssetUnavailableError) as caught:
        watcher.create_order("starter", BTC_PAYER, rate_usd=19000.0)
    assert caught.value.status_code == 503
    assert caught.value.code == "ASSET_UNAVAILABLE"
    assert "too busy" in str(caught.value)
    assert "pick another crypto" in str(caught.value)
    eth_order = watcher.create_order("starter", EVM_PAYER, asset="eth")
    assert eth_order.asset == "eth"
    statuses = {item["asset"]: item for item in watcher.asset_statuses()
                if item["asset"] in ("btc", "eth")}
    assert statuses["btc"]["available"] is False
    assert statuses["btc"]["code"] == "ASSET_UNAVAILABLE"
    assert statuses["eth"]["available"] is True


def test_watcher_sweep_survives_and_records_provider_outage():
    chain = FakeChain()
    watcher = Watcher(plans=PLANS, store=MemoryStore(), fetcher=chain,
                      rate_fetcher=FakePrice(),
                      address_pools={"btc": ["pool-a"]})
    order = watcher.create_order("starter", BTC_PAYER, rate_usd=19000.0)
    chain.error = ProviderError("fake-chain", "unreachable", "down")
    result = watcher.sweep()
    assert result["errors"] == 1
    assert result["provider_errors"][0]["kind"] == "unreachable"
    stored = watcher.get_order(order.id)
    assert stored.status == "pending"
    assert stored.provider_error_kind == "unreachable"


# --------------------------------------------------------------- storage

def test_sqlite_migration_from_v1_and_multi_asset_roundtrip(tmp_path):
    path = str(tmp_path / "watch.db")
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE orders (
            id TEXT PRIMARY KEY, plan_id TEXT, price_usd REAL,
            payer_address TEXT, receive_address TEXT,
            expected_sats INTEGER, created_at TEXT, status TEXT,
            buyer_ref TEXT, seen_sats INTEGER, seen_txid TEXT,
            txid TEXT, tx_received_sats INTEGER, confirmations INTEGER,
            paid_at TEXT, expires_at TEXT, late_payment_note TEXT
        )""")
    conn.execute(
        "INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("old1", "starter", 19, BTC_PAYER, "legacy-receive", 100000,
         "2026-01-01T00:00:00+00:00", "pending", "", 0, None, None, 0, 0,
         None, "2026-01-01T01:00:00+00:00", None))
    conn.commit()
    conn.close()

    store = SQLiteStore(path)
    old = store.get("old1")
    assert old.asset == "btc"
    assert old.receive_address == "legacy-receive"
    columns = {row[1] for row in store._conn.execute("PRAGMA table_info(orders)")}
    assert {"asset", "required_confirmations", "provider", "provider_error",
            "provider_error_kind", "last_checked_at"} <= columns

    chain = FakeChain()
    watcher = Watcher(plans=PLANS, store=store, fetcher=chain,
                      rate_fetcher=FakePrice(1900.0),
                      address_pools={"eth": [EVM_RECEIVE]})
    order = watcher.create_order("starter", EVM_PAYER, asset="eth")
    reopened = SQLiteStore(path)
    assert reopened.get(order.id).asset == "eth"
    assert reopened.get(order.id).receive_address == EVM_RECEIVE
    assert reopened.active_counts("eth")[EVM_RECEIVE] == 1


def test_supported_asset_list_is_registry_driven():
    assert supported_assets() == ("btc", "eth", "bnb", "sol")


def test_api_busy_asset_response_and_assets_listing():
    from fastapi.testclient import TestClient

    from paywatch.api import create_app

    down = ProviderError("btc-down", "rate_limited", "HTTP 429", 429)
    watcher = Watcher(
        plans=PLANS, store=MemoryStore(),
        chain_fetchers={"btc": FakeChain(error=down), "eth": FakeChain(),
                        "bnb": FakeChain(), "sol": FakeChain()},
        rate_fetcher=FakePrice(1900.0),
        address_pools={"btc": ["btc-pool"], "eth": [EVM_RECEIVE],
                       "bnb": [EVM_RECEIVE], "sol": [SOL_RECEIVE]})
    client = TestClient(create_app(watcher), raise_server_exceptions=False)
    response = client.post("/orders", json={
        "plan_id": "starter", "payer_address": BTC_PAYER, "asset": "btc"})
    assert response.status_code == 503
    assert response.json()["code"] == "ASSET_UNAVAILABLE"
    response = client.post("/orders", json={
        "plan_id": "starter", "payer_address": EVM_PAYER, "asset": "eth"})
    assert response.status_code == 200
    assets = client.get("/assets").json()["assets"]
    by_asset = {item["asset"]: item for item in assets}
    assert by_asset["btc"]["available"] is False
    assert by_asset["eth"]["available"] is True

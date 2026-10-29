"""Order stores. The host picks one; the watcher only talks to the
interface below, so in-memory (demo/tests) and SQLite (real use)
can never drift apart on the rules that matter.

The single-use transaction rule lives here too: claim_txid() returns
False when another order already claimed that transaction id or hash.
Comparison is exact for BTC and Solana and lower-case normalized for
EVM hashes by the watcher and matcher.
"""
from __future__ import annotations

import sqlite3
from collections import Counter
from datetime import datetime, timezone
from typing import Dict, Optional

from .core import Order

OPEN_STATUSES = ("pending", "confirming", "underpaid")


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


class MemoryStore:
    def __init__(self) -> None:
        self.orders: Dict[str, Order] = {}
        self.counters: Dict[str, int] = {}

    def save(self, order: Order) -> None:
        self.orders[order.id] = order

    def get(self, order_id: str) -> Optional[Order]:
        return self.orders.get(order_id)

    def all_open(self) -> list:
        return [o for o in self.orders.values() if o.status in OPEN_STATUSES]

    def all_orders(self) -> list:
        return list(self.orders.values())

    def active_counts(self, asset: Optional[str] = None) -> Counter:
        counts: Counter = Counter()
        for order in self.orders.values():
            if order.status not in OPEN_STATUSES:
                continue
            if asset is not None and order.asset != asset:
                continue
            counts[order.receive_address] += 1
        return counts

    def txid_claimed_by(self, txid: str, exclude_order_id: str) -> Optional[str]:
        for o in self.orders.values():
            if o.id != exclude_order_id and o.txid == txid:
                return o.id
        return None

    def get_counter(self, name: str) -> int:
        return int(self.counters.get(name, 0))

    def set_counter(self, name: str, value: int) -> None:
        self.counters[name] = int(value)


class SQLiteStore:
    """Durable store with additive, backward compatible migrations.

    Existing v1 databases gain asset, confirmation, provider, and
    check metadata columns in place. Existing rows default to BTC and
    retain their original amounts and transaction ids.
    """

    _COLUMNS = {
        "asset": "TEXT DEFAULT 'btc'",
        "required_confirmations": "INTEGER DEFAULT 2",
        "provider": "TEXT",
        "provider_error": "TEXT",
        "provider_error_kind": "TEXT",
        "last_checked_at": "TEXT",
    }

    def __init__(self, path: str):
        self.path = path
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id TEXT PRIMARY KEY, plan_id TEXT, price_usd REAL,
                payer_address TEXT, receive_address TEXT,
                expected_sats INTEGER, created_at TEXT, status TEXT,
                buyer_ref TEXT, seen_sats INTEGER, seen_txid TEXT,
                txid TEXT, tx_received_sats INTEGER, confirmations INTEGER,
                paid_at TEXT, expires_at TEXT, late_payment_note TEXT
            )""")
        existing = {row[1] for row in self._conn.execute(
            "PRAGMA table_info(orders)").fetchall()}
        for name, ddl in self._COLUMNS.items():
            if name not in existing:
                self._conn.execute(f"ALTER TABLE orders ADD COLUMN {name} {ddl}")
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_txid "
            "ON orders(txid) WHERE txid IS NOT NULL")
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_orders_asset_receive_status "
            "ON orders(asset, receive_address, status)")
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS paywatch_meta (
                key TEXT PRIMARY KEY, value INTEGER NOT NULL DEFAULT 0
            )""")
        self._conn.commit()

    def save(self, order: Order) -> None:
        self._conn.execute("""
            INSERT INTO orders (
                id, plan_id, price_usd, payer_address, receive_address,
                expected_sats, created_at, status, buyer_ref, seen_sats,
                seen_txid, txid, tx_received_sats, confirmations, paid_at,
                expires_at, late_payment_note, asset,
                required_confirmations, provider, provider_error,
                provider_error_kind, last_checked_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                status=excluded.status, seen_sats=excluded.seen_sats,
                seen_txid=excluded.seen_txid, txid=excluded.txid,
                tx_received_sats=excluded.tx_received_sats,
                confirmations=excluded.confirmations,
                paid_at=excluded.paid_at,
                expires_at=excluded.expires_at,
                late_payment_note=excluded.late_payment_note,
                asset=excluded.asset,
                required_confirmations=excluded.required_confirmations,
                provider=excluded.provider,
                provider_error=excluded.provider_error,
                provider_error_kind=excluded.provider_error_kind,
                last_checked_at=excluded.last_checked_at""",
            (order.id, order.plan_id, order.price_usd, order.payer_address,
             order.receive_address, order.expected_amount,
             order.created_at.isoformat(), order.status, order.buyer_ref,
             order.seen_amount, order.seen_txid, order.txid,
             order.tx_received_amount, order.confirmations,
             order.paid_at.isoformat() if order.paid_at else None,
             order.expires_at.isoformat() if order.expires_at else None,
             order.late_payment_note, order.asset,
             order.required_confirmations, order.provider,
             order.provider_error, order.provider_error_kind,
             order.last_checked_at.isoformat() if order.last_checked_at else None))
        self._conn.commit()

    def _row_to_order(self, row) -> Order:
        d = dict(row)
        return Order(
            id=d["id"], plan_id=d["plan_id"], price_usd=d["price_usd"],
            payer_address=d["payer_address"],
            receive_address=d["receive_address"],
            expected_sats=d["expected_sats"],
            created_at=_parse_dt(d["created_at"]) or datetime.now(timezone.utc),
            status=d["status"], buyer_ref=d["buyer_ref"] or "",
            seen_sats=d["seen_sats"] or 0, seen_txid=d["seen_txid"],
            txid=d["txid"], tx_received_sats=d["tx_received_sats"] or 0,
            confirmations=d["confirmations"] or 0,
            paid_at=_parse_dt(d["paid_at"]),
            expires_at=_parse_dt(d["expires_at"]),
            late_payment_note=d["late_payment_note"],
            asset=d.get("asset") or "btc",
            required_confirmations=d.get("required_confirmations") or 0,
            provider=d.get("provider"),
            provider_error=d.get("provider_error"),
            provider_error_kind=d.get("provider_error_kind"),
            last_checked_at=_parse_dt(d.get("last_checked_at")))

    def get(self, order_id: str) -> Optional[Order]:
        row = self._conn.execute(
            "SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        return self._row_to_order(row) if row else None

    def all_open(self) -> list:
        rows = self._conn.execute(
            "SELECT * FROM orders WHERE status IN "
            "('pending','confirming','underpaid')").fetchall()
        return [self._row_to_order(r) for r in rows]

    def all_orders(self) -> list:
        rows = self._conn.execute("SELECT * FROM orders").fetchall()
        return [self._row_to_order(r) for r in rows]

    def active_counts(self, asset: Optional[str] = None) -> Counter:
        if asset is None:
            rows = self._conn.execute(
                "SELECT receive_address, COUNT(*) AS n FROM orders "
                "WHERE status IN ('pending','confirming','underpaid') "
                "GROUP BY receive_address").fetchall()
        else:
            rows = self._conn.execute(
                "SELECT receive_address, COUNT(*) AS n FROM orders "
                "WHERE asset=? AND status IN "
                "('pending','confirming','underpaid') "
                "GROUP BY receive_address", (asset,)).fetchall()
        return Counter({row["receive_address"]: row["n"] for row in rows})

    def txid_claimed_by(self, txid: str, exclude_order_id: str) -> Optional[str]:
        row = self._conn.execute(
            "SELECT id FROM orders WHERE txid=? AND id!=?",
            (txid, exclude_order_id)).fetchone()
        return row[0] if row else None

    def get_counter(self, name: str) -> int:
        row = self._conn.execute(
            "SELECT value FROM paywatch_meta WHERE key=?", (name,)).fetchone()
        return int(row[0]) if row else 0

    def set_counter(self, name: str, value: int) -> None:
        self._conn.execute(
            "INSERT INTO paywatch_meta(key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (name, int(value)))
        self._conn.commit()

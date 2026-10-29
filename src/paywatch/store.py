"""Order stores. The host picks one; the watcher only talks to the
interface below, so in-memory (demo/tests) and SQLite (real use)
can never drift apart on the rules that matter.

The single-use txid rule lives here too: claim_txid() returns False
when another order already claimed that txid.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Dict, Optional

from .core import Order


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

    def save(self, order: Order) -> None:
        self.orders[order.id] = order

    def get(self, order_id: str) -> Optional[Order]:
        return self.orders.get(order_id)

    def all_open(self) -> list:
        return [o for o in self.orders.values()
                if o.status in ("pending", "confirming", "underpaid")]

    def txid_claimed_by(self, txid: str, exclude_order_id: str) -> Optional[str]:
        for o in self.orders.values():
            if o.id != exclude_order_id and o.txid == txid:
                return o.id
        return None


class SQLiteStore:
    """Durable store. Fixes the monico-agent limitation where orders
    lived only in memory and were lost on restart (a buyer then had
    to re-create the order; no funds were ever at risk, because the
    app never holds funds)."""

    def __init__(self, path: str):
        self.path = path
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id TEXT PRIMARY KEY, plan_id TEXT, price_usd REAL,
                payer_address TEXT, receive_address TEXT,
                expected_sats INTEGER, created_at TEXT, status TEXT,
                buyer_ref TEXT, seen_sats INTEGER, seen_txid TEXT,
                txid TEXT, tx_received_sats INTEGER, confirmations INTEGER,
                paid_at TEXT, expires_at TEXT, late_payment_note TEXT
            )""")
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_txid "
            "ON orders(txid) WHERE txid IS NOT NULL")
        self._conn.commit()

    def save(self, order: Order) -> None:
        self._conn.execute("""
            INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                status=excluded.status, seen_sats=excluded.seen_sats,
                seen_txid=excluded.seen_txid, txid=excluded.txid,
                tx_received_sats=excluded.tx_received_sats,
                confirmations=excluded.confirmations,
                paid_at=excluded.paid_at,
                late_payment_note=excluded.late_payment_note""",
            (order.id, order.plan_id, order.price_usd, order.payer_address,
             order.receive_address, order.expected_sats,
             order.created_at.isoformat(), order.status, order.buyer_ref,
             order.seen_sats, order.seen_txid, order.txid,
             order.tx_received_sats, order.confirmations,
             order.paid_at.isoformat() if order.paid_at else None,
             order.expires_at.isoformat() if order.expires_at else None,
             order.late_payment_note))
        self._conn.commit()

    def _row_to_order(self, row) -> Order:
        keys = ("id", "plan_id", "price_usd", "payer_address",
                "receive_address", "expected_sats", "created_at", "status",
                "buyer_ref", "seen_sats", "seen_txid", "txid",
                "tx_received_sats", "confirmations", "paid_at",
                "expires_at", "late_payment_note")
        d = dict(zip(keys, row))
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
            late_payment_note=d["late_payment_note"])

    def get(self, order_id: str) -> Optional[Order]:
        row = self._conn.execute(
            "SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        return self._row_to_order(row) if row else None

    def all_open(self) -> list:
        rows = self._conn.execute(
            "SELECT * FROM orders WHERE status IN "
            "('pending','confirming','underpaid')").fetchall()
        return [self._row_to_order(r) for r in rows]

    def txid_claimed_by(self, txid: str, exclude_order_id: str) -> Optional[str]:
        row = self._conn.execute(
            "SELECT id FROM orders WHERE txid=? AND id!=?",
            (txid, exclude_order_id)).fetchone()
        return row[0] if row else None

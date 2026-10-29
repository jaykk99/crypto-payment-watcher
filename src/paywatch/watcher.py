"""Watcher: create order, check payment, sweep, manual approve.

The host project owns entitlement: it passes plans in, and it passes
an on_unlock(order, tx) callback that grants whatever its plan means
(access code, tier flag, days of access). This package never decides
what a plan unlocks beyond recording paid + paid_at; the callback is
where the host maps plan_id -> its own entitlement. Overpayment
unlocks the picked tier only; the excess is never refunded here.
"""
from __future__ import annotations

import os
import secrets
from datetime import datetime, timezone
from typing import Callable, Dict, Optional

from .core import (
    REQUIRED_CONFIRMATIONS, INVOICE_EXPIRY_MINUTES, Order, Plan,
    load_plans, match_payment, usd_to_sats, valid_btc_address,
)
from .mempool import MempoolFetcher, RateFetcher
from .store import MemoryStore


class WatcherError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def receive_address() -> str:
    """Host's public BTC receive address, env only, never hardcoded.
    Empty means checkout is honestly closed, not silently broken."""
    return os.environ.get("PAYWATCH_BTC_ADDRESS", "").strip()


def checkout_enabled() -> bool:
    return bool(receive_address())


class Watcher:
    def __init__(self, plans=None, store=None, fetcher=None,
                 rate_fetcher=None, on_unlock: Optional[Callable] = None):
        self.plans: Dict[str, Plan] = load_plans(plans)
        self.store = store or MemoryStore()
        self.fetcher = fetcher or MempoolFetcher()
        self.rate_fetcher = rate_fetcher or RateFetcher()
        self.on_unlock = on_unlock

    # ---------------------------------------------------------- orders

    def create_order(self, plan_id: str, payer_address: str,
                     buyer_ref: str = "", rate_usd: Optional[float] = None,
                     now: Optional[datetime] = None) -> Order:
        if not checkout_enabled():
            raise WatcherError(
                "checkout closed: PAYWATCH_BTC_ADDRESS is not set", 503)
        plan = self.plans.get(plan_id)
        if plan is None:
            raise WatcherError(f"unknown plan: {plan_id}", 404)
        if not valid_btc_address(payer_address):
            raise WatcherError("payer address is not a valid BTC address", 400)
        rate = rate_usd if rate_usd is not None else self.rate_fetcher.btc_usd()
        if not rate:
            raise WatcherError(
                "BTC rate unavailable, order not created", 503)
        now = now or datetime.now(timezone.utc)
        order = Order(
            id=secrets.token_hex(8),
            plan_id=plan.id, price_usd=plan.price_usd,
            payer_address=payer_address.strip(),
            receive_address=receive_address(),
            expected_sats=usd_to_sats(plan.price_usd, rate),
            created_at=now, buyer_ref=buyer_ref)
        self.store.save(order)
        return order

    def get_order(self, order_id: str) -> Optional[Order]:
        return self.store.get(order_id)

    # ---------------------------------------------------------- checking

    def _unlock(self, order: Order, tx: Dict, now: datetime) -> None:
        order.status = "paid"
        order.paid_at = now
        order.txid = tx["txid"]
        order.tx_received_sats = tx["received_sats"]
        order.confirmations = tx["confirmations"]
        self.store.save(order)
        if self.on_unlock is not None:
            self.on_unlock(order, tx)

    def check_order(self, order_id: str, fetcher=None,
                    now: Optional[datetime] = None) -> Dict:
        """One check, idempotent: paid / manually_approved / paid_late
        orders are never re-processed, and a confirmed txid is never
        reused by another order."""
        now = now or datetime.now(timezone.utc)
        order = self.store.get(order_id)
        if order is None:
            raise WatcherError(f"unknown order: {order_id}", 404)
        if order.status in ("paid", "manually_approved", "paid_late"):
            return {"action": "already_paid", "order": order.public()}
        if not checkout_enabled():
            return {"action": "watcher_disabled", "order": order.public()}
        fetcher = fetcher or self.fetcher
        expired = order.expires_at is not None and now > order.expires_at
        try:
            txs = fetcher.address_txs(order.receive_address)
            tip = fetcher.tip_height()
        except Exception as exc:  # explorer down: record, do not unlock
            return {"action": "explorer_error", "error": str(exc)[:200],
                    "order": order.public()}
        res = match_payment(txs, order.payer_address, order.receive_address,
                            order.expected_sats, tip)
        cand = res["match"]
        order.seen_sats = max(order.seen_sats, res["best_seen_sats"])
        if res["best_seen_txid"]:
            order.seen_txid = res["best_seen_txid"]
        if cand is not None:
            order.confirmations = cand["confirmations"]
            if cand["confirmations"] >= REQUIRED_CONFIRMATIONS:
                if expired:
                    order.status = "paid_late"
                    order.late_payment_note = (
                        f"qualifying tx {cand['txid']} confirmed after "
                        "invoice expiry; owner manual resolution required")
                    self.store.save(order)
                    return {"action": "paid_late", "order": order.public()}
                clash = self.store.txid_claimed_by(cand["txid"], order.id)
                if clash is not None:
                    order.status = "underpaid"
                    order.late_payment_note = (
                        f"tx {cand['txid']} already claimed by another "
                        "purchase; not reused")
                    self.store.save(order)
                    return {"action": "txid_reused", "order": order.public()}
                self._unlock(order, cand, now)
                return {"action": "unlocked", "order": order.public()}
            order.status = "confirming"
            self.store.save(order)
            return {"action": "confirming", "order": order.public()}
        if expired:
            order.status = "expired"
            self.store.save(order)
            return {"action": "expired", "order": order.public()}
        if res["best_seen_sats"] > 0:
            order.status = "underpaid"
            self.store.save(order)
            return {"action": "underpaid", "order": order.public()}
        order.status = "pending"
        self.store.save(order)
        return {"action": "pending", "order": order.public()}

    def sweep(self, fetcher=None, now: Optional[datetime] = None) -> Dict:
        """Check every still-open order. Used by a cron / background
        loop in the host project; returns action counts."""
        now = now or datetime.now(timezone.utc)
        counts = {"checked": 0, "unlocked": 0, "confirming": 0,
                  "underpaid": 0, "pending": 0, "expired": 0,
                  "paid_late": 0, "errors": 0}
        if not checkout_enabled():
            return {"watcher_enabled": False, **counts}
        for order in self.store.all_open():
            counts["checked"] += 1
            try:
                res = self.check_order(order.id, fetcher=fetcher, now=now)
            except Exception:
                counts["errors"] += 1
                continue
            action = res["action"]
            if action in counts:
                counts[action] += 1
        return {"watcher_enabled": True, **counts}

    # ---------------------------------------------------------- fallback

    def approve_order(self, order_id: str, owner_secret: Optional[str] = None,
                      now: Optional[datetime] = None) -> Dict:
        """Owner manual approve. If PAYWATCH_CRON_SECRET is set, the
        caller must present it; if it is unset, the host is expected
        to gate this route itself (admin session), so this package
        does not invent an auth scheme. Unlocks via the same callback
        as an automatic unlock, so the two can never drift apart."""
        required = os.environ.get("PAYWATCH_CRON_SECRET", "").strip()
        if required and owner_secret != required:
            raise WatcherError("owner approval denied", 403)
        now = now or datetime.now(timezone.utc)
        order = self.store.get(order_id)
        if order is None:
            raise WatcherError(f"unknown order: {order_id}", 404)
        if order.status in ("paid", "manually_approved"):
            return {"action": "already_paid", "order": order.public()}
        order.status = "manually_approved"
        order.paid_at = now
        self.store.save(order)
        if self.on_unlock is not None:
            self.on_unlock(order, {"txid": order.txid,
                                   "received_sats": order.tx_received_sats,
                                   "confirmations": order.confirmations})
        return {"action": "manually_approved", "order": order.public()}


def check_order(watcher: Watcher, order_id: str, **kwargs) -> Dict:
    return watcher.check_order(order_id, **kwargs)


__all__ = ["Watcher", "WatcherError", "check_order",
           "checkout_enabled", "receive_address",
           "REQUIRED_CONFIRMATIONS", "INVOICE_EXPIRY_MINUTES"]

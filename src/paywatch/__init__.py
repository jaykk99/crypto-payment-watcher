"""crypto-payment-watcher: reusable BTC payment watcher.

Buyer pays manually from their own wallet. The app never connects a
wallet, never signs, never takes custody, and never holds a private
key. The watcher polls public mempool.space (no key) and unlocks the
host project's entitlement when a qualifying payment confirms.

Proven rules (from ReconAnything paywatch and monico-agent BTC
checkout, extracted cleanly):
  - 2 confirmations before unlock
  - 60-minute invoice window
  - 1% tolerance (at least 99% of the pinned amount qualifies)
  - payer binding: at least one tx input spends from the declared
    payer address, and the amount sent TO the receive address counts
  - single-use txid: a txid already claimed by another order is
    never reused
  - expired invoice paid late is recorded as paid_late, NO auto-unlock
  - underpayment does NOT unlock; overpayment unlocks the picked
    tier only, with NO auto-refund
  - owner manual approve is the fallback for anything the watcher
    cannot match
"""

from .core import (
    REQUIRED_CONFIRMATIONS,
    INVOICE_EXPIRY_MINUTES,
    TOLERANCE_PCT,
    Order,
    Plan,
    btc_to_sats,
    threshold_sats,
    usd_to_sats,
    valid_btc_address,
    match_payment,
    load_plans,
)
from .store import MemoryStore, SQLiteStore
from .watcher import Watcher, check_order

__all__ = [
    "REQUIRED_CONFIRMATIONS",
    "INVOICE_EXPIRY_MINUTES",
    "TOLERANCE_PCT",
    "Order",
    "Plan",
    "btc_to_sats",
    "threshold_sats",
    "usd_to_sats",
    "valid_btc_address",
    "match_payment",
    "load_plans",
    "MemoryStore",
    "SQLiteStore",
    "Watcher",
    "check_order",
]

__version__ = "1.0.0"

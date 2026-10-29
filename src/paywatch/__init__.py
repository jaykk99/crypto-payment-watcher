"""crypto-payment-watcher: reusable multi-asset payment watcher.

Buyer pays manually from their own wallet. The app never connects a
wallet, never signs, never takes custody, and never holds a private
key. The watcher polls keyless public providers and unlocks the host
project's entitlement when a qualifying payment reaches the asset's
confirmation rule.

Rules carried across BTC, ETH, BNB, and SOL:
  - 60-minute invoice window
  - 1% tolerance (at least 99% of the pinned amount qualifies)
  - payer binding to the buyer declared sending address
  - single-use transaction id or hash across all assets
  - expired invoice paid late is recorded as paid_late, NO auto-unlock
  - underpayment does NOT unlock; overpayment unlocks the picked
    tier only, with NO auto-refund
  - owner manual approve is the fallback for anything the watcher
    cannot match
"""

from .assets import AssetSpec, get_asset, register_asset, supported_assets
from .core import (
    ETH_REQUIRED_CONFIRMATIONS,
    INVOICE_EXPIRY_MINUTES,
    REQUIRED_CONFIRMATIONS,
    TOLERANCE_PCT,
    Order,
    Plan,
    address_pool,
    btc_to_sats,
    default_address_pools,
    eth_to_wei,
    load_address_pools,
    match_account_payment,
    match_payment,
    load_plans,
    threshold_amount,
    threshold_sats,
    usd_to_amount,
    usd_to_sats,
    usd_to_wei,
    valid_btc_address,
    valid_eth_address,
    valid_sol_address,
)
from .providers import (
    AllProvidersUnavailable, FailoverChainFetcher, PriceFetcher,
    ProviderError, RateFetcher, build_chain_fetcher,
)
from .store import MemoryStore, SQLiteStore
from .watcher import AssetUnavailableError, Watcher, check_order

__all__ = [
    "AllProvidersUnavailable", "AssetSpec", "AssetUnavailableError",
    "ETH_REQUIRED_CONFIRMATIONS", "FailoverChainFetcher",
    "INVOICE_EXPIRY_MINUTES", "Order", "Plan", "PriceFetcher",
    "ProviderError", "REQUIRED_CONFIRMATIONS", "RateFetcher",
    "TOLERANCE_PCT", "address_pool", "btc_to_sats", "build_chain_fetcher",
    "check_order", "default_address_pools", "eth_to_wei", "get_asset",
    "load_address_pools",
    "load_plans", "match_account_payment", "match_payment", "register_asset",
    "supported_assets", "threshold_amount", "threshold_sats",
    "usd_to_amount", "usd_to_sats", "usd_to_wei", "valid_btc_address",
    "valid_eth_address", "valid_sol_address", "MemoryStore", "SQLiteStore",
    "Watcher",
]

__version__ = "2.0.0"

"""Pluggable asset registry.

An asset entry carries everything the generic watcher needs that is
specific to a coin: CoinGecko price id, fallback price market, amount
unit and decimals, address validation, transaction family, default
confirmations, and the names or factories of its chain providers.

BTC and ETH are registered here. A future UTXO or account based asset
can be registered by host code with :func:`register_asset`, plus a
provider factory whose fetcher returns the normalized transaction
shape documented in ``paywatch.providers``. Neither the core matcher
dispatch nor the watcher order flow needs an asset specific branch
for such an addition.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional, Tuple

from .core import valid_btc_address, valid_eth_address, valid_sol_address

AddressValidator = Callable[[Optional[str]], bool]
ProviderFactory = Callable[[], object]


@dataclass(frozen=True)
class AssetSpec:
    key: str
    symbol: str
    display_name: str
    price_id: str
    unit_name: str
    decimals: int
    default_confirmations: int
    transaction_family: str  # "utxo" or "account"
    address_validator: AddressValidator
    coinbase_product: str = ""
    kraken_pair: str = ""
    provider_names: Tuple[str, ...] = ()
    provider_factories: Tuple[ProviderFactory, ...] = ()
    asset_family: str = ""
    explorer_base_url: str = ""

    def validate_address(self, address: Optional[str]) -> bool:
        try:
            return bool(self.address_validator(address))
        except Exception:
            return False

    def required_confirmations(self) -> int:
        """Default confirmations, overridable per asset by env.

        Supported names are PAYWATCH_BTC_CONFIRMATIONS and
        PAYWATCH_ETH_CONFIRMATIONS. The generic pattern is
        PAYWATCH_<ASSET>_CONFIRMATIONS.
        """
        raw = os.environ.get(
            f"PAYWATCH_{self.key.upper()}_CONFIRMATIONS", "").strip()
        if raw:
            try:
                value = int(raw)
            except ValueError:
                value = 0
            if value > 0:
                return value
        return self.default_confirmations


ASSET_REGISTRY: Dict[str, AssetSpec] = {}


def register_asset(spec: AssetSpec, *, replace: bool = False) -> AssetSpec:
    """Register an asset and, optionally, its provider factories.

    Provider factories are callables returning a fetcher with either
    ``fetch_address(address)`` or ``address_txs(address)`` plus
    ``tip_height()``. Registering a provider in this way is the
    extension point for a third chain.
    """
    key = spec.key.strip().lower()
    if not key:
        raise ValueError("asset key is required")
    if key in ASSET_REGISTRY and not replace:
        raise ValueError(f"asset already registered: {key}")
    if spec.transaction_family not in ("utxo", "account", "solana-rpc"):
        raise ValueError(
            "transaction_family must be 'utxo', 'account', or 'solana-rpc'")
    ASSET_REGISTRY[key] = spec
    return spec


def get_asset(asset: Optional[str]) -> AssetSpec:
    key = (asset or "btc").strip().lower()
    try:
        return ASSET_REGISTRY[key]
    except KeyError:
        raise ValueError(f"unsupported asset: {key}") from None


def supported_assets() -> Tuple[str, ...]:
    return tuple(ASSET_REGISTRY.keys())


register_asset(AssetSpec(
    key="btc",
    symbol="BTC",
    display_name="Bitcoin",
    price_id="bitcoin",
    unit_name="sats",
    decimals=8,
    default_confirmations=2,
    transaction_family="utxo",
    address_validator=valid_btc_address,
    coinbase_product="BTC-USD",
    kraken_pair="XBTUSD",
    provider_names=("mempool.space", "blockstream.info"),
    asset_family="utxo",
))

# EVM family: one provider implementation, parameterized only by the
# registry fields below. ETH and BNB deliberately share validation,
# amount math, transaction shape, and watcher flow.
register_asset(AssetSpec(
    key="eth",
    symbol="ETH",
    display_name="Ethereum",
    price_id="ethereum",
    unit_name="wei",
    decimals=18,
    default_confirmations=12,
    transaction_family="account",
    address_validator=valid_eth_address,
    coinbase_product="ETH-USD",
    kraken_pair="ETHUSD",
    provider_names=("blockscout-evm",),
    asset_family="evm",
    explorer_base_url="https://eth.blockscout.com",
))

register_asset(AssetSpec(
    key="bnb",
    symbol="BNB",
    display_name="BNB Chain",
    price_id="binancecoin",
    unit_name="wei",
    decimals=18,
    default_confirmations=15,
    transaction_family="account",
    address_validator=valid_eth_address,
    coinbase_product="BNB-USD",
    kraken_pair="BNBUSD",
    provider_names=("blockscout-evm",),
    asset_family="evm",
    explorer_base_url="https://bsc.blockscout.com",
))

# Solana is a separate RPC family. Its confirmation value is a gate,
# not a target count displayed to buyers: 1 means finalized is
# required, and unfinalized candidates remain at 0 confirmations.
register_asset(AssetSpec(
    key="sol",
    symbol="SOL",
    display_name="Solana",
    price_id="solana",
    unit_name="lamports",
    decimals=9,
    default_confirmations=1,
    transaction_family="solana-rpc",
    address_validator=valid_sol_address,
    coinbase_product="SOL-USD",
    kraken_pair="SOLUSD",
    provider_names=("solana-mainnet-rpc", "solana-publicnode-rpc"),
    asset_family="solana",
    explorer_base_url="https://api.mainnet-beta.solana.com",
))

__all__ = [
    "ASSET_REGISTRY", "AssetSpec", "ProviderFactory", "get_asset",
    "register_asset", "supported_assets", "valid_eth_address",
    "valid_sol_address",
]

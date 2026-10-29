"""Backward compatible provider imports.

v1 exposed this module as ``mempool.py``. The implementation now
lives in :mod:`paywatch.providers`, where BTC has mempool.space and
blockstream.info failover and the price layer has fallbacks too.
"""

from .providers import *  # noqa: F401,F403
from .providers import (  # noqa: F401
    MEMPOOL_BASE, MEMPOOL_PAGES, MEMPOOL_PAGE_SIZE, RATE_TTL_S, _get_json,
)

__all__ = [
    "AllProvidersUnavailable", "BlockstreamFetcher", "CoinGeckoPriceProvider",
    "CoinbasePriceProvider", "EsploraFetcher", "FailoverChainFetcher",
    "FetchResult", "KrakenPriceProvider", "MempoolFetcher", "PriceFetcher",
    "PriceQuote", "ProviderError", "RateFetcher", "_get_json",
    "MEMPOOL_BASE", "MEMPOOL_PAGES", "MEMPOOL_PAGE_SIZE", "RATE_TTL_S",
]

"""Read-only public explorers. Stdlib only (urllib), no API key.

mempool.space returns 25 transactions per page; the fetcher pages up
to 5 pages (125 recent transactions) on the receive address, exactly
as the proven watcher did. A payment older than that window is missed
by the automatic watcher and falls back to owner manual approval.
CoinGecko is keyless, with a 60s in-process rate cache; a failed rate
fetch must fail the order honestly, never invent a rate.
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
from typing import Dict, List, Optional

MEMPOOL_BASE = "https://mempool.space/api"
MEMPOOL_PAGES = 5
MEMPOOL_PAGE_SIZE = 25
COINGECKO_URL = ("https://api.coingecko.com/api/v3/simple/price"
                 "?ids=bitcoin&vs_currencies=usd")
RATE_TTL_S = 60


def _get_json(url: str, timeout: float = 15.0):
    req = urllib.request.Request(url, headers={"User-Agent": "crypto-payment-watcher/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


class MempoolFetcher:
    """Injectable for tests: any object with address_txs(addr) and
    tip_height() works in its place."""

    def __init__(self, base: Optional[str] = None, timeout: float = 15.0):
        self.base = (base or os.environ.get("MEMPOOL_BASE_URL")
                     or MEMPOOL_BASE).rstrip("/")
        self.timeout = timeout

    def address_txs(self, address: str) -> List[Dict]:
        out: List[Dict] = []
        page = _get_json(f"{self.base}/address/{address}/txs", self.timeout) or []
        out.extend(page)
        last_txid = page[-1]["txid"] if page else None
        for _ in range(MEMPOOL_PAGES - 1):
            if not page or len(page) < MEMPOOL_PAGE_SIZE or not last_txid:
                break
            page = _get_json(
                f"{self.base}/address/{address}/txs/chain/{last_txid}",
                self.timeout) or []
            out.extend(page)
            last_txid = page[-1]["txid"] if page else None
        return out

    def tip_height(self) -> int:
        return int(_get_json(f"{self.base}/blocks/tip/height", self.timeout))


class RateFetcher:
    def __init__(self) -> None:
        self._rate: Optional[float] = None
        self._ts: float = 0.0

    def btc_usd(self) -> Optional[float]:
        now = time.time()
        if self._rate and now - self._ts < RATE_TTL_S:
            return self._rate
        url = os.environ.get("COINGECKO_URL") or COINGECKO_URL
        try:
            data = _get_json(url, timeout=10.0)
            rate = float(data["bitcoin"]["usd"])
            if rate <= 0:
                return None
        except Exception:
            return None
        self._rate, self._ts = rate, now
        return rate

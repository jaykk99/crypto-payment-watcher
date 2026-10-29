"""Keyless chain and price providers with explicit failover.

Normalized chain transaction contracts
---------------------------------------
UTXO providers return the established Esplora shape: ``txid``,
``vin[].prevout.scriptpubkey_address``, ``vout[]`` entries with
``scriptpubkey_address`` and integer sats ``value``, and ``status``.

Account providers, including every EVM chain and Solana, return:
``txid``, ``from_address``, ``to_address``, ``value_amount`` (plus
``value_wei`` on EVM or ``value_lamports`` on Solana), and integer
``confirmations``. Solana sets confirmations to 1 only when the
transaction is finalized and 0 otherwise.

No provider here needs an API key or receives a private key, seed, or
wallet credential. Receive and payer addresses are public data.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence

from .assets import AssetSpec, get_asset

RATE_TTL_S = 60
STALE_PRICE_MAX_AGE_S = 600
MEMPOOL_BASE = "https://mempool.space/api"
BLOCKSTREAM_BASE = "https://blockstream.info/api"
MEMPOOL_PAGES = 5
MEMPOOL_PAGE_SIZE = 25
MAX_UTXO_TXS = 125


class ProviderError(Exception):
    """A provider failure with a machine readable error kind."""

    KINDS = ("rate_limited", "unreachable", "bad_response")

    def __init__(self, provider: str, kind: str, message: str,
                 status: Optional[int] = None):
        if kind not in self.KINDS:
            kind = "bad_response"
        super().__init__(f"{provider}: {message}")
        self.provider = provider
        self.kind = kind
        self.status = status
        self.message = message

    def as_dict(self) -> Dict:
        return {"provider": self.provider, "kind": self.kind,
                "status": self.status, "error": self.message}


class AllProvidersUnavailable(ProviderError):
    def __init__(self, errors: Sequence[ProviderError], operation: str = "fetch"):
        self.errors = list(errors)
        kinds = {e.kind for e in self.errors}
        kind = kinds.pop() if len(kinds) == 1 else "unreachable"
        detail = "; ".join(str(e) for e in self.errors) or "no providers configured"
        super().__init__("all-providers", kind,
                         f"{operation} failed on every provider: {detail}")
        self.operation = operation

    def as_dict(self) -> Dict:
        data = super().as_dict()
        data["providers"] = [e.as_dict() for e in self.errors]
        return data


def _classify_http(provider: str, status: int, message: str) -> ProviderError:
    kind = "rate_limited" if status == 429 else (
        "unreachable" if status >= 500 else "bad_response")
    return ProviderError(provider, kind, message, status=status)


def _get_json(provider: str, url: str, timeout: float = 15.0):
    req = urllib.request.Request(
        url, headers={"User-Agent": "crypto-payment-watcher/2.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise _classify_http(provider, exc.code, f"HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ProviderError(provider, "unreachable", str(exc)) from exc
    try:
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ProviderError(provider, "bad_response", "invalid JSON") from exc


def _post_json(provider: str, url: str, payload: Dict,
               timeout: float = 15.0):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=body,
        headers={"User-Agent": "crypto-payment-watcher/2.0",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise _classify_http(provider, exc.code, f"HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ProviderError(provider, "unreachable", str(exc)) from exc
    try:
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ProviderError(provider, "bad_response", "invalid JSON") from exc


@dataclass
class FetchResult:
    txs: List[Dict]
    tip_height: Optional[int]
    provider: str
    errors: List[Dict] = field(default_factory=list)


class EsploraFetcher:
    """One keyless Esplora compatible BTC explorer."""

    def __init__(self, name: str, base: str, timeout: float = 15.0):
        self.name = name
        self.base = base.rstrip("/")
        self.timeout = timeout

    def _get(self, path: str):
        return _get_json(self.name, f"{self.base}{path}", self.timeout)

    def address_txs(self, address: str) -> List[Dict]:
        quoted = urllib.parse.quote(address, safe="")
        first = self._get(f"/address/{quoted}/txs")
        if not isinstance(first, list):
            raise ProviderError(self.name, "bad_response",
                                "address transactions were not a list")
        out = list(first)
        page_size = len(first)
        last_txid = first[-1].get("txid") if first else None
        while (first and page_size and len(first) == page_size
               and len(out) < MAX_UTXO_TXS and last_txid):
            page = self._get(f"/address/{quoted}/txs/chain/{last_txid}")
            if not isinstance(page, list):
                raise ProviderError(self.name, "bad_response",
                                    "address transaction page was not a list")
            if not page:
                break
            out.extend(page)
            first = page
            last_txid = page[-1].get("txid") if page else None
        return out[:MAX_UTXO_TXS]

    def tip_height(self) -> int:
        value = self._get("/blocks/tip/height")
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise ProviderError(self.name, "bad_response",
                                "tip height was not an integer") from exc

    def fetch_address(self, address: str) -> FetchResult:
        return FetchResult(self.address_txs(address), self.tip_height(), self.name)

    def health(self) -> str:
        self.tip_height()
        return self.name


class MempoolFetcher(EsploraFetcher):
    """Backward compatible mempool.space fetcher."""

    def __init__(self, base: Optional[str] = None, timeout: float = 15.0):
        super().__init__(
            "mempool.space",
            base or os.environ.get("MEMPOOL_BASE_URL") or MEMPOOL_BASE,
            timeout)


class BlockstreamFetcher(EsploraFetcher):
    def __init__(self, base: Optional[str] = None, timeout: float = 15.0):
        super().__init__(
            "blockstream.info",
            base or os.environ.get("BLOCKSTREAM_BASE_URL") or BLOCKSTREAM_BASE,
            timeout)


def _address_hash(value) -> str:
    if isinstance(value, dict):
        return str(value.get("hash") or value.get("address") or "")
    return str(value or "")


class BlockscoutEVMFetcher:
    """One Blockscout implementation for every EVM registry entry.

    Only registry parameters change between EVM chains: asset key,
    explorer base URL, and display provider name. There is no ETH or
    BNB branch in this class or anywhere above it.
    """

    def __init__(self, asset: str, base: Optional[str] = None,
                 timeout: float = 15.0, name: Optional[str] = None):
        spec = get_asset(asset)
        if spec.asset_family != "evm":
            raise ValueError(f"asset is not in the EVM family: {asset}")
        self.asset = spec.key
        self.name = name or f"blockscout-{spec.key}"
        env_base = os.environ.get(f"BLOCKSCOUT_{spec.key.upper()}_BASE_URL")
        self.base = (base or env_base or spec.explorer_base_url).rstrip("/")
        self.timeout = timeout

    def _get(self, path: str):
        return _get_json(self.name, f"{self.base}{path}", self.timeout)

    def address_txs(self, address: str) -> List[Dict]:
        quoted = urllib.parse.quote(address, safe="")
        out: List[Dict] = []
        path = f"/api/v2/addresses/{quoted}/transactions?page_size=50"
        for _ in range(3):
            data = self._get(path)
            if not isinstance(data, dict) or not isinstance(data.get("items"), list):
                raise ProviderError(self.name, "bad_response",
                                    "transactions response had no items list")
            for item in data["items"]:
                if item.get("status") not in (None, "ok", "success"):
                    continue
                block_number = item.get("block_number")
                out.append({
                    "txid": str(item.get("hash") or "").lower(),
                    "hash": str(item.get("hash") or "").lower(),
                    "from_address": _address_hash(item.get("from")),
                    "to_address": _address_hash(item.get("to")),
                    "value": str(item.get("value") or "0"),
                    "value_amount": int(item.get("value") or 0),
                    "value_wei": int(item.get("value") or 0),
                    "confirmations": int(item.get("confirmations") or 0),
                    "block_number": block_number,
                    "status": {"confirmed": block_number is not None,
                               "block_height": block_number},
                })
            params = data.get("next_page_params")
            if not isinstance(params, dict) or not params:
                break
            query = urllib.parse.urlencode(params)
            path = f"/api/v2/addresses/{quoted}/transactions?{query}"
        return out

    def tip_height(self) -> int:
        data = self._get("/api?module=block&action=eth_block_number")
        result = data.get("result") if isinstance(data, dict) else None
        try:
            return int(result, 16) if isinstance(result, str) else int(result)
        except (TypeError, ValueError) as exc:
            raise ProviderError(self.name, "bad_response",
                                "block number was not readable") from exc

    def fetch_address(self, address: str) -> FetchResult:
        txs = self.address_txs(address)
        # Blockscout transaction items already carry confirmations;
        # a separate tip call would add a failure point without
        # changing matching. Health still probes the block endpoint.
        return FetchResult(txs, None, self.name)

    def health(self) -> str:
        self.tip_height()
        return self.name


# Descriptive alias: this is a family implementation, not an ETH one.
EVMFetcher = BlockscoutEVMFetcher
BlockscoutFetcher = BlockscoutEVMFetcher


class SolanaRPCFetcher:
    """Keyless Solana JSON-RPC provider.

    ``transport`` is injectable for offline tests. It receives the
    endpoint and JSON-RPC payload and returns the decoded response.
    """

    def __init__(self, endpoint: Optional[str] = None,
                 name: str = "solana-mainnet-rpc",
                 timeout: float = 30.0,
                 transport: Optional[Callable[[str, Dict], Dict]] = None):
        self.endpoint = (endpoint or os.environ.get("SOLANA_RPC_URL")
                         or "https://api.mainnet-beta.solana.com").rstrip("/")
        self.name = name
        self.timeout = timeout
        self.transport = transport

    def _rpc(self, method: str, params: list):
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        if self.transport is not None:
            data = self.transport(self.endpoint, payload)
        else:
            data = _post_json(self.name, self.endpoint, payload, self.timeout)
        if not isinstance(data, dict):
            raise ProviderError(self.name, "bad_response", "RPC response was not an object")
        if data.get("error"):
            error = data["error"]
            message = str(error.get("message") if isinstance(error, dict) else error)
            lowered = message.lower()
            kind = ("rate_limited" if "rate" in lowered or "429" in lowered
                    else "bad_response")
            raise ProviderError(self.name, kind, message)
        return data.get("result")

    @staticmethod
    def _account_key(entry) -> str:
        if isinstance(entry, dict):
            return str(entry.get("pubkey") or "")
        return str(entry or "")

    def _normalize_transaction(self, signature_info: Dict, tx: Dict,
                                 receive_address: str) -> Optional[Dict]:
        if not isinstance(tx, dict) or (tx.get("meta") or {}).get("error"):
            return None
        signature = str(signature_info.get("signature") or "")
        finalized = (signature_info.get("confirmationStatus") == "finalized"
                     or signature_info.get("finalized") is True)
        details = tx.get("transaction") or {}
        message = details.get("message") or {}
        account_keys = message.get("accountKeys") or []
        addresses = [self._account_key(k) for k in account_keys]
        instructions = message.get("instructions") or []
        source = ""
        amount = 0
        for instruction in instructions:
            parsed = instruction.get("parsed") if isinstance(instruction, dict) else None
            if not isinstance(parsed, dict):
                continue
            info = parsed.get("info") or {}
            if (parsed.get("type") == "transfer"
                    and info.get("destination") == receive_address):
                source = str(info.get("source") or "")
                amount = int(info.get("lamports") or 0)
                break
        if amount <= 0:
            # Balance delta fallback covers parsed shapes that do not
            # expose a simple SystemProgram transfer instruction.
            try:
                index = addresses.index(receive_address)
            except ValueError:
                return None
            meta = tx.get("meta") or {}
            pre = meta.get("preBalances") or []
            post = meta.get("postBalances") or []
            if index >= len(pre) or index >= len(post):
                return None
            amount = int(post[index]) - int(pre[index])
            if amount <= 0:
                return None
            source = addresses[0] if addresses else ""
        if not signature or not source:
            return None
        return {
            "txid": signature,
            "signature": signature,
            "from_address": source,
            "to_address": receive_address,
            "value": amount,
            "value_amount": amount,
            "value_lamports": amount,
            "confirmations": 1 if finalized else 0,
            "finalized": finalized,
            "slot": tx.get("slot"),
            "status": {"confirmed": finalized,
                       "block_height": tx.get("slot")},
        }

    def address_txs(self, address: str) -> List[Dict]:
        # Confirmed lets the watcher observe a candidate before it
        # is finalized. The matcher still receives confirmations=0
        # until the signature itself reports finalized, so observing
        # an unfinalized payment can never unlock it.
        signatures = self._rpc(
            "getSignaturesForAddress",
            [address, {"limit": 100, "commitment": "confirmed"}])
        if not isinstance(signatures, list):
            raise ProviderError(self.name, "bad_response",
                                "signatures response was not a list")
        out: List[Dict] = []
        for info in signatures:
            if not isinstance(info, dict) or info.get("err"):
                continue
            signature = info.get("signature")
            if not signature:
                continue
            tx = self._rpc("getTransaction", [
                signature,
                {"encoding": "jsonParsed", "commitment": "confirmed",
                 "maxSupportedTransactionVersion": 0},
            ])
            normalized = self._normalize_transaction(info, tx or {}, address)
            if normalized is not None:
                out.append(normalized)
        return out

    def tip_height(self) -> int:
        result = self._rpc("getBlockHeight", [{"commitment": "finalized"}])
        try:
            return int(result)
        except (TypeError, ValueError) as exc:
            raise ProviderError(self.name, "bad_response",
                                "block height was not an integer") from exc

    def fetch_address(self, address: str) -> FetchResult:
        return FetchResult(self.address_txs(address), self.tip_height(), self.name)

    def health(self) -> str:
        self.tip_height()
        return self.name


class FailoverChainFetcher:
    """Try chain providers in registry order and retain every error."""

    def __init__(self, asset: str, providers: Sequence[object]):
        self.asset = asset
        self.providers = list(providers)
        self.last_provider: Optional[str] = None
        self.last_errors: List[Dict] = []
        self._cache: Dict[str, FetchResult] = {}

    @staticmethod
    def _provider_name(provider) -> str:
        return str(getattr(provider, "name", provider.__class__.__name__))

    @staticmethod
    def _wrap(provider, exc: Exception) -> ProviderError:
        if isinstance(exc, ProviderError):
            return exc
        return ProviderError(FailoverChainFetcher._provider_name(provider),
                             "bad_response", str(exc)[:300])

    def fetch_address(self, address: str) -> FetchResult:
        if address in self._cache:
            return self._cache[address]
        errors: List[ProviderError] = []
        for provider in self.providers:
            name = self._provider_name(provider)
            try:
                if hasattr(provider, "fetch_address"):
                    result = provider.fetch_address(address)
                    if isinstance(result, FetchResult):
                        txs, tip = result.txs, result.tip_height
                    else:
                        txs, tip = result, None
                else:
                    txs = provider.address_txs(address)
                    tip = provider.tip_height() if hasattr(provider, "tip_height") else None
                if not isinstance(txs, list):
                    raise ProviderError(name, "bad_response", "transactions were not a list")
            except Exception as exc:
                errors.append(self._wrap(provider, exc))
                continue
            result = FetchResult(txs, tip, name,
                                 [e.as_dict() for e in errors])
            self.last_provider = name
            self.last_errors = result.errors
            self._cache[address] = result
            return result
        self.last_errors = [e.as_dict() for e in errors]
        raise AllProvidersUnavailable(errors, "address fetch")

    def address_txs(self, address: str) -> List[Dict]:
        return self.fetch_address(address).txs

    def tip_height(self) -> Optional[int]:
        if self._cache:
            return next(iter(self._cache.values())).tip_height
        self.health()
        return None

    def health(self) -> str:
        errors: List[ProviderError] = []
        for provider in self.providers:
            name = self._provider_name(provider)
            try:
                if hasattr(provider, "health"):
                    provider.health()
                elif hasattr(provider, "tip_height"):
                    provider.tip_height()
                else:
                    continue
            except Exception as exc:
                errors.append(self._wrap(provider, exc))
                continue
            self.last_provider = name
            self.last_errors = [e.as_dict() for e in errors]
            return name
        self.last_errors = [e.as_dict() for e in errors]
        raise AllProvidersUnavailable(errors, "chain health check")


@dataclass
class PriceQuote:
    rate_usd: float
    provider: str
    stale: bool = False
    fetched_at: float = 0.0


class CoinGeckoPriceProvider:
    name = "coingecko"

    def get_price(self, spec: AssetSpec) -> float:
        override = os.environ.get("COINGECKO_URL", "")
        if override and spec.key == "btc":
            url = override
        elif override and "ids=bitcoin" in override:
            url = override.replace("ids=bitcoin", f"ids={spec.price_id}")
        else:
            base = os.environ.get("COINGECKO_BASE_URL",
                                  "https://api.coingecko.com/api/v3/simple/price")
            query = urllib.parse.urlencode(
                {"ids": spec.price_id, "vs_currencies": "usd"})
            url = f"{base}?{query}"
        data = _get_json(self.name, url, timeout=10.0)
        try:
            rate = float(data[spec.price_id]["usd"])
        except Exception as exc:
            raise ProviderError(self.name, "bad_response",
                                "price field was not readable") from exc
        if rate <= 0:
            raise ProviderError(self.name, "bad_response", "price was not positive")
        return rate


class CoinbasePriceProvider:
    name = "coinbase"

    def get_price(self, spec: AssetSpec) -> float:
        if not spec.coinbase_product:
            raise ProviderError(self.name, "bad_response",
                                f"no Coinbase product for {spec.key}")
        base = os.environ.get("COINBASE_BASE_URL", "https://api.coinbase.com/v2")
        url = f"{base}/prices/{spec.coinbase_product}/spot"
        data = _get_json(self.name, url, timeout=10.0)
        try:
            rate = float(data["data"]["amount"])
        except Exception as exc:
            raise ProviderError(self.name, "bad_response",
                                "spot price field was not readable") from exc
        if rate <= 0:
            raise ProviderError(self.name, "bad_response", "price was not positive")
        return rate


class KrakenPriceProvider:
    """Available as another keyless fallback for custom price stacks."""

    name = "kraken"

    def get_price(self, spec: AssetSpec) -> float:
        if not spec.kraken_pair:
            raise ProviderError(self.name, "bad_response",
                                f"no Kraken pair for {spec.key}")
        base = os.environ.get("KRAKEN_BASE_URL", "https://api.kraken.com/0/public")
        query = urllib.parse.urlencode({"pair": spec.kraken_pair})
        data = _get_json(self.name, f"{base}/Ticker?{query}", timeout=10.0)
        if not isinstance(data, dict) or data.get("error"):
            raise ProviderError(self.name, "bad_response", "Kraken returned an error")
        try:
            result = data["result"]
            first = next(iter(result.values()))
            rate = float(first["c"][0])
        except Exception as exc:
            raise ProviderError(self.name, "bad_response",
                                "ticker field was not readable") from exc
        if rate <= 0:
            raise ProviderError(self.name, "bad_response", "price was not positive")
        return rate


class PriceFetcher:
    """CoinGecko first, Coinbase fallback, bounded stale cache."""

    def __init__(self, providers: Optional[Sequence[object]] = None):
        self.providers = list(providers or [
            CoinGeckoPriceProvider(), CoinbasePriceProvider(),
            KrakenPriceProvider()])
        self._cache: Dict[str, PriceQuote] = {}
        self.last_errors: List[Dict] = []

    @staticmethod
    def _wrap(provider, exc: Exception) -> ProviderError:
        if isinstance(exc, ProviderError):
            return exc
        return ProviderError(str(getattr(provider, "name", "price-provider")),
                             "bad_response", str(exc)[:300])

    def rate_info(self, asset: str = "btc", *, now: Optional[float] = None) -> PriceQuote:
        spec = get_asset(asset)
        now = time.time() if now is None else now
        cached = self._cache.get(spec.key)
        if cached and now - cached.fetched_at < RATE_TTL_S:
            return PriceQuote(cached.rate_usd, cached.provider, False,
                              cached.fetched_at)
        errors: List[ProviderError] = []
        for provider in self.providers:
            name = str(getattr(provider, "name", provider.__class__.__name__))
            try:
                if hasattr(provider, "get_price"):
                    rate = provider.get_price(spec)
                elif hasattr(provider, "rate_info"):
                    quote = provider.rate_info(spec.key)
                    rate = quote.rate_usd
                else:
                    rate = provider(spec)
                rate = float(rate)
                if rate <= 0:
                    raise ProviderError(name, "bad_response", "price was not positive")
            except Exception as exc:
                errors.append(self._wrap(provider, exc))
                continue
            quote = PriceQuote(rate, name, False, now)
            self._cache[spec.key] = quote
            self.last_errors = [e.as_dict() for e in errors]
            return quote
        self.last_errors = [e.as_dict() for e in errors]
        stale_max = float(os.environ.get(
            "PAYWATCH_PRICE_STALE_MAX_AGE_S", STALE_PRICE_MAX_AGE_S))
        if cached and now - cached.fetched_at <= stale_max:
            return PriceQuote(cached.rate_usd, cached.provider, True,
                              cached.fetched_at)
        raise AllProvidersUnavailable(errors, f"{spec.symbol} price fetch")

    def rate_usd(self, asset: str = "btc") -> Optional[float]:
        try:
            return self.rate_info(asset).rate_usd
        except Exception:
            return None

    def btc_usd(self) -> Optional[float]:
        return self.rate_usd("btc")

    def eth_usd(self) -> Optional[float]:
        return self.rate_usd("eth")


# Backward compatible name used by v1.
RateFetcher = PriceFetcher


def build_chain_fetcher(asset: str):
    """Build the registered provider stack for one asset."""
    spec = get_asset(asset)
    if spec.provider_factories:
        providers = [factory() for factory in spec.provider_factories]
        return FailoverChainFetcher(spec.key, providers)
    if spec.key == "btc":
        providers = [MempoolFetcher(), BlockstreamFetcher()]
    elif spec.asset_family == "evm":
        providers = [BlockscoutEVMFetcher(spec.key)]
    elif spec.asset_family == "solana":
        providers = [
            SolanaRPCFetcher(
                os.environ.get("SOLANA_RPC_URL",
                               "https://api.mainnet-beta.solana.com"),
                name="solana-mainnet-rpc"),
            SolanaRPCFetcher(
                os.environ.get("SOLANA_FALLBACK_RPC_URL",
                               "https://solana-rpc.publicnode.com"),
                name="solana-publicnode-rpc"),
        ]
    else:
        providers = []
    return FailoverChainFetcher(spec.key, providers)


__all__ = [
    "AllProvidersUnavailable", "BlockscoutEVMFetcher", "BlockscoutFetcher",
    "BlockstreamFetcher", "CoinGeckoPriceProvider", "CoinbasePriceProvider",
    "EVMFetcher", "EsploraFetcher", "FailoverChainFetcher", "FetchResult",
    "KrakenPriceProvider", "MempoolFetcher", "PriceFetcher", "PriceQuote",
    "ProviderError", "RateFetcher", "SolanaRPCFetcher", "build_chain_fetcher",
]

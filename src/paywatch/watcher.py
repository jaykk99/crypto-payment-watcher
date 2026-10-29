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
import time
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

from .assets import AssetSpec, get_asset, supported_assets
from .core import (
    INVOICE_EXPIRY_MINUTES, Order, Plan, address_pool, load_address_pools,
    load_plans, match_payment, usd_to_amount_for_decimals,
)
from .providers import (
    AllProvidersUnavailable, FetchResult, PriceFetcher, PriceQuote,
    ProviderError, build_chain_fetcher,
)
from .store import MemoryStore


class WatcherError(Exception):
    def __init__(self, message: str, status_code: int = 400,
                 code: Optional[str] = None,
                 provider_errors: Optional[List[Dict]] = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.provider_errors = provider_errors or []


class AssetUnavailableError(WatcherError):
    """The honest busy state Jay asked for.

    The machine readable code is stable so a checkout page can grey
    out this asset and offer another one instead of showing a generic
    failure.
    """

    code = "ASSET_UNAVAILABLE"

    def __init__(self, spec: AssetSpec, reason: str,
                 provider_errors: Optional[List[Dict]] = None):
        self.asset = spec.key
        self.symbol = spec.symbol
        self.reason = reason
        message = (f"{spec.symbol} payments are too busy right now. "
                   "Please pick another crypto.")
        super().__init__(message, 503, code=self.code,
                         provider_errors=provider_errors)


def receive_addresses(asset: str = "btc") -> List[str]:
    """Configured public receive pool for one asset.

    Environment configuration wins per asset; assets the environment
    does not configure fall back to the shipped default pool.
    """
    return address_pool(asset)


def receive_address(asset: str = "btc") -> str:
    """First configured receive address for an asset.

    Kept for v1 callers. New code should use the pool through Watcher,
    because invoice creation assigns the least busy pool address.
    """
    pool = receive_addresses(asset)
    return pool[0] if pool else ""


def checkout_enabled(asset: Optional[str] = None) -> bool:
    pools = load_address_pools()
    if asset is not None:
        return bool(pools.get(asset.lower()))
    return any(pools.values())


class _CachedFetcher:
    """Per sweep cache so each pool address is fetched once."""

    def __init__(self, underlying, cache: Dict[str, object]):
        self.underlying = underlying
        self.cache = cache
        self.name = getattr(underlying, "name", underlying.__class__.__name__)
        self.last_errors: List[Dict] = []

    def fetch_address(self, address: str):
        if address in self.cache:
            cached = self.cache[address]
            if isinstance(cached, Exception):
                raise cached
            return cached
        try:
            if hasattr(self.underlying, "fetch_address"):
                result = self.underlying.fetch_address(address)
                if not isinstance(result, FetchResult):
                    result = FetchResult(
                        result, None,
                        getattr(self.underlying, "name", self.name),
                        list(getattr(self.underlying, "last_errors", []) or []))
            else:
                txs = self.underlying.address_txs(address)
                tip = (self.underlying.tip_height()
                       if hasattr(self.underlying, "tip_height") else None)
                result = FetchResult(
                    txs, tip, getattr(self.underlying, "name", self.name),
                    list(getattr(self.underlying, "last_errors", []) or []))
        except Exception as exc:
            self.cache[address] = exc
            raise
        self.cache[address] = result
        self.last_errors = result.errors
        return result

    def address_txs(self, address: str):
        return self.fetch_address(address).txs

    def tip_height(self):
        if self.cache:
            first = next(iter(self.cache.values()))
            if isinstance(first, FetchResult):
                return first.tip_height
        if hasattr(self.underlying, "tip_height"):
            return self.underlying.tip_height()
        return None

    def health(self):
        if hasattr(self.underlying, "health"):
            return self.underlying.health()
        return self.tip_height()


class Watcher:
    def __init__(self, plans=None, store=None, fetcher=None,
                 rate_fetcher=None, on_unlock: Optional[Callable] = None,
                 chain_fetchers: Optional[Dict[str, object]] = None,
                 address_pools: Optional[Dict[str, List[str]]] = None):
        self.plans: Dict[str, Plan] = load_plans(plans)
        self.store = store or MemoryStore()
        # ``fetcher`` is the v1 injection point. When supplied it is
        # honored for every asset in that test or host integration.
        self.fetcher = fetcher
        self.chain_fetchers: Dict[str, object] = dict(chain_fetchers or {})
        self.rate_fetcher = rate_fetcher or PriceFetcher()
        self.on_unlock = on_unlock
        if address_pools is None:
            self.address_pools = load_address_pools()
        else:
            self.address_pools = {
                str(asset).lower(): [str(a).strip() for a in values if str(a).strip()]
                for asset, values in address_pools.items()
            }
        self._fallback_counters: Dict[str, int] = {}
        self._health_cache: Dict[str, tuple] = {}

    # ---------------------------------------------------------- assets

    def pool_for(self, asset: str) -> List[str]:
        return list(self.address_pools.get(asset.lower(), []))

    def get_fetcher(self, asset: str):
        asset = asset.lower()
        if asset in self.chain_fetchers:
            return self.chain_fetchers[asset]
        if self.fetcher is not None:
            return self.fetcher
        fetcher = build_chain_fetcher(asset)
        self.chain_fetchers[asset] = fetcher
        return fetcher

    def _provider_error_dicts(self, exc: Exception) -> List[Dict]:
        if isinstance(exc, AllProvidersUnavailable):
            return [e.as_dict() for e in exc.errors]
        if isinstance(exc, ProviderError):
            return [exc.as_dict()]
        return [{"provider": "unknown", "kind": "bad_response",
                 "status": None, "error": str(exc)[:300]}]

    def _chain_health(self, asset: str) -> tuple:
        fetcher = self.get_fetcher(asset)
        try:
            if hasattr(fetcher, "health"):
                provider = fetcher.health()
            elif hasattr(fetcher, "fetch_address"):
                pool = self.pool_for(asset)
                result = fetcher.fetch_address(pool[0] if pool else "")
                provider = result.provider if isinstance(result, FetchResult) \
                    else getattr(fetcher, "name", "chain-provider")
            elif hasattr(fetcher, "tip_height"):
                fetcher.tip_height()
                provider = getattr(fetcher, "name", "chain-provider")
            else:
                provider = getattr(fetcher, "name", "chain-provider")
        except Exception as exc:
            errors = self._provider_error_dicts(exc)
            return None, errors
        errors = list(getattr(fetcher, "last_errors", []) or [])
        return str(provider), errors

    def _price_quote(self, asset: str,
                     rate_usd: Optional[float] = None) -> PriceQuote:
        spec = get_asset(asset)
        if rate_usd is not None:
            if rate_usd <= 0:
                raise AssetUnavailableError(spec, "invalid_injected_price")
            return PriceQuote(float(rate_usd), "injected", False, time.time())
        fetcher = self.rate_fetcher
        try:
            if hasattr(fetcher, "rate_info"):
                quote = fetcher.rate_info(spec.key)
                if isinstance(quote, PriceQuote):
                    return quote
                return PriceQuote(float(quote), "price-provider", False, time.time())
            method = getattr(fetcher, f"{spec.key}_usd", None)
            if method is None and spec.key == "btc":
                method = getattr(fetcher, "btc_usd", None)
            rate = method() if method is not None else None
            if not rate:
                raise AllProvidersUnavailable([], "price fetch")
            return PriceQuote(float(rate), "price-provider", False, time.time())
        except AssetUnavailableError:
            raise
        except Exception as exc:
            raise AssetUnavailableError(
                spec, "price_unavailable", self._provider_error_dicts(exc)) from exc

    def asset_status(self, asset: str, *, use_cache: bool = True) -> Dict:
        spec = get_asset(asset)
        cache_key = spec.key
        now_ts = time.time()
        if use_cache and cache_key in self._health_cache:
            checked_at, cached = self._health_cache[cache_key]
            if now_ts - checked_at < 30:
                return dict(cached)
        pool = self.pool_for(spec.key)
        base = {
            "asset": spec.key,
            "symbol": spec.symbol,
            "name": spec.display_name,
            "family": spec.asset_family or spec.transaction_family,
            "transaction_family": spec.transaction_family,
            "amount_unit": spec.unit_name,
            "decimals": spec.decimals,
            "required_confirmations": spec.required_confirmations(),
            "pool_size": len(pool),
            "checkout_open": bool(pool),
            "available": False,
            "code": None,
            "reason": None,
            "message": None,
            "price_usd": None,
            "price_stale": False,
            "price_provider": None,
            "chain_provider": None,
            "provider_errors": [],
        }
        if not pool:
            base.update({
                "code": "CHECKOUT_CLOSED",
                "reason": "no_receive_addresses",
                "message": f"{spec.symbol} checkout is closed: no receive addresses are configured.",
            })
            self._health_cache[cache_key] = (now_ts, dict(base))
            return base
        errors: List[Dict] = []
        try:
            quote = self._price_quote(spec.key)
            base.update({"price_usd": quote.rate_usd,
                         "price_stale": quote.stale,
                         "price_provider": quote.provider})
        except AssetUnavailableError as exc:
            errors.extend(exc.provider_errors)
            base.update({"code": AssetUnavailableError.code,
                         "reason": "price_unavailable",
                         "message": str(exc), "provider_errors": errors})
            self._health_cache[cache_key] = (now_ts, dict(base))
            return base
        provider, chain_errors = self._chain_health(spec.key)
        errors.extend(chain_errors)
        base["provider_errors"] = errors
        if provider is None:
            base.update({
                "code": AssetUnavailableError.code,
                "reason": "chain_unavailable",
                "message": (f"{spec.symbol} payments are too busy right now. "
                            "Please pick another crypto."),
            })
        else:
            base.update({"available": True, "chain_provider": provider})
        self._health_cache[cache_key] = (now_ts, dict(base))
        return base

    def asset_statuses(self) -> List[Dict]:
        return [self.asset_status(asset) for asset in supported_assets()]

    # Backwards friendly descriptive alias.
    def list_assets(self) -> List[Dict]:
        return self.asset_statuses()

    def _select_receive_address(self, asset: str) -> str:
        pool = self.pool_for(asset)
        counts = (self.store.active_counts(asset)
                  if hasattr(self.store, "active_counts") else {})
        minimum = min((counts.get(address, 0) for address in pool), default=0)
        tied = [address for address in pool if counts.get(address, 0) == minimum]
        if len(tied) == 1:
            return tied[0]
        counter_name = f"pool_rotation:{asset}"
        if hasattr(self.store, "get_counter") and hasattr(self.store, "set_counter"):
            counter = self.store.get_counter(counter_name)
            selected = tied[counter % len(tied)]
            self.store.set_counter(counter_name, counter + 1)
            return selected
        counter = self._fallback_counters.get(asset, 0)
        selected = tied[counter % len(tied)]
        self._fallback_counters[asset] = counter + 1
        return selected

    # ---------------------------------------------------------- orders

    def create_order(self, plan_id: str, payer_address: str,
                     buyer_ref: str = "", rate_usd: Optional[float] = None,
                     now: Optional[datetime] = None,
                     asset: str = "btc") -> Order:
        try:
            spec = get_asset(asset)
        except ValueError as exc:
            raise WatcherError(str(exc), 404, code="ASSET_NOT_SUPPORTED") from exc
        pool = self.pool_for(spec.key)
        if not pool:
            raise WatcherError(
                f"checkout closed: no {spec.symbol} receive addresses are configured",
                503, code="CHECKOUT_CLOSED")
        plan = self.plans.get(plan_id)
        if plan is None:
            raise WatcherError(f"unknown plan: {plan_id}", 404)
        if not spec.validate_address(payer_address):
            raise WatcherError(
                f"payer address is not a valid {spec.symbol} address", 400)
        quote = self._price_quote(spec.key, rate_usd)
        provider, errors = self._chain_health(spec.key)
        if provider is None:
            raise AssetUnavailableError(spec, "chain_unavailable", errors)
        now = now or datetime.now(timezone.utc)
        order = Order(
            id=secrets.token_hex(8),
            plan_id=plan.id, price_usd=plan.price_usd,
            payer_address=payer_address.strip(),
            receive_address=self._select_receive_address(spec.key),
            expected_sats=usd_to_amount_for_decimals(
                plan.price_usd, quote.rate_usd, spec.decimals),
            created_at=now, buyer_ref=buyer_ref, asset=spec.key,
            required_confirmations=spec.required_confirmations(),
            provider=provider)
        self.store.save(order)
        return order

    def get_order(self, order_id: str) -> Optional[Order]:
        return self.store.get(order_id)

    # ---------------------------------------------------------- checking

    def _unlock(self, order: Order, tx: Dict, now: datetime) -> None:
        order.status = "paid"
        order.paid_at = now
        order.txid = tx["txid"]
        order.tx_received_amount = tx["received_amount"]
        order.confirmations = tx["confirmations"]
        self.store.save(order)
        if self.on_unlock is not None:
            self.on_unlock(order, tx)

    def check_order(self, order_id: str, fetcher=None,
                    now: Optional[datetime] = None) -> Dict:
        """One check, idempotent across every registered asset."""
        now = now or datetime.now(timezone.utc)
        order = self.store.get(order_id)
        if order is None:
            raise WatcherError(f"unknown order: {order_id}", 404)
        if order.status in ("paid", "manually_approved", "paid_late"):
            return {"action": "already_paid", "order": order.public()}
        if not self.pool_for(order.asset) and fetcher is None \
                and order.asset not in self.chain_fetchers and self.fetcher is None:
            return {"action": "watcher_disabled", "order": order.public()}
        fetcher = fetcher or self.get_fetcher(order.asset)
        expired = order.expires_at is not None and now > order.expires_at
        provider_errors: List[Dict] = []
        provider_name: Optional[str] = None
        try:
            if hasattr(fetcher, "fetch_address"):
                fetched = fetcher.fetch_address(order.receive_address)
                if isinstance(fetched, FetchResult):
                    txs, tip = fetched.txs, fetched.tip_height
                    provider_name = fetched.provider
                    provider_errors = list(fetched.errors or [])
                elif isinstance(fetched, tuple) and len(fetched) >= 2:
                    txs, tip = fetched[0], fetched[1]
                    provider_name = getattr(fetcher, "name", None)
                else:
                    txs = fetched
                    tip = fetcher.tip_height() if hasattr(fetcher, "tip_height") else None
                    provider_name = getattr(fetcher, "name", None)
            else:
                txs = fetcher.address_txs(order.receive_address)
                tip = fetcher.tip_height() if hasattr(fetcher, "tip_height") else None
                provider_name = getattr(fetcher, "name", None)
            provider_errors.extend(
                list(getattr(fetcher, "last_errors", []) or []))
        except Exception as exc:  # provider down: record, do not unlock
            errors = self._provider_error_dicts(exc)
            kind = errors[0]["kind"] if errors else "bad_response"
            order.provider_error = str(exc)[:500]
            order.provider_error_kind = kind
            order.last_checked_at = now
            self.store.save(order)
            return {"action": "explorer_error", "error": str(exc)[:500],
                    "error_kind": kind, "provider_errors": errors,
                    "order": order.public()}
        order.last_checked_at = now
        order.provider = provider_name
        if provider_errors:
            # A fallback succeeded after another provider failed. Keep
            # that visible without treating the check as failed.
            order.provider_error = "; ".join(
                str(e.get("error") or e) for e in provider_errors)[:500]
            order.provider_error_kind = provider_errors[-1].get("kind")
        else:
            order.provider_error = None
            order.provider_error_kind = None
        try:
            spec = get_asset(order.asset)
            family = spec.transaction_family
        except ValueError:
            family = None
        res = match_payment(
            txs, order.payer_address, order.receive_address,
            order.expected_amount, tip, asset=order.asset,
            transaction_family=family)
        cand = res["match"]
        order.seen_amount = max(order.seen_amount, res["best_seen_amount"])
        if res["best_seen_txid"]:
            order.seen_txid = res["best_seen_txid"]
        if cand is not None:
            order.confirmations = cand["confirmations"]
            if cand["confirmations"] >= order.required_confirmations:
                if expired:
                    order.status = "paid_late"
                    order.late_payment_note = (
                        f"qualifying tx {cand['txid']} confirmed after "
                        "invoice expiry; owner manual resolution required")
                    self.store.save(order)
                    return {"action": "paid_late", "provider": provider_name,
                            "provider_errors": provider_errors,
                            "order": order.public()}
                clash = self.store.txid_claimed_by(cand["txid"], order.id)
                if clash is not None:
                    order.status = "underpaid"
                    order.late_payment_note = (
                        f"tx {cand['txid']} already claimed by another "
                        "purchase; not reused")
                    self.store.save(order)
                    return {"action": "txid_reused", "provider": provider_name,
                            "provider_errors": provider_errors,
                            "order": order.public()}
                self._unlock(order, cand, now)
                return {"action": "unlocked", "provider": provider_name,
                        "provider_errors": provider_errors,
                        "order": order.public()}
            if family == "solana-rpc":
                # Solana has a binary confirmation model in this
                # provider family: finalized unlocks, anything else
                # remains pending rather than entering a count based
                # confirming state.
                order.status = "pending"
                self.store.save(order)
                return {"action": "pending", "provider": provider_name,
                        "provider_errors": provider_errors,
                        "order": order.public()}
            order.status = "confirming"
            self.store.save(order)
            return {"action": "confirming", "provider": provider_name,
                    "provider_errors": provider_errors,
                    "order": order.public()}
        if expired:
            order.status = "expired"
            self.store.save(order)
            return {"action": "expired", "provider": provider_name,
                    "provider_errors": provider_errors,
                    "order": order.public()}
        if res["best_seen_amount"] > 0:
            order.status = "underpaid"
            self.store.save(order)
            return {"action": "underpaid", "provider": provider_name,
                    "provider_errors": provider_errors,
                    "order": order.public()}
        order.status = "pending"
        self.store.save(order)
        return {"action": "pending", "provider": provider_name,
                "provider_errors": provider_errors,
                "order": order.public()}

    def sweep(self, fetcher=None, now: Optional[datetime] = None) -> Dict:
        """Check every open order and probe every configured pool.

        Each distinct pool address is fetched once per sweep, even if
        several open invoices share it. Provider failures are returned
        in the result and recorded on affected orders; one bad asset
        or address does not stop the rest of the sweep.
        """
        now = now or datetime.now(timezone.utc)
        counts = {"checked": 0, "unlocked": 0, "confirming": 0,
                  "underpaid": 0, "pending": 0, "expired": 0,
                  "paid_late": 0, "errors": 0}
        pools = {asset: pool for asset, pool in self.address_pools.items() if pool}
        if not pools and fetcher is None and not self.chain_fetchers \
                and self.fetcher is None:
            return {"watcher_enabled": False, "provider_errors": [], **counts}
        caches: Dict[str, Dict[str, object]] = {}
        cached_fetchers: Dict[str, _CachedFetcher] = {}
        provider_errors: List[Dict] = []

        def cached_fetcher(asset: str):
            if fetcher is not None:
                underlying = fetcher
            else:
                underlying = self.get_fetcher(asset)
            if asset not in cached_fetchers:
                cache: Dict[str, object] = {}
                caches[asset] = cache
                cached_fetchers[asset] = _CachedFetcher(underlying, cache)
            return cached_fetchers[asset]

        # Probe every pool address, including addresses with no open
        # invoice right now, and cache the result for order checks.
        for asset, pool in pools.items():
            cache_fetcher = cached_fetcher(asset)
            for address in pool:
                try:
                    cache_fetcher.fetch_address(address)
                except Exception as exc:
                    provider_errors.extend(self._provider_error_dicts(exc))
        for order in self.store.all_open():
            counts["checked"] += 1
            try:
                res = self.check_order(
                    order.id, fetcher=cached_fetcher(order.asset), now=now)
            except Exception:
                counts["errors"] += 1
                continue
            action = res["action"]
            if action == "explorer_error":
                counts["errors"] += 1
                provider_errors.extend(res.get("provider_errors") or [])
            elif action in counts:
                counts[action] += 1
            provider_errors.extend(
                e for e in (res.get("provider_errors") or [])
                if e not in provider_errors)
        return {"watcher_enabled": True, "provider_errors": provider_errors,
                "pool_addresses_checked": sum(len(p) for p in pools.values()),
                **counts}

    # ---------------------------------------------------------- fallback

    def approve_order(self, order_id: str, owner_secret: Optional[str] = None,
                      now: Optional[datetime] = None) -> Dict:
        """Owner manual approve, using the same unlock callback."""
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
            self.on_unlock(order, {
                "txid": order.txid,
                "received_sats": order.tx_received_amount,
                "received_amount": order.tx_received_amount,
                "confirmations": order.confirmations})
        return {"action": "manually_approved", "order": order.public()}


def check_order(watcher: Watcher, order_id: str, **kwargs) -> Dict:
    return watcher.check_order(order_id, **kwargs)


__all__ = ["Watcher", "WatcherError", "AssetUnavailableError", "check_order",
           "checkout_enabled", "receive_address", "receive_addresses",
           "INVOICE_EXPIRY_MINUTES"]

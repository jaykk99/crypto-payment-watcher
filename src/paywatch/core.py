"""Pure core: amounts, address validation, plans, payment matching.

No network, no storage, no secrets here. Everything in this module
is unit-testable with plain dicts shaped like mempool.space txs.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Dict, List, Optional

REQUIRED_CONFIRMATIONS = 2
ETH_REQUIRED_CONFIRMATIONS = 12
INVOICE_EXPIRY_MINUTES = 60
TOLERANCE_PCT = 1.0
PLAN_DURATION_DAYS = 30

# Smallest unit decimals by asset. The asset registry in assets.py is
# the public extension point; these two values let the pure matcher
# and amount helpers stay free of a registry import cycle.
ASSET_DECIMALS = {"btc": 8, "eth": 18, "bnb": 18, "sol": 9}
ASSET_UNITS = {"btc": "sats", "eth": "wei", "bnb": "wei", "sol": "lamports"}
REQUIRED_CONFIRMATIONS_BY_ASSET = {"btc": 2, "eth": 12, "bnb": 15, "sol": 1}

# ---------------------------------------------------------------- plans
# Plans are NEVER hardcoded to one project. The host passes its own
# plans (env JSON, a file, or a dict). The two dicts below are
# EXAMPLES ONLY, kept in examples/, not used as defaults: a project
# with no configured plans must fail honestly at order creation,
# not silently sell something it did not configure.


@dataclass
class Plan:
    id: str
    name: str
    price_usd: float
    duration_days: int = PLAN_DURATION_DAYS
    features: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict:
        return asdict(self)


def load_plans(source: Optional[object] = None) -> Dict[str, Plan]:
    """Load plans from a dict, a JSON string, a JSON file path, or
    the PAYWATCH_PLANS_JSON / PAYWATCH_PLANS_FILE env vars.

    Each entry: {"id": ..., "name": ..., "price_usd": ...,
    "duration_days": 30, "features": [...]}. Raises ValueError when
    no usable plans are configured, so checkout never opens with an
    invented price list.
    """
    raw: Optional[object] = source
    if raw is None:
        raw = os.environ.get("PAYWATCH_PLANS_JSON") or ""
        if not raw:
            path = os.environ.get("PAYWATCH_PLANS_FILE", "")
            if path:
                with open(path, encoding="utf-8") as fh:
                    raw = fh.read()
    if isinstance(raw, dict):
        items = raw.get("plans", raw) if "plans" in raw else raw
        if isinstance(items, dict):
            items = [{"id": k, **v} for k, v in items.items()]
    elif isinstance(raw, str) and raw.strip():
        data = json.loads(raw)
        items = data.get("plans", data) if isinstance(data, dict) else data
    elif isinstance(raw, list):
        items = raw
    else:
        items = []
    plans: Dict[str, Plan] = {}
    for entry in items or []:
        plan = Plan(
            id=str(entry["id"]),
            name=str(entry.get("name", entry["id"])),
            price_usd=float(entry["price_usd"]),
            duration_days=int(entry.get("duration_days", PLAN_DURATION_DAYS)),
            features=list(entry.get("features") or []),
        )
        plans[plan.id] = plan
    if not plans:
        raise ValueError(
            "no plans configured: set PAYWATCH_PLANS_JSON or pass plans "
            "to Watcher; see examples/plans.example.json")
    return plans


# ---------------------------------------------------------------- orders

@dataclass
class Order:
    """One invoice. Status flow:
    pending -> confirming -> paid (unlocked)
    pending -> underpaid (seen, below threshold; still checkable)
    pending -> expired (window passed, nothing qualifying seen)
    any qualifying payment after expiry -> paid_late (recorded, NOT unlocked)
    any state -> manually_approved (owner fallback, unlocks)
    """
    id: str
    plan_id: str
    price_usd: float
    payer_address: str
    receive_address: str
    # Canonical amount in the asset's smallest unit. The historical
    # field name says sats; for ETH the value is wei. Generic aliases
    # below are the preferred names for new code.
    expected_sats: int
    created_at: datetime
    status: str = "pending"
    buyer_ref: str = ""  # host project's own user/customer id, opaque here
    seen_sats: int = 0
    seen_txid: Optional[str] = None
    txid: Optional[str] = None
    tx_received_sats: int = 0
    confirmations: int = 0
    paid_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    late_payment_note: Optional[str] = None
    asset: str = "btc"
    required_confirmations: int = 0
    provider: Optional[str] = None
    provider_error: Optional[str] = None
    provider_error_kind: Optional[str] = None
    last_checked_at: Optional[datetime] = None

    def __post_init__(self) -> None:
        self.asset = (self.asset or "btc").strip().lower()
        if self.required_confirmations <= 0:
            self.required_confirmations = REQUIRED_CONFIRMATIONS_BY_ASSET.get(
                self.asset, REQUIRED_CONFIRMATIONS)
        if self.expires_at is None:
            created = self.created_at
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            self.expires_at = created + timedelta(minutes=INVOICE_EXPIRY_MINUTES)

    @property
    def expected_amount(self) -> int:
        return self.expected_sats

    @expected_amount.setter
    def expected_amount(self, value: int) -> None:
        self.expected_sats = int(value)

    @property
    def expected_wei(self) -> int:
        return self.expected_sats

    @property
    def seen_amount(self) -> int:
        return self.seen_sats

    @seen_amount.setter
    def seen_amount(self, value: int) -> None:
        self.seen_sats = int(value)

    @property
    def seen_wei(self) -> int:
        return self.seen_sats

    @property
    def tx_received_amount(self) -> int:
        return self.tx_received_sats

    @tx_received_amount.setter
    def tx_received_amount(self, value: int) -> None:
        self.tx_received_sats = int(value)

    @property
    def tx_received_wei(self) -> int:
        return self.tx_received_sats

    @property
    def threshold_sats(self) -> int:
        return threshold_amount(self.expected_sats)

    @property
    def threshold_amount(self) -> int:
        return threshold_amount(self.expected_sats)

    @property
    def threshold_wei(self) -> int:
        return self.threshold_amount

    @property
    def shortfall_sats(self) -> int:
        return max(self.threshold_amount - self.seen_amount, 0)

    @property
    def shortfall_amount(self) -> int:
        return self.shortfall_sats

    @property
    def amount_unit(self) -> str:
        return ASSET_UNITS.get(self.asset, "smallest units")

    def public(self) -> Dict:
        """Buyer-safe payload. No secrets exist on this path, and the
        receive address is the host's public receive address only."""
        payload = {
            "id": self.id,
            "asset": self.asset,
            "symbol": self.asset.upper(),
            "plan_id": self.plan_id,
            "price_usd": self.price_usd,
            "status": self.status,
            "payer_address": self.payer_address,
            "receive_address": self.receive_address,
            "amount_unit": self.amount_unit,
            "expected_amount": self.expected_amount,
            "threshold_amount": self.threshold_amount,
            "seen_amount": self.seen_amount,
            "shortfall_amount": self.shortfall_amount,
            "tx_received_amount": self.tx_received_amount,
            "seen_txid": self.seen_txid,
            "txid": self.txid,
            "confirmations": self.confirmations,
            "required_confirmations": self.required_confirmations,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "paid_at": self.paid_at.isoformat() if self.paid_at else None,
            "last_checked_at": (self.last_checked_at.isoformat()
                                if self.last_checked_at else None),
            "provider": self.provider,
            "provider_error": self.provider_error,
            "provider_error_kind": self.provider_error_kind,
            "late_payment_note": self.late_payment_note,
        }
        # Legacy BTC names remain for existing host projects. For ETH,
        # the corresponding asset specific names carry wei values.
        if self.asset in ("eth", "bnb"):
            payload.update({
                "expected_wei": self.expected_amount,
                "threshold_wei": self.threshold_amount,
                "seen_wei": self.seen_amount,
                "shortfall_wei": self.shortfall_amount,
                "tx_received_wei": self.tx_received_amount,
            })
        elif self.asset == "sol":
            payload.update({
                "expected_lamports": self.expected_amount,
                "threshold_lamports": self.threshold_amount,
                "seen_lamports": self.seen_amount,
                "shortfall_lamports": self.shortfall_amount,
                "tx_received_lamports": self.tx_received_amount,
                "finalized": self.confirmations >= self.required_confirmations,
            })
        else:
            payload.update({
                "expected_sats": self.expected_amount,
                "threshold_sats": self.threshold_amount,
                "seen_sats": self.seen_amount,
                "shortfall_sats": self.shortfall_amount,
                "tx_received_sats": self.tx_received_amount,
            })
        return payload


# ---------------------------------------------------------------- amounts

def btc_to_sats(amount_btc: str) -> int:
    """Exact BTC decimal string -> sats (rounded down, never up)."""
    return int((Decimal(str(amount_btc)) * Decimal(100_000_000))
               .to_integral_value(rounding=ROUND_DOWN))


def usd_to_sats(price_usd: float, rate_usd_per_btc: float) -> int:
    """Pin an order: USD price / live BTC rate, 8dp rounded down,
    then to sats. Rounded down so a rounding edge never short-changes
    the buyer; the 1% tolerance covers the remaining slack."""
    return usd_to_amount(price_usd, rate_usd_per_btc, "btc")


def usd_to_amount(price_usd: float, rate_usd: float, asset: str = "btc") -> int:
    """Pin a USD price in an asset's smallest unit, rounded down.

    Integer smallest units are used after this conversion, so ETH and
    other EVM assets calculate in wei with no floating point amounts.
    Unknown future assets use 18 decimals only when a caller has not
    supplied a more specific conversion; registered host assets should
    pass their registry decimals through ``usd_to_amount_for_decimals``.
    """
    decimals = ASSET_DECIMALS.get((asset or "btc").lower(), 18)
    return usd_to_amount_for_decimals(price_usd, rate_usd, decimals)


def usd_to_amount_for_decimals(price_usd: float, rate_usd: float,
                               decimals: int) -> int:
    if rate_usd <= 0:
        raise ValueError("asset rate must be positive")
    quantum = Decimal(1).scaleb(-decimals)
    amount = (Decimal(str(price_usd)) / Decimal(str(rate_usd))).quantize(
        quantum, rounding=ROUND_DOWN)
    return int(amount * (Decimal(10) ** decimals))


def eth_to_wei(amount_eth: str) -> int:
    return int((Decimal(str(amount_eth)) * Decimal(10) ** 18)
               .to_integral_value(rounding=ROUND_DOWN))


def usd_to_wei(price_usd: float, rate_usd_per_eth: float) -> int:
    return usd_to_amount(price_usd, rate_usd_per_eth, "eth")


def threshold_sats(expected_sats: int) -> int:
    """Qualifying amount: expected minus the 1% tolerance."""
    return threshold_amount(expected_sats)


def threshold_amount(expected_amount: int) -> int:
    return expected_amount - expected_amount // 100


# ------------------------------------------------------- address check
# Bech32 validation (BIP-173), ported from the proven watcher. This
# is a UX guard only: mempool.space is the authority on whether an
# address exists on chain. Legacy base58 gets a charset/length check.

_B32 = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def _bech32_polymod(values: List[int]) -> int:
    chk = 1
    for v in values:
        top = chk >> 25
        chk = ((chk & 0x1FFFFFF) << 5) ^ v
        for i, g in enumerate((0x3B6A57B2, 0x26508E6D, 0x1EA119FA,
                               0x3D4233DD, 0x2A1462B3)):
            if (top >> i) & 1:
                chk ^= g
    return chk


def _bech32_hrp_expand(hrp: str) -> List[int]:
    return [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]


def _convertbits(data: List[int], frombits: int, tobits: int,
                 pad: bool = True) -> Optional[List[int]]:
    acc = 0
    bits = 0
    out: List[int] = []
    maxv = (1 << tobits) - 1
    for v in data:
        acc = (acc << frombits) | v
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            out.append((acc >> bits) & maxv)
    if pad:
        if bits:
            out.append((acc << (tobits - bits)) & maxv)
    elif bits >= frombits or ((acc << (tobits - bits)) & maxv):
        return None
    return out


def valid_btc_address(addr: Optional[str]) -> bool:
    if not addr or not isinstance(addr, str):
        return False
    s = addr.strip()
    if s.startswith("bc1"):
        low = s.lower()
        if low != s and s.upper() != s:
            return False
        pos = low.rfind("1")
        if pos < 1 or pos + 7 > len(low):
            return False
        hrp, part = low[:pos], low[pos + 1:]
        if hrp != "bc":
            return False
        try:
            data = [_B32.index(c) for c in part]
        except ValueError:
            return False
        if _bech32_polymod(_bech32_hrp_expand(hrp) + data) not in (1, 0x2BC830A3):
            return False
        version = data[0]
        if (s.startswith("bc1q") and version != 0) or \
           (s.startswith("bc1p") and version != 1):
            return False
        prog = _convertbits(data[1:-6], 5, 8, pad=False)
        if prog is None:
            return False
        if version == 0:
            return len(prog) in (20, 32)
        return len(prog) == 32
    if s[0] in ("1", "3") and 26 <= len(s) <= 35:
        b58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
        return all(c in b58 for c in s)
    return False


def valid_eth_address(addr: Optional[str]) -> bool:
    """Shared EVM address format check: 0x plus 40 hex characters."""
    if not addr or not isinstance(addr, str):
        return False
    s = addr.strip()
    if len(s) != 42 or not s.startswith(("0x", "0X")):
        return False
    try:
        int(s[2:], 16)
    except ValueError:
        return False
    return True


def valid_sol_address(addr: Optional[str]) -> bool:
    """Base58 Solana address check, including a 32 byte decode."""
    if not addr or not isinstance(addr, str):
        return False
    s = addr.strip()
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    if not (32 <= len(s) <= 44) or any(c not in alphabet for c in s):
        return False
    value = 0
    for char in s:
        value = value * 58 + alphabet.index(char)
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big") if value else b""
    leading = len(s) - len(s.lstrip("1"))
    return leading + len(raw) == 32


# ------------------------------------------------------- receive pools

def _clean_address_list(values) -> List[str]:
    out: List[str] = []
    seen = set()
    for value in values or []:
        address = str(value).strip()
        if not address:
            continue
        # EVM addresses are case insensitive. Other assets, notably
        # BTC, are not normalized because their encodings can matter.
        key = address.lower() if valid_eth_address(address) else address
        if key not in seen:
            seen.add(key)
            out.append(address)
    return out


# ------------------------------------------------- shipped default pools
#
# The owner ships his real PUBLIC receive addresses in
# default_addresses.json, next to this module. Receive addresses are
# public by design: every buyer is shown the assigned one at checkout,
# so they are configuration, not secrets. Private keys and seeds are a
# different matter entirely and must NEVER be committed anywhere.
#
# Precedence, per asset: explicit environment configuration wins when
# the asset is configured there at all (an explicitly empty env pool
# closes that asset's checkout); otherwise the shipped default pool
# below applies, so a fresh checkout works out of the box.

_DEFAULT_VALIDATORS = {
    "btc": valid_btc_address,
    "eth": valid_eth_address,
    "bnb": valid_eth_address,
    "sol": valid_sol_address,
}

_cached_default_pools: Optional[Dict[str, List[str]]] = None


def default_address_pools() -> Dict[str, List[str]]:
    """The shipped default receive pools from default_addresses.json."""
    global _cached_default_pools
    if _cached_default_pools is None:
        path = Path(__file__).with_name("default_addresses.json")
        parsed = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(parsed, dict):
            raise ValueError("default_addresses.json must be a JSON object")
        pools: Dict[str, List[str]] = {}
        for asset, values in parsed.items():
            if isinstance(values, str):
                values = [values]
            key = str(asset).lower()
            cleaned = _clean_address_list(values)
            validator = _DEFAULT_VALIDATORS.get(key)
            if validator is not None:
                for address in cleaned:
                    if not validator(address):
                        raise ValueError(
                            f"invalid shipped default address for {key}")
            pools[key] = cleaned
        _cached_default_pools = pools
    return {asset: list(values)
            for asset, values in _cached_default_pools.items()}


def load_address_pools(env: Optional[Dict[str, str]] = None) -> Dict[str, List[str]]:
    """Load receive address pools, env first, shipped defaults as fallback.

    Canonical format is PAYWATCH_ADDRESSES as JSON by asset:
    ``{"btc": ["addr1", "addr2"], "eth": ["0x..."]}``.

    Per-asset comma separated variables are also accepted:
    PAYWATCH_BTC_ADDRESSES, PAYWATCH_ETH_ADDRESSES, and generically
    PAYWATCH_<ASSET>_ADDRESSES. Legacy PAYWATCH_BTC_ADDRESS is a one
    address BTC pool. Sources for the same asset are combined in JSON,
    per-asset env, legacy order, with duplicates removed. The same EVM
    address may appear under multiple EVM assets; that is valid and
    is not treated as a configuration error.

    An asset that the environment configures at all, even with an
    explicitly empty pool, is governed by the environment alone (an
    empty pool is how an operator closes one asset's checkout). Every
    asset the environment does not mention falls back to the shipped
    default pool in default_addresses.json.
    """
    source = os.environ if env is None else env
    pools: Dict[str, List[str]] = {}
    mentioned = set()
    raw_json = source.get("PAYWATCH_ADDRESSES", "").strip()
    if raw_json:
        try:
            parsed = json.loads(raw_json)
        except json.JSONDecodeError as exc:
            raise ValueError("PAYWATCH_ADDRESSES is not valid JSON") from exc
        if not isinstance(parsed, dict):
            raise ValueError("PAYWATCH_ADDRESSES must be a JSON object")
        for asset, values in parsed.items():
            if isinstance(values, str):
                values = [values]
            key = str(asset).lower()
            mentioned.add(key)
            pools[key] = _clean_address_list(values)
    # Per asset variables can cover registered and future assets. Scan
    # deterministically from the environment names that are present.
    for name, value in sorted(source.items()):
        if not name.startswith("PAYWATCH_") or not name.endswith("_ADDRESSES"):
            continue
        if name == "PAYWATCH_ADDRESSES":
            continue
        asset = name[len("PAYWATCH_"):-len("_ADDRESSES")].lower()
        mentioned.add(asset)
        additions = _clean_address_list(value.split(","))
        pools[asset] = _clean_address_list((pools.get(asset) or []) + additions)
    legacy = source.get("PAYWATCH_BTC_ADDRESS", "").strip()
    if legacy:
        mentioned.add("btc")
        pools["btc"] = _clean_address_list((pools.get("btc") or []) + [legacy])
    for asset, values in default_address_pools().items():
        if asset not in mentioned and not pools.get(asset):
            pools[asset] = values
    return pools


def address_pool(asset: str, env: Optional[Dict[str, str]] = None) -> List[str]:
    return load_address_pools(env).get((asset or "btc").lower(), [])


# ---------------------------------------------------------------- matching

def tx_received_sats(tx: Dict, receive_addr: str) -> int:
    return sum(int(v.get("value") or 0) for v in (tx.get("vout") or [])
               if v.get("scriptpubkey_address") == receive_addr)


def tx_spends_from(tx: Dict, payer_address: Optional[str]) -> bool:
    if not payer_address:
        return False
    for vin in tx.get("vin") or []:
        prev = vin.get("prevout") or {}
        if prev.get("scriptpubkey_address") == payer_address:
            return True
    return False


def tx_confirmations(tx: Dict, tip_height: Optional[int]) -> int:
    status = tx.get("status") or {}
    if status.get("confirmed") and status.get("block_height") is not None \
            and tip_height is not None:
        return max(tip_height - int(status["block_height"]) + 1, 0)
    return 0


def _address_value(value) -> str:
    if isinstance(value, dict):
        return str(value.get("hash") or value.get("address") or "")
    return str(value or "")


def _int_value(value, default: int = 0) -> int:
    try:
        if isinstance(value, str) and value.lower().startswith("0x"):
            return int(value, 16)
        return int(value)
    except (TypeError, ValueError):
        return default


def match_account_payment(txs: List[Dict], payer_address: Optional[str],
                          receive_addr: str, expected_amount: int,
                          *, lowercase_txid: bool = True,
                          case_insensitive_addresses: bool = True) -> Dict:
    """Pure matcher for normalized account chain transactions.

    The provider normalization contract is txid, from_address,
    to_address, value_wei (or value_amount), and confirmations. This
    is the single matcher used by every EVM asset. Address comparison
    is case insensitive, while the declared payer must match exactly
    apart from EVM hex casing.
    """
    need = threshold_amount(expected_amount)
    best_seen = 0
    best_txid: Optional[str] = None
    candidates: List[Dict] = []
    def normalize_address(value: str) -> str:
        value = value.strip()
        return value.lower() if case_insensitive_addresses else value

    payer = normalize_address(payer_address or "")
    receive = normalize_address(receive_addr or "")
    for tx in txs or []:
        txid = tx.get("txid") or tx.get("hash")
        to_address = normalize_address(_address_value(
            tx.get("to_address", tx.get("to"))))
        if not txid or to_address != receive:
            continue
        from_address = normalize_address(_address_value(
            tx.get("from_address", tx.get("from"))))
        if not payer or from_address != payer:
            continue
        value = _int_value(tx.get("value_wei", tx.get(
            "value_lamports", tx.get("value_amount", tx.get("value")))), 0)
        if value <= 0:
            continue
        if value > best_seen:
            best_seen = value
            best_txid = str(txid)
        if value < need:
            continue
        if "confirmations" in tx:
            confirmations = _int_value(tx.get("confirmations"), 0)
        elif "finalized" in tx:
            confirmations = 1 if tx.get("finalized") else 0
        else:
            confirmations = 0
        candidates.append({
            "txid": str(txid).lower() if lowercase_txid else str(txid),
            "received_sats": value,
            "received_amount": value,
            "confirmations": confirmations,
        })
    candidates.sort(key=lambda c: (c["confirmations"], c["received_amount"]),
                    reverse=True)
    return {"match": candidates[0] if candidates else None,
            "best_seen_sats": best_seen,
            "best_seen_amount": best_seen,
            "best_seen_txid": best_txid}


def match_payment(txs: List[Dict], payer_address: Optional[str],
                  receive_addr: str, expected_sats: int,
                  tip_height: Optional[int] = None,
                  asset: str = "btc",
                  transaction_family: Optional[str] = None) -> Dict:
    """Pure payment matcher over one explorer transaction list.

    Returns {"match": candidate|None, "best_seen_sats": int,
    "best_seen_txid": str|None}. A UTXO candidate must spend from the
    declared payer address AND send at least the threshold amount to
    the receive address. Change outputs back to the payer do not
    count: only outputs TO the receive address count. A payment from
    an exchange hot wallet, or any address other than the declared
    one, does NOT match. Account family assets use the normalized
    EVM matcher above; no individual EVM chain is special cased here.
    """
    family = transaction_family or (
        "account" if (asset or "btc").lower() in ("eth", "bnb")
        else "solana-rpc" if (asset or "btc").lower() == "sol" else "utxo")
    if family in ("account", "solana-rpc"):
        return match_account_payment(
            txs, payer_address, receive_addr, expected_sats,
            lowercase_txid=(family == "account"),
            case_insensitive_addresses=(family == "account"))
    need = threshold_amount(expected_sats)
    best_seen_sats = 0
    best_seen_txid: Optional[str] = None
    candidates: List[Dict] = []
    for tx in txs or []:
        to_receive = tx_received_sats(tx, receive_addr)
        if to_receive <= 0:
            continue
        if not tx_spends_from(tx, payer_address):
            continue
        if to_receive > best_seen_sats:
            best_seen_sats = to_receive
            best_seen_txid = tx.get("txid")
        if to_receive < need:
            continue
        candidates.append({
            "txid": tx.get("txid"),
            "received_sats": to_receive,
            "received_amount": to_receive,
            "confirmations": tx_confirmations(tx, tip_height),
        })
    candidates.sort(key=lambda c: (c["confirmations"], c["received_amount"]),
                    reverse=True)
    return {"match": candidates[0] if candidates else None,
            "best_seen_sats": best_seen_sats,
            "best_seen_amount": best_seen_sats,
            "best_seen_txid": best_seen_txid}

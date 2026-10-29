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
from typing import Dict, List, Optional

REQUIRED_CONFIRMATIONS = 2
INVOICE_EXPIRY_MINUTES = 60
TOLERANCE_PCT = 1.0
PLAN_DURATION_DAYS = 30

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

    def __post_init__(self) -> None:
        if self.expires_at is None:
            created = self.created_at
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            self.expires_at = created + timedelta(minutes=INVOICE_EXPIRY_MINUTES)

    @property
    def threshold_sats(self) -> int:
        return threshold_sats(self.expected_sats)

    @property
    def shortfall_sats(self) -> int:
        return max(self.threshold_sats - self.seen_sats, 0)

    def public(self) -> Dict:
        """Buyer-safe payload. No secrets exist on this path, and the
        receive address is the host's public receive address only."""
        return {
            "id": self.id,
            "plan_id": self.plan_id,
            "price_usd": self.price_usd,
            "status": self.status,
            "expected_sats": self.expected_sats,
            "threshold_sats": self.threshold_sats,
            "seen_sats": self.seen_sats,
            "shortfall_sats": self.shortfall_sats,
            "seen_txid": self.seen_txid,
            "txid": self.txid,
            "confirmations": self.confirmations,
            "required_confirmations": REQUIRED_CONFIRMATIONS,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "paid_at": self.paid_at.isoformat() if self.paid_at else None,
            "late_payment_note": self.late_payment_note,
        }


# ---------------------------------------------------------------- amounts

def btc_to_sats(amount_btc: str) -> int:
    """Exact BTC decimal string -> sats (rounded down, never up)."""
    return int((Decimal(str(amount_btc)) * Decimal(100_000_000))
               .to_integral_value(rounding=ROUND_DOWN))


def usd_to_sats(price_usd: float, rate_usd_per_btc: float) -> int:
    """Pin an order: USD price / live BTC rate, 8dp rounded down,
    then to sats. Rounded down so a rounding edge never short-changes
    the buyer; the 1% tolerance covers the remaining slack."""
    if rate_usd_per_btc <= 0:
        raise ValueError("BTC rate must be positive")
    btc = (Decimal(str(price_usd)) / Decimal(str(rate_usd_per_btc))).quantize(
        Decimal("0.00000001"), rounding=ROUND_DOWN)
    return btc_to_sats(str(btc))


def threshold_sats(expected_sats: int) -> int:
    """Qualifying amount: expected minus the 1% tolerance."""
    return expected_sats - expected_sats // 100


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


def match_payment(txs: List[Dict], payer_address: Optional[str],
                  receive_addr: str, expected_sats: int,
                  tip_height: Optional[int] = None) -> Dict:
    """Pure payment matcher over one explorer transaction list.

    Returns {"match": candidate|None, "best_seen_sats": int,
    "best_seen_txid": str|None}. A candidate must spend from the
    declared payer address AND send at least the threshold amount to
    the receive address. Change outputs back to the payer do not
    count: only outputs TO the receive address count. A payment from
    an exchange hot wallet, or any address other than the declared
    one, does NOT match.
    """
    need = threshold_sats(expected_sats)
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
            "confirmations": tx_confirmations(tx, tip_height),
        })
    candidates.sort(key=lambda c: (c["confirmations"], c["received_sats"]),
                    reverse=True)
    return {"match": candidates[0] if candidates else None,
            "best_seen_sats": best_seen_sats,
            "best_seen_txid": best_seen_txid}

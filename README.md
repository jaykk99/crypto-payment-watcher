# crypto-payment-watcher

A standalone, reusable BTC payment watcher. Drop it into any project
that wants to sell something for Bitcoin without a card processor,
without wallet connections, and without ever touching a private key.

Extracted cleanly from the watcher already proven in ReconAnything
(`backend/app/paywatch.py`, PR #43) and monico-agent
(`backend/btc_checkout.py`, PR #10). Same rules, no project-specific
code, no hardcoded plans, no hardcoded receive address.

## What it does

1. Your project defines its own plans (price in USD, duration).
2. A buyer picks a plan and enters the wallet address they will
   send **from**.
3. The order pins the BTC amount at the live CoinGecko rate.
4. The buyer pays manually from their own wallet app to **your**
   public receive address.
5. The watcher polls public mempool.space (no key, read-only) and
   unlocks the plan when a qualifying payment confirms.
6. Your project's `on_unlock` callback grants the entitlement:
   a tier flag, an access code, a subscription extension. This
   package records `paid` + `paid_at`; what a plan *means* is the
   host project's job.

## The rules (these are the whole point)

- **2 confirmations** before unlock. 0-1 confirmations = confirming,
  not unlocked.
- **60-minute invoice window.** A qualifying payment that confirms
  after expiry is recorded as `paid_late` with **no auto-unlock**.
- **1% tolerance.** At least 99% of the pinned amount qualifies.
  Below that = underpayment, no unlock, shortfall shown in sats.
- **Payer binding.** At least one transaction input must spend from
  the buyer's declared address, and the amount sent **to** your
  receive address is what counts. Change back to the payer does not
  count. A payment from an exchange hot wallet or any other address
  does not match.
- **Single-use txid.** A txid already claimed by another order is
  never reused.
- **Overpayment unlocks the picked tier only.** The excess is **not**
  refunded automatically.
- **Owner manual approve** is the fallback for payments the watcher
  cannot match (old payment outside the mempool pagination window,
  paid late, or any other edge case).

## What it does NOT do

- No custody. The app never holds, sends, or moves crypto.
- No wallet connection, no signing, no private key, no seed, ever.
- No auto-refund.
- No card processor, no third-party checkout.
- BTC only in v1.

## Quickstart

```bash
pip install -e .            # core is stdlib only; fastapi optional
export PAYWATCH_BTC_ADDRESS="<your project's public receive address>"
export PAYWATCH_PLANS_JSON='{"plans":[{"id":"starter","name":"Starter","price_usd":19}]}'
python3 examples/server.py  # stdlib demo server on :8000
```

```python
from paywatch import Watcher

def grant(order, tx):
    # YOUR entitlement logic: set a tier, mint an access code, etc.
    print(f"grant {order.plan_id} to {order.buyer_ref} via {tx['txid']}")

watcher = Watcher(plans=[{"id": "starter", "name": "Starter", "price_usd": 19}],
                  on_unlock=grant)
order = watcher.create_order("starter", payer_address="<buyer declared address>")
print(order.public())  # pinned sats, receive address, expiry
result = watcher.check_order(order.id)  # polls mempool.space
```

FastAPI version: see `src/paywatch/api.py` (`create_app(watcher)`).
It needs `pip install fastapi` and is optional; the core does not.

## Environment variables

| Variable | Required | Meaning |
|---|---|---|
| `PAYWATCH_BTC_ADDRESS` | Yes | Your project's public BTC receive address. Empty = checkout honestly closed (503), never an invented address. |
| `PAYWATCH_PLANS_JSON` | Yes, unless plans passed in code | JSON plans: `{"plans":[{"id","name","price_usd","duration_days","features"}]}`. |
| `PAYWATCH_PLANS_FILE` | Alternative | Path to a JSON plans file (see `examples/plans.example.json`). |
| `PAYWATCH_CRON_SECRET` | Optional | If set, `approve_order` requires it. If unset, the host must gate approval itself (admin session). |
| `MEMPOOL_BASE_URL` | Optional | Default `https://mempool.space/api`. |
| `COINGECKO_URL` | Optional | Default CoinGecko simple price endpoint. |

No wallet address, private key, seed, or API key is ever stored in
code, README, tests, or fixtures. Test fixtures use plain fake
strings plus one public BIP-173 test vector, clearly marked.

## Apply it to a new project (under 10 steps)

1. Vendor or install this package.
2. Set `PAYWATCH_BTC_ADDRESS` to that project's receive address.
3. Set `PAYWATCH_PLANS_JSON` (or pass plans in code) to that
   project's own tiers. The Recon ($19/$49/$149) and monico
   ($149/$499) tiers in `examples/plans.example.json` are examples
   only, not defaults.
4. Write an `on_unlock(order, tx)` callback that grants that
   project's entitlement for `order.plan_id`.
5. Use `SQLiteStore` (durable) in real use; `MemoryStore` is for
   tests and demos only. The in-memory-only orders limitation from
   monico-agent is fixed by the SQLite store: a restart no longer
   loses pending orders.
6. Expose create / get / check / approve, copying `api.py` or
   `examples/server.py`.
7. Call `watcher.sweep()` from your cron or background loop.
8. Show the buyer, honestly: receive address, pinned sats, the
   60-minute window, 2 confirmations, 99% tolerance, and that
   late / underpaid / reused payments do not auto-unlock.
9. Test with the suite below before going live.
10. Prove one real test order end to end before advertising paid
    tiers as live.

## Honest limits

- **mempool.space pagination:** 25 txs per page, 5 pages checked
  (125 recent transactions on the receive address). Older payments
  fall back to owner manual approval.
- **mempool.space / CoinGecko rate limits:** both are public,
  keyless, and rate-limited. A failed rate fetch fails order
  creation honestly; a failed explorer fetch records
  `explorer_error` and unlocks nothing.
- **BTC only in v1.** No other coin, no Lightning, no token.
- **Buyer must send from a wallet they control**, from the declared
  address. Exchange withdrawals that come from a hot wallet will not
  match; say so in your buyer copy.
- **In-memory store loses orders on restart.** Use SQLiteStore.

## Tests

```bash
python3 -m pytest tests/ -q
```

Covers: pinning/tolerance, payer binding, txid reuse, expiry /
paid_late, confirmation counting, underpayment, overpayment
(unlocks tier only), wrong recipient, change outputs, address
validation, checkout-closed state, SQLite roundtrip, and the unlock
callback. No network calls in tests.

## Layout

```
src/paywatch/
  core.py     amounts, plans, address validation, pure matcher
  mempool.py  mempool.space + CoinGecko clients (stdlib urllib)
  store.py    MemoryStore + SQLiteStore, txid claim guard
  watcher.py  create / check / sweep / manual approve + callback
  api.py      optional FastAPI surface
examples/
  server.py            stdlib demo server
  minimal_integration.py  6-step host example
  plans.example.json   example tiers only (Recon + monico prices)
tests/
  test_paywatch.py     offline unit tests
```

## License

MIT.

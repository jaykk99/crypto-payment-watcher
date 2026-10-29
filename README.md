# crypto-payment-watcher

A standalone, reusable multi-asset crypto payment watcher. Drop it
into any project that wants to sell something for crypto without a
card processor, without wallet connections, and without ever
touching a private key.

v2 keeps the proven BTC rules and adds receive address pools, ETH,
BNB Chain, Solana, provider failover, and an honest busy state when
one asset cannot safely take a new invoice.

## What it does

1. Your project defines its own plans (price in USD, duration).
2. A buyer picks a plan and a crypto, then enters the wallet address
   they will send from.
3. The watcher checks that the asset has a fresh or acceptably stale
   price and at least one working chain provider.
4. The order pins the amount at the live rate and assigns the pool
   address with the fewest active invoices for that asset.
5. The buyer pays manually from their own wallet app to the assigned
   public receive address.
6. The watcher polls keyless public providers and unlocks the plan
   when a qualifying payment reaches that asset's confirmation rule.
7. Your project's `on_unlock` callback grants the entitlement: a
   tier flag, an access code, a subscription extension. This package
   records `paid` plus `paid_at`; what a plan means is the host
   project's job.

## Supported assets

| Asset | Key | Smallest unit | Price id | Default confirmation rule |
|---|---|---:|---|---|
| Bitcoin | `btc` | sats | `bitcoin` | 2 confirmations |
| Ethereum | `eth` | wei | `ethereum` | 12 confirmations |
| BNB Chain | `bnb` | wei | `binancecoin` | 15 confirmations |
| Solana | `sol` | lamports | `solana` | finalized required |

Confirmation defaults can be changed with
`PAYWATCH_BTC_CONFIRMATIONS`, `PAYWATCH_ETH_CONFIRMATIONS`,
`PAYWATCH_BNB_CONFIRMATIONS`, and `PAYWATCH_SOL_CONFIRMATIONS`.
For Solana the value is a gate rather than a displayed block count:
a positive value means finalized is required, and an unfinalized
candidate stays pending.

ETH and BNB are one EVM asset family. They share address validation,
wei math, the normalized transaction matcher, and one Blockscout
provider implementation. Only registry data changes: explorer base,
price id, native symbol, and confirmation default. The same EVM
address may be configured in both pools at once.

BNB status note: the BNB registry entry and mocked payment flow are
complete, but a live check on 2026-10-29 received HTTP 404 from the
public `bsc.blockscout.com` API paths. BNB therefore fails closed as
`ASSET_UNAVAILABLE` until `BLOCKSCOUT_BNB_BASE_URL` points at a
working Blockscout instance for BNB Chain. Do not advertise BNB
checkout as live before a real provider check passes.

Solana is a separate RPC family. Its provider reads signatures for
the receive address, loads candidate transactions, checks a
SystemProgram transfer or the receive account balance delta, binds
the source to the declared payer, and only treats finalized
transactions as confirmed.

## The rules (these are the whole point)

- **60-minute invoice window.** A qualifying payment that confirms
  after expiry is recorded as `paid_late` with no auto-unlock.
- **1% tolerance.** At least 99% of the pinned amount qualifies.
  Below that is underpayment, no unlock, and the shortfall is shown
  in the asset's smallest unit.
- **Payer binding.** The on-chain source must be the buyer's declared
  address. For BTC, at least one input must spend from that address
  and only outputs to the assigned receive address count. For EVM
  assets, address comparison is case insensitive. For Solana, the
  transfer source or fee payer must match exactly.
- **Single-use transaction id.** A txid, EVM hash, or Solana
  signature already claimed by another order is never reused, across
  all assets.
- **Overpayment unlocks the picked tier only.** The excess is not
  refunded automatically.
- **Owner manual approve** is the fallback for payments the watcher
  cannot match, including a payment outside an explorer's recent
  transaction window, a late payment, and other edge cases.
- **No custody.** The app never holds, sends, signs, or moves
  crypto. It never accepts a private key or seed.

## Receive address pools

Pools resolve per asset with this precedence:

1. **Environment configuration wins** for every asset it configures.
   Set any of the variables below and they govern that asset. An
   explicitly empty env pool (for example `PAYWATCH_ADDRESSES` with
   `{"btc": []}`) closes that asset's checkout.
2. **Shipped defaults fill the rest.** Assets the environment does
   not mention fall back to the default pools in
   `src/paywatch/default_addresses.json`, so a fresh checkout has
   working pools for BTC, ETH, BNB, and SOL out of the box.

The shipped defaults are the owner's real **public** receive
addresses, committed on his direct order. That is safe for receive
addresses: they are shown to every buyer at checkout, so they are
public configuration, not secrets. (The same ETH pool address also
serves BNB; it is one multi-chain EVM address.) What must never be
committed, here or anywhere, is a private key or seed. This app
never accepts one in the first place.

Canonical env format:

```bash
export PAYWATCH_ADDRESSES='{"btc":["btc-receive-1","btc-receive-2"],"eth":["evm-receive-1"],"bnb":["evm-receive-1"],"sol":["sol-receive-1"]}'
```

Per-asset comma separated variables are also accepted:

```bash
export PAYWATCH_BTC_ADDRESSES="btc-receive-1,btc-receive-2"
export PAYWATCH_ETH_ADDRESSES="evm-receive-1,evm-receive-2"
export PAYWATCH_BNB_ADDRESSES="evm-receive-1"
export PAYWATCH_SOL_ADDRESSES="sol-receive-1,sol-receive-2"
```

The legacy variable still works as a one address BTC pool:

```bash
export PAYWATCH_BTC_ADDRESS="btc-receive-1"
```

Sources for one asset are combined and deduplicated. Invoice
creation picks the pool address with the fewest currently active
invoices (unexpired and unpaid). Ties rotate. The assigned address
is stored on the invoice, including in SQLite.

An asset with an empty pool reports checkout closed for that asset.
That is an honest closed state, not a server error and not an
invented address.

A pool reduces address reuse; it does not eliminate it. With a fixed
pool, addresses will repeat over time and an observer can still link
payments sent to the same pool address. The future privacy step is
xpub based derivation, where each invoice can receive a fresh
address without putting a private key in this app. xpub derivation
is intentionally not in this pass.

## Provider failover and the busy state

### Chain providers

- BTC: mempool.space first, blockstream.info second. Both are
  keyless Esplora APIs and are normalized to the same transaction
  shape.
- EVM: Blockscout is the keyless provider, parameterized per chain
  (`eth.blockscout.com`, `bsc.blockscout.com`, or another EVM
  Blockscout instance from the registry). No second genuinely
  independent keyless EVM indexer is wired in this pass. The
  provider list is structured so one can be added without changing
  watcher logic.
- Solana: the public Solana JSON-RPC at
  `api.mainnet-beta.solana.com` first and the keyless PublicNode RPC
  at `solana-rpc.publicnode.com` second.

### Price providers

CoinGecko is primary. Coinbase public spot prices and Kraken public
tickers are keyless fallbacks. Prices are cached for 60 seconds.
During a brief outage, a cached price up to 10 minutes old may be
used and is marked `price_stale: true` in asset status. The stale
limit can be changed with `PAYWATCH_PRICE_STALE_MAX_AGE_S`.

### Asset availability

An asset is unavailable for new invoices when either:

- no fresh or acceptably stale price can be fetched, or
- every chain provider for that asset is erroring or rate limited.

Invoice creation then returns HTTP 503 with this stable shape:

```json
{
  "error": "BTC payments are too busy right now. Please pick another crypto.",
  "code": "ASSET_UNAVAILABLE",
  "provider_errors": [
    {"provider": "mempool.space", "kind": "rate_limited", "status": 429, "error": "HTTP 429"}
  ]
}
```

The symbol changes to match the requested asset. `GET /assets`
returns availability for every registered asset, and `GET /plans`
returns availability for configured assets, so a checkout page can
grey out the busy asset and let the buyer pick another crypto.

Existing invoices are never abandoned because a provider is busy.
The sweep keeps polling with whichever providers respond. Provider
failures are recorded on the check result and on the invoice as
`provider_error` and `provider_error_kind`, without unlocking
anything. Error kinds are `rate_limited`, `unreachable`, and
`bad_response`.

Failover reduces rate limit fragility. It does not remove it: if
every provider for an asset is down or throttled at the same time,
new invoices for that asset pause honestly instead of being created
against data the watcher cannot verify.

## Quickstart

```bash
pip install -e .            # core is stdlib only; fastapi optional
export PAYWATCH_ADDRESSES='{"btc":["<project btc receive address>"],"eth":["<project evm receive address>"],"sol":["<project sol receive address>"]}'
export PAYWATCH_PLANS_JSON='{"plans":[{"id":"starter","name":"Starter","price_usd":19}]}'
python3 examples/server.py  # stdlib demo server on :8000
```

```python
from paywatch import Watcher

def grant(order, tx):
    # YOUR entitlement logic: set a tier, mint an access code, etc.
    print(f"grant {order.plan_id} to {order.buyer_ref} via {tx['txid']}")

watcher = Watcher(
    plans=[{"id": "starter", "name": "Starter", "price_usd": 19}],
    on_unlock=grant)
order = watcher.create_order(
    "starter", payer_address="<buyer declared address>", asset="eth")
print(order.public())  # pinned wei, assigned receive address, expiry
result = watcher.check_order(order.id)
assets = watcher.asset_statuses()  # availability for checkout UI
```

FastAPI version: see `src/paywatch/api.py` (`create_app(watcher)`).
It needs `pip install fastapi` and is optional; the core does not.

## Checkout page

A ready-made buyer page ships with the package at
`src/paywatch/static/checkout.html`. Both `api.py` and
`examples/server.py` serve it at `GET /checkout`.

Link a buyer to it with the plan already chosen by your project:

    /checkout?plan=pro&ref=<buyer id>&return=https://yourapp/account

| Param | Meaning |
|---|---|
| `plan` | required; the plan id whose USD price is charged |
| `ref` | optional; stored as `buyer_ref` so `on_unlock` knows who paid |
| `return` | optional; where the Continue button goes after payment |
| `api` | optional; base URL of the JSON routes if not same origin |

Flow on the page: it fetches `/plans` for the price and coin
availability, the buyer picks a coin and pastes the wallet they will
send from, `POST /orders` locks the amount and assigns a receive
address, then the page shows the amount, a QR code, the address and a
live countdown to `expires_at`. It calls `POST /orders/{id}/check`
every 30 seconds (and when the tab comes back into view) and switches
to confirmed, underpaid or expired from the server's answer. An open
invoice survives a page refresh. The page never asks for a wallet
connection or a key.

## Environment variables

| Variable | Required | Meaning |
|---|---|---|
| `PAYWATCH_ADDRESSES` | Optional override | JSON object of receive address pools by asset key. Overrides the shipped defaults for the assets it names. |
| `PAYWATCH_BTC_ADDRESSES` | Optional | Comma separated BTC receive pool. |
| `PAYWATCH_ETH_ADDRESSES` | Optional | Comma separated Ethereum receive pool. |
| `PAYWATCH_BNB_ADDRESSES` | Optional | Comma separated BNB Chain receive pool. |
| `PAYWATCH_SOL_ADDRESSES` | Optional | Comma separated Solana receive pool. |
| `PAYWATCH_BTC_ADDRESS` | Optional legacy | One address BTC pool. |
| `PAYWATCH_PLANS_JSON` | Yes, unless plans passed in code | JSON plans: `{"plans":[{"id","name","price_usd","duration_days","features"}]}`. |
| `PAYWATCH_PLANS_FILE` | Alternative | Path to a JSON plans file (see `examples/plans.example.json`). |
| `PAYWATCH_CRON_SECRET` | Optional | If set, `approve_order` requires it. If unset, the host must gate approval itself. |
| `PAYWATCH_BTC_CONFIRMATIONS` | Optional | Positive integer. Default 2. |
| `PAYWATCH_ETH_CONFIRMATIONS` | Optional | Positive integer. Default 12. |
| `PAYWATCH_BNB_CONFIRMATIONS` | Optional | Positive integer. Default 15. |
| `PAYWATCH_SOL_CONFIRMATIONS` | Optional | Positive value requires finalized. Default 1. |
| `PAYWATCH_PRICE_STALE_MAX_AGE_S` | Optional | Maximum stale price age in seconds. Default 600. |
| `MEMPOOL_BASE_URL` | Optional | Default `https://mempool.space/api`. |
| `BLOCKSTREAM_BASE_URL` | Optional | Default `https://blockstream.info/api`. |
| `BLOCKSCOUT_ETH_BASE_URL` | Optional | Host level override pattern for an EVM explorer base. Registry values are used by default. |
| `SOLANA_RPC_URL` | Optional | Primary Solana RPC override for a single provider instance. Production failover uses the two public endpoints below unless a host injects its own stack. |
| `SOLANA_FALLBACK_RPC_URL` | Optional | Default `https://solana-rpc.publicnode.com`. |
| `COINGECKO_URL` | Optional | Legacy BTC CoinGecko URL override. |
| `COINGECKO_BASE_URL` | Optional | Default CoinGecko simple price base. |
| `COINBASE_BASE_URL` | Optional | Default `https://api.coinbase.com/v2`. |
| `KRAKEN_BASE_URL` | Optional | Default `https://api.kraken.com/0/public`. |

No private key, seed, or API key is ever stored in code, README,
tests, or fixtures. Test fixtures use fake placeholder addresses and
hashes, except the shipped-defaults tests, which assert the owner's
public receive addresses from `default_addresses.json` (see the
Receive address pools section for why those are public).

## Adding another crypto

The asset and provider layers are registries, not asset specific
branches in the watcher.

1. Register one `AssetSpec` with `register_asset`: asset key, native
   symbol, CoinGecko price id, fallback price market, smallest unit
   name, decimals, address validator, transaction family, and
   default confirmation rule.
2. Add a provider factory to that registry entry, or inject a
   fetcher for the asset when constructing `Watcher`. A provider
   returns either an Esplora UTXO transaction list for the `utxo`
   family, or normalized account transactions with `txid`,
   `from_address`, `to_address`, `value_amount`, and
   `confirmations`.
3. Give that asset a receive pool: add it to the shipped defaults in
   `default_addresses.json`, or configure it in the environment.
4. Add mocked provider tests for invoice creation, matching, payer
   mismatch, and provider failure.

For another EVM chain, step 2 is normally just a registry entry
pointing at that chain's Blockscout instance. The existing EVM
provider, matcher, pool logic, availability logic, and watcher flow
are reused unchanged. That is exactly how BNB Chain ships in v2.

## Apply it to a new project (under 10 steps)

1. Vendor or install this package.
2. Set `PAYWATCH_ADDRESSES` with that project's own public receive
   pools for only the assets it wants to offer.
3. Set `PAYWATCH_PLANS_JSON` (or pass plans in code) to that
   project's own tiers. Prices in the example file are examples
   only, not defaults.
4. Write an `on_unlock(order, tx)` callback that grants that
   project's entitlement for `order.plan_id`.
5. Use `SQLiteStore` in real use; `MemoryStore` is for tests and
   demos only. SQLite migrations are additive and keep v1 orders.
6. Expose assets, plans, create, get, check, and approve, copying
   `api.py` or `examples/server.py`.
7. Call `watcher.sweep()` from your cron or background loop.
8. Show the buyer, honestly: assigned receive address, pinned
   amount and unit, the 60-minute window, the asset confirmation
   rule, 99% tolerance, and that late, underpaid, or reused payments
   do not auto-unlock. Grey out any asset whose status is
   unavailable and offer another crypto.
9. Test with the suite below before going live.
10. Prove one real test order end to end before advertising paid
    tiers as live.

## Honest limits

- **Address reuse is reduced, not solved.** A fixed pool repeats.
  xpub derivation is the future step.
- **Provider failover is not immunity.** Independent providers can
  be busy at the same time. The busy state is the correct fallback.
- **Explorer history windows are finite.** BTC providers are read
  up to 125 recent transactions per receive address, Blockscout up
  to 150 recent transactions in this implementation, and Solana up
  to 100 recent signatures. Older payments fall back to owner manual
  approval.
- **EVM has one chain provider family in this pass.** Blockscout is
  parameterized per chain, but a Blockscout-wide outage can still
  make an EVM asset unavailable. The provider list is ready for an
  independent fallback when a genuinely keyless one is available.
- **Lightning is still unsupported.** BTC payments are on chain, so
  buyers pay the network fee and wait for confirmations.
- **Exchange withdrawal friction is unchanged.** The buyer must send
  from a wallet or account whose on-chain source is the address they
  declared. An exchange withdrawal from a different hot wallet will
  not match; say so in buyer copy.
- **In-memory store loses orders on restart.** Use SQLiteStore.

## Tests

```bash
python3 -m pytest tests/ -q
```

The suite is offline and mocks all network providers. It covers the
original BTC rules plus pool assignment and rotation, legacy single
address config, SQLite migration, ETH and BNB matching, a newly
registered EVM asset, Solana RPC matching and finalized gating, BTC
chain failover, price failover and stale marking, the 503
`ASSET_UNAVAILABLE` response while another asset still creates, and
a sweep surviving a provider outage.

## Layout

```text
src/paywatch/
  assets.py    asset registry and family metadata
  core.py      amounts, plans, address validation, pure matchers
  providers.py keyless chain and price providers with failover
  mempool.py   backward compatible provider imports
  store.py     MemoryStore + SQLiteStore, transaction claim guard
  watcher.py   pools, availability, create / check / sweep / approve
  api.py       optional FastAPI surface
examples/
  server.py            stdlib demo server
  minimal_integration.py  host example
  plans.example.json   example tiers only
tests/
  test_paywatch.py     original offline unit tests
  test_paywatch_v2.py  pools, assets, providers, and availability
```

## License

MIT.

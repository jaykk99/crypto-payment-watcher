"""Minimal integration: drop the watcher into any project in 6 steps.

1. pip / vendor this package (src/paywatch, stdlib only).
2. Set PAYWATCH_BTC_ADDRESS to YOUR project's public receive address.
3. Set PAYWATCH_PLANS_JSON to YOUR plans (see plans.example.json).
4. Create a Watcher with YOUR on_unlock callback. That callback is
   where your project grants entitlement: set a tier flag, mint an
   access code, extend a subscription. This package only records
   paid + paid_at; it never knows what your plan means.
5. Expose create/get/check/approve (api.py for FastAPI, or copy the
   pattern from examples/server.py for stdlib).
6. Call sweep() from your own cron/background loop, and show the
   buyer the receive address + pinned sats + 60-minute window.

Buyer flow your UI must show, honestly:
  - Pick a plan, enter the wallet address they will send FROM.
  - Pay manually from their own wallet to your receive address.
  - No wallet connection, no signing, no keys, no custody.
  - Unlock needs 2 confirmations, within 60 minutes, at least 99%
    of the pinned amount, from the declared address.
  - Late, underpaid, or reused-txid payments do NOT auto-unlock;
    the owner approves those manually.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from paywatch import Watcher  # noqa: E402

# Demo only: a clearly fake placeholder so this file runs offline.
# Real use sets PAYWATCH_BTC_ADDRESS to the project's real address.
os.environ.setdefault("PAYWATCH_BTC_ADDRESS",
                      "example-receive-address-for-demo-only")

# Your entitlement map: plan_id -> whatever YOUR project grants.
ENTITLEMENTS = {
    "starter": {"tier": "starter", "days": 30},
    "pro": {"tier": "pro", "days": 30},
}


def grant_entitlement(order, tx):
    grant = ENTITLEMENTS.get(order.plan_id, {"tier": order.plan_id})
    # Replace this print with your real grant: DB update, access code,
    # subscription extension, etc.
    print(f"GRANT {grant} to buyer {order.buyer_ref} "
          f"for tx {tx.get('txid')}")


watcher = Watcher(
    plans={"starter": {"id": "starter", "name": "Starter", "price_usd": 19},
           "pro": {"id": "pro", "name": "Pro", "price_usd": 49}},
    on_unlock=grant_entitlement,
)

if __name__ == "__main__":
    # No live network in this example: rate is injected, so this
    # runs offline and shows the order shape only.
    order = watcher.create_order(
        "starter", payer_address="bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq",
        buyer_ref="demo-buyer", rate_usd=50000.0)
    print(order.public())

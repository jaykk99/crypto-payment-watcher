"""Optional FastAPI surface. Requires fastapi (pip install fastapi);
the core package itself is stdlib only, and examples/server.py shows
the same endpoints on the stdlib http.server if you do not want
FastAPI at all.

Routes (host mounts this app, or copies the pattern):
  GET  /plans
  POST /orders            {plan_id, payer_address, buyer_ref?}
  GET  /orders/{order_id}
  POST /orders/{order_id}/check
  POST /orders/{order_id}/approve   {owner_secret?} (see approve rules)
  POST /sweep             (host cron calls this, or calls sweep())
"""
from __future__ import annotations

from .watcher import Watcher, WatcherError, checkout_enabled


def create_app(watcher: Watcher):
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse

    app = FastAPI(title="crypto-payment-watcher")

    @app.exception_handler(WatcherError)
    async def _watcher_error(_request: Request, exc: WatcherError):
        return JSONResponse({"error": str(exc)}, status_code=exc.status_code)

    @app.get("/plans")
    async def plans():
        return {
            "checkout_enabled": checkout_enabled(),
            "plans": [p.as_dict() for p in watcher.plans.values()],
            "required_confirmations": 2,
            "invoice_minutes": 60,
            "tolerance_pct": 1.0,
        }

    @app.post("/orders")
    async def create_order(body: dict):
        order = watcher.create_order(
            body["plan_id"], body["payer_address"],
            buyer_ref=body.get("buyer_ref", ""))
        return order.public()

    @app.get("/orders/{order_id}")
    async def get_order(order_id: str):
        order = watcher.get_order(order_id)
        if order is None:
            raise WatcherError(f"unknown order: {order_id}", 404)
        return order.public()

    @app.post("/orders/{order_id}/check")
    async def check_order(order_id: str):
        return watcher.check_order(order_id)

    @app.post("/orders/{order_id}/approve")
    async def approve_order(order_id: str, body: dict):
        return watcher.approve_order(
            order_id, owner_secret=body.get("owner_secret"))

    @app.post("/sweep")
    async def sweep():
        return watcher.sweep()

    return app

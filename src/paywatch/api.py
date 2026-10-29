"""Optional FastAPI surface. Requires fastapi (pip install fastapi);
the core package itself is stdlib only, and examples/server.py shows
the same endpoints on the stdlib http.server if you do not want
FastAPI at all.

Routes (host mounts this app, or copies the pattern):
  GET  /assets            per-asset availability for a checkout page
  GET  /plans             plans plus configured asset availability
  POST /orders            {plan_id, payer_address, asset?, buyer_ref?}
  GET  /orders/{order_id}
  POST /orders/{order_id}/check
  POST /orders/{order_id}/approve   {owner_secret?} (see approve rules)
  POST /sweep             (host cron calls this, or calls sweep())
"""
from __future__ import annotations

from .assets import supported_assets
from .watcher import Watcher, WatcherError


def create_app(watcher: Watcher):
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse

    app = FastAPI(title="crypto-payment-watcher")

    @app.exception_handler(WatcherError)
    async def _watcher_error(_request: Request, exc: WatcherError):
        payload = {"error": str(exc)}
        if exc.code:
            payload["code"] = exc.code
        if exc.provider_errors:
            payload["provider_errors"] = exc.provider_errors
        return JSONResponse(payload, status_code=exc.status_code)

    @app.get("/assets")
    async def assets():
        return {"assets": watcher.asset_statuses()}

    @app.get("/plans")
    async def plans():
        configured = [watcher.asset_status(asset)
                      for asset in supported_assets()
                      if watcher.pool_for(asset)]
        return {
            "checkout_enabled": bool(configured),
            "assets": configured,
            "plans": [p.as_dict() for p in watcher.plans.values()],
            "required_confirmations": {
                asset: watcher.asset_status(asset)["required_confirmations"]
                for asset in supported_assets() if watcher.pool_for(asset)
            },
            "invoice_minutes": 60,
            "tolerance_pct": 1.0,
        }

    @app.post("/orders")
    async def create_order(body: dict):
        order = watcher.create_order(
            body["plan_id"], body["payer_address"],
            buyer_ref=body.get("buyer_ref", ""),
            asset=body.get("asset", "btc"))
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

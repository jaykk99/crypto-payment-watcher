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
  GET  /checkout          buyer checkout page (?plan=<plan_id>&ref=&return=)
"""
from __future__ import annotations

from importlib import resources

from .assets import supported_assets
from .watcher import Watcher, WatcherError


def validate_order_payload(body) -> Dict:
    """Check a POST /orders body before the watcher touches it.

    Missing or wrong-typed plan_id / payer_address used to raise a
    raw KeyError out of the route and come back as a 500. Callers get
    a 400 with a stable code instead.
    """
    if not isinstance(body, dict):
        raise WatcherError("order body must be a JSON object", 400,
                           code="INVALID_ORDER")
    plan_id = body.get("plan_id")
    payer_address = body.get("payer_address")
    buyer_ref = body.get("buyer_ref", "")
    asset = body.get("asset", "btc")
    if not isinstance(plan_id, str) or not plan_id.strip():
        raise WatcherError("plan_id is required", 400, code="INVALID_ORDER")
    if not isinstance(payer_address, str) or not payer_address.strip():
        raise WatcherError("payer_address is required", 400,
                           code="INVALID_ORDER")
    if not isinstance(buyer_ref, str):
        raise WatcherError("buyer_ref must be a string", 400,
                           code="INVALID_ORDER")
    if not isinstance(asset, str) or not asset.strip():
        raise WatcherError("asset must be a string", 400, code="INVALID_ORDER")
    return {"plan_id": plan_id.strip(),
            "payer_address": payer_address.strip(),
            "buyer_ref": buyer_ref,
            "asset": asset.strip()}


def checkout_html() -> str:
    """The bundled buyer checkout page. Serve it next to the JSON
    routes; it calls them on the same origin (or ?api=<base url>)."""
    return resources.files("paywatch").joinpath(
        "static/checkout.html").read_text(encoding="utf-8")


def create_app(watcher: Watcher):
    from fastapi import FastAPI, Request
    from fastapi.responses import HTMLResponse, JSONResponse

    app = FastAPI(title="crypto-payment-watcher")

    @app.exception_handler(WatcherError)
    async def _watcher_error(_request: Request, exc: WatcherError):
        payload = {"error": str(exc)}
        if exc.code:
            payload["code"] = exc.code
        if exc.provider_errors:
            payload["provider_errors"] = exc.provider_errors
        return JSONResponse(payload, status_code=exc.status_code)

    @app.get("/checkout", response_class=HTMLResponse)
    async def checkout():
        return HTMLResponse(checkout_html())

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
        fields = validate_order_payload(body)
        order = watcher.create_order(
            fields["plan_id"], fields["payer_address"],
            buyer_ref=fields["buyer_ref"], asset=fields["asset"])
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

"""Stdlib demo server: same idea as api.py, zero dependencies.

Run:  PAYWATCH_BTC_ADDRESS=<host receive address> \\
          PAYWATCH_PLANS_FILE=examples/plans.example.json \\
          python3 examples/server.py
Then:   curl localhost:8000/plans
        curl -X POST localhost:8000/orders -d '{"plan_id":"starter",
          "payer_address":"<buyer declared address>"}'

This is a demo, not a production deploy: put it behind the host
project's own auth, and use SQLiteStore in real use, not MemoryStore.
"""
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from paywatch import Watcher  # noqa: E402
from paywatch.watcher import WatcherError  # noqa: E402


def on_unlock(order, tx):
    # Host hook: grant whatever order.plan_id means in YOUR project.
    print(f"UNLOCK plan={order.plan_id} buyer={order.buyer_ref} "
          f"tx={tx.get('txid')} sats={tx.get('received_sats')}", flush=True)


watcher = Watcher(on_unlock=on_unlock)


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"{}") if length else {}

    def do_GET(self):
        try:
            if self.path == "/plans":
                return self._send(200, {
                    "plans": [p.as_dict() for p in watcher.plans.values()]})
            if self.path.startswith("/orders/"):
                order = watcher.get_order(self.path.split("/")[-1])
                if order is None:
                    return self._send(404, {"error": "unknown order"})
                return self._send(200, order.public())
            return self._send(404, {"error": "not found"})
        except WatcherError as exc:
            return self._send(exc.status_code, {"error": str(exc)})

    def do_POST(self):
        try:
            body = self._body()
            if self.path == "/orders":
                order = watcher.create_order(
                    body["plan_id"], body["payer_address"],
                    buyer_ref=body.get("buyer_ref", ""))
                return self._send(200, order.public())
            parts = self.path.strip("/").split("/")
            if len(parts) == 3 and parts[0] == "orders" and parts[2] == "check":
                return self._send(200, watcher.check_order(parts[1]))
            if len(parts) == 3 and parts[0] == "orders" and parts[2] == "approve":
                return self._send(200, watcher.approve_order(
                    parts[1], owner_secret=body.get("owner_secret")))
            if self.path == "/sweep":
                return self._send(200, watcher.sweep())
            return self._send(404, {"error": "not found"})
        except WatcherError as exc:
            return self._send(exc.status_code, {"error": str(exc)})

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    print(f"crypto-payment-watcher demo on :{port}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()

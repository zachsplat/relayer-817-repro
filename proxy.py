"""Lagging-RPC proxy for the #817 reproduction.

Forwards JSON-RPC to anvil unchanged, except eth_getTransactionReceipt: a receipt is
hidden (null is returned) until HIDE_SECS have passed since the proxy first saw a
non-null receipt for that hash. eth_getTransactionCount is never delayed, so the
relayer sees the advanced nonce while the receipt is still "missing", which is the
load-balanced-provider inconsistency the issue describes. Set HIDE_SECS=0 to disable.
Control endpoint: GET /stats returns counters as JSON.
"""
import json, os, sys, time, threading, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM = os.environ.get("UPSTREAM", "http://anvil:8545")
HIDE_SECS = float(os.environ.get("HIDE_SECS", "300"))
LOG = os.environ.get("PROXY_LOG", "/logs/proxy.log")
first_seen = {}           # hash -> time the real receipt first appeared
lock = threading.Lock()
stats = {"receipt_calls": 0, "receipt_hidden": 0, "receipt_passed": 0, "nonce_calls": 0, "send_raw": 0, "send_raw_errors": []}

def log(msg):
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    with open(LOG, "a") as f: f.write(line + "\n")

def forward(body: bytes) -> bytes:
    req = urllib.request.Request(UPSTREAM, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()

def handle_one(obj):
    method = obj.get("method")
    raw = forward(json.dumps(obj).encode())
    resp = json.loads(raw)
    if method == "eth_getTransactionReceipt":
        h = (obj.get("params") or [""])[0]
        with lock:
            stats["receipt_calls"] += 1
            if resp.get("result") is not None:
                t0 = first_seen.setdefault(h, time.time())
                age = time.time() - t0
                if age < HIDE_SECS:
                    stats["receipt_hidden"] += 1
                    if stats["receipt_hidden"] in (1, 2, 5, 10, 20, 50) or int(age) % 60 == 0:
                        log(f"hiding receipt for {h[:12]}.. (visible on anvil for {int(age)} s, hidden until {int(HIDE_SECS)} s)")
                    resp["result"] = None
                else:
                    stats["receipt_passed"] += 1
                    if stats["receipt_passed"] == 1: log(f"receipt for {h[:12]}.. now passed through")
    elif method == "eth_getTransactionCount":
        with lock: stats["nonce_calls"] += 1
    elif method == "eth_sendRawTransaction":
        with lock:
            stats["send_raw"] += 1
            if "error" in resp:
                stats["send_raw_errors"].append(str(resp["error"].get("message"))[:120])
                log(f"send_raw_transaction error: {resp['error'].get('message')}")
            else:
                log(f"send_raw_transaction ok: {str(resp.get('result'))[:14]}..")
    return resp

class H(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/stats"):
            with lock: body = json.dumps(stats).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(body); return
        self.send_response(404); self.end_headers()
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n)
        try:
            obj = json.loads(body)
            out = [handle_one(o) for o in obj] if isinstance(obj, list) else handle_one(obj)
            data = json.dumps(out).encode()
            self.send_response(200)
        except Exception as e:
            log(f"proxy error: {e!r}")
            data = json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32000, "message": f"proxy: {e}"}}).encode()
            self.send_response(200)
        self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
    def log_message(self, *a): pass

if __name__ == "__main__":
    log(f"proxy up: upstream {UPSTREAM}, HIDE_SECS {HIDE_SECS}")
    ThreadingHTTPServer(("0.0.0.0", 8546), H).serve_forever()

"""Reproduce OpenZeppelin Relayer issue #817: a mined transaction is finalised as Failed when
eth_getTransactionReceipt transiently returns null while eth_getTransactionCount already shows
the advanced nonce.

Stack: anvil (auto-mining every 2 s) behind proxy.py, which hides each receipt for HIDE_SECS after
it first appears on anvil; nonce reads pass through. Relayer v1.8.0 talks only to the proxy.
Sequence: submit one transfer, then poll the relayer's view, anvil's own receipt (direct), and the
proxy counters until the relayer reaches a final status or 15 minutes pass. Logs to logs/run.log.
"""
import json, time
import httpx

RELAYER = "http://127.0.0.1:18681"; KEY = "repro817-api-key-0123456789abcdef"
ANVIL = "http://127.0.0.1:18645"; PROXY = "http://127.0.0.1:18646"
RID = "anvil-lag"; TO = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"
LOG = open(__file__.rsplit("/", 1)[0] + "/logs/run.log", "a")

def log(m):
    line = f"{time.strftime('%H:%M:%S')} {m}"; print(line, flush=True); LOG.write(line + "\n"); LOG.flush()

def rpc(method, params=None):
    j = httpx.post(ANVIL, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []}, timeout=20).json()
    if "error" in j: raise RuntimeError(f"{method}: {j['error']}")
    return j["result"]

def api(method, path, body=None):
    r = httpx.request(method, RELAYER + path, headers={"Authorization": f"Bearer {KEY}"}, json=body, timeout=30)
    return r.status_code, r.json()

def tx_state(tx_id):
    d = api("GET", f"/api/v1/relayers/{RID}/transactions/{tx_id}")[1].get("data", {})
    return d.get("status"), d.get("hash"), d.get("hashes") or [], d.get("nonce"), d.get("status_reason")

def anvil_receipt(h):
    r = rpc("eth_getTransactionReceipt", [h])
    return None if r is None else (int(r["status"], 16), int(r["blockNumber"], 16))

def stats():
    try: return httpx.get(PROXY + "/stats", timeout=5).json()
    except Exception as e: return {"err": str(e)}

def main():
    log("=== repro #817 start; relayer openzeppelin/openzeppelin-relayer:v1.8.0 behind lagging proxy")
    log(f"anvil block {int(rpc('eth_blockNumber'), 16)}, sender nonce {int(rpc('eth_getTransactionCount', ['0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266', 'latest']), 16)}")
    code, j = api("POST", f"/api/v1/relayers/{RID}/transactions", {"to": TO, "value": 1, "data": "0x", "speed": "fast", "gas_limit": 21000})
    log(f"submit -> {code} {json.dumps(j)[:240]}")
    tx_id = j["data"]["id"]
    t0 = time.time(); verdict = "UNRESOLVED after 15 min"; last = None; mined_at = None; mined_hash = None
    while time.time() - t0 < 900:
        st = tx_state(tx_id)
        h = st[1]
        if h and mined_at is None:
            rc = anvil_receipt(h)
            if rc:
                mined_at = time.time(); mined_hash = h
                log(f"anvil: hash {h[:14]}.. mined in block {rc[1]} with status {rc[0]} (t+{int(mined_at-t0)} s); on-chain nonce now {int(rpc('eth_getTransactionCount', ['0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266', 'latest']), 16)}")
        if st != last:
            log(f"relayer state t+{int(time.time()-t0)} s: status={st[0]} hash={(st[1] or '')[:14]} hashes={len(st[2])} nonce={st[3]} reason={st[4]}")
            last = st
        if int(time.time() - t0) % 60 < 10:
            log(f"proxy {json.dumps(stats())}")
        if st[0] in ("failed", "expired", "canceled"):
            rc = anvil_receipt(mined_hash) if mined_hash else None
            verdict = f"REPRODUCED: relayer status {st[0]} (reason: {st[4]}) while anvil has hash {str(mined_hash)[:14]}.. mined with status {rc[0] if rc else '?'} at nonce {st[3]}"; break
        if st[0] in ("mined", "confirmed"):
            verdict = f"NOT REPRODUCED: relayer reached {st[0]} after {int(time.time()-t0)} s"; break
        time.sleep(10)
    log(f"final relayer state {tx_state(tx_id)}")
    log(f"final proxy {json.dumps(stats())}")
    log(f"=== VERDICT: {verdict}")

if __name__ == "__main__":
    main()

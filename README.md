# Reproduction of OpenZeppelin Relayer issue #817 on v1.8.0

A successfully mined transaction is finalised as `Failed` when `eth_getTransactionReceipt` transiently returns `null` while `eth_getTransactionCount` already shows the advanced nonce: https://github.com/OpenZeppelin/openzeppelin-relayer/issues/817

This runs the unmodified `openzeppelin/openzeppelin-relayer:v1.8.0` image against anvil through a small proxy that makes the read inconsistency of a load-balanced provider deterministic: for `HIDE_SECS` after a receipt first exists on anvil, the proxy answers `null` to `eth_getTransactionReceipt` for that hash; every other call, including `eth_getTransactionCount`, passes through unchanged.

## What it shows (run of 2026-10-06, UTC)

1. 19:10:58 one transfer submitted at `speed: fast`.
2. 19:11:08 anvil mined it in block 16 with receipt status 1; the sender's on-chain nonce became 1. The relayer saw `null` and logged `transaction not yet mined` every 12 s (16 times).
3. 19:14:06, age 188 s (the `fast` resubmit timeout is 3 minutes): `transaction has been pending for too long, resubmitting`. The re-signed transaction was sent; anvil answered `JSON-RPC error (code -32003): nonce too low`, because the original had mined.
4. Same second: `resubmission got nonce too low - scheduling nonce recovery`, then `nonce recovery hint detected - performing nonce reconciliation hint=NonceTooLow`, then `nonce recovery: nonce consumed externally, marking as Failed tx_nonce=0 on_chain_nonce=1`.
5. Final status `failed`, reason `Nonce 0 consumed externally (on-chain nonce: 1). No matching transaction hash found on-chain.` The webhook receiver got `sent`, `submitted`, `failed`. Proxy counters at the end: 17 receipt reads, all hidden; 2 `eth_sendRawTransaction` (the original and the rejected resend).

Driver output is in `logs/run.log`, the proxy's view in `logs/proxy.log`, the relayer's decisive lines in `logs/relayer-excerpt.log`, the three webhook payloads in `logs/webhooks.log`.

## Why, as far as I can tell from the 1.8.0 source

`src/domain/transaction/evm/status.rs`, `reconcile_tx_nonce_state`: step 1 treats `Ok(None)` from `get_transaction_receipt` as authoritative (only `Err` sets `had_rpc_errors`); step 2 (historical hashes) is skipped because the record holds one hash; step 3 sees the on-chain nonce above the transaction's nonce and finalises `Failed`. The function is entered through the one-shot `NonceTooLow` hint set in `resubmit_transaction` (`src/domain/transaction/evm/evm_transaction.rs`) when the resend is rejected.

The maintainers' PR #899 (superseding #894) records the re-signed hash before broadcast and stops treating lookup errors as failure. As I read its description against this code, a stale `Ok(None)` still passes step 1, so this scenario is not covered by it. I have not built that branch.

Differences from the original report: the reporter's lag came from a provider pool on Base at steady throughput; here it is one transaction on anvil behind a proxy that hides every receipt read for a fixed window. The code path and the end state are the same.

## Run it

Requirements: Docker with Compose, Python 3.10+, `pip install httpx eth-account`.

```bash
python3 generate_keystore.py          # keystore for anvil's public dev account #0
touch logs/receiver.log               # bind-mounted into the receiver; must exist as a file
docker compose -p repro817 up -d      # anvil (2 s blocks), redis, webhook receiver, proxy, relayer 1.8.0
# wait ~15 s for "all health checks passed" in: docker logs repro817-relayer
python3 run_repro.py                  # about 3.5 minutes at speed fast; prints a VERDICT line
docker compose -p repro817 down -v
```

Ports on localhost: 18645 (anvil, direct), 18646 (proxy; `GET /stats` shows the counters), 18681 (relayer API). `HIDE_SECS` in `docker-compose.yml` is 420; anything above the speed's resubmit timeout (3 minutes for `fast`) reproduces it, anything below does not reach the path. The API key and webhook signing key are throwaway values for this test.

## Files

- `docker-compose.yml`: anvil 1.8.1 (`--block-time 2`), Redis, the receiver, `proxy.py`, `openzeppelin/openzeppelin-relayer:v1.8.0`.
- `proxy.py`: the receipt-hiding JSON-RPC proxy (standard library only).
- `config/config.json`: one EVM relayer with `eip1559_pricing` and no gas price cap, local keystore signer, inline network pointing at the proxy.
- `run_repro.py`: submits one transfer, watches the relayer's view and anvil's own receipt, prints the verdict.
- `logs/`: the run described above.

MIT. Not affiliated with OpenZeppelin.

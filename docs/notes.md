# komet-node: Status Notes

## What this does

`komet-node` is a local Stellar testnet backed by the K semantics of Soroban. The compiled semantics are a one-shot interpreter (one process per request, no networking, no state between runs), so Python wraps them into a long-running server: it holds the HTTP socket, keeps state on disk between runs, and decodes Stellar XDR, which K cannot. The RPC layer itself — method dispatch, receipt bookkeeping, ledger accounting, status, and response formatting — runs inside the K semantics ([`node.md`](node-semantics.md)).

---

## Module map

| Module | Role |
|---|---|
| [`server.py`](server.md) — `StellarRpcServer` | Long-running HTTP/JSON-RPC server wrapping the one-shot K interpreter; `handle_rpc` dispatch; owns the io-dir files. Holds no ledger or receipt state. |
| [`transaction.py`](transaction.md) — `TransactionEncoder` | XDR → request envelope + (for wasm uploads) kasmer steps; address/contract-id helpers. |
| [`interpreter.py`](interpreter.md) — `NodeInterpreter` | Runs request envelopes through `llvm_interpret`; persists `state.kore`. No `kast`↔`kore` whole-config conversions. |
| `scval.py` | XDR `SCVal` ↔ Komet `SCValue` (`scvalue_from_xdr`) and XDR `SCVal` → request JSON (`scval_to_json`). |
| [`kdist/node.md`](node-semantics.md) | The K RPC layer: reads `request.json`, dispatches, updates `metadata.json` and the per-transaction `receipts/` files, writes `response.json`. |

State lives in the io dir as `state.kore` (KORE world state) and `metadata.json` (ledger counter), with per-transaction receipts and traces under `receipts/` and `traces/`. See [architecture.md](architecture.md).

---

## Tests (`src/tests/integration/`)

- `test_server.py` drives the running HTTP server end-to-end. It exercises the read-only methods, `sendTransaction` + `getTransaction`, ledger increments, the full lifecycle (create → upload wasm → deploy → invoke), and the `traceTransaction` flows. `test_call_tx_with_args` deploys `args.wat` and calls functions with `bool`, `u32`, `i32`, `u64`, `i64`, `u128`, `i128`, and `symbol` arguments, exercising the `scval_to_json` / `#decodeArg` pipeline.
- `test_integration.py` and `test_unit.py` hold small sanity checks.

Run with `make test` (requires `make kdist-build` first).

The tests do not yet cover `bytes` / `address` SCVal arguments or `SCVec` / `SCMap` (and `scval_to_json` does not yet encode the latter).

---

## Known gaps

Measured against the official Stellar RPC surface for protocol 22 (the OpenRPC spec plus the `protocols/rpc` structs in go-stellar-sdk, which are what real stellar-rpc emits).

### Missing methods

`simulateTransaction`, `getLedgerEntries`, `getEvents`, `getTransactions`, `getLedgers`, `getFeeStats`, and `getVersionInfo` are not implemented and return `-32601` Method not found. TTL/footprint operations are likewise unsupported.

### Deviations in the implemented methods

- `getHealth` returns only `{status}`; the spec adds `latestLedger`, `oldestLedger`, and `ledgerRetentionWindow` (numbers).
- `getNetwork` returns `protocolVersion` as a string (`"22"`) where the spec has a number, and `friendbotUrl: null` where real stellar-rpc omits the field entirely.
- `getLatestLedger` returns `protocolVersion` as a string, and `id` is a constant all-zeros hash instead of a per-ledger value.
- `sendTransaction` returns `latestLedger` as a string; the statuses `DUPLICATE`, `ERROR`, and `TRY_AGAIN_LATER` are never returned (resubmitting a transaction re-executes it instead of reporting `DUPLICATE`), and `errorResultXdr` / `diagnosticEventsXdr` are never returned.
- `getTransaction` is missing the required `oldestLedger` / `oldestLedgerCloseTime` (all statuses) and `applicationOrder` / `feeBump` (success/failed); `latestLedger` and `ledger` are strings where the spec has numbers; `resultXdr` / `resultMetaXdr` are empty-string stubs (contract return values are not surfaced); the `hash` parameter is not validated against the 64-lowercase-hex format.

### Cross-cutting

- The `xdrFormat` parameter is accepted on `getTransaction` and `sendTransaction`, but only the default `'base64'` is supported; `'json'` is rejected with `-32602`.
- The K semantics' unknown-method fallback answers with `result: null` instead of error `-32601`. This is currently unreachable — the Python layer rejects unknown methods before the semantics run — but latent.
- `SCVec` / `SCMap` contract arguments are not yet encoded.
- `traceTransaction` is a komet-specific extension, absent from real Stellar RPC (see [server.md](server.md#tracetransaction-komet-specific-extension)).

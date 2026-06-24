# komet-node: Status Notes

## What this does

`komet-node` is a local Stellar testnet backed by the K semantics of Soroban. The compiled semantics are a one-shot interpreter (one process per request, no networking, no state between runs), so Python wraps them into a long-running server: it holds the HTTP socket, keeps state on disk between runs, and decodes Stellar XDR, which K cannot. The RPC layer itself — method dispatch, receipt bookkeeping, ledger accounting, status, and response formatting — runs inside the K semantics ([`node.md`](node-semantics.md)).

See `src/komet_node/demo.py` for an end-to-end example: empty state → create account → upload wasm → deploy contract → call `foo()`, with each step's K configuration pretty-printed.

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

- `resultXdr` / `resultMetaXdr` are empty stubs (contract return values not surfaced).
- `SCVec` / `SCMap` contract arguments are not yet encoded.
- `simulateTransaction`, `getEvents`, `getLedgerEntries`, `getFeeStats`, and TTL/footprint operations are not implemented.

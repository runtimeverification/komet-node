# komet-node: Status Notes

## What this does

`komet-node` is a local Stellar testnet backed by the K semantics of Soroban. Python is a thin shim that decodes Stellar XDR and shuttles bytes; the RPC layer itself — method dispatch, the transaction store, ledger accounting, status, and response formatting — runs inside the K semantics ([`node.md`](node-semantics.md)).

See `src/komet_node/demo.py` for an end-to-end example: empty state → create account → upload wasm → deploy contract → call `foo()`, with each step's K configuration pretty-printed.

---

## Module map

| Module | Role |
|---|---|
| [`server.py`](server.md) — `StellarRpcServer` | Raw HTTP/JSON-RPC shim; `handle_rpc` dispatch; owns the io-dir files. Holds no ledger or tx state. |
| [`transaction.py`](transaction.md) — `TransactionEncoder` | XDR → request envelope + (for wasm uploads) kasmer steps; address/contract-id helpers. |
| [`interpreter.py`](interpreter.md) — `NodeInterpreter` | Runs request envelopes through `llvm_interpret`; persists `state.kore`. No `kast`↔`kore` whole-config conversions. |
| `scval.py` | XDR `SCVal` ↔ Komet `SCValue` (`scvalue_from_xdr`) and XDR `SCVal` → request JSON (`scval_to_json`). |
| [`kdist/node.md`](node-semantics.md) | The K RPC layer: reads `request.json`, dispatches, updates `metadata.json` / `transactions.json`, writes `response.json`. |

State lives in the io dir as `state.kore` (KORE world state), `metadata.json` (ledger counter), and `transactions.json` (tx store). See [architecture.md](architecture.md).

---

## Tests (`src/tests/integration/`)

- `test_server.py` — drives the running HTTP server end-to-end: the read-only methods, `sendTransaction` + `getTransaction`, ledger increments, the full lifecycle (create → upload wasm → deploy → invoke), the `traceTransaction` flows, and `test_call_tx_with_args` (deploys `args.wat` and calls functions with `bool`, `u32`, `i32`, `u64`, `i64`, `u128`, `i128`, and `symbol` args — the `scval_to_json` / `#decodeArg` pipeline).
- `test_integration.py` / `test_unit.py` — small sanity checks.

Run with `make test` (requires `make kdist-build` first).

Not yet covered: `bytes` / `address` SCVal args, and `SCVec` / `SCMap` (the latter are not yet encoded by `scval_to_json`).

---

## Known gaps

- `resultXdr` / `resultMetaXdr` are empty stubs (contract return values not surfaced).
- `SCVec` / `SCMap` contract arguments are not yet encoded.
- No `simulateTransaction`, `getEvents`, `getLedgerEntries`, `getFeeStats`, or TTL/footprint operations.

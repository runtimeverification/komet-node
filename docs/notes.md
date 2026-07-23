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
| `scval.py` | XDR `SCVal` ↔ request/response JSON (`scval_to_json`, `scval_from_json`). |
| `ledger_entries.py` | `getLedgerEntries` XDR translation: base64 `LedgerKey` → key descriptors for the semantics, intermediate entries → base64 `LedgerEntryData`. |
| [`kdist/node.md`](node-semantics.md) | The K RPC layer: reads `request.json`, dispatches, updates `metadata.json` and the per-transaction `receipts/` files, writes `response.json`. |

State lives in the io dir as `state.kore` (KORE world state) and `metadata.json` (ledger counter), with per-transaction receipts and traces under `receipts/` and `traces/`. See [architecture.md](architecture.md).

---

## Tests (`src/tests/integration/`)

- `test_server.py` drives the running HTTP server end-to-end. It exercises the read-only methods, `sendTransaction` + `getTransaction`, the history methods (`getTransactions`/`getLedgers` response shapes, pagination, and parameter validation against the official spec and the Go protocol structs), ledger increments, the full lifecycle (create → upload wasm → deploy → invoke), the `traceTransaction` flows, and `getLedgerEntries` (account, contract code, contract instance, and persistent storage entries via `storage.wat`, plus its parameter validation). `test_call_tx_with_args` deploys `args.wat` and calls functions with `bool`, `u32`, `i32`, `u64`, `i64`, `u128`, `i128`, and `symbol` arguments, exercising the `scval_to_json` / `#decodeArg` pipeline.
- `test_get_events.py` covers `getEvents`: request validation, the empty-window shape, the full shape of an event emitted via `contract_event` (`data/wasm/events.wat`), filtering, and pagination.
- `test_integration.py` and `test_unit.py` hold small sanity checks.

Run with `make test` (requires `make kdist-build` first).

The tests do not yet cover `bytes` / `address` SCVal arguments or `SCVec` / `SCMap` (and `scval_to_json` does not yet encode the latter).

---

## Known gaps

- `resultXdr` / `resultMetaXdr` are synthesised: `feeCharged` is 0, ledger-entry change
  sets are empty, and the InvokeHostFunction success hash covers only the return value
  (komet-node does not track fees, entry changes, or events).
- `sendTransaction`'s `errorResultXdr` is always a generic `txMALFORMED` result (no per-cause codes such as `txBAD_SEQ` — sequence numbers, fees, and signatures are not modelled), and `diagnosticEventsXdr` is never populated (both fields are optional in the spec). `TRY_AGAIN_LATER` is never returned by design: it signals mempool backpressure, which cannot arise in a synchronous node without a mempool.
- `SCVec` / `SCMap` contract arguments are not yet encoded.
- The `xdrFormat` parameter is accepted on `getTransaction`, `sendTransaction`, `getTransactions`, `getLedgers`, `getLedgerEntries`, and `getEvents`, but only the default `'base64'` is supported; `'json'` is rejected with `-32602`.
- `simulateTransaction` only simulates invoke-contract host functions (not uploads/deploys); `auth` is always empty, `minResourceFee`/`transactionData` are synthetic constants, and `events`/`restorePreamble`/`stateChanges` and the `resourceConfig`/`authMode`/`xdrFormat` parameters are not supported. `ScMap` return values are not yet encoded.
- `getEvents` serves contract events only: `system` events are never emitted (the semantics have no source of them), and events whose topics/data have no staging representation (maps, errors, 256-bit ints) are dropped with a warning.
- TTL/footprint operations are not implemented.
- `getFeeStats` reports constant distributions (there is no fee market); only its `latestLedger` is live.
- `getTransactions` / `getLedgers` serve only ledgers with an index file under `ledgers/`; io-dirs created before the ledger index existed resume fine, but their earlier ledgers do not appear in the history.
- `getLedgerEntries` covers the entry types the K state tracks (`ACCOUNT`, `CONTRACT_DATA`, `CONTRACT_CODE`); other key types are reported as not found. `lastModifiedLedgerSeq` is approximated by the current ledger (per-entry modification ledgers are not tracked), and only the balance is real in `ACCOUNT` entries (sequence number, thresholds, etc. are synthesised constants).
- Receipts are not a stable format: older io-dirs store `ledger`/`createdAt` with pre-spec types and lack `applicationOrder`/`feeBump`. Resume only io-dirs written by the same version, or start fresh.

# komet-node: Architecture

## Overview

komet-node is a local Stellar testnet whose execution engine is the [K formal semantics](https://github.com/runtimeverification/komet) of Soroban. It decodes incoming Stellar transactions into K steps and executes them through the compiled semantics.

The split between Python and K follows one rule: K does everything that is part of the Soroban/Stellar protocol, and Python does only what K structurally cannot. The compiled K semantics are a *one-shot interpreter* — one invocation runs one request to completion and exits, with no network, no memory between invocations, and no decoder for Stellar's binary XDR format. Python supplies exactly those three missing pieces: it is the long-running process that holds the HTTP socket, it keeps the world state on disk between invocations, and it decodes the XDR envelope (and parses uploaded wasm). Everything else — RPC method dispatch, the transaction store, ledger-sequence accounting, status determination, and JSON-RPC response formatting — runs inside the K semantics (`node.md`).

```
Stellar client
     │  JSON-RPC request (base64 XDR transaction)
     ▼
StellarRpcServer                    ← server.py   (long-running http.server; holds the socket + state files)
     │
     ├─ TransactionEncoder          ← transaction.py
     │     XDR → request envelope (+ kasmer steps for wasm uploads)
     │
     └─ NodeInterpreter             ← interpreter.py
           request.json + state.kore  →  llvm_interpret  →  response.json
                  ▼
        K semantics (LLVM backend)  ← kdist/node.md + soroban-semantics
           reads request.json, dispatches the RPC method, updates the
           world state + bookkeeping files, writes response.json
                  ▼
        state.kore · metadata.json · transactions.json   (persisted in the io dir)
```

---

## Components

### `server.py` — `StellarRpcServer`

`StellarRpcServer` is the long-running process around the semantics: a plain `http.server.HTTPServer` that keeps the HTTP socket open and the state files on disk across requests — the networking and persistence the one-shot K interpreter has no notion of. It receives JSON-RPC requests, uses `TransactionEncoder` to turn a transaction into a request envelope, hands the envelope to `NodeInterpreter`, and returns the `response.json` the semantics produced. It holds **no** ledger counter, transaction store, or response-formatting logic — those live in K.

`handle_rpc(method, params, id)` is the dispatch entry point and is usable without the HTTP layer (scripts, tests).

→ **[Detailed documentation](server.md)**

The server implements six RPC methods — `getHealth`, `getNetwork`, `getLatestLedger`, `sendTransaction`, `getTransaction`, and `traceTransaction` — and the K semantics answer all of them.

`sendTransaction` always returns `PENDING` and clients poll `getTransaction` for the result — matching the Stellar RPC async pattern even though the transaction executes synchronously. See [server.md](server.md) for details.

---

### `transaction.py` — `TransactionEncoder`

`TransactionEncoder` decodes Stellar's binary XDR, the wire format K cannot read. It turns a `stellar_sdk` transaction envelope into a JSON request envelope containing the RPC method, the transaction hash, the envelope XDR, and the decoded operations as JSON "steps". For the one case K cannot consume as JSON — a wasm upload, whose `ModuleDecl` has no JSON form — it produces the kasmer steps in K-AST form for direct injection into the `<program>` cell.

→ **[Detailed documentation](transaction.md)**

---

### `interpreter.py` — `NodeInterpreter`

`NodeInterpreter` runs request envelopes through the K semantics. It builds the initial configuration, feeds a request envelope to the LLVM interpreter against `state.kore`, and persists the resulting state. It knows nothing about Stellar. It performs **no** whole-configuration `kast`↔`kore` conversions — the initial config is built directly in KORE and request steps are spliced into the `<program>` cell at the KORE level.

→ **[Detailed documentation](interpreter.md)**

---

### `kdist/node.md` — K Semantics

`node.md` is the K module compiled into the LLVM binary. It implements the whole RPC layer on the K side: it reads `request.json`, dispatches on the `method` field, reads and updates the bookkeeping files (`metadata.json`, `transactions.json`), executes transaction steps via KASMER, and writes the JSON-RPC `response.json`. KASMER is the Komet execution harness whose `Step`s — `setAccount`, `deployContract`, `callTx`, `uploadWasm` — carry out the Soroban operations a transaction decodes into.

→ **[Detailed documentation](node-semantics.md)**

---

## State management

Server state is split across the *io dir* (the directory containing the state file, by default the working directory):

| File | Owner | Contents |
|---|---|---|
| `state.kore` | round-tripped by `NodeInterpreter` | the full K world-state configuration — accounts, contract code (incl. uploaded wasm `ModuleDecl`s), contract storage, ledger metadata — serialized in KORE |
| `metadata.json` | read/written by the K semantics | `{"latest_ledger": N}` — the server ledger counter |
| `transactions.json` | read/written by the K semantics | map from tx hash → stored receipt, answering `getTransaction` |

The world state stays in KORE (rather than a JSON snapshot) because an uploaded wasm module is a `ModuleDecl` that the semantics cannot reconstruct from bytes — only `wasm2kast` (Python) can produce it. The RPC bookkeeping, by contrast, is plain data and lives in the two JSON sidecar files, which the semantics read and write directly via the file-system hooks.

```
startup (state.kore absent):
          → NodeInterpreter.empty_config() builds the idle K configuration in KORE
            and runs it through llvm_interpret (no krun, no kast conversion)
          → server writes state.kore, and seeds metadata.json ({latest_ledger: 0})
            and transactions.json ({})

per successful transaction:
          → the semantics execute the steps, append a SUCCESS receipt to
            transactions.json, bump latest_ledger in metadata.json, and write
            response.json; NodeInterpreter persists the new state.kore

per failed (stuck) transaction:
          → no response.json is produced; state.kore is left unchanged and the
            ledger is not bumped. The server synthesises a FAILED receipt.
```

Because all three files live on disk, the server can be stopped and restarted without losing the world state, the ledger counter, or the transaction store. To resume a session, point `--state-file` at a saved `state.kore` (its sidecar files are read if present). To start fresh, delete the files.

---

## Request flow (end to end)

```
1. Client: POST {"method": "sendTransaction", "params": {"transaction": "<base64 XDR>"}}

2. StellarRpcServer.handle_rpc:
   - TransactionEncoder.build_tx_request("sendTransaction", id, xdr, now, force_trace=False)
       → ( request envelope {method, id, now, txHash, envelopeXdr, trace, steps},
           program_steps )   # program_steps is None unless the tx uploads wasm

3. NodeInterpreter.run(state_file, io_dir, envelope, program_steps):
   - writes request.json
   - (wasm only) splices the upload steps into the <program> cell, in KORE
   - llvm_interpret on state.kore  → the semantics handle the request

4. node.md:
   - insert-handleRequestFile → #dispatch → #dispatchMethod("sendTransaction")
   - run steps → record a SUCCESS receipt in transactions.json
   - bump latest_ledger in metadata.json
   - write response.json: {hash, status: "PENDING", latestLedger, latestLedgerCloseTime}

5. NodeInterpreter persists the new state.kore and returns response.json verbatim.

6. Client: POST {"method": "getTransaction", "params": {"hash": "<hash>"}}
   → the semantics look up transactions.json and return
     {status: SUCCESS, ledger, createdAt, envelopeXdr, trace, ...}
```

---

## Dependencies

| Dependency | Role |
|---|---|
| `komet` | Soroban K semantics, `SorobanDefinition`, `SCValue` dataclasses, `kasmer` step types |
| `pyk` | K toolchain Python bindings: `llvm_interpret`, `kdist`, KORE parsing/prelude, `kast_to_kore` (only for the wasm module) |
| `pykwasm` | Wasm → K AST conversion (`wasm2kast`), used for the wasm-upload step |
| `stellar_sdk` | Stellar transaction types, XDR encoding/decoding, `TransactionEnvelope` |

---

## What's not yet implemented

- `resultXdr` / `resultMetaXdr` in `getTransaction` responses (contract return values)
- `simulateTransaction` (dry-run without state mutation)
- `getEvents`, `getLedgerEntries`, `getFeeStats` and other read-only RPC methods
- `ExtendFootprintTTL` and `RestoreFootprint` operations
- `SCVec` / `SCMap` contract-argument types in the request encoder (`scval_to_json`)

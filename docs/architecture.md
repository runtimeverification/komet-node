# komet-node: Architecture

## Overview

komet-node is a local Stellar testnet whose execution engine is the [K formal semantics](https://github.com/runtimeverification/komet) of Soroban. Rather than running a real Stellar validator, it translates incoming Stellar transactions into K steps and executes them through the compiled K semantics according to the formal Soroban specification.

The server receives and decodes a transaction and manages the current blockchain state (state.kore).
The interpreter translates the transaction into K steps and runs them through krun producing an updated state.

```
Stellar client
     │  JSON-RPC request (XDR transaction)
     ▼
StellarRpcServer                    ← server.py
     │  decoded Transaction + state.kore
     ▼
NodeInterpreter                     ← interpreter.py
     │  request.json (encoded steps) + state.kore
     ▼
K semantics (LLVM backend)          ← kdist/node.md + soroban-semantics
     │  updated state.kore
     ▼
state.kore  (written back for the next transaction)
```

---

## Components

### `server.py` — `StellarRpcServer`

The HTTP/JSON-RPC layer. Exposes the Stellar RPC API to clients, manages the `state.kore` file on disk, and dispatches transactions to `NodeInterpreter`.

→ **[Detailed documentation](server.md)**

Implemented RPC methods: `getHealth`, `getNetwork`, `getLatestLedger`, `sendTransaction`, `getTransaction`.

`sendTransaction` always returns `PENDING` and clients poll `getTransaction` for the result — matching the Stellar RPC async pattern even though krun executes the transaction immediately. See [server.md](server.md) for details.

---

### `interpreter.py` — `NodeInterpreter`

The core execution engine. Translates a decoded Transaction into K steps and runs them through krun, returning the updated state as a KORE string.

→ **[Detailed documentation](interpreter.md)**

---

### `kdist/node.md` — K Semantics

The K module compiled into the LLVM binary. Implements the `request.json` lifecycle on the K side: detects the file, parses and decodes the JSON steps, executes them via KASMER, removes the file, and halts with the updated state as output.

→ **[Detailed documentation](node-semantics.md)**

---

## State management

The entire blockchain state is a single KORE file (`state.kore`). It contains the full K configuration: accounts, contract code, contract storage, and ledger metadata, serialized in KORE (K's internal term format).

```
startup:  state.kore does not exist
          → StellarRpcServer calls interpreter.empty_config()
          → empty_config() runs krun with setExitCode(0) as the only step,
            producing the initial empty idle K configuration
            (no accounts, no contracts, no storage)
          → server writes the result to state.kore

per successful transaction:
          → NodeInterpreter reads state.kore as krun input
          → krun executes the transaction steps
          → krun outputs the updated configuration to stdout
          → server.py overwrites state.kore with the new state
          → ledger_seq incremented

per failed transaction:
          → state.kore is NOT updated (implicit rollback)
          → ledger_seq is NOT incremented
```

Because `state.kore` lives on disk, the server can be stopped and restarted between transactions without losing state. To resume from a previous session, point `--state-file` at a saved `state.kore`. To start fresh, delete or omit the file.

---

## Request flow (end to end)

```
1. Client: POST {"method": "sendTransaction", "params": {"transaction": "<base64 XDR>"}}

2. StellarRpcServer.exec_send_transaction:
   - TransactionEnvelope.from_xdr(xdr, network_passphrase)
   - tx_hash = envelope.hash_hex()
   - NodeInterpreter.run_transaction(state_file, envelope.transaction)

3. NodeInterpreter.run_transaction:
   - encode_transaction_to_json(tx) → JSON string (or None for wasm upload)
   - run_request_file(state_file, json_str)  ← JSON path
     OR
     run_steps(state_file, kast_steps)       ← KORE path

4. run_request_file:
   - writes request.json to temp dir
   - krun state.kore --definition simbolik --output kore --parser cat --term
   - K semantics: insert-handleRequestFile → handleRequest → decode JSON
     → execute steps → removeRequestFile → setExitCode(0)
   - returns InterpreterResponse(final_kore=stdout, trace=...)

5. StellarRpcServer:
   - state_file.write_text(result.final_kore)
   - ledger_seq += 1
   - _transactions[tx_hash] = {status: SUCCESS, trace: result.trace, ...}
   - returns {hash, status: PENDING, latestLedger, latestLedgerCloseTime}

6. Client: POST {"method": "getTransaction", "params": {"hash": "<hash>"}}
   → returns {status: SUCCESS, ledger, createdAt, envelopeXdr, trace, ...}
```

---

## Dependencies

| Dependency | Role |
|---|---|
| `komet` | Soroban K semantics, `SorobanDefinition`, `SCValue` dataclasses, `kasmer` step types |
| `pyk` | K toolchain Python bindings: `krun`, `kdist`, KORE/KAst parsing, `JsonRpcServer` |
| `pykwasm` | Wasm → K AST conversion (`wasm2kast`), used in the KORE path for wasm upload |
| `stellar_sdk` | Stellar transaction types, XDR encoding/decoding, `TransactionEnvelope` |

---

## What's not yet implemented

- `resultXdr` / `resultMetaXdr` in `getTransaction` responses (contract return values)
- `simulateTransaction` (dry-run without state mutation)
- `getEvents`, `getLedgerEntries`, `getFeeStats` and other read-only RPC methods
- Persistent transaction store (results lost on server restart)
- Persistent ledger counter (resets to 0 on server restart)
- `ExtendFootprintTTL` and `RestoreFootprint` operations

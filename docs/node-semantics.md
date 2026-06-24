# `kdist/node.md` — K Semantics

`node.md` is the K module that implements the **entire RPC layer** on the K side. It reads `request.json`, dispatches on the RPC method, reads and updates the bookkeeping files, executes transaction steps via KASMER (Komet's harness for running Soroban operations as `Step`s), and writes the JSON-RPC `response.json`. Everything that is part of the Soroban/Stellar protocol — method dispatch, receipt bookkeeping, ledger accounting, status determination, response formatting — lives here rather than in Python.

It is compiled by `kdist/plugin.py` into the `komet-node.simbolik` LLVM binary, cached under `~/.cache/kdist-*/komet-node/simbolik/`.

---

## Files

The semantics communicate with the Python process through files in the working directory (the io dir), using the file-system hooks. All paths are relative, resolved against the cwd that `NodeInterpreter` sets before each run. The table below covers only the files the semantics touch through these hooks; for the complete io-dir layout, including `state.kore`, see [architecture.md](architecture.md#the-io-dir).

| File | Direction | Contents |
|---|---|---|
| `request.json` | Python → K | the request envelope (`method`, `id`, `now`, and method-specific fields) |
| `response.json` | K → Python | the JSON-RPC response (`{jsonrpc, id, result}`) |
| `metadata.json` | K ↔ K | `{"latest_ledger": N}` — the ledger counter |
| `receipts/receipt_<hash>.json` | K → Python | one stored receipt per transaction, keyed by tx hash |
| `traces/trace_<hash>.jsonl` | K → Python | one execution trace per transaction (per-instruction records), keyed by tx hash |

---

## Request lifecycle

The lifecycle fires when K starts in the idle state — `<k>`, `<instrs>`, and `<program>` all empty — and `request.json` is present.

```
K starts (idle state read from state.kore)
         │
         ├─ request.json exists? ──no──► halt immediately (idle state is the output)
         │
         yes
         ▼
insert-handleRequestFile → handleRequestFile
         │
         ▼
#dispatch(String2JSON(#readFile("request.json")))
         │
         ▼
#dispatchMethod(method, request)        ← routes on the "method" field
         │
         ├─ getHealth / getNetwork / getLatestLedger / getTransaction / traceTransaction → #respond(...)
         │
         └─ sendTransaction → #runTx → run steps
                → #finalizeTx → record receipt + bump ledger → #respond(...)
         ▼
#respond(id, result)
    write response.json {jsonrpc, id, result} ; remove request.json ; exitCode 0
         ▼
K halts — the updated idle state is the output, saved as state.kore
```

If `request.json` is absent, `insert-handleRequestFile` does not fire and K halts immediately with the idle state.

---

## Dispatch and the read-only methods

`#dispatch` reads the `method` field and routes to a per-method rule. The read-only methods answer directly from constants and the bookkeeping files:

- `getHealth` → `{ "status": "healthy" }`
- `getNetwork` → `{ "friendbotUrl": null, "passphrase": ..., "protocolVersion": ... }` (passphrase/version come from the request, keeping the semantics network-agnostic)
- `getLatestLedger` → reads `metadata.json` and returns `{ "id": <64 zeros>, "protocolVersion": ..., "sequence": <latest_ledger> }`
- `getTransaction` → reads the hash's `receipts/receipt_<hash>.json` file; returns the stored receipt merged with the current `latestLedger`/`latestLedgerCloseTime`, or `{ "status": "NOT_FOUND", ... }` when the file is absent

`#respond(ID, RESULT)` is the shared terminal: it writes the JSON-RPC envelope to `response.json`, removes `request.json`, and sets the exit code to 0.

---

## Transaction methods

`sendTransaction` is the only method that executes a transaction, via `#runTx`. `traceTransaction` does not run anything; it reads back the trace `sendTransaction` already stored (see [traceTransaction](#tracetransaction) below).

```
#runTx(request)
   => #enableTrace(traces/trace_<hash>.jsonl)     ← clear the trace file and point <ioDir> at it
   ~> setLedgerSequence(<latest_ledger from metadata.json>)
   ~> #decodeSteps(<the "steps" array>)           ← KASMER runs each decoded step
   ~> #finalizeTx(request)
```

`#finalizeTx` reads `metadata.json`, then:

1. writes `metadata.json` with `latest_ledger + 1`,
2. writes the receipt to `receipts/receipt_<hash>.json`:
   `{ status: "SUCCESS", ledger, createdAt, envelopeXdr, resultXdr: "", resultMetaXdr: "" }`,
3. responds with `{hash, status: "PENDING", latestLedger, latestLedgerCloseTime}`.

The trace is not part of the receipt — the executing steps already appended it to `traces/trace_<hash>.jsonl`. Reaching `#finalizeTx` means the steps completed without getting stuck, so the status is `SUCCESS`. A failed transaction gets stuck before this point, `response.json` is never written, and the Python server records the `FAILED` receipt instead.

### traceTransaction

`traceTransaction` is a read-only lookup. It takes a `hash` (the same parameter `getTransaction` takes) and responds with the contents of `traces/trace_<hash>.jsonl`, or `null` when no trace file exists for that hash. Because tracing is always on, every `sendTransaction` writes this file.

### Two ways steps are delivered

- **JSON steps** (the common case): the operations are decoded from the `"steps"` array of the request envelope by `#decodeSteps` / `#decodeStep`.
- **`<program>` injection** (wasm uploads only): the `uploadWasm` step — whose `ModuleDecl` has no JSON form — is spliced into the `<program>` cell by `NodeInterpreter` before the run. KASMER's `load-program` rule runs it first; once `<program>` drains, `insert-handleRequestFile` fires and the request envelope (with an empty `"steps"`) drives the bookkeeping. Both paths converge on `#finalizeTx`.

---

## JSON helpers

`node.md` carries a small set of order-independent JSON accessors used for the request envelope and the bookkeeping files:

- `#getJSON(key, obj[, default])`, `#getString(key, obj)`, `#getInt(key, obj)` — read a field
- `#concatJSONs(a, b)` — append object entries (used to merge `latestLedger` fields into a stored receipt)
- `#receiptFile(hash)`, `#traceFile(hash)` — build the per-transaction file paths (`receipts/receipt_<hash>.json`, `traces/trace_<hash>.jsonl`)

These complement the **order-sensitive** step decoders below.

---

## JSON step decoding

Step decoding pattern-matches on the `JSON` sort. Key order in the step objects **must** match the order of keys in the K patterns exactly, because K's `JSON` sort is ordered.

```
#decodeSteps(S, SS)                            →  #decodeStep(S) #decodeSteps(SS)
#decodeStep({ "op": "setLedgerSequence", ... })→  setLedgerSequence(...)
#decodeStep({ "op": "setAccount",        ... })→  setAccount(...)
#decodeStep({ "op": "deployContract",    ... })→  deployContract(...)
#decodeStep({ "op": "callTx",            ... })→  callTx(...)
```

SCVal arguments are decoded by `#decodeArg`, which matches on `"type"` and produces a K `ScVal` constructor (`SCBool`, `I32`, `U32`, `I64`, `U64`, `I128`, `U128`, `Symbol`, `ScBytes`, `ScAddress`).

The `steps-done` rule (mirroring KASMER's `steps-empty` but with a `...` frame) consumes the final `.Steps` so the `#finalizeTx` continuation can proceed.

---

## Helper functions

### `HexBytes(String) → Bytes`

`HexBytes` decodes a lowercase hex string to `Bytes` (big-endian), preserving leading zero bytes via an explicit byte count.

```k
rule HexBytes("") => .Bytes
rule HexBytes(S)  => Int2Bytes(lengthString(S) /Int 2, String2Base(S, 16), BE)
  requires lengthString(S) >Int 0
```

### `string2WasmToken(String) → WasmStringToken`

`string2WasmToken` wraps a K `String` into a `WasmStringToken` (`hook(STRING.string2token)`). It is required because `callTx` expects a `WasmString` for the function name.

---

## Supporting modules

### `fs.md` — `FILE-OPERATIONS`

`fs.md` provides `#readFile`, `#writeFile`, `#appendFile`, `#fileExists`, and `#remove` as K functions backed by K's built-in I/O hooks (`#open`, `#read`, `#write`, `#close`). The request/response files, the bookkeeping files, and the tracing rules all use them.

### `json.md` — JSON sort

`json.md` is K Framework's built-in JSON module (not a project file). It provides the `JSON` sort with `String2JSON` / `JSON2String`, which the semantics use to parse `request.json` and to serialize `response.json` and the bookkeeping files.

---

## Tracing integration

`node.md` is compiled with `md_selector: 'k | k-tracing'`, which includes the tracing rules from `soroban-semantics`. They intercept each WebAssembly instruction and append a JSON record to the file named by the `<ioDir>` cell.

Tracing is always on. Before running the steps, `#enableTrace` clears the transaction's `traces/trace_<hash>.jsonl` file and points `<ioDir>` at it, so the intercepted instructions append to it. After the steps run, `#finalizeTx` resets `<ioDir>` to empty; the trace file is left in place for `traceTransaction` to read.

**Trace format** (one JSON record per line):

```json
{"pos": 597, "instr": ["local.get", 0], "stack": [["i64", 4]], "locals": {"0": ["i64", 4]}}
```

| Field | Description |
|---|---|
| `pos` | Byte offset of the instruction in the binary, or `null` for synthetic instructions |
| `instr` | Instruction name and operands as a JSON array |
| `stack` | Value stack at instruction entry, as `[type, value]` pairs |
| `locals` | Local variable bindings, keyed by index, as `[type, value]` pairs |

---

## Build

```sh
make kdist-build
# or
uv run kdist build komet-node.simbolik
```

`kdist/plugin.py` defines the build:
- Backend: LLVM
- Main file: `node.md`
- Syntax module: `NODE-SYNTAX`
- MD selector: `k | k-tracing`
- Depends on: `soroban-semantics.source` (the komet repo)

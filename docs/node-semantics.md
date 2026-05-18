# `kdist/node.md` — K Semantics

`node.md` is the K module that implements the JSON fast path on the K side. It defines the request lifecycle rules that fire when `request.json` is present and execute the decoded steps against the Soroban state.

It is compiled by `kdist/plugin.py` into the `komet-node.simbolik` LLVM binary, cached under `~/.cache/kdist-*/komet-node/simbolik/`.

---

## Request lifecycle

The lifecycle fires when K starts in the idle state: `<k>`, `<instrs>`, and `<program>` are all empty.

```
K starts (idle state read from state.kore)
         │
         ├─ request.json exists? ──no──► halt immediately
         │                               (idle state is the output — saved as state.kore)
         │
         yes
         │
         ▼
insert-handleRequestFile
    <k> .K => #handleRequestFile </k>
         │
         ▼
handleRequestFile
    setExitCode(1)                  ← guard: non-zero means "in progress"
    ~> #handleRequest(file_contents)
    ~> #removeRequestFile
    ~> setExitCode(0)               ← success marker
         │
         ▼
handleRequest
    String2JSON(S) → #decodeRequest → Steps injected into <k>
         │
         ▼
steps-seq / steps-done
    KASMER executes each Step; steps-done consumes .Steps
    and lets the continuation (#removeRequestFile ~> setExitCode(0)) proceed
         │
         ▼
removeRequestFile
    #remove("request.json")
         │
         ▼
setExitCode(0)
    K halts with exitCode=0 — the idle state is the output
```

If `request.json` does not exist when K starts, `insert-handleRequestFile` does not fire and K halts immediately with the idle state as output. This idle state is then saved as `state.kore` and reused for the next request.

---

## Rules

### `insert-handleRequestFile`

```k
rule [insert-handleRequestFile]:
    <k> .K => #handleRequestFile </k>
    <instrs> .K </instrs>
    <program> .Steps </program>
  requires #fileExists("request.json")
```

Entry point. Fires only when all three cells are empty **and** `request.json` is present. If the file is absent, this rule does not fire and execution terminates (idle state).

### `handleRequestFile`

```k
rule [handleRequestFile]:
    <k> #handleRequestFile
     => setExitCode(1)
     ~> #handleRequest({#readFile("request.json")}:>String)
     ~> #removeRequestFile
     ~> setExitCode(0) ...
    </k>
```

Reads the file and sets up the full execution pipeline with an exit-code guard. `setExitCode(1)` means "execution in progress / failed"; it is overwritten to `0` only if all steps complete without error.

### `handleRequest` / `#decodeRequest`

```k
rule [handleRequest]:
    <k> #handleRequest(S:String) => #decodeRequest(String2JSON(S)) ... </k>
```

Parses the JSON string and decodes it into a `Steps` sequence using `#decodeRequest` → `#decodeSteps` → `#decodeStep`.

### `steps-done`

```k
rule [steps-done]:
    <k> .Steps => .K ... </k>
    <instrs> .K </instrs>
```

KASMER's standard `steps-empty` rule requires `<k> .Steps </k>` with no frame (exact match). In the JSON path, steps are injected into `<k>` with a continuation (`#removeRequestFile ~> setExitCode(0)`), so `steps-empty` would never fire. This supplementary rule handles `.Steps` when a continuation follows, consuming it and allowing the rest to proceed.

### `removeRequestFile`

```k
rule [removeRequestFile]:
    <k> #removeRequestFile => #remove("request.json") ... </k>
```

Deletes `request.json` after the steps have been executed, cleaning up the temp dir.

---

## JSON decoding

JSON decoding is a chain of pure K functions that pattern-match on the `JSON` sort. Key order in the JSON objects **must** match the order of keys in the K patterns exactly, because K's `JSON` sort is ordered (backed by an ordered list of key-value pairs).

```
#decodeRequest({ "steps": [SS] })  →  #decodeSteps(SS)
#decodeSteps(S, SS)                →  #decodeStep(S) #decodeSteps(SS)
#decodeStep({ "op": "setAccount",     ... })  →  setAccount(...)
#decodeStep({ "op": "deployContract", ... })  →  deployContract(...)
#decodeStep({ "op": "callTx",         ... })  →  callTx(...)
```

SCVal arguments are decoded by `#decodeArg`, which pattern-matches on `"type"` and produces a K `ScVal` constructor (`SCBool`, `I32`, `U32`, `I64`, `U64`, `I128`, `U128`, `Symbol`, `ScBytes`, `ScAddress`).

---

## Helper functions

### `HexBytes(String) → Bytes`

Decodes a lowercase hex string to `Bytes` (big-endian, length = `hex_length / 2`). Uses `String2Base(S, 16)` for the integer value and `Int2Bytes` with an explicit byte count to preserve leading zero bytes.

```k
rule HexBytes("") => .Bytes
rule HexBytes(S)  => Int2Bytes(lengthString(S) /Int 2, String2Base(S, 16), BE)
  requires lengthString(S) >Int 0
```

### `string2WasmToken(String) → WasmStringToken`

Wraps a K `String` into a `WasmStringToken` using `hook(STRING.string2token)`. Required because `callTx` expects a `WasmString` (the function name), not a plain K `String`.

---

## Supporting modules

### `fs.md` — `FILE-OPERATIONS`

Provides `#readFile`, `#writeFile`, `#appendFile`, `#fileExists`, and `#remove` as K functions backed by K's built-in I/O hooks (`#open`, `#read`, `#write`, `#close`). Used by `node.md` for the `request.json` lifecycle and by the tracing rules for appending trace records.

### `json.md` — JSON sort

Provides the `JSON` sort and `String2JSON` used by `handleRequest` to parse the request body.

---

## Tracing integration

`node.md` is compiled with `md_selector: 'k | k-tracing'`, which includes the tracing K rules from `soroban-semantics`. These rules intercept each WebAssembly instruction before execution and append a JSON record to the file at path `<ioDir>`.

Tracing is activated by `<ioDir>` being non-empty. When the server is started with `--trace`, `empty_config()` bakes `<ioDir>trace.jsonl</ioDir>` into `state.kore`, enabling tracing for all subsequent transactions. See [interpreter.md](interpreter.md) for details.

---

## Build

```sh
make kdist-build
# or
uv run kdist build komet-node.simbolik
```

Defined in `kdist/plugin.py`:
- Backend: LLVM
- Main file: `node.md`
- Syntax module: `NODE-SYNTAX`
- MD selector: `k | k-tracing`
- Depends on: `soroban-semantics.source` (the komet repo)

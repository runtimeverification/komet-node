# komet-node: Handoff Notes

## What this does

`komet-node` is a Stellar node backed by the K semantics. The core component implemented so far is `NodeInterpreter`, which takes a `.kore` file (blockchain state) and a Stellar `Transaction`, executes the operations through `krun`, and returns the updated state as kore text. A server that listens for incoming transactions, manages state persistence, and calls `NodeInterpreter` is yet to be built.

See `src/komet_node/demo.py` for an end-to-end example: empty state → create account → upload wasm → deploy contract → call `foo()`.

---

## What's implemented

### `NodeInterpreter` (`src/komet_node/interpreter.py`)

Responsible for translating a Stellar `Transaction` into K steps and executing it against a given `.kore` state file. It is not the top-level entry point — a server (not yet implemented) will sit above it, handling incoming requests, managing the state file on disk, and owning the request/response lifecycle.

`run_transaction(input_file, transaction)` translates each Stellar operation to a K step and runs it.

Supported operations:

| Stellar operation | K step |
|---|---|
| `CreateAccount` | `setAccount(Account(bytes), stroops)` |
| `InvokeHostFunction` / upload wasm | `uploadWasm(hash, ModuleDecl)` |
| `InvokeHostFunction` / create contract (V1, V2) | `deployContract(from, address, wasmHash)` |
| `InvokeHostFunction` / invoke contract | `callTx(from, to, func, args, Void)` |

Execution path (`run_steps`):
1. Parse `input.kore` → Python AST (`kore_to_kast`)
2. Inject K steps into the `PROGRAM_CELL` of the config
3. Re-serialize to KORE and run `krun`

### `scval.py`

Converts Stellar XDR `SCVal` to Komet `SCValue` dataclasses, used when building `callTx` arguments. Covers all numeric types, bool, symbol, bytes, address, vec, map.

### K semantics (`src/komet_node/kdist/node.md`)

These rules exist specifically to support the JSON fast path. When the configuration is on the idle state (empty `<k>`, `<instrs>`, `<program>` cells):
- If `request.json` exists in the working directory → read it, remove it, dispatch `#handleRequest(contents)`, halt
- If not → halt immediately (this is the expected idle state)

`#handleRequest(String)` is **declared but not implemented** — that's the main TODO.

---

## What needs to be implemented

The current `run_steps` path is expensive — it parses the KORE file, converts to KAst, mutates the AST, and re-serializes.
This round-trip is only necessary when uploading wasm, because the wasm module must be embedded as a `ModuleDecl` in the K AST.
For all other operations, a JSON fast path can be used: Python writes a `request.json` describing the transaction,
and the K semantics read and execute it directly against the idle state — no KORE parsing or AST manipulation required.

1. Design the JSON format for `request.json`
2. Implement the transaction encoder in Python (`interpreter.py`)
3. Implement the transaction decoder in K (`node.md`) — parse the JSON into K terms
4. Implement the transaction handler in K (`node.md`) — execute the decoded steps against the state

# komet-node: Handoff Notes

## What this does

`komet-node` is a Stellar node backed by the K semantics. The core component is `NodeInterpreter`, which takes a `.kore` file (blockchain state) and a Stellar `Transaction`, executes the operations through `krun`, and returns the updated state as kore text. A server that listens for incoming transactions, manages state persistence, and calls `NodeInterpreter` is yet to be built.

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

Execution paths:

**JSON fast path** (`run_request_file`) — used for all operations except wasm upload:
1. Encode the transaction as a JSON string (`encode_transaction_to_json`)
2. Write it as `request.json` in a temp working directory
3. Run `krun` on the idle `.kore` state — K reads the file, decodes it, executes the steps, removes the file, and halts

**KORE round-trip** (`run_steps`) — used only for wasm upload, which must embed a `ModuleDecl` in the K AST:
1. Parse `input.kore` → Python AST (`kore_to_kast`)
2. Inject K steps into the `PROGRAM_CELL` of the config
3. Re-serialize to KORE and run `krun`

### `scval.py`

Converts Stellar XDR `SCVal` to Komet `SCValue` dataclasses, used when building `callTx` arguments. Covers all numeric types, bool, symbol, bytes, address, vec, map.

### K semantics (`src/komet_node/kdist/node.md`)

Implements the JSON fast path on the K side. When the configuration is in the idle state (empty `<k>`, `<instrs>`, `<program>` cells):
- If `request.json` exists → read it, dispatch `#handleRequest(contents)`, remove the file, halt
- If not → halt immediately (expected idle state, ready for the next request)

`#handleRequest` decodes the JSON string into K `Steps` using `String2JSON` and a set of `#decodeStep` / `#decodeArg` rules, then injects them directly into `<k>`. A `steps-done` rule (mirroring KASMER's `steps-empty` but with `...`) is needed to let the continuation proceed once the decoded steps finish.

Supported JSON step types and SCVal arg types mirror the Python encoder — see the format comment in `node.md`.

### Tests (`src/tests/integration/`)

- `test_full_lifecycle` — end-to-end: create account → upload wasm (KORE path) → deploy contract → call `foo()` (no args)
- `test_callTx_with_args` — deploys `args.wat` and calls functions with `bool`, `u32`, `i32`, `u64`, `i64`, `u128`, `i128`, and `symbol` args, exercising the full `_encode_scval` / `#decodeArg` pipeline

Not yet covered by tests: `bytes` and `address` SCVal types (require host object allocation).

---

## What needs to be implemented

A server that sits above `NodeInterpreter`: listens for incoming Stellar RPC requests, manages the `.kore` state file on disk, and calls `run_transaction` for each incoming transaction.

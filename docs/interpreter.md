# `interpreter.py` — `NodeInterpreter`

`NodeInterpreter` runs komet-node's RPC requests through the compiled K semantics (LLVM backend). It builds the initial configuration, feeds each request envelope to the interpreter against `state.kore`, and persists the resulting world state. It knows nothing about Stellar — XDR decoding lives in [`TransactionEncoder`](transaction.md), and RPC dispatch / bookkeeping / response formatting live in [`node.md`](node-semantics.md).

---

## Class structure

```python
class NodeInterpreter:
    definition: SimbolikDefinition   # compiled K definition (komet-node.simbolik)
```

`SimbolikDefinition` is a thin subclass of `komet.SorobanDefinition`, pointing to the `komet-node.simbolik` compiled K definition (cached under `~/.cache/kdist-*/komet-node/simbolik/`). There is no `network_passphrase` or `trace` here — those belong to the request side (`TransactionEncoder`); the interpreter only runs K.

---

## No `kast`↔`kore` conversions

Whole-configuration `kore_to_kast` / `kast_to_kore` conversions take seconds and get slower as the configuration grows, so the interpreter avoids them entirely. `state.kore` is only ever parsed with `KoreParser` (KORE text → KORE AST, which is cheap) and handed straight to `llvm_interpret`. Terms that must be constructed are built directly in KORE.

### `empty_config()`

`empty_config()` produces the initial blank-slate `state.kore`. It builds the top-cell initializer **in KORE** — seeding `$PGM` with a single `setExitCode(0)` step and `$TRACE` with an empty string — and runs it through the interpreter. No `krun` subprocess and no kast conversion are involved.

```python
config = top_cell_initializer({
    '$PGM':   inj(SortSteps, K_ITEM, kasmerSteps(setExitCode(0), .Steps)),  # built in KORE
    '$TRACE': inj(SortString, K_ITEM, str_dv('')),
})
with tempfile.TemporaryDirectory() as isolated_dir:        # see note below
    return _llvm_interpret(self.definition.path, config, cwd=isolated_dir).text
```

The result is the empty idle K configuration — no accounts, no contracts, no storage.

The run happens in a throwaway empty directory on purpose. The idle configuration ends with empty `<k>`/`<program>` cells, which is exactly the precondition that makes the request-handling rule fire if a `request.json` is present. Running in an empty directory guarantees no stray `request.json` is picked up and dispatched into the configuration that is about to be saved as `state.kore`.

### `run(state_file, io_dir, request, program_steps=None)`

`run` is the main entry point. It runs a single RPC request envelope through the following steps:

1. Write the request envelope to `request.json` in `io_dir`, and delete any stale `response.json`.
2. Parse `state.kore` with `KoreParser` (no kast conversion).
3. For a wasm upload only, splice the upload steps into the `<program>` cell (see below).
4. Run the interpreter with its subprocess working directory set to `io_dir` (so the K file-system hooks resolve the relative paths `request.json`, `response.json`, `metadata.json`, `transactions.json`, `trace.jsonl`). The directory is set on the subprocess only — the server's own process never `chdir`s, so concurrent requests in other threads are unaffected.
5. If the semantics wrote `response.json`, persist the new configuration to `state.kore` and return the response text. If not, the transaction got stuck (failed) — leave `state.kore` unchanged and return `None`, so the caller can synthesise a failure response.

### `_inject_program(pattern, steps)` — the wasm-upload path

A wasm upload cannot be expressed as a JSON step, because the resulting `ModuleDecl` (the parsed Wasm AST from `wasm2kast`) has no JSON form. Instead the steps are injected directly into the `<program>` cell of the already-parsed configuration, so KASMER runs them before the request is dispatched.

The injection is done at the **KORE level**: the small steps term is converted to KORE and spliced into the `<program>` cell of the parsed pattern. The whole-configuration round-trip is deliberately avoided. The one remaining `kast_to_kore` call here is bounded by the size of the uploaded module (the only thing that can originate solely as KAST), not by the accumulated world state.

```python
steps_kore = kast_to_kore(self.definition.kdefinition, steps_of(steps), KSort('Steps'))
return _set_cell(pattern, "<program> cell symbol", steps_kore)   # KORE-level splice
```

Because Soroban allows only a single host-function operation per transaction, a wasm-upload transaction is exactly one `uploadWasm` op — this path never carries anything else.

### `pretty_print(kore_str)`

`pretty_print` is a debugging helper that pretty-prints a KORE configuration string using `krun --output pretty --depth 0`. `demo.py` uses it to render each step of a contract lifecycle.

---

## Supported operations

The mapping from Stellar operations to kasmer steps is performed by [`TransactionEncoder`](transaction.md); the interpreter only runs the result.

| Stellar operation | kasmer step | Delivered via |
|---|---|---|
| `CreateAccount` | `setAccount(Account(bytes), stroops)` | JSON step in `request.json` |
| `InvokeHostFunction` / upload wasm | `uploadWasm(hash, ModuleDecl)` | `<program>` cell (KORE) |
| `InvokeHostFunction` / create contract (V1, V2) | `deployContract(from, address, wasmHash)` | JSON step in `request.json` |
| `InvokeHostFunction` / invoke contract | `callTx(from, to, func, args, Void)` | JSON step in `request.json` |

---

## Error handling

`NodeInterpreterError` is raised for interpreter-level failures (e.g. `pretty_print`). A *transaction* failure is not an exception: the semantics get stuck without writing `response.json`, so `run` returns `None` and the server records a `FAILED` receipt while leaving `state.kore` unchanged (the state effectively rolls back).

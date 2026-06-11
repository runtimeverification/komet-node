# `interpreter.py` — `NodeInterpreter`

`NodeInterpreter` is the core of komet-node. It translates a `stellar_sdk.Transaction` into K execution steps, runs them through the compiled K semantics via `krun`, and returns the updated blockchain state.

---

## Class structure

```python
class NodeInterpreter:
    definition: SimbolikDefinition   # compiled K definition (komet-node.simbolik)
    network_passphrase: str
    trace: bool                      # whether to enable instruction tracing
```

`SimbolikDefinition` is a thin subclass of `komet.SorobanDefinition`, pointing to the `komet-node.simbolik` compiled K definition (cached under `~/.cache/kdist-*/komet-node/simbolik/`).

---

## Execution paths

`run_transaction(state_file, transaction)` is the main entry point. It chooses between two execution strategies depending on the transaction content:

```
Transaction
    │
    ├─ can encode to JSON? ──yes──► JSON fast path (run_request_file)
    │                                   writes request.json, krun reads it
    │
    └─ no (wasm upload) ────────► KORE round-trip (run_steps)
                                    Python parses state.kore, mutates AST,
                                    re-serializes, krun on full KORE term
```

### JSON fast path (`run_request_file`)

Used for all operations except wasm upload. The goal is to avoid Python-side KORE parsing and AST manipulation entirely.

1. `encode_transaction_to_json(transaction)` serializes the transaction as a JSON string. Returns `None` if any operation cannot be expressed as JSON (currently only wasm upload).
2. A temporary working directory is created. `request.json` is written there.
3. `krun` is invoked with the current `state.kore` as input (`--term`). The K semantics detect `request.json`, read and decode it, execute the steps, remove the file, and halt. The updated state is captured from stdout.
4. If tracing is enabled, `trace.jsonl` is read from the temp dir and included in the response.

```python
with temp_working_directory() as root:
    (root / 'request.json').write_text(request_str)
    res = _krun(input_file=state_file, definition_dir=..., term=True, output=KORE)
    trace = (root / 'trace.jsonl').read_text() if tracing else None
    return InterpreterResponse(final_kore=res.stdout, trace=trace)
```

Each request runs in its own temp dir so that concurrent requests (if added) and trace files are isolated.

### KORE round-trip (`run_steps`)

Used only for wasm upload. Wasm upload requires embedding a parsed `ModuleDecl` (the Wasm AST) directly into the K configuration — something that cannot be expressed as a flat JSON string.

1. Parse `state.kore` into a Python KORE AST.
2. Convert the AST to KAst (`kore_to_kast`).
3. Inject K steps (including the `ModuleDecl`) into the `<program>` cell.
4. Re-serialize to KORE (`kast_to_kore`) and run krun on the full term.

This path is slower because it involves full KORE parsing and re-serialization on every wasm upload, but it is only triggered once per contract deployment.

---

## Supported operations

| Stellar operation | K step | Execution path |
|---|---|---|
| `CreateAccount` | `setAccount(Account(bytes), stroops)` | JSON |
| `InvokeHostFunction` / upload wasm | `uploadWasm(hash, ModuleDecl)` | KORE round-trip |
| `InvokeHostFunction` / create contract (V1, V2) | `deployContract(from, address, wasmHash)` | JSON |
| `InvokeHostFunction` / invoke contract | `callTx(from, to, func, args, Void)` | JSON |

---

## JSON request format

The JSON fast path writes `request.json` with the following structure. **Key ordering is significant**: the K JSON sort is ordered, so Python dicts must produce keys in the same order as the K pattern-match rules in `node.md`.

```json
{
  "steps": [
    { "op": "setAccount",     "account": "<hex32>", "balance": <int> },
    { "op": "deployContract", "from": "<hex32>", "address": "<hex32>", "wasmHash": "<hex32>" },
    { "op": "callTx",         "from": "<hex32>", "fromIsContract": <bool>,
                               "func": "<name>", "to": "<hex32>", "args": [ ... ] }
  ]
}
```

### SCVal argument encoding

Contract function arguments (`callTx` args) are encoded as JSON dicts:

| SCVal type | JSON encoding |
|---|---|
| `SCV_BOOL` | `{"type": "bool", "value": true\|false}` |
| `SCV_I32` / `SCV_U32` | `{"type": "i32"\|"u32", "value": <int>}` |
| `SCV_I64` / `SCV_U64` | `{"type": "i64"\|"u64", "value": <int>}` |
| `SCV_I128` / `SCV_U128` | `{"type": "i128"\|"u128", "value": <int>}` (combined hi/lo) |
| `SCV_SYMBOL` | `{"type": "symbol", "value": "<str>"}` |
| `SCV_BYTES` | `{"type": "bytes", "value": "<lowercase hex>"}` |
| `SCV_ADDRESS` (account) | `{"type": "address", "addrType": "account", "value": "<hex32>"}` |
| `SCV_ADDRESS` (contract) | `{"type": "address", "addrType": "contract", "value": "<hex32>"}` |

---

## Initial configuration (`empty_config`)

`empty_config()` produces the initial blank-slate `state.kore` by running krun with a single `setExitCode(0)` step. The output is the empty idle K configuration — no accounts, no contracts, no storage — which the server writes to `state.kore` on first startup.

When `trace=True`, `empty_config` passes two extra arguments to krun:

```python
cmap = {'TRACE': str_dv('trace.jsonl').text}   # K string token
pmap = {'TRACE': 'cat'}                         # parser: pass through as-is
```

These initialize the `<ioDir>` configuration cell (part of the `<trace>` cell, compiled in from the `k-tracing` selector) to `"trace.jsonl"`. Because this value is baked into `state.kore`, every subsequent krun invocation reads it and writes traces to `trace.jsonl` in the current working directory — which is the per-request temp dir.

---

## Tracing

When the server is started with `--trace`, every `callTx` (contract invocation) produces an instruction-level execution trace. The trace records the VM state at each WebAssembly instruction.

**How it works**:

1. `empty_config()` bakes `<ioDir>trace.jsonl</ioDir>` into `state.kore`.
2. For each transaction, `run_request_file` creates a temp dir, runs krun from it.
3. The tracing K rules (from `soroban-semantics.tracing`) detect the non-empty `<ioDir>` and append one JSON record per instruction to `trace.jsonl` in the temp dir.
4. After krun finishes, the trace file is read and returned in `InterpreterResponse.trace`.
5. The server stores the trace string in `_transactions[hash]['trace']`, retrievable via `getTransaction`.

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

Tracing is only active for the LLVM backend. The `komet-node.simbolik` definition is compiled with `md_selector: 'k | k-tracing'`, so the tracing rules are always present; they are activated solely by `<ioDir>` being non-empty.

---

## `InterpreterResponse`

```python
class InterpreterResponse(NamedTuple):
    final_kore: str        # updated K configuration (to write back to state.kore)
    trace: str | None      # JSONL trace string, or None if tracing is disabled
```

---

## Error handling

`NodeInterpreterError` is raised when krun exits with a non-zero return code. The server catches this, stores a `FAILED` result for the transaction, and leaves `state.kore` unchanged (the state effectively rolls back).

---

## Address utilities

`NodeInterpreter` also provides helpers for Stellar address encoding/decoding:

- `decode_account_id(addr)` — G-strkey → 32-byte public key
- `decode_contract_id(addr)` — C-strkey → 32-byte contract ID
- `contract_address_from_deployer_address(deployer, salt)` — computes the C-strkey that `CREATE_CONTRACT` will assign
- `contract_id_from_preimage(preimage)` — SHA-256 of the `HashIDPreimage` as used by Stellar

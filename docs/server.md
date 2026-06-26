# `server.py` — `StellarRpcServer`

`StellarRpcServer` exposes the [Stellar RPC API](https://developers.stellar.org/docs/data/apis/rpc) over HTTP/JSON-RPC. Its job is to make the K semantics usable as a server. The compiled semantics are a one-shot interpreter — one process invocation per request, with no networking and no memory between runs — and `StellarRpcServer` is the long-running process wrapped around them: it keeps the HTTP socket open and the state files on disk, decodes the XDR envelope (via `TransactionEncoder`), runs each request through the semantics (via `NodeInterpreter`), and returns whatever `response.json` the semantics produced. All RPC dispatch, receipt bookkeeping, ledger accounting, and response formatting happen in K — the server holds none of that state itself.

---

## Class structure

```python
class StellarRpcServer:
    interpreter: NodeInterpreter     # the K runner
    encoder:     TransactionEncoder  # the XDR → request-envelope decoder
    io_dir:      Path                # directory holding every artifact
    state_file:  Path                # io_dir / 'state.kore'
    receipts_dir: Path               # io_dir / 'receipts'  — receipt_<hash>.json per transaction
    traces_dir:   Path               # io_dir / 'traces'    — trace_<hash>.jsonl per transaction
    requests_dir: Path               # io_dir / 'requests'  — request_<n>.json archive
```

The server is a plain `http.server.HTTPServer` (not pyk's `JsonRpcServer`). A `BaseHTTPRequestHandler` reads each POST body and calls `_handle`, which parses the JSON-RPC frame and delegates to `handle_rpc`.

### `handle_rpc(method, params, request_id) -> str`

`handle_rpc` is the dispatch entry point; it returns the JSON-RPC response envelope as a string. You can call it **without** the HTTP layer, which is convenient for scripts and tests:

```python
server = StellarRpcServer(io_dir=Path('out'))
server.handle_rpc('sendTransaction', {'transaction': xdr})
```

For `sendTransaction` it builds the request envelope with `encoder.build_tx_request` and runs it with `interpreter.run`; for the read-only methods (`getHealth`, `getNetwork`, `getLatestLedger`, `getTransaction`, `traceTransaction`) it builds a small envelope and runs it. In every case the *content* of the response is produced by the semantics (`node.md`), not by Python — the one exception is the failure fallback (below). Each call is logged to stderr.

---

## Startup

```python
server = StellarRpcServer(
    host='localhost',
    port=8000,
    io_dir=Path('out'),  # omit for a fresh temporary directory
    network_passphrase=Network.TESTNET_NETWORK_PASSPHRASE,
)
server.serve()
```

`io_dir` defaults to `None`, in which case the server creates a fresh temporary directory (`tempfile.mkdtemp`) and runs against that — a throwaway chain that starts empty on every launch and leaves the working directory untouched. Pass an explicit `io_dir` to keep the state in a known place.

At construction the server prepares the *io dir*, where `state.kore` lives at `io_dir / 'state.kore'`:

- **`state.kore` absent** — `interpreter.empty_config()` produces the initial idle K configuration (a blank-slate state with no accounts, contracts, or storage) and writes it; `metadata.json` is seeded with `{"latest_ledger": 0}`.
- **`state.kore` present** — it is used as-is, and `metadata.json` is seeded only if missing. This lets you resume a previous session (ledger counter and stored receipts included) or start against a pre-built state.

In both cases the server creates the `receipts/`, `traces/`, and `requests/` directories if they do not already exist, because the K file-system hooks write into them but cannot create them.

Once the socket is bound, `serve` logs three lines to stderr: whether it is starting from a fresh state (an empty io-dir) or resuming an existing one (with the latest ledger), the io-dir path, and the listening address. Instruction tracing is always on, so every transaction the semantics run produces a trace. (Tracing only produces records for contract invocations.)

---

## State lifecycle

`state.kore` and `metadata.json` hold the persistent chain state; per-transaction receipts and traces live under `receipts/` and `traces/`; the server and semantics also exchange a few transient files per request. See [architecture.md](architecture.md#the-io-dir) for the complete io-dir layout.

```
startup (state.kore absent):
          → empty_config() → state.kore ; metadata.json {latest_ledger:0}
          → create receipts/ traces/ requests/

per successful transaction:
          → the semantics run the steps (trace → traces/trace_<hash>.jsonl),
            write receipts/receipt_<hash>.json, bump latest_ledger in metadata.json,
            and write response.json
          → NodeInterpreter persists the new state.kore

per failed (stuck) transaction:
          → no response.json is produced; state.kore and metadata.json are left unchanged
          → the server writes a FAILED receipts/receipt_<hash>.json (see below)
```

Because these artifacts live on disk, the server can be stopped and restarted without losing the world state, the ledger counter, or the stored receipts.

---

## RPC methods

All methods are answered by the K semantics and follow the [Stellar RPC specification](https://developers.stellar.org/docs/data/apis/rpc/methods).

### `getHealth`

`getHealth` returns `{"status": "healthy"}`.

### `getNetwork`

```json
{ "friendbotUrl": null, "passphrase": "Test SDF Network ; September 2015", "protocolVersion": "22" }
```

### `getLatestLedger`

`getLatestLedger` returns the current ledger sequence (the `latest_ledger` from `metadata.json`), which increments by 1 per successfully committed transaction.

```json
{ "id": "0000...0000", "protocolVersion": "22", "sequence": 4 }
```

### `sendTransaction`

`sendTransaction` submits a base64-encoded XDR transaction envelope.

**Execution model**: real Stellar RPC was designed around a mempool and ledger close, so the API requires `sendTransaction` to return `PENDING` and have clients poll `getTransaction`. komet-node has no mempool — the semantics execute the transaction immediately — but it still returns `PENDING` to stay compatible with the two-step pattern.

**Response**:
```json
{ "hash": "<64-char hex>", "status": "PENDING", "latestLedger": "5", "latestLedgerCloseTime": "1716000000" }
```

### `traceTransaction`

`traceTransaction` retrieves the instruction trace of a previously submitted transaction. It takes a `hash` parameter (the same one `getTransaction` takes) and returns the trace that `sendTransaction` stored for that transaction. The result is a JSON array with one record per executed WebAssembly instruction (empty when the transaction ran no instructions), or `null` when no transaction with that hash exists.

```json
[
  {"pos": 3, "instr": ["const", "i32", 1048576], "stack": [], "locals": {}}
]
```

### `getTransaction`

`getTransaction` reads the hash's `receipts/receipt_<hash>.json` file.

| Status | Meaning |
|---|---|
| `NOT_FOUND` | No `receipts/receipt_<hash>.json` for that hash |
| `SUCCESS` | Transaction executed successfully |
| `FAILED` | Transaction was submitted but got stuck in the semantics |

**`SUCCESS` response**:
```json
{
  "status": "SUCCESS", "ledger": "5", "createdAt": "1716000000",
  "envelopeXdr": "<base64 XDR>", "resultXdr": "", "resultMetaXdr": "",
  "latestLedger": "5", "latestLedgerCloseTime": "1716000000"
}
```

`resultXdr` and `resultMetaXdr` are currently empty stubs. The receipt carries no trace — use `traceTransaction` with the same hash to fetch it.

---

## Failure fallback

A failed transaction leaves the semantics stuck without writing `response.json`, so `interpreter.run` returns `None`. Only `sendTransaction` executes a transaction, so it is the only method that reaches this path. The server then synthesises the response in Python: it writes a `FAILED` `receipts/receipt_<hash>.json` (so a later `getTransaction` finds it), without bumping the ledger, and returns `PENDING`. This is the only response content the server builds itself.

---

## CLI

```
komet-node [--host HOST] [--port PORT] [--io-dir DIR]
```

| Flag | Default | Description |
|---|---|---|
| `--host` | `localhost` | Bind address |
| `--port` | `8000` | Port |
| `--io-dir` | a fresh temp dir | Directory holding every artifact (`state.kore`, `metadata.json`, `receipts/`, `traces/`, `requests/`) |

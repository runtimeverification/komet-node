# `server.py` — `StellarRpcServer`

`StellarRpcServer` exposes the [Stellar RPC API](https://developers.stellar.org/docs/data/apis/rpc) over HTTP/JSON-RPC. Its job is to make the K semantics usable as a server. The compiled semantics are a one-shot interpreter — one process invocation per request, with no networking and no memory between runs — and `StellarRpcServer` is the long-running process wrapped around them: it keeps the HTTP socket open and the state files on disk, decodes the XDR envelope (via `TransactionEncoder`), runs each request through the semantics (via `NodeInterpreter`), and returns whatever `response.json` the semantics produced. All RPC dispatch, the transaction store, ledger accounting, and response formatting happen in K — the server holds none of that state itself.

---

## Class structure

```python
class StellarRpcServer:
    interpreter: NodeInterpreter     # the K runner
    encoder:     TransactionEncoder  # the XDR → request-envelope decoder
    io_dir:      Path                # directory holding every artifact
    state_file:  Path                # io_dir / 'state.kore' — alongside metadata.json / transactions.json
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
    io_dir=Path('.'),
    network_passphrase=Network.TESTNET_NETWORK_PASSPHRASE,
)
server.serve()
```

At construction the server prepares the *io dir*, where `state.kore` lives at `io_dir / 'state.kore'`:

- **`state.kore` absent** — `interpreter.empty_config()` produces the initial idle K configuration (a blank-slate state with no accounts, contracts, or storage) and writes it; `metadata.json` is seeded with `{"latest_ledger": 0}` and `transactions.json` with `{}`.
- **`state.kore` present** — it is used as-is, and the sidecar files are seeded only if missing. This lets you resume a previous session (ledger counter and transaction store included) or start against a pre-built state.

Once the socket is bound, `serve` logs the listening address to stderr and reports whether it is starting from a fresh state (an empty io-dir) or resuming an existing one (with the latest ledger). Instruction tracing is always on, so every transaction the semantics run produces a trace. (Tracing only produces records for contract invocations.)

---

## State file lifecycle

Server state is split across three files in the io dir; see [architecture.md](architecture.md#state-management) for the full table.

```
startup (state.kore absent):
          → empty_config() → state.kore ; metadata.json {latest_ledger:0} ; transactions.json {}

per successful transaction:
          → the semantics run the steps, append a SUCCESS receipt to transactions.json,
            bump latest_ledger in metadata.json, and write response.json
          → NodeInterpreter persists the new state.kore

per failed (stuck) transaction:
          → no response.json is produced; state.kore and metadata.json are left unchanged
          → the server synthesises a FAILED receipt (see below)
```

Because all three files live on disk, the server can be stopped and restarted without losing the world state, the ledger counter, or the transaction store.

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

`traceTransaction` retrieves the instruction trace of a previously submitted transaction. It takes a `hash` parameter (the same one `getTransaction` takes) and returns the trace that `sendTransaction` stored on that transaction's receipt. The result is the trace itself: a JSONL string with one record per executed WebAssembly instruction, or `null` when no transaction with that hash exists.

```json
"<jsonl string>"
```

### `getTransaction`

`getTransaction` looks up the stored receipt in `transactions.json`.

| Status | Meaning |
|---|---|
| `NOT_FOUND` | Hash not in `transactions.json` |
| `SUCCESS` | Transaction executed successfully |
| `FAILED` | Transaction was submitted but got stuck in the semantics |

**`SUCCESS` response**:
```json
{
  "status": "SUCCESS", "ledger": "5", "createdAt": "1716000000",
  "envelopeXdr": "<base64 XDR>", "resultXdr": "", "resultMetaXdr": "",
  "trace": "<jsonl string or null>",
  "latestLedger": "5", "latestLedgerCloseTime": "1716000000"
}
```

`resultXdr` and `resultMetaXdr` are currently empty stubs.

---

## Failure fallback

A failed transaction leaves the semantics stuck without writing `response.json`, so `interpreter.run` returns `None`. Only `sendTransaction` executes a transaction, so it is the only method that reaches this path. The server then synthesises the response in Python: it records a `FAILED` receipt in `transactions.json` (so a later `getTransaction` finds it), without bumping the ledger, and returns `PENDING`. This is the only response content the server builds itself.

---

## CLI

```
komet-node [--host HOST] [--port PORT] [--io-dir DIR]
```

| Flag | Default | Description |
|---|---|---|
| `--host` | `localhost` | Bind address |
| `--port` | `8000` | Port |
| `--io-dir` | `.` | Directory holding every artifact (`state.kore`, `metadata.json`, `transactions.json`) |

# `server.py` — `StellarRpcServer`

`StellarRpcServer` is the outermost layer of komet-node. It exposes the [Stellar RPC API](https://developers.stellar.org/docs/data/apis/rpc) over HTTP/JSON-RPC and owns the request/response lifecycle. It is a thin shim: it decodes the XDR envelope (via `TransactionEncoder`), runs the request through the K semantics (via `NodeInterpreter`), and returns whatever `response.json` the semantics produced. All RPC dispatch, the transaction store, ledger accounting, and response formatting happen in K — the server holds none of that state.

---

## Class structure

```python
class StellarRpcServer:
    interpreter: NodeInterpreter     # the K runner
    encoder:     TransactionEncoder  # the XDR → request-envelope decoder
    state_file:  Path                # state.kore on disk
    io_dir:      Path                # state_file.parent — also holds metadata.json / transactions.json
```

The server is a plain `http.server.HTTPServer` (not pyk's `JsonRpcServer`). A `BaseHTTPRequestHandler` reads each POST body and calls `_handle`, which parses the JSON-RPC frame and delegates to `handle_rpc`.

### `handle_rpc(method, params, request_id) -> str`

The dispatch entry point, returning the JSON-RPC response envelope as a string. It is usable **without** the HTTP layer, which is convenient for scripts and tests:

```python
server = StellarRpcServer(state_file=Path('out/state.kore'))
server.handle_rpc('sendTransaction', {'transaction': xdr})
```

For `sendTransaction` / `traceTransaction` it builds the request envelope with `encoder.build_tx_request` and runs it with `interpreter.run`; for the read-only methods it builds a small envelope and runs it. In every case the *content* of the response is produced by the semantics (`node.md`), not by Python — the one exception is the failure fallback (below).

---

## Startup

```python
server = StellarRpcServer(
    host='localhost',
    port=8000,
    state_file=Path('state.kore'),
    network_passphrase=Network.TESTNET_NETWORK_PASSPHRASE,
    trace=False,
)
server.serve()
```

At construction the server prepares the *io dir* (`state_file.parent`):

- **`state.kore` absent** — `interpreter.empty_config()` produces the initial idle K configuration (a blank-slate state with no accounts, contracts, or storage) and writes it; `metadata.json` is seeded with `{"latest_ledger": 0}` and `transactions.json` with `{}`.
- **`state.kore` present** — it is used as-is, and the sidecar files are seeded only if missing. This lets you resume a previous session (ledger counter and transaction store included) or start against a pre-built state.

The `trace` flag is passed to `TransactionEncoder`; when set, every transaction request carries `"trace": true`, so the semantics enable instruction tracing. (Tracing only produces records for contract invocations.)

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

Returns `{"status": "healthy"}`.

### `getNetwork`

```json
{ "friendbotUrl": null, "passphrase": "Test SDF Network ; September 2015", "protocolVersion": "22" }
```

### `getLatestLedger`

Returns the current ledger sequence (the `latest_ledger` from `metadata.json`), which increments by 1 per successfully committed transaction.

```json
{ "id": "0000...0000", "protocolVersion": "22", "sequence": 4 }
```

### `sendTransaction`

Submits a base64-encoded XDR transaction envelope.

**Execution model**: real Stellar RPC was designed around a mempool and ledger close, so the API requires `sendTransaction` to return `PENDING` and have clients poll `getTransaction`. komet-node has no mempool — the semantics execute the transaction immediately — but it still returns `PENDING` to stay compatible with the two-step pattern.

**Response**:
```json
{ "hash": "<64-char hex>", "status": "PENDING", "latestLedger": "5", "latestLedgerCloseTime": "1716000000" }
```

### `traceTransaction`

Like `sendTransaction`, but enables instruction tracing and returns the result **inline** in a single call (no polling). The `trace` field is a JSONL string, one record per executed WebAssembly instruction.

```json
{ "hash": "<hex>", "status": "SUCCESS", "ledger": "5", "trace": "<jsonl>",
  "latestLedger": "5", "latestLedgerCloseTime": "1716000000" }
```

### `getTransaction`

Looks up the stored receipt in `transactions.json`.

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

A failed transaction leaves the semantics stuck without writing `response.json`, so `interpreter.run` returns `None`. The server then synthesises the response in Python: it records a `FAILED` receipt in `transactions.json` (so a later `getTransaction` finds it), without bumping the ledger, and returns `PENDING` (for `sendTransaction`) or the `FAILED` result (for `traceTransaction`). This is the only response content the server builds itself.

---

## CLI

```
komet-node [--host HOST] [--port PORT] [--state-file PATH] [--trace]
```

| Flag | Default | Description |
|---|---|---|
| `--host` | `localhost` | Bind address |
| `--port` | `8000` | Port |
| `--state-file` | `state.kore` | Path to the persistent state file (its directory also holds `metadata.json` / `transactions.json`) |
| `--trace` | off | Enable instruction-level execution tracing |

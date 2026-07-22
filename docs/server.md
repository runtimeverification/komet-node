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
    ledgers_dir:  Path               # io_dir / 'ledgers'   — ledger_<seq>.json per closed ledger
    requests_dir: Path               # io_dir / 'requests'  — request_<n>.json archive
```

The server is a plain `http.server.HTTPServer` (not pyk's `JsonRpcServer`). A `BaseHTTPRequestHandler` reads each POST body and calls `_handle`, which parses the JSON-RPC frame and delegates to `handle_rpc`.

### JSON-RPC framing

The server implements the JSON-RPC 2.0 framing rules, including batch calls:

- A single request object gets a single response object.
- An **array** body is a batch: each element is validated and dispatched on its own, and the response is an array with one entry per answered element (matched by `id`; invalid elements each get an Invalid Request error with `id: null`). Batch elements run sequentially — the server is single-threaded by design, so a batch is equivalent to sending its elements one at a time. An empty array is answered with a single Invalid Request error object, per the spec.
- A request without an `id` member is a **notification**: it is executed but never answered, not even with an error. A batch of only notifications (or a single notification) produces an empty response body.

Framing happens entirely in Python (`_handle` / `_handle_batch` / `_handle_single`); the K semantics see one request envelope per invocation regardless of how requests were framed on the wire.

### The `xdrFormat` parameter

`getTransaction` and `sendTransaction` accept the spec's optional `xdrFormat` parameter. Only `'base64'`, the spec default, is supported: XDR fields in responses are always base64 strings. The alternative `'json'` format (XDR rendered as JSON objects) is not implemented and is rejected with error `-32602` and a message saying so; any other value is rejected with `-32602` as well. The check runs before anything else, so a `sendTransaction` with a bad `xdrFormat` is rejected without executing the transaction.

### `handle_rpc(method, params, request_id) -> str`

`handle_rpc` is the dispatch entry point; it returns the JSON-RPC response envelope as a string. You can call it **without** the HTTP layer, which is convenient for scripts and tests:

```python
server = StellarRpcServer(io_dir=Path('out'))
server.handle_rpc('sendTransaction', {'transaction': xdr})
```

For `sendTransaction` it builds the request envelope with `encoder.build_tx_request` and runs it with `interpreter.run` (skipping the `<program>` wasm injection when the hash already has a receipt, so a duplicate is not re-executed — the semantics answer `DUPLICATE`); for the read-only methods (`getHealth`, `getNetwork`, `getLatestLedger`, `getTransaction`, `getTransactions`, `getLedgers`, `traceTransaction`) it builds a small envelope and runs it. For the history methods (`getTransactions`, `getLedgers`) the server also validates the pagination parameters — limit range, `startLedger` bounds, `cursor`/`startLedger` exclusivity — and resolves the cursor to the first ledger sequence to serve, because the JSON-RPC parameter-error path lives here. In every case the *content* of the response is produced by the semantics (`node.md`), not by Python — the exceptions are the failure fallback (below) and the per-ledger XDR artifacts (`_record_closed_ledger`), which K cannot construct. Each call is logged to stderr.

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

In both cases the server creates the `receipts/`, `traces/`, `ledgers/`, and `requests/` directories if they do not already exist, because the K file-system hooks write into them but cannot create them.

Once the socket is bound, `serve` logs three lines to stderr: whether it is starting from a fresh state (an empty io-dir) or resuming an existing one (with the latest ledger), the io-dir path, and the listening address. Instruction tracing is always on, so every transaction the semantics run produces a trace. (Tracing only produces records for contract invocations.)

---

## State lifecycle

`state.kore` and `metadata.json` hold the persistent chain state; per-transaction receipts and traces live under `receipts/` and `traces/`; the server and semantics also exchange a few transient files per request. See [architecture.md](architecture.md#the-io-dir) for the complete io-dir layout.

```
startup (state.kore absent):
          → empty_config() → state.kore ; metadata.json {latest_ledger:0}
          → create receipts/ traces/ requests/ wasms/

per successful transaction:
          → the semantics run the steps (trace → traces/trace_<hash>.jsonl),
            write receipts/receipt_<hash>.json, bump latest_ledger in metadata.json,
            and write response.json
          → NodeInterpreter persists the new state.kore
          → the server writes ledgers/ledger_<seq>.json (the ledger→tx index entry,
            with the ledger-header XDR artifacts)

per failed (stuck) transaction:
          → no response.json is produced; state.kore and metadata.json are left unchanged
          → the server writes a FAILED receipts/receipt_<hash>.json (see below)
```

Because these artifacts live on disk, the server can be stopped and restarted without losing the world state, the ledger counter, or the stored receipts.

---

## RPC methods

All methods are answered by the K semantics and follow the [Stellar RPC specification](https://developers.stellar.org/docs/data/apis/rpc/methods) — except `traceTransaction`, which is a komet-specific extension (see below) — including its serialization quirks: ledger sequence numbers and `protocolVersion` are JSON numbers, while close-time fields (`latestLedgerCloseTime`, `oldestLedgerCloseTime`, `createdAt`) are int64s serialized as JSON strings holding a decimal integer, matching what real stellar-rpc emits.

### `getHealth`

```json
{ "status": "healthy", "latestLedger": 4, "latestLedgerCloseTime": "1716000000", "oldestLedger": 0, "oldestLedgerCloseTime": "0", "ledgerRetentionWindow": 5 }
```

The node retains every ledger since genesis, so `oldestLedger` is always 0 and the retention window covers all ledgers so far. Per-ledger close times are not recorded: the latest close time is approximated by the request time and the oldest by the epoch.

### `getNetwork`

```json
{ "passphrase": "Test SDF Network ; September 2015", "protocolVersion": 22 }
```

`friendbotUrl` is omitted (not `null`) because the node runs no friendbot — matching real stellar-rpc's `omitempty` serialization.

### `getLatestLedger`

`getLatestLedger` returns the current ledger sequence (the `latest_ledger` from `metadata.json`), which increments by 1 per successfully committed transaction. The `id` is a deterministic per-ledger stand-in for the ledger-header hash, derived from the sequence (see `#ledgerId` in [node-semantics.md](node-semantics.md)).

```json
{ "id": "1715609f7c74...", "protocolVersion": 22, "sequence": 4 }
```

### `getVersionInfo`

`getVersionInfo` reports the komet-node package version as the RPC server version and the komet package (the K semantics of Soroban executing the transactions) as the "Captive Core". komet-node is a Python package, so no commit hash or build timestamp is baked in at build time — those fields are all-zeros / epoch placeholders with the spec-correct types. `protocolVersion` is a JSON number.

```json
{ "version": "0.1.0", "commitHash": "0000...0000", "buildTimestamp": "1970-01-01T00:00:00", "captiveCoreVersion": "komet 0.1.79 (K semantics of Soroban)", "protocolVersion": 22 }
```

### `getFeeStats`

`getFeeStats` returns constant fee distributions: komet-node has no fee market, so every statistic is the network minimum inclusion fee of 100 stroops over an empty sample (`transactionCount: "0"`, `ledgerCount: 0`). Matching real stellar-rpc, every distribution field except `ledgerCount` is a JSON string holding a decimal number; `latestLedger` is a JSON number read live from `metadata.json`.

```json
{ "sorobanInclusionFee": { "max": "100", "min": "100", "mode": "100", "p10": "100", ..., "p99": "100", "transactionCount": "0", "ledgerCount": 0 }, "inclusionFee": { ... }, "latestLedger": 4 }
```

### `sendTransaction`

`sendTransaction` submits a base64-encoded XDR transaction envelope.

**Execution model**: real Stellar RPC was designed around a mempool and ledger close, so the API requires `sendTransaction` to return `PENDING` and have clients poll `getTransaction`. komet-node has no mempool — the semantics execute the transaction immediately — but it still returns `PENDING` to stay compatible with the two-step pattern.

**Response**:
```json
{ "hash": "<64-char hex>", "status": "PENDING", "latestLedger": 5, "latestLedgerCloseTime": "1716000000" }
```

Resubmitting a transaction whose hash already has a receipt returns status `DUPLICATE` without re-executing it: the ledger does not advance and the stored receipt is untouched.

A transaction that decodes as XDR but cannot be processed (e.g. it contains an operation the semantics do not support) is rejected at admission time with status `ERROR` and an `errorResultXdr` carrying a `txMALFORMED` `TransactionResult` — mirroring how real stellar-rpc reports core's admission rejections. Such a transaction never reaches the ledger: no receipt is stored (`getTransaction` stays `NOT_FOUND`) and the ledger does not advance. Undecodable XDR remains a plain `-32602 Invalid params` error, as in real stellar-rpc. `TRY_AGAIN_LATER` is never returned: it signals mempool backpressure, and komet-node executes synchronously without a mempool, so the condition it reports cannot arise.

### `traceTransaction` (komet-specific extension)

`traceTransaction` is **not part of the Stellar RPC specification** — it exists only on komet-node, and clients must not expect it from real Stellar RPC endpoints. It keeps its plain name rather than a vendor-prefixed one (`komet_traceTransaction`): the official spec has no method of that name and none is announced, so there is no collision to avoid, and renaming would break every existing client for no gain. If stellar-rpc ever claims the name, the method will be renamed with a prefix.

`traceTransaction` retrieves the instruction trace of a previously submitted transaction. It takes a `hash` parameter (the same one `getTransaction` takes) and returns the trace that `sendTransaction` stored on that transaction's receipt. The result is the trace itself: a JSONL string with one record per executed WebAssembly instruction, or `null` when no transaction with that hash exists.

```json
"<jsonl string>"
```

### `getTransaction`

`getTransaction` reads the hash's `receipts/receipt_<hash>.json` file. The `hash` parameter must be a 64-character hex string; anything else is rejected with `-32602 Invalid params` (this and `traceTransaction` share the validation).

| Status | Meaning |
|---|---|
| `NOT_FOUND` | No `receipts/receipt_<hash>.json` for that hash |
| `SUCCESS` | Transaction executed successfully |
| `FAILED` | Transaction was submitted but got stuck in the semantics |

**`SUCCESS` response**:
```json
{
  "status": "SUCCESS", "applicationOrder": 1, "feeBump": false,
  "envelopeXdr": "<base64 XDR>", "resultXdr": "", "resultMetaXdr": "",
  "ledger": 5, "createdAt": "1716000000",
  "latestLedger": 5, "latestLedgerCloseTime": "1716000000",
  "oldestLedger": 0, "oldestLedgerCloseTime": "0"
}
```

The ledger-range fields (`latestLedger` through `oldestLedgerCloseTime`) are present on every response, `NOT_FOUND` included. Every ledger contains exactly one transaction, so `applicationOrder` is always 1; fee-bump envelopes are not supported, so `feeBump` is always false. `resultXdr` and `resultMetaXdr` are currently empty stubs. The receipt carries no trace — use `traceTransaction` with the same hash to fetch it.

Receipts are an internal format, not a stable one: receipts written by older komet-node versions stored `ledger` as a string and lack `applicationOrder`/`feeBump`, and `getTransaction` returns stored receipts verbatim. Start from a fresh io-dir after upgrading rather than resuming one with old receipts.

### `getTransactions`

`getTransactions` returns the transactions in a ledger range, in chain order. Params: `startLedger` (number; mutually exclusive with a cursor), `pagination` `{cursor, limit}` (limit 1–200, default 50), and `xdrFormat` (`base64` only; `json` is rejected with `-32602`). The records are joined from the per-ledger index files (`ledgers/ledger_<seq>.json`) and the stored receipts. Failed transactions never close a ledger, so they do not appear in the history.

```json
{
  "transactions": [
    { "status": "SUCCESS", "txHash": "<64-char hex>", "applicationOrder": 1, "feeBump": false,
      "envelopeXdr": "<base64 XDR>", "ledger": 5, "createdAt": 1716000000 }
  ],
  "latestLedger": 5, "latestLedgerCloseTimestamp": 1716000000,
  "oldestLedger": 0, "oldestLedgerCloseTimestamp": 0,
  "cursor": ""
}
```

Serialization follows real stellar-rpc: ledger sequences and the top-level close times are JSON numbers, and per-transaction `createdAt` is a number too (unlike the singular `getTransaction`, where it is a string — an upstream quirk). `resultXdr`/`resultMetaXdr` are omitted while the receipts carry empty stubs. Each ledger holds exactly one transaction on this node, so `applicationOrder` is always `1`. The `cursor` is a TOID-style stringified integer (`ledger << 32 | applicationOrder << 12`) naming the page's last transaction when the page is full, and empty otherwise; resume by passing it as `pagination.cursor` (without `startLedger`).

### `getLedgers`

`getLedgers` returns the closed ledgers in a range and takes the same parameters as `getTransactions`. Each record comes straight from the ledger's index file; `headerXdr` (a `LedgerHeaderHistoryEntry`) and `metadataXdr` (a `LedgerCloseMeta`) are built by the server when the ledger closes, since K cannot construct XDR, and the ledger `hash` is the SHA-256 of the header XDR — unique per ledger and chained through `previousLedgerHash`.

```json
{
  "ledgers": [
    { "hash": "<64-char hex>", "sequence": 5, "ledgerCloseTime": "1716000000",
      "headerXdr": "<base64 XDR>", "metadataXdr": "<base64 XDR>" }
  ],
  "latestLedger": 5, "latestLedgerCloseTime": 1716000000,
  "oldestLedger": 0, "oldestLedgerCloseTime": 0,
  "cursor": ""
}
```

Per-ledger `ledgerCloseTime` is a *string* holding a decimal number (matching real stellar-rpc's Go `,string` encoding), while the top-level close times are numbers. The `cursor` is the last returned ledger sequence, stringified, under the same full-page rule as `getTransactions`.

### `getLedgerEntries`

`getLedgerEntries` takes `keys` (an array of up to 200 base64-encoded `LedgerKey` XDR strings; required) and an optional `xdrFormat` (only `"base64"` is supported — `"json"` is rejected with `-32602`). It returns the entries found for the supported key types — `ACCOUNT`, `CONTRACT_DATA` (both the contract-instance entry and persistent/temporary storage), and `CONTRACT_CODE` — the ones the K world state tracks. Keys that do not resolve (unknown, or of an untracked type) are not an error; they are simply absent from `entries`.

**Response** (`latestLedger` and `lastModifiedLedgerSeq` are JSON numbers; `liveUntilLedgerSeq` appears only on Soroban entries):
```json
{
  "entries": [
    { "key": "<base64 LedgerKey>", "xdr": "<base64 LedgerEntryData>",
      "lastModifiedLedgerSeq": 4, "liveUntilLedgerSeq": 4095 }
  ],
  "latestLedger": 4
}
```

This is the one method whose response the server post-processes: the semantics look the keys up in the K state and answer with intermediate JSON entries, and `ledger_entries.py` re-encodes them as `LedgerEntryData` XDR (K cannot produce XDR). Because the K state keeps uploaded wasm parsed, the server stores the raw bytes under `wasms/<hash>.wasm` at upload time and reattaches them to `CONTRACT_CODE` entries. The semantics do not track per-entry modification ledgers, so `lastModifiedLedgerSeq` reports the current ledger; `ACCOUNT` entries carry the real balance but synthesised constants for the remaining required fields (sequence number 0, master weight 1, no signers).

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
| `--io-dir` | a fresh temp dir | Directory holding every artifact (`state.kore`, `metadata.json`, `receipts/`, `traces/`, `ledgers/`, `requests/`, `wasms/`) |

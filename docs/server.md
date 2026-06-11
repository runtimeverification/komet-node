# `server.py` — `StellarRpcServer`

`StellarRpcServer` is the outermost layer of komet-node. 
It exposes the [Stellar RPC API](https://developers.stellar.org/docs/data/apis/rpc) over HTTP/JSON-RPC, handling incoming requests and owning the request/response lifecycle.
It translates incoming requests into calls to `NodeInterpreter` and manages the shared state file. 

---

## Class structure

```
StellarRpcServer(JsonRpcServer)   ← pyk.rpc.rpc.JsonRpcServer
    interpreter: NodeInterpreter
    state_file:  Path             ← state.kore on disk
    ledger_seq:  int              ← incremented per committed transaction
    _transactions: dict           ← in-memory tx results, keyed by hash
```

The server extends pyk's `JsonRpcServer`, which handles HTTP, JSON-RPC framing, and method dispatch. Each Stellar RPC method is registered with `register_method(name, fn)`.

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

At construction time, the server checks whether `state_file` exists:

- **File does not exist**: `interpreter.empty_config()` is called to produce the initial idle K configuration — a blank-slate blockchain state with no accounts, contracts, or storage — and written to disk.
- **File exists**: it is used as-is. This allows you to start the server against any pre-built state, for example a state snapshotted from mainnet and converted to KORE format, to debug a transaction against realistic data.

The `trace` flag controls whether instruction-level execution traces are generated. When `True`, the initial `state.kore` is produced with `<ioDir>trace.jsonl</ioDir>` baked in, enabling the tracing K rules for all subsequent transactions. See [interpreter.md](interpreter.md) for details.

---

## State file lifecycle

`state.kore` is the single file representing the entire blockchain state. It is a serialized K configuration (KORE format) containing all accounts, contract code, contract storage, and ledger metadata.

```
startup:  if state.kore does not exist
          → server calls interpreter.empty_config()
          → writes the initial empty K configuration to state.kore

per successful transaction:
          → NodeInterpreter reads state.kore (krun input) and translates transaction steps
          → krun executes the transaction steps
          → krun outputs the updated configuration to stdout
          → server overwrites state.kore with the new configuration
          → ledger_seq incremented

per failed transaction:
          → state.kore is NOT updated (state rolls back implicitly)
          → ledger_seq is NOT incremented
```

Because `state.kore` lives on disk, the server can be stopped and restarted between transactions without losing state. To start fresh, delete `state.kore`. To resume from a checkpoint, provide a pre-built kore file via `--state-file`.

---

## RPC methods

All methods follow the [Stellar RPC specification](https://developers.stellar.org/docs/data/apis/rpc/methods).

### `getHealth`

Returns `{"status": "healthy"}`. Used by clients to check server liveness.

### `getNetwork`

Returns the network passphrase and protocol version. Clients use this to verify they are connected to the expected network.

```json
{
  "friendbotUrl": null,
  "passphrase": "Test SDF Network ; September 2015",
  "protocolVersion": "22"
}
```

### `getLatestLedger`

Returns the current ledger sequence number. This increments by 1 for each successfully committed transaction.

```json
{
  "id": "0000...0000",
  "protocolVersion": "22",
  "sequence": 4
}
```

### `sendTransaction`

The main entry point for submitting transactions. Accepts a base64-encoded XDR transaction envelope.

**Execution model**: The Stellar RPC API was designed for a real Stellar validator, where transactions enter a mempool and are only executed after a ledger close (which takes a few seconds). The API contract therefore requires `sendTransaction` to always return `PENDING`, with clients expected to poll `getTransaction` for the final outcome.

In komet-node there is no mempool or ledger close. krun executes the transaction immediately, inside the `sendTransaction` call itself. By the time the method returns, the result is already known and stored internally — but we still return `PENDING` to stay compatible with Stellar clients that expect the two-step pattern.

**Flow**:
1. Decode the XDR envelope via `TransactionEnvelope.from_xdr`
2. Compute `tx_hash = envelope.hash_hex()`
3. Call `interpreter.run_transaction(state_file, envelope.transaction)`
4. On success: overwrite `state.kore`, increment `ledger_seq`, store `SUCCESS` result
5. On `NodeInterpreterError`: store `FAILED` result (state unchanged)
6. Return `{hash, status: "PENDING", latestLedger, latestLedgerCloseTime}`

**Response**:
```json
{
  "hash": "<64-char hex>",
  "status": "PENDING",
  "latestLedger": "5",
  "latestLedgerCloseTime": "1716000000"
}
```

### `getTransaction`

Returns the stored result for a previously submitted transaction.

**Statuses**:

| Status | Meaning |
|---|---|
| `NOT_FOUND` | Hash not in `_transactions` (never submitted, or server restarted) |
| `SUCCESS` | Transaction executed successfully |
| `FAILED` | Transaction was submitted but krun returned an error |

**`SUCCESS` response**:
```json
{
  "status": "SUCCESS",
  "ledger": "5",
  "createdAt": "1716000000",
  "envelopeXdr": "<base64 XDR>",
  "resultXdr": "",
  "resultMetaXdr": "",
  "trace": "<jsonl string or null>",
  "latestLedger": "5",
  "latestLedgerCloseTime": "1716000000"
}
```

The `trace` field is `null` unless the server was started with `--trace`. When tracing is enabled, it contains newline-separated JSON records, one per executed WebAssembly instruction. See [interpreter.md](interpreter.md) for the trace format.

Note: `resultXdr` and `resultMetaXdr` are currently empty stubs. Contract return values are not yet surfaced.

---

## Transaction storage

`_transactions` is an in-memory dict keyed by transaction hash. It is not persisted to disk, so results are lost on server restart. Only the blockchain state (`state.kore`) survives restarts.

---

## CLI

```
komet-node [--host HOST] [--port PORT] [--state-file PATH] [--trace]
```

| Flag | Default | Description |
|---|---|---|
| `--host` | `localhost` | Bind address |
| `--port` | `8000` | Port |
| `--state-file` | `state.kore` | Path to the persistent state file |
| `--trace` | off | Enable instruction-level execution tracing |

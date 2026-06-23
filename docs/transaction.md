# `transaction.py` — `TransactionEncoder`

`TransactionEncoder` decodes Stellar's binary XDR, the wire format the K semantics cannot read. It turns a transaction envelope into the JSON *request envelope* consumed by [`node.md`](node-semantics.md), computes the transaction hash and contract ids, and (for wasm uploads only) parses the bytecode into a `ModuleDecl`.

---

## Class structure

```python
class TransactionEncoder:
    network_passphrase: str
    trace: bool                      # whether --trace traces every transaction
```

The encoder is stateless apart from these two configuration values; it holds no ledger or transaction state (that lives in K).

---

## `build_tx_request(method, rpc_id, transaction_xdr, now, force_trace)`

`build_tx_request` is the entry point. It decodes the XDR envelope and returns a `(request, program_steps)` pair:

```python
request = {
    'method':      method,            # "sendTransaction" | "traceTransaction"
    'id':          rpc_id,            # JSON-RPC id, echoed back in the response
    'now':         now,               # epoch seconds (string); wall-clock can't live in K
    'txHash':      envelope.hash_hex(),
    'envelopeXdr': transaction_xdr,
    'trace':       force_trace or self.trace,
    'steps':       [ ... ] or [],     # JSON steps, or [] for the wasm path
}
```

- For the common case, every operation is encoded as a JSON step and `program_steps` is `None`.
- For a wasm-upload transaction, the operations cannot be expressed as JSON (the `ModuleDecl` has no JSON form), so `steps` is `[]` and `program_steps` carries the kasmer `uploadWasm` step for the interpreter to splice into the `<program>` cell. Soroban allows only one host-function operation per transaction, so such a transaction is exactly one upload op.

`now` is read from the wall clock here, in Python, and passed through the envelope because K has no clock; the semantics use it to fill `createdAt` / `latestLedgerCloseTime`.

---

## JSON step encoding

Each operation is encoded by `_encode_operation`. **Key ordering is significant**: the K `JSON` sort is ordered, so these dicts must produce keys in the same order as the `#decodeStep` rules in `node.md`.

```json
{ "op": "setAccount",     "account": "<hex32>", "balance": <int> }
{ "op": "deployContract", "from": "<hex32>", "address": "<hex32>", "wasmHash": "<hex32>" }
{ "op": "callTx",         "from": "<hex32>", "fromIsContract": <bool>,
                          "func": "<name>", "to": "<hex32>", "args": [ ... ] }
```

`_encode_operation` returns `None` for a wasm upload, which is the signal that the whole transaction takes the `<program>`-injection path instead.

### SCVal argument encoding

Contract-call arguments are encoded by `scval_to_json` (in [`scval.py`](../src/komet_node/scval.py)), alongside the XDR→`SCValue` decoder. Key ordering matters here too — it must match the `#decodeArg` rules.

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

`SCV_VEC` / `SCV_MAP` are not yet encoded; they raise `NotImplementedError`.

---

## Address / contract-id helpers

- `decode_account_id(addr)` — G-strkey → 32-byte public key
- `decode_contract_id(addr)` — C-strkey → 32-byte contract ID
- `contract_id_from_preimage(preimage)` — SHA-256 of the `HashIDPreimage` as used by Stellar (network-passphrase dependent)
- `contract_address_from_deployer_address(deployer, salt)` — computes the C-strkey that `CREATE_CONTRACT` will assign when deploying from an account with the given salt

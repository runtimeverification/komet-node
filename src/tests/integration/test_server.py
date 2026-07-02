from __future__ import annotations

import json
import shutil
import socket
import subprocess
import threading
import time
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from stellar_sdk import Account, Keypair, Network, StrKey, TransactionBuilder, xdr
from stellar_sdk.xdr.sc_val_type import SCValType

from komet_node.server import StellarRpcServer

if TYPE_CHECKING:
    from stellar_sdk import TransactionEnvelope

EMPTY_CONTRACT_WAT = (Path(__file__).parent / 'data' / 'wasm' / 'empty.wat').resolve(strict=True)
ARGS_CONTRACT_WAT = (Path(__file__).parent / 'data' / 'wasm' / 'args.wat').resolve(strict=True)
ADDER_CONTRACT_WAT = (Path(__file__).parent / 'data' / 'wasm' / 'adder.wat').resolve(strict=True)


def wat_to_wasm(wat_path: Path) -> bytes:
    proc_res = subprocess.run(['wat2wasm', str(wat_path), '--output=/dev/stdout'], check=True, capture_output=True)
    return proc_res.stdout


def _find_free_port() -> int:
    with socket.socket() as s:
        s.bind(('', 0))
        return s.getsockname()[1]


def _wait_for_server(host: str, port: int, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.1):
                return
        except OSError:
            time.sleep(0.05)
    raise TimeoutError(f'Server did not start on {host}:{port}')


def _rpc(port: int, method: str, params: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params}).encode()
    return _post(port, body)


def _post(port: int, body: bytes) -> dict[str, Any]:
    req = urllib.request.Request(
        f'http://localhost:{port}',
        data=body,
        headers={'Content-Type': 'application/json'},
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


@pytest.fixture
def server(tmp_path: Path):
    port = _find_free_port()
    srv = StellarRpcServer(
        host='localhost',
        port=port,
        io_dir=tmp_path,
        network_passphrase=Network.TESTNET_NETWORK_PASSPHRASE,
    )
    thread = threading.Thread(target=srv.serve, daemon=True)
    thread.start()
    _wait_for_server('localhost', port)
    yield srv
    srv.shutdown()


def test_default_io_dir_is_a_fresh_temp_dir() -> None:
    """With no io_dir, the server provisions a fresh temporary directory and seeds it."""
    srv = StellarRpcServer(host='localhost', port=0)
    try:
        assert srv.io_dir.exists()
        assert srv.io_dir.resolve() != Path.cwd()
        assert srv.state_file == srv.io_dir / 'state.kore'
        assert srv.state_file.exists()
        assert (srv.io_dir / 'metadata.json').exists()
        # The per-item artifact directories are created up front (the K hooks won't).
        assert (srv.io_dir / 'receipts').is_dir()
        assert (srv.io_dir / 'traces').is_dir()
        assert (srv.io_dir / 'requests').is_dir()
    finally:
        shutil.rmtree(srv.io_dir, ignore_errors=True)


def test_get_health(server: StellarRpcServer) -> None:
    result = _rpc(server.port(), 'getHealth', {})
    assert result['result'] == {'status': 'healthy'}


def test_get_network(server: StellarRpcServer) -> None:
    result = _rpc(server.port(), 'getNetwork', {})
    assert result['result']['passphrase'] == Network.TESTNET_NETWORK_PASSPHRASE
    assert result['result']['protocolVersion'] == '22'


def test_get_latest_ledger_initial(server: StellarRpcServer) -> None:
    result = _rpc(server.port(), 'getLatestLedger', {})
    assert result['result']['sequence'] == 0


def test_get_transaction_not_found(server: StellarRpcServer) -> None:
    result = _rpc(server.port(), 'getTransaction', {'hash': '0' * 64})
    assert result['result']['status'] == 'NOT_FOUND'


def test_unknown_method_returns_method_not_found(server: StellarRpcServer) -> None:
    result = _rpc(server.port(), 'noSuchMethod', {})
    assert result['error']['code'] == -32601


def test_send_transaction_missing_params_returns_invalid_params(server: StellarRpcServer) -> None:
    result = _rpc(server.port(), 'sendTransaction', {})
    assert result['error']['code'] == -32602


def test_send_transaction_bad_xdr_returns_invalid_params(server: StellarRpcServer) -> None:
    result = _rpc(server.port(), 'sendTransaction', {'transaction': 'not-valid-xdr'})
    assert result['error']['code'] == -32602


def test_get_transaction_missing_hash_returns_invalid_params(server: StellarRpcServer) -> None:
    result = _rpc(server.port(), 'getTransaction', {})
    assert result['error']['code'] == -32602


def test_malformed_body_returns_parse_error(server: StellarRpcServer) -> None:
    result = _post(server.port(), b'{ this is not json')
    assert result['error']['code'] == -32700


def test_non_object_frame_returns_invalid_request(server: StellarRpcServer) -> None:
    result = _post(server.port(), b'[1, 2, 3]')
    assert result['error']['code'] == -32600


def test_missing_method_returns_invalid_request(server: StellarRpcServer) -> None:
    result = _post(server.port(), b'{"jsonrpc": "2.0", "id": 1}')
    assert result['error']['code'] == -32600


def test_non_string_method_returns_invalid_request(server: StellarRpcServer) -> None:
    result = _post(server.port(), b'{"jsonrpc": "2.0", "id": 1, "method": 123}')
    assert result['error']['code'] == -32600


def test_wrong_jsonrpc_version_returns_invalid_request(server: StellarRpcServer) -> None:
    result = _post(server.port(), b'{"jsonrpc": "1.0", "id": 1, "method": "getHealth"}')
    assert result['error']['code'] == -32600


def test_non_object_params_returns_invalid_params(server: StellarRpcServer) -> None:
    result = _post(server.port(), b'{"jsonrpc": "2.0", "id": 1, "method": "getHealth", "params": "oops"}')
    assert result['error']['code'] == -32602


def test_send_transaction_and_get_result(server: StellarRpcServer) -> None:
    """Send a CreateAccount transaction through the HTTP server and poll for the result."""
    keypair = Keypair.random()
    account = Account(keypair.public_key, sequence=0)

    envelope = (
        TransactionBuilder(account, Network.TESTNET_NETWORK_PASSPHRASE)
        .append_create_account_op(destination=keypair.public_key, starting_balance='1000')
        .set_timeout(30)
        .build()
    )
    envelope.sign(keypair)
    xdr_str = envelope.to_xdr()

    # sendTransaction always returns PENDING
    send_result = _rpc(server.port(), 'sendTransaction', {'transaction': xdr_str})
    assert send_result['result']['status'] == 'PENDING'
    tx_hash = send_result['result']['hash']

    # since the interpreter runs synchronously, the result is already stored
    get_result = _rpc(server.port(), 'getTransaction', {'hash': tx_hash})
    assert get_result['result']['status'] == 'SUCCESS'
    assert get_result['result']['envelopeXdr'] == xdr_str


def test_io_dir_splits_into_per_item_files(server: StellarRpcServer) -> None:
    """Each receipt, trace, and request lands in its own file; there is no transactions.json."""
    keypair = Keypair.random()
    account = Account(keypair.public_key, sequence=0)
    envelope = (
        TransactionBuilder(account, Network.TESTNET_NETWORK_PASSPHRASE)
        .append_create_account_op(destination=keypair.public_key, starting_balance='1000')
        .set_timeout(30)
        .build()
    )
    envelope.sign(keypair)

    # sendTransaction is the first RPC call in this test, so it is archived as request_0.json.
    tx_hash = _rpc(server.port(), 'sendTransaction', {'transaction': envelope.to_xdr()})['result']['hash']

    assert (server.io_dir / 'receipts' / f'receipt_{tx_hash}.json').exists()
    assert (server.io_dir / 'traces' / f'trace_{tx_hash}.jsonl').exists()
    assert not (server.io_dir / 'transactions.json').exists()

    # Each incoming request is archived under its own monotonic index.
    assert (server.io_dir / 'requests' / 'request_0.json').exists()
    _rpc(server.port(), 'getTransaction', {'hash': tx_hash})
    assert (server.io_dir / 'requests' / 'request_1.json').exists()


def test_failed_transaction_records_failed_receipt(server: StellarRpcServer) -> None:
    """A transaction that gets stuck in the semantics is recorded as FAILED in Python.

    Invoking a contract that was never deployed traps in the semantics, so no response.json
    is produced and the server synthesises the FAILED receipt (the _failure_response path).
    """
    keypair = Keypair.random()
    account = Account(keypair.public_key, sequence=0)

    missing_contract = StrKey.encode_contract(b'\x11' * 32)  # valid C-strkey, never deployed
    envelope = (
        TransactionBuilder(account, Network.TESTNET_NETWORK_PASSPHRASE)
        .append_invoke_contract_function_op(missing_contract, 'foo', [])
        .set_timeout(30)
        .build()
    )
    envelope.sign(keypair)
    xdr_str = envelope.to_xdr()

    # sendTransaction still returns PENDING, even though the tx will fail.
    send_result = _rpc(server.port(), 'sendTransaction', {'transaction': xdr_str})
    assert send_result['result']['status'] == 'PENDING'
    tx_hash = send_result['result']['hash']

    # The synthesised receipt is FAILED and echoes the envelope.
    get_result = _rpc(server.port(), 'getTransaction', {'hash': tx_hash})['result']
    assert get_result['status'] == 'FAILED'
    assert get_result['envelopeXdr'] == xdr_str

    # A failed transaction must not advance the ledger.
    assert _rpc(server.port(), 'getLatestLedger', {})['result']['sequence'] == 0


def test_ledger_seq_increments(server: StellarRpcServer) -> None:
    """The ledger sequence increments by 1 for each successful transaction."""
    keypair = Keypair.random()
    account = Account(keypair.public_key, sequence=0)

    def send_create_account() -> None:
        envelope = (
            TransactionBuilder(account, Network.TESTNET_NETWORK_PASSPHRASE)
            .append_create_account_op(destination=keypair.public_key, starting_balance='1000')
            .set_timeout(30)
            .build()
        )
        envelope.sign(keypair)
        _rpc(server.port(), 'sendTransaction', {'transaction': envelope.to_xdr()})

    send_create_account()
    assert _rpc(server.port(), 'getLatestLedger', {})['result']['sequence'] == 1

    send_create_account()
    assert _rpc(server.port(), 'getLatestLedger', {})['result']['sequence'] == 2


def test_full_lifecycle_over_http(server: StellarRpcServer) -> None:
    """Full contract lifecycle through the HTTP server: account → upload → deploy → invoke."""
    keypair = Keypair.random()
    account = Account(keypair.public_key, sequence=0)

    def send(envelope_xdr: str) -> dict[str, Any]:
        send_res = _rpc(server.port(), 'sendTransaction', {'transaction': envelope_xdr})
        assert send_res['result']['status'] == 'PENDING'
        tx_hash = send_res['result']['hash']
        get_res = _rpc(server.port(), 'getTransaction', {'hash': tx_hash})
        assert get_res['result']['status'] == 'SUCCESS', f'Transaction failed: {get_res}'
        return get_res['result']

    def builder() -> TransactionBuilder:
        return TransactionBuilder(account, Network.TESTNET_NETWORK_PASSPHRASE)

    def sign_and_xdr(tb: TransactionBuilder) -> str:
        env = tb.set_timeout(30).build()
        env.sign(keypair)
        return env.to_xdr()

    # 1. Create account
    send(sign_and_xdr(builder().append_create_account_op(keypair.public_key, '1000')))

    # 2. Upload wasm
    wasm_bytecode = wat_to_wasm(EMPTY_CONTRACT_WAT)
    send(sign_and_xdr(builder().append_upload_contract_wasm_op(wasm_bytecode)))

    # 3. Deploy contract
    from stellar_sdk.utils import sha256

    wasm_hash = sha256(wasm_bytecode)
    salt = b'\x00' * 32
    send(sign_and_xdr(builder().append_create_contract_op(wasm_hash, keypair.public_key, None, salt)))

    # 4. Invoke foo()
    contract_address = server.encoder.contract_address_from_deployer_address(keypair.public_key, salt)
    send(sign_and_xdr(builder().append_invoke_contract_function_op(contract_address, 'foo', [])))


def test_trace_transaction_retrieves_trace_by_hash(server: StellarRpcServer) -> None:
    """traceTransaction returns the trace of a previously submitted transaction, keyed by hash."""
    keypair = Keypair.random()
    account = Account(keypair.public_key, sequence=0)

    envelope = (
        TransactionBuilder(account, Network.TESTNET_NETWORK_PASSPHRASE)
        .append_create_account_op(destination=keypair.public_key, starting_balance='1000')
        .set_timeout(30)
        .build()
    )
    envelope.sign(keypair)

    send_result = _rpc(server.port(), 'sendTransaction', {'transaction': envelope.to_xdr()})['result']
    assert send_result['status'] == 'PENDING'

    # The trace is keyed by the same hash getTransaction uses. A create-account op runs no
    # wasm instructions, so the stored trace is the empty string (resolved, not null/NOT_FOUND).
    trace = _rpc(server.port(), 'traceTransaction', {'hash': send_result['hash']})['result']
    assert trace == ''


def test_trace_transaction_unknown_hash_returns_null(server: StellarRpcServer) -> None:
    """traceTransaction returns null when no transaction with that hash exists."""
    result = _rpc(server.port(), 'traceTransaction', {'hash': 'deadbeef'})['result']
    assert result is None


def test_trace_transaction_missing_hash_returns_invalid_params(server: StellarRpcServer) -> None:
    result = _rpc(server.port(), 'traceTransaction', {})
    assert result['error']['code'] == -32602


def test_trace_transaction_produces_trace_on_contract_invocation(server: StellarRpcServer) -> None:
    """traceTransaction returns non-empty trace JSONL for a submitted contract invocation."""
    keypair = Keypair.random()
    account = Account(keypair.public_key, sequence=0)

    def builder() -> TransactionBuilder:
        return TransactionBuilder(account, Network.TESTNET_NETWORK_PASSPHRASE)

    def sign_and_xdr(tb: TransactionBuilder) -> str:
        env = tb.set_timeout(30).build()
        env.sign(keypair)
        return env.to_xdr()

    def send(xdr: str) -> str:
        res = _rpc(server.port(), 'sendTransaction', {'transaction': xdr})
        assert res['result']['status'] == 'PENDING'
        tx_hash = res['result']['hash']
        assert _rpc(server.port(), 'getTransaction', {'hash': tx_hash})['result']['status'] == 'SUCCESS'
        return tx_hash

    # Set up: create account, upload wasm, deploy contract
    send(sign_and_xdr(builder().append_create_account_op(keypair.public_key, '1000')))

    wasm_bytecode = wat_to_wasm(EMPTY_CONTRACT_WAT)
    send(sign_and_xdr(builder().append_upload_contract_wasm_op(wasm_bytecode)))

    from stellar_sdk.utils import sha256

    wasm_hash = sha256(wasm_bytecode)
    salt = b'\x00' * 32
    send(sign_and_xdr(builder().append_create_contract_op(wasm_hash, keypair.public_key, None, salt)))

    # Submit the invocation, then retrieve its trace by hash.
    contract_address = server.encoder.contract_address_from_deployer_address(keypair.public_key, salt)
    invoke_xdr = sign_and_xdr(builder().append_invoke_contract_function_op(contract_address, 'foo', []))
    tx_hash = send(invoke_xdr)

    trace = _rpc(server.port(), 'traceTransaction', {'hash': tx_hash})['result']

    assert trace is not None
    # Trace is newline-separated JSON records; verify each line parses as JSON
    lines = [line for line in trace.splitlines() if line.strip()]
    assert len(lines) > 0
    import json as _json

    for line in lines:
        record = _json.loads(line)
        assert 'instr' in record


def test_call_tx_with_args(server: StellarRpcServer) -> None:
    """Exercise the scval_to_json / #decodeArg pipeline for each supported SCVal type.

    Uses a minimal contract (args.wat) whose functions accept various arg types and return
    Void. Covers: bool, u32, i32, u64, i64, u128, i128, symbol.
    """
    keypair = Keypair.random()
    account = Account(keypair.public_key, sequence=0)

    def builder() -> TransactionBuilder:
        return TransactionBuilder(account, Network.TESTNET_NETWORK_PASSPHRASE)

    def send(tb: TransactionBuilder) -> None:
        env = tb.set_timeout(30).build()
        env.sign(keypair)
        res = _rpc(server.port(), 'sendTransaction', {'transaction': env.to_xdr()})
        assert res['result']['status'] == 'PENDING'
        tx_hash = res['result']['hash']
        get_res = _rpc(server.port(), 'getTransaction', {'hash': tx_hash})['result']
        assert get_res['status'] == 'SUCCESS', f'Transaction failed: {get_res}'

    # Set up: create account, upload args.wat, deploy contract
    send(builder().append_create_account_op(keypair.public_key, '1000'))

    wasm_bytecode = wat_to_wasm(ARGS_CONTRACT_WAT)
    send(builder().append_upload_contract_wasm_op(wasm_bytecode))

    from stellar_sdk.utils import sha256

    wasm_hash = sha256(wasm_bytecode)
    salt = b'\x00' * 32
    send(builder().append_create_contract_op(wasm_hash, keypair.public_key, None, salt))

    contract_address = server.encoder.contract_address_from_deployer_address(keypair.public_key, salt)

    def invoke(func: str, args: list[xdr.SCVal]) -> None:
        send(builder().append_invoke_contract_function_op(contract_address, func, args))

    invoke('test_bool', [xdr.SCVal(type=SCValType.SCV_BOOL, b=True)])
    invoke(
        'test_integers',
        [
            xdr.SCVal(type=SCValType.SCV_U32, u32=xdr.Uint32(42)),
            xdr.SCVal(type=SCValType.SCV_I32, i32=xdr.Int32(-7)),
            xdr.SCVal(type=SCValType.SCV_U64, u64=xdr.Uint64(100)),
            xdr.SCVal(type=SCValType.SCV_I64, i64=xdr.Int64(-200)),
        ],
    )
    invoke(
        'test_wide_integers',
        [
            xdr.SCVal(type=SCValType.SCV_U128, u128=xdr.UInt128Parts(hi=xdr.Uint64(0), lo=xdr.Uint64(999))),
            xdr.SCVal(type=SCValType.SCV_I128, i128=xdr.Int128Parts(hi=xdr.Int64(0), lo=xdr.Uint64(888))),
        ],
    )
    invoke('test_symbol', [xdr.SCVal(type=SCValType.SCV_SYMBOL, sym=xdr.SCSymbol(sc_symbol=b'hello'))])


def test_call_tx_with_return_value(server: StellarRpcServer) -> None:
    """A contract invocation that returns a non-Void value succeeds.

    Regression test: transactions used to be decoded into ``callTx(..., Void)``, which
    asserts the call returns Void. Invoking ``add(2, 3)`` (returning U32(5)) therefore got
    stuck in the semantics and was recorded as FAILED. ``uncheckedCallTx`` drops the return
    value check.
    """
    keypair = Keypair.random()
    account = Account(keypair.public_key, sequence=0)

    def send(tb: TransactionBuilder) -> None:
        env = tb.set_timeout(30).build()
        env.sign(keypair)
        res = _rpc(server.port(), 'sendTransaction', {'transaction': env.to_xdr()})
        assert res['result']['status'] == 'PENDING'
        tx_hash = res['result']['hash']
        get_res = _rpc(server.port(), 'getTransaction', {'hash': tx_hash})['result']
        assert get_res['status'] == 'SUCCESS', f'Transaction failed: {get_res}'

    def builder() -> TransactionBuilder:
        return TransactionBuilder(account, Network.TESTNET_NETWORK_PASSPHRASE)

    # Set up: create account, upload adder.wat, deploy contract
    send(builder().append_create_account_op(keypair.public_key, '1000'))

    wasm_bytecode = wat_to_wasm(ADDER_CONTRACT_WAT)
    send(builder().append_upload_contract_wasm_op(wasm_bytecode))

    from stellar_sdk.utils import sha256

    wasm_hash = sha256(wasm_bytecode)
    salt = b'\x00' * 32
    send(builder().append_create_contract_op(wasm_hash, keypair.public_key, None, salt))

    # add(2, 3) returns U32(5), not Void — the send() helper asserts SUCCESS.
    contract_address = server.encoder.contract_address_from_deployer_address(keypair.public_key, salt)
    send(
        builder().append_invoke_contract_function_op(
            contract_address,
            'add',
            [
                xdr.SCVal(type=SCValType.SCV_U32, u32=xdr.Uint32(2)),
                xdr.SCVal(type=SCValType.SCV_U32, u32=xdr.Uint32(3)),
            ],
        )
    )

    # All four transactions, including the non-Void invocation, advanced the ledger.
    assert _rpc(server.port(), 'getLatestLedger', {})['result']['sequence'] == 4


# ---------------------------------------------------------------------------
# simulateTransaction
#
# Response shape per the official OpenRPC spec (methods/simulateTransaction.json) and the
# stellar-rpc Go serialization structs: `latestLedger` is a JSON number and the only
# always-required field; `minResourceFee` is a stringified number; `results` holds exactly
# one `{xdr, auth}` entry for the host-function invocation; optional fields are omitted
# (Go `omitempty`), not null. On failure only `error` (+ `latestLedger`) is returned.
# ---------------------------------------------------------------------------


def _deploy_adder_contract(server: StellarRpcServer, keypair: Keypair, account: Account) -> str:
    """Create the account, upload adder.wat, and deploy it; return the contract address.

    Submits three transactions, so the ledger sequence afterwards is 3.
    """
    from stellar_sdk.utils import sha256

    def send(tb: TransactionBuilder) -> None:
        env = tb.set_timeout(30).build()
        env.sign(keypair)
        res = _rpc(server.port(), 'sendTransaction', {'transaction': env.to_xdr()})
        assert res['result']['status'] == 'PENDING'
        get_res = _rpc(server.port(), 'getTransaction', {'hash': res['result']['hash']})['result']
        assert get_res['status'] == 'SUCCESS', f'Transaction failed: {get_res}'

    def builder() -> TransactionBuilder:
        return TransactionBuilder(account, Network.TESTNET_NETWORK_PASSPHRASE)

    send(builder().append_create_account_op(keypair.public_key, '1000'))
    wasm_bytecode = wat_to_wasm(ADDER_CONTRACT_WAT)
    send(builder().append_upload_contract_wasm_op(wasm_bytecode))
    wasm_hash = sha256(wasm_bytecode)
    salt = b'\x00' * 32
    send(builder().append_create_contract_op(wasm_hash, keypair.public_key, None, salt))
    return server.encoder.contract_address_from_deployer_address(keypair.public_key, salt)


def _build_add_invocation(account: Account, contract_address: str) -> TransactionEnvelope:
    """Build an *unsigned* envelope invoking add(2, 3) — simulation takes unsigned txs."""
    return (
        TransactionBuilder(account, Network.TESTNET_NETWORK_PASSPHRASE)
        .append_invoke_contract_function_op(
            contract_address,
            'add',
            [
                xdr.SCVal(type=SCValType.SCV_U32, u32=xdr.Uint32(2)),
                xdr.SCVal(type=SCValType.SCV_U32, u32=xdr.Uint32(3)),
            ],
        )
        .set_timeout(30)
        .build()
    )


def test_simulate_transaction_missing_params_returns_invalid_params(server: StellarRpcServer) -> None:
    result = _rpc(server.port(), 'simulateTransaction', {})
    assert result['error']['code'] == -32602


def test_simulate_transaction_bad_xdr_returns_invalid_params(server: StellarRpcServer) -> None:
    result = _rpc(server.port(), 'simulateTransaction', {'transaction': 'not-valid-xdr'})
    assert result['error']['code'] == -32602


def test_simulate_transaction_returns_invocation_return_value(server: StellarRpcServer) -> None:
    """A successful simulation reports the host function's return value as base64 SCVal XDR."""
    keypair = Keypair.random()
    account = Account(keypair.public_key, sequence=0)
    contract_address = _deploy_adder_contract(server, keypair, account)

    envelope = _build_add_invocation(account, contract_address)
    response = _rpc(server.port(), 'simulateTransaction', {'transaction': envelope.to_xdr()})
    assert 'error' not in response, f'simulateTransaction failed: {response}'
    result = response['result']

    # A successful simulation carries no error field.
    assert 'error' not in result

    # latestLedger is a JSON number reflecting the chain tip (three transactions committed).
    assert isinstance(result['latestLedger'], int) and not isinstance(result['latestLedger'], bool)
    assert result['latestLedger'] == 3

    # minResourceFee is a stringified number (Go int64 with `,string` encoding).
    assert isinstance(result['minResourceFee'], str)
    assert result['minResourceFee'].isdigit()

    # Exactly one host-function result: the return value of add(2, 3), plus auth entries.
    results = result['results']
    assert isinstance(results, list)
    assert len(results) == 1
    assert set(results[0]) == {'xdr', 'auth'}
    return_value = xdr.SCVal.from_xdr(results[0]['xdr'])
    assert return_value.type == SCValType.SCV_U32
    assert return_value.u32 is not None
    assert return_value.u32.uint32 == 5
    assert isinstance(results[0]['auth'], list)
    assert all(isinstance(entry, str) for entry in results[0]['auth'])

    # transactionData is valid base64-encoded SorobanTransactionData XDR.
    assert isinstance(result['transactionData'], str)
    xdr.SorobanTransactionData.from_xdr(result['transactionData'])

    # events is optional; when present it is an array of base64 strings.
    if 'events' in result:
        assert isinstance(result['events'], list)
        assert all(isinstance(event, str) for event in result['events'])


def test_simulate_transaction_does_not_commit(server: StellarRpcServer) -> None:
    """Simulation must not change the chain: no ledger bump, no receipt, no trace."""
    keypair = Keypair.random()
    account = Account(keypair.public_key, sequence=0)
    contract_address = _deploy_adder_contract(server, keypair, account)
    assert _rpc(server.port(), 'getLatestLedger', {})['result']['sequence'] == 3

    envelope = _build_add_invocation(account, contract_address)
    tx_hash = envelope.hash_hex()
    response = _rpc(server.port(), 'simulateTransaction', {'transaction': envelope.to_xdr()})
    assert 'error' not in response, f'simulateTransaction failed: {response}'
    assert 'error' not in response['result']

    # The ledger did not advance and no per-transaction artifacts were persisted.
    assert _rpc(server.port(), 'getLatestLedger', {})['result']['sequence'] == 3
    assert not (server.io_dir / 'receipts' / f'receipt_{tx_hash}.json').exists()
    assert not (server.io_dir / 'traces' / f'trace_{tx_hash}.jsonl').exists()
    assert _rpc(server.port(), 'getTransaction', {'hash': tx_hash})['result']['status'] == 'NOT_FOUND'

    # Simulation is repeatable: simulating the same envelope again yields the same result.
    repeat = _rpc(server.port(), 'simulateTransaction', {'transaction': envelope.to_xdr()})
    assert repeat['result']['results'] == response['result']['results']
    assert _rpc(server.port(), 'getLatestLedger', {})['result']['sequence'] == 3


def test_simulate_transaction_failure_returns_error(server: StellarRpcServer) -> None:
    """A failing simulation returns {error, latestLedger}; success-only fields are omitted."""
    keypair = Keypair.random()
    account = Account(keypair.public_key, sequence=0)

    missing_contract = StrKey.encode_contract(b'\x22' * 32)  # valid C-strkey, never deployed
    envelope = (
        TransactionBuilder(account, Network.TESTNET_NETWORK_PASSPHRASE)
        .append_invoke_contract_function_op(missing_contract, 'foo', [])
        .set_timeout(30)
        .build()
    )
    response = _rpc(server.port(), 'simulateTransaction', {'transaction': envelope.to_xdr()})
    # Simulation failure is reported in the result body, not as a JSON-RPC error.
    assert 'error' not in response, f'expected a result-level error, got: {response}'
    result = response['result']

    assert isinstance(result['error'], str)
    assert result['error'] != ''
    assert isinstance(result['latestLedger'], int) and not isinstance(result['latestLedger'], bool)
    assert result['latestLedger'] == 0

    # Not present in case of error (Go omitempty — omitted, not null).
    assert 'results' not in result
    assert 'transactionData' not in result
    assert 'minResourceFee' not in result

    # A failed simulation likewise commits nothing.
    assert _rpc(server.port(), 'getLatestLedger', {})['result']['sequence'] == 0
    assert not (server.io_dir / 'receipts' / f'receipt_{envelope.hash_hex()}.json').exists()


def test_simulate_transaction_non_invoke_host_function_returns_error(server: StellarRpcServer) -> None:
    """Simulating a non-InvokeHostFunction transaction reports a result-level error.

    Per the spec, the provided transaction must contain only a single operation of type
    invokeHostFunction; real stellar-rpc reports the violation in the result body.
    """
    keypair = Keypair.random()
    account = Account(keypair.public_key, sequence=0)
    envelope = (
        TransactionBuilder(account, Network.TESTNET_NETWORK_PASSPHRASE)
        .append_create_account_op(destination=keypair.public_key, starting_balance='1000')
        .set_timeout(30)
        .build()
    )
    response = _rpc(server.port(), 'simulateTransaction', {'transaction': envelope.to_xdr()})
    assert 'error' not in response, f'expected a result-level error, got: {response}'
    result = response['result']

    assert isinstance(result['error'], str)
    assert result['error'] != ''
    assert isinstance(result['latestLedger'], int) and not isinstance(result['latestLedger'], bool)
    assert 'results' not in result

    # Nothing was committed: the create-account operation did not execute.
    assert _rpc(server.port(), 'getLatestLedger', {})['result']['sequence'] == 0
    assert not (server.io_dir / 'receipts' / f'receipt_{envelope.hash_hex()}.json').exists()

from __future__ import annotations

import json
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

import pytest
from stellar_sdk import Account, Asset, Keypair, Network, StrKey, TransactionBuilder, xdr
from stellar_sdk.xdr.sc_val_type import SCValType

from komet_node.server import StellarRpcServer

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


# Spec-shape helpers. The official serialization rules come from the Go structs in
# stellar/go-stellar-sdk protocols/rpc (what real stellar-rpc emits): ledger sequence
# numbers and protocolVersion are JSON numbers; the close-time fields on the singular
# methods are int64 with Go's `,string` encoding, i.e. JSON strings holding a decimal
# integer; hashes are 64 lowercase hex characters.

_HEX64_RE = re.compile(r'[0-9a-f]{64}')
_INT_STRING_RE = re.compile(r'-?[0-9]+')


def _is_number(value: Any) -> bool:
    """True for a JSON number decoded to int (bool is a distinct JSON type)."""
    return isinstance(value, int) and not isinstance(value, bool)


def _is_int_string(value: Any) -> bool:
    """True for a JSON string holding a decimal integer (Go int64 `,string` encoding)."""
    return isinstance(value, str) and _INT_STRING_RE.fullmatch(value) is not None


def _is_hex64(value: Any) -> bool:
    return isinstance(value, str) and _HEX64_RE.fullmatch(value) is not None


def _assert_ledger_bounds(result: dict[str, Any]) -> None:
    """Check the latest/oldest ledger-range fields required on every getTransaction response."""
    assert _is_number(result['latestLedger'])
    assert _is_int_string(result['latestLedgerCloseTime'])
    assert _is_number(result['oldestLedger'])
    assert _is_int_string(result['oldestLedgerCloseTime'])
    assert result['oldestLedger'] <= result['latestLedger']


# The full field surface of GetTransactionResponse (Go struct, v22 + optional v23 extras).
_GET_TRANSACTION_KEYS = {
    'status',
    'txHash',
    'applicationOrder',
    'feeBump',
    'envelopeXdr',
    'resultXdr',
    'resultMetaXdr',
    'diagnosticEventsXdr',
    'events',
    'ledger',
    'createdAt',
    'latestLedger',
    'latestLedgerCloseTime',
    'oldestLedger',
    'oldestLedgerCloseTime',
}


def _create_account_xdr(keypair: Keypair, account: Account) -> str:
    """Build and sign a minimal create-account transaction, returned as base64 XDR."""
    envelope = (
        TransactionBuilder(account, Network.TESTNET_NETWORK_PASSPHRASE)
        .append_create_account_op(destination=keypair.public_key, starting_balance='1000')
        .set_timeout(30)
        .build()
    )
    envelope.sign(keypair)
    return envelope.to_xdr()


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
    """getHealth returns the spec shape: status plus the ledger range, all sequences as numbers."""
    result = _rpc(server.port(), 'getHealth', {})['result']
    assert result['status'] == 'healthy'
    assert _is_number(result['latestLedger'])
    assert _is_number(result['oldestLedger'])
    assert _is_number(result['ledgerRetentionWindow'])
    assert result['oldestLedger'] <= result['latestLedger']
    assert result['ledgerRetentionWindow'] >= 1
    # The close-time fields are always emitted by real stellar-rpc but are not part of this
    # node's required surface; when present they must use the int64-as-string encoding.
    for key in ('latestLedgerCloseTime', 'oldestLedgerCloseTime'):
        if key in result:
            assert _is_int_string(result[key])
    assert set(result) <= {
        'status',
        'latestLedger',
        'latestLedgerCloseTime',
        'oldestLedger',
        'oldestLedgerCloseTime',
        'ledgerRetentionWindow',
    }


def test_get_network(server: StellarRpcServer) -> None:
    """getNetwork: protocolVersion is a JSON number; friendbotUrl is omitted (no friendbot here)."""
    result = _rpc(server.port(), 'getNetwork', {})['result']
    assert result['passphrase'] == Network.TESTNET_NETWORK_PASSPHRASE
    assert _is_number(result['protocolVersion'])
    assert result['protocolVersion'] == 22
    # friendbotUrl is `omitempty` in real stellar-rpc: unset means absent, not null.
    assert 'friendbotUrl' not in result
    assert set(result) == {'passphrase', 'protocolVersion'}


def test_get_latest_ledger_initial(server: StellarRpcServer) -> None:
    """getLatestLedger on a fresh chain: sequence 0, protocolVersion as number, 64-hex id."""
    result = _rpc(server.port(), 'getLatestLedger', {})['result']
    assert result['sequence'] == 0
    assert _is_number(result['sequence'])
    assert _is_number(result['protocolVersion'])
    assert result['protocolVersion'] == 22
    assert _is_hex64(result['id'])
    # closeTime/headerXdr/metadataXdr are protocol-23 extras; when present, closeTime uses
    # the int64-as-string encoding.
    if 'closeTime' in result:
        assert _is_int_string(result['closeTime'])
    assert set(result) <= {'id', 'protocolVersion', 'sequence', 'closeTime', 'headerXdr', 'metadataXdr'}


def test_get_latest_ledger_id_changes_per_ledger(server: StellarRpcServer) -> None:
    """The ledger id is not a constant: each ledger reports its own hash."""
    keypair = Keypair.random()
    account = Account(keypair.public_key, sequence=0)

    first = _rpc(server.port(), 'getLatestLedger', {})['result']
    _rpc(server.port(), 'sendTransaction', {'transaction': _create_account_xdr(keypair, account)})
    second = _rpc(server.port(), 'getLatestLedger', {})['result']

    assert second['sequence'] == first['sequence'] + 1
    assert _is_hex64(first['id'])
    assert _is_hex64(second['id'])
    assert first['id'] != second['id']


def test_get_transaction_not_found(server: StellarRpcServer) -> None:
    """A NOT_FOUND response still carries the full ledger range, with spec-conformant types."""
    result = _rpc(server.port(), 'getTransaction', {'hash': '0' * 64})['result']
    assert result['status'] == 'NOT_FOUND'
    _assert_ledger_bounds(result)
    assert set(result) <= _GET_TRANSACTION_KEYS


def test_get_transaction_malformed_hash_returns_invalid_params(server: StellarRpcServer) -> None:
    """The hash param must be a 64-character hex string; anything else is Invalid params."""
    for bad_hash in ('deadbeef', '0' * 63, '0' * 65, 'x' * 64, '0' * 63 + 'g'):
        response = _rpc(server.port(), 'getTransaction', {'hash': bad_hash})
        assert 'result' not in response, f'expected an error for hash {bad_hash!r}'
        assert response['error']['code'] == -32602, f'hash {bad_hash!r}'


def test_unknown_method_returns_method_not_found(server: StellarRpcServer) -> None:
    result = _rpc(server.port(), 'noSuchMethod', {})
    assert result['error']['code'] == -32601


def test_k_unknown_method_fallback_returns_method_not_found(server: StellarRpcServer) -> None:
    """The K semantics' own unknown-method fallback answers with JSON-RPC error -32601.

    The Python layer filters unknown methods before they reach K, so this drives the
    interpreter directly with an envelope for a method the semantics do not implement.
    The fallback must produce an error response, not ``result: null``.
    """
    envelope = {'method': 'noSuchMethod', 'id': 7, 'now': str(int(time.time()))}
    raw = server.interpreter.run(server.state_file, server.io_dir, envelope, None)
    assert raw is not None
    response = json.loads(raw)
    assert 'result' not in response
    assert response['error']['code'] == -32601
    assert response['id'] == 7


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
    """Send a CreateAccount transaction through the HTTP server and poll for the result.

    Asserts the exact spec shape of both responses: ledger sequences are JSON numbers,
    close times are string-encoded int64s, and the receipt carries the transaction details
    required for a SUCCESS status (ledger, createdAt, applicationOrder, feeBump).
    """
    keypair = Keypair.random()
    account = Account(keypair.public_key, sequence=0)
    xdr_str = _create_account_xdr(keypair, account)

    # sendTransaction returns PENDING for a fresh transaction
    send_result = _rpc(server.port(), 'sendTransaction', {'transaction': xdr_str})['result']
    assert send_result['status'] == 'PENDING'
    assert _is_hex64(send_result['hash'])
    assert _is_number(send_result['latestLedger'])
    assert _is_int_string(send_result['latestLedgerCloseTime'])
    assert set(send_result) == {'hash', 'status', 'latestLedger', 'latestLedgerCloseTime'}
    tx_hash = send_result['hash']

    # since the interpreter runs synchronously, the result is already stored
    get_result = _rpc(server.port(), 'getTransaction', {'hash': tx_hash})['result']
    assert get_result['status'] == 'SUCCESS'
    assert get_result['envelopeXdr'] == xdr_str
    _assert_ledger_bounds(get_result)
    assert _is_number(get_result['ledger'])
    assert get_result['ledger'] == 1
    # createdAt is a string on getTransaction (singular) — a known quirk of real stellar-rpc.
    assert _is_int_string(get_result['createdAt'])
    assert _is_number(get_result['applicationOrder'])
    assert get_result['applicationOrder'] == 1
    assert get_result['feeBump'] is False
    assert set(get_result) <= _GET_TRANSACTION_KEYS


def test_send_transaction_unsupported_operation_returns_error_status(server: StellarRpcServer) -> None:
    """A transaction that decodes but cannot be processed is rejected with status ERROR.

    Mirrors real stellar-rpc's admission-time rejection: the response carries a txMALFORMED
    TransactionResult in errorResultXdr, the transaction never reaches the ledger (no
    receipt, no ledger bump), and getTransaction stays NOT_FOUND.
    """
    keypair = Keypair.random()
    account = Account(keypair.public_key, sequence=0)
    envelope = (
        TransactionBuilder(account, Network.TESTNET_NETWORK_PASSPHRASE)
        .append_payment_op(destination=Keypair.random().public_key, asset=Asset.native(), amount='1')
        .set_timeout(30)
        .build()
    )
    envelope.sign(keypair)

    result = _rpc(server.port(), 'sendTransaction', {'transaction': envelope.to_xdr()})['result']
    assert result['status'] == 'ERROR'
    assert result['hash'] == envelope.hash_hex()
    assert _is_number(result['latestLedger'])
    assert _is_int_string(result['latestLedgerCloseTime'])
    assert set(result) == {'hash', 'status', 'errorResultXdr', 'latestLedger', 'latestLedgerCloseTime'}

    tx_result = xdr.TransactionResult.from_xdr(result['errorResultXdr'])
    assert tx_result.result.code == xdr.TransactionResultCode.txMALFORMED
    assert tx_result.fee_charged.int64 == 0

    # The rejected transaction never reached the ledger.
    assert _rpc(server.port(), 'getLatestLedger', {})['result']['sequence'] == 0
    get_result = _rpc(server.port(), 'getTransaction', {'hash': envelope.hash_hex()})['result']
    assert get_result['status'] == 'NOT_FOUND'


def test_send_transaction_duplicate_is_not_reexecuted(server: StellarRpcServer) -> None:
    """Resubmitting an already-executed transaction returns DUPLICATE and leaves the chain alone."""
    keypair = Keypair.random()
    account = Account(keypair.public_key, sequence=0)
    xdr_str = _create_account_xdr(keypair, account)

    first = _rpc(server.port(), 'sendTransaction', {'transaction': xdr_str})['result']
    assert first['status'] == 'PENDING'
    assert _rpc(server.port(), 'getLatestLedger', {})['result']['sequence'] == 1

    second = _rpc(server.port(), 'sendTransaction', {'transaction': xdr_str})['result']
    assert second['status'] == 'DUPLICATE'
    assert second['hash'] == first['hash']
    assert _is_number(second['latestLedger'])
    assert _is_int_string(second['latestLedgerCloseTime'])
    assert set(second) == {'hash', 'status', 'latestLedger', 'latestLedgerCloseTime'}

    # The duplicate was not re-executed: the ledger did not advance and the original
    # SUCCESS receipt is untouched.
    assert _rpc(server.port(), 'getLatestLedger', {})['result']['sequence'] == 1
    get_result = _rpc(server.port(), 'getTransaction', {'hash': first['hash']})['result']
    assert get_result['status'] == 'SUCCESS'
    assert get_result['ledger'] == 1


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

    # sendTransaction still returns PENDING, even though the tx will fail. The response
    # keeps the spec types: latestLedger a number, latestLedgerCloseTime a string.
    send_result = _rpc(server.port(), 'sendTransaction', {'transaction': xdr_str})['result']
    assert send_result['status'] == 'PENDING'
    assert _is_number(send_result['latestLedger'])
    assert _is_int_string(send_result['latestLedgerCloseTime'])
    tx_hash = send_result['hash']

    # The synthesised receipt is FAILED and echoes the envelope; the ledger-range fields
    # are required for every status, and any transaction details keep the spec types.
    get_result = _rpc(server.port(), 'getTransaction', {'hash': tx_hash})['result']
    assert get_result['status'] == 'FAILED'
    assert get_result['envelopeXdr'] == xdr_str
    _assert_ledger_bounds(get_result)
    if 'ledger' in get_result:
        assert _is_number(get_result['ledger'])
    if 'createdAt' in get_result:
        assert _is_int_string(get_result['createdAt'])
    assert set(get_result) <= _GET_TRANSACTION_KEYS

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
    """traceTransaction returns null when no transaction with that hash exists.

    Uses a well-formed (64-hex) hash so this stays a lookup-miss test regardless of any
    hash-format validation on the shared hash parameter.
    """
    result = _rpc(server.port(), 'traceTransaction', {'hash': 'ab' * 32})['result']
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

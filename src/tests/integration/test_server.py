from __future__ import annotations

import importlib.metadata
import json
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from stellar_sdk import Account, Address, Asset, Keypair, Network, StrKey, TransactionBuilder, xdr
from stellar_sdk.xdr.sc_val_type import SCValType

from komet_node.server import StellarRpcServer

if TYPE_CHECKING:
    from collections.abc import Iterator

EMPTY_CONTRACT_WAT = (Path(__file__).parent / 'data' / 'wasm' / 'empty.wat').resolve(strict=True)
ARGS_CONTRACT_WAT = (Path(__file__).parent / 'data' / 'wasm' / 'args.wat').resolve(strict=True)
ADDER_CONTRACT_WAT = (Path(__file__).parent / 'data' / 'wasm' / 'adder.wat').resolve(strict=True)
STORAGE_CONTRACT_WAT = (Path(__file__).parent / 'data' / 'wasm' / 'storage.wat').resolve(strict=True)


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


def _post(port: int, body: bytes) -> Any:
    return json.loads(_post_raw(port, body))


def _post_raw(port: int, body: bytes) -> bytes:
    req = urllib.request.Request(
        f'http://localhost:{port}',
        data=body,
        headers={'Content-Type': 'application/json'},
    )
    with urllib.request.urlopen(req) as resp:
        return resp.read()


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
def server(tmp_path: Path) -> Iterator[StellarRpcServer]:
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
    result = _post(server.port(), b'"just a string"')
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


# ----------------------------------------------------------------------
# getLedgerEntries
# ----------------------------------------------------------------------
#
# Spec: stellar-docs OpenRPC getLedgerEntries.json + stellar-rpc's Go serialization
# (GetLedgerEntriesResponse). Result is {entries, latestLedger}; latestLedger and
# lastModifiedLedgerSeq are JSON numbers; liveUntilLedgerSeq is optional (omitted when the
# entry has no TTL); only found entries are returned; `key`/`xdr` are base64 LedgerKey /
# LedgerEntryData strings.


def _account_ledger_key(public_key: str) -> str:
    return xdr.LedgerKey(
        type=xdr.LedgerEntryType.ACCOUNT,
        account=xdr.LedgerKeyAccount(account_id=Keypair.from_public_key(public_key).xdr_account_id()),
    ).to_xdr()


def _contract_code_ledger_key(wasm_hash: bytes) -> str:
    return xdr.LedgerKey(
        type=xdr.LedgerEntryType.CONTRACT_CODE,
        contract_code=xdr.LedgerKeyContractCode(hash=xdr.Hash(wasm_hash)),
    ).to_xdr()


def _contract_data_ledger_key(contract_address: str, key: xdr.SCVal, durability: xdr.ContractDataDurability) -> str:
    return xdr.LedgerKey(
        type=xdr.LedgerEntryType.CONTRACT_DATA,
        contract_data=xdr.LedgerKeyContractData(
            contract=Address(contract_address).to_xdr_sc_address(),
            key=key,
            durability=durability,
        ),
    ).to_xdr()


def _assert_ledger_entry_shape(entry: dict[str, Any], expected_key: str) -> None:
    """Assert one entry matches the Go LedgerEntryResult serialization (base64 format)."""
    assert {'key', 'xdr', 'lastModifiedLedgerSeq'} <= set(entry)
    assert set(entry) <= {'key', 'xdr', 'lastModifiedLedgerSeq', 'liveUntilLedgerSeq'}
    assert entry['key'] == expected_key
    assert type(entry['xdr']) is str and entry['xdr'] != ''
    assert type(entry['lastModifiedLedgerSeq']) is int  # JSON number, not string
    if 'liveUntilLedgerSeq' in entry:  # optional; only Soroban entries carry a TTL
        assert type(entry['liveUntilLedgerSeq']) is int


def _send_tx(server: StellarRpcServer, keypair: Keypair, tb: TransactionBuilder) -> None:
    env = tb.set_timeout(30).build()
    env.sign(keypair)
    res = _rpc(server.port(), 'sendTransaction', {'transaction': env.to_xdr()})
    assert res['result']['status'] == 'PENDING'
    get_res = _rpc(server.port(), 'getTransaction', {'hash': res['result']['hash']})['result']
    assert get_res['status'] == 'SUCCESS', f'Transaction failed: {get_res}'


def test_get_ledger_entries_account(server: StellarRpcServer) -> None:
    """An ACCOUNT ledger key resolves to an AccountEntry; unknown keys are silently dropped."""
    keypair = Keypair.random()
    account = Account(keypair.public_key, sequence=0)
    _send_tx(
        server,
        keypair,
        TransactionBuilder(account, Network.TESTNET_NETWORK_PASSPHRASE).append_create_account_op(
            destination=keypair.public_key, starting_balance='1000'
        ),
    )

    account_key = _account_ledger_key(keypair.public_key)
    missing_key = _account_ledger_key(Keypair.random().public_key)
    result = _rpc(server.port(), 'getLedgerEntries', {'keys': [account_key, missing_key]})['result']

    assert set(result) == {'entries', 'latestLedger'}
    assert result['latestLedger'] == 1
    assert type(result['latestLedger']) is int  # JSON number, not string

    # Only the found entry is returned; the unknown key is not an error, just absent.
    assert len(result['entries']) == 1
    entry = result['entries'][0]
    _assert_ledger_entry_shape(entry, account_key)
    assert 0 <= entry['lastModifiedLedgerSeq'] <= result['latestLedger']

    data = xdr.LedgerEntryData.from_xdr(entry['xdr'])
    assert data.type == xdr.LedgerEntryType.ACCOUNT
    assert data.account is not None
    assert data.account.account_id == Keypair.from_public_key(keypair.public_key).xdr_account_id()
    assert data.account.balance.int64 == 10_000_000_000  # 1000 XLM in stroops


def test_get_ledger_entries_contract_code_and_data(server: StellarRpcServer) -> None:
    """CONTRACT_CODE, the CONTRACT_DATA instance entry, and persistent CONTRACT_DATA storage."""
    from stellar_sdk.utils import sha256

    keypair = Keypair.random()
    account = Account(keypair.public_key, sequence=0)

    def builder() -> TransactionBuilder:
        return TransactionBuilder(account, Network.TESTNET_NETWORK_PASSPHRASE)

    # Set up: create account, upload storage.wat, deploy, invoke store() which writes the
    # persistent storage entry U32(7) -> U32(42).
    _send_tx(server, keypair, builder().append_create_account_op(keypair.public_key, '1000'))
    wasm_bytecode = wat_to_wasm(STORAGE_CONTRACT_WAT)
    _send_tx(server, keypair, builder().append_upload_contract_wasm_op(wasm_bytecode))
    wasm_hash = sha256(wasm_bytecode)
    salt = b'\x00' * 32
    _send_tx(server, keypair, builder().append_create_contract_op(wasm_hash, keypair.public_key, None, salt))
    contract_address = server.encoder.contract_address_from_deployer_address(keypair.public_key, salt)
    _send_tx(server, keypair, builder().append_invoke_contract_function_op(contract_address, 'store', []))

    code_key = _contract_code_ledger_key(wasm_hash)
    instance_key = _contract_data_ledger_key(
        contract_address,
        xdr.SCVal(type=SCValType.SCV_LEDGER_KEY_CONTRACT_INSTANCE),
        xdr.ContractDataDurability.PERSISTENT,
    )
    storage_key_scval = xdr.SCVal(type=SCValType.SCV_U32, u32=xdr.Uint32(7))
    storage_key = _contract_data_ledger_key(contract_address, storage_key_scval, xdr.ContractDataDurability.PERSISTENT)

    result = _rpc(server.port(), 'getLedgerEntries', {'keys': [code_key, instance_key, storage_key]})['result']
    assert set(result) == {'entries', 'latestLedger'}
    assert result['latestLedger'] == 4
    assert type(result['latestLedger']) is int

    entries = {entry['key']: entry for entry in result['entries']}
    assert set(entries) == {code_key, instance_key, storage_key}
    for key, entry in entries.items():
        _assert_ledger_entry_shape(entry, key)

    # CONTRACT_CODE: the uploaded wasm bytecode round-trips through the ledger entry.
    code_data = xdr.LedgerEntryData.from_xdr(entries[code_key]['xdr'])
    assert code_data.type == xdr.LedgerEntryType.CONTRACT_CODE
    assert code_data.contract_code is not None
    assert code_data.contract_code.hash.hash == wasm_hash
    assert code_data.contract_code.code == wasm_bytecode

    # CONTRACT_DATA (instance): the deployed contract's instance entry points at the wasm.
    instance_data = xdr.LedgerEntryData.from_xdr(entries[instance_key]['xdr'])
    assert instance_data.type == xdr.LedgerEntryType.CONTRACT_DATA
    assert instance_data.contract_data is not None
    assert instance_data.contract_data.contract == Address(contract_address).to_xdr_sc_address()
    assert instance_data.contract_data.durability == xdr.ContractDataDurability.PERSISTENT
    assert instance_data.contract_data.key.type == SCValType.SCV_LEDGER_KEY_CONTRACT_INSTANCE
    assert instance_data.contract_data.val.type == SCValType.SCV_CONTRACT_INSTANCE
    instance = instance_data.contract_data.val.instance
    assert instance is not None
    assert instance.executable.type == xdr.ContractExecutableType.CONTRACT_EXECUTABLE_WASM
    assert instance.executable.wasm_hash is not None
    assert instance.executable.wasm_hash.hash == wasm_hash

    # CONTRACT_DATA (persistent): the value written by store() is readable.
    storage_data = xdr.LedgerEntryData.from_xdr(entries[storage_key]['xdr'])
    assert storage_data.type == xdr.LedgerEntryType.CONTRACT_DATA
    assert storage_data.contract_data is not None
    assert storage_data.contract_data.contract == Address(contract_address).to_xdr_sc_address()
    assert storage_data.contract_data.durability == xdr.ContractDataDurability.PERSISTENT
    assert storage_data.contract_data.key == storage_key_scval
    assert storage_data.contract_data.val == xdr.SCVal(type=SCValType.SCV_U32, u32=xdr.Uint32(42))


def test_get_ledger_entries_no_matches_returns_empty_entries(server: StellarRpcServer) -> None:
    """Unknown keys are not an error: the result is an empty entries array."""
    result = _rpc(server.port(), 'getLedgerEntries', {'keys': [_account_ledger_key(Keypair.random().public_key)]})[
        'result'
    ]
    assert result['entries'] == []
    assert result['latestLedger'] == 0
    assert type(result['latestLedger']) is int


def test_get_ledger_entries_unsupported_entry_type_is_not_found(server: StellarRpcServer) -> None:
    """A well-formed key of a type komet-node does not track (DATA) is simply not found."""
    data_key = xdr.LedgerKey(
        type=xdr.LedgerEntryType.DATA,
        data=xdr.LedgerKeyData(
            account_id=Keypair.random().xdr_account_id(),
            data_name=xdr.String64(b'config'),
        ),
    ).to_xdr()
    result = _rpc(server.port(), 'getLedgerEntries', {'keys': [data_key]})['result']
    assert result['entries'] == []


def test_get_ledger_entries_xdr_format_base64_accepted(server: StellarRpcServer) -> None:
    """xdrFormat 'base64' is the explicit spelling of the default and must be accepted."""
    keys = [_account_ledger_key(Keypair.random().public_key)]
    result = _rpc(server.port(), 'getLedgerEntries', {'keys': keys, 'xdrFormat': 'base64'})
    assert 'error' not in result
    assert result['result']['entries'] == []


def test_get_ledger_entries_xdr_format_json_rejected(server: StellarRpcServer) -> None:
    """komet-node does not support the JSON XDR format; asking for it is an invalid-params error."""
    keys = [_account_ledger_key(Keypair.random().public_key)]
    result = _rpc(server.port(), 'getLedgerEntries', {'keys': keys, 'xdrFormat': 'json'})
    assert result['error']['code'] == -32602
    assert type(result['error']['message']) is str and result['error']['message'] != ''


def test_get_ledger_entries_invalid_xdr_format_rejected(server: StellarRpcServer) -> None:
    keys = [_account_ledger_key(Keypair.random().public_key)]
    result = _rpc(server.port(), 'getLedgerEntries', {'keys': keys, 'xdrFormat': 'xml'})
    assert result['error']['code'] == -32602


def test_get_ledger_entries_missing_keys_returns_invalid_params(server: StellarRpcServer) -> None:
    result = _rpc(server.port(), 'getLedgerEntries', {})
    assert result['error']['code'] == -32602


def test_get_ledger_entries_non_array_keys_returns_invalid_params(server: StellarRpcServer) -> None:
    result = _rpc(server.port(), 'getLedgerEntries', {'keys': 'AAAAAA=='})
    assert result['error']['code'] == -32602


def test_get_ledger_entries_non_string_key_returns_invalid_params(server: StellarRpcServer) -> None:
    result = _rpc(server.port(), 'getLedgerEntries', {'keys': [42]})
    assert result['error']['code'] == -32602


def test_get_ledger_entries_invalid_key_xdr_returns_invalid_params(server: StellarRpcServer) -> None:
    result = _rpc(server.port(), 'getLedgerEntries', {'keys': ['not-a-ledger-key']})
    assert result['error']['code'] == -32602


def test_get_ledger_entries_too_many_keys_returns_invalid_params(server: StellarRpcServer) -> None:
    """The spec caps a request at 200 ledger keys."""
    key = _account_ledger_key(Keypair.random().public_key)
    result = _rpc(server.port(), 'getLedgerEntries', {'keys': [key] * 201})
    assert result['error']['code'] == -32602


# ---------------------------------------------------------------------------
# getTransactions / getLedgers (transaction history)
#
# Ground truth: the official OpenRPC spec (stellar-docs, methods/getTransactions.json and
# methods/getLedgers.json) and the Go serialization structs from stellar/go-stellar-sdk
# protocols/rpc, which is what real stellar-rpc emits. Notable serialization traps asserted
# below:
#   - ledger sequences and the top-level close-time fields are JSON numbers,
#   - per-transaction `createdAt` in getTransactions is a JSON *number* (upstream quirk;
#     the singular getTransaction returns it as a string),
#   - per-ledger `ledgerCloseTime` in getLedgers is a *string* (Go int64 `,string`),
#   - XDR fields are `omitempty`: real base64 XDR or absent, never empty strings.
# ---------------------------------------------------------------------------


def _rpc_result(port: int, method: str, params: dict[str, Any]) -> dict[str, Any]:
    """Call an RPC method and return its result, failing the test on a JSON-RPC error."""
    response = _rpc(port, method, params)
    assert 'error' not in response, f'{method} returned an error: {response["error"]}'
    return response['result']


def _send_create_accounts(server: StellarRpcServer, count: int) -> list[tuple[str, str]]:
    """Submit ``count`` successful create-account transactions; return (hash, envelopeXdr) pairs.

    Each successful transaction closes its own ledger, so after this call the latest ledger
    is ``count`` and transaction ``i`` (1-based) sits alone in ledger ``i``.
    """
    keypair = Keypair.random()
    account = Account(keypair.public_key, sequence=0)
    sent: list[tuple[str, str]] = []
    for _ in range(count):
        envelope = (
            TransactionBuilder(account, Network.TESTNET_NETWORK_PASSPHRASE)
            .append_create_account_op(destination=keypair.public_key, starting_balance='1000')
            .set_timeout(30)
            .build()
        )
        envelope.sign(keypair)
        xdr_str = envelope.to_xdr()
        send_res = _rpc(server.port(), 'sendTransaction', {'transaction': xdr_str})
        assert send_res['result']['status'] == 'PENDING'
        tx_hash = send_res['result']['hash']
        assert _rpc(server.port(), 'getTransaction', {'hash': tx_hash})['result']['status'] == 'SUCCESS'
        sent.append((tx_hash, xdr_str))
    return sent


def test_get_transactions_spec_shape(server: StellarRpcServer) -> None:
    """getTransactions returns the response shape of GetTransactionsResponse (Go SDK)."""
    before = int(time.time())
    sent = _send_create_accounts(server, 3)
    after = int(time.time())

    result = _rpc_result(server.port(), 'getTransactions', {'startLedger': 1})

    # All six top-level fields lack `omitempty` in the Go struct, so all must be present.
    required_keys = {
        'transactions',
        'latestLedger',
        'latestLedgerCloseTimestamp',
        'oldestLedger',
        'oldestLedgerCloseTimestamp',
        'cursor',
    }
    assert required_keys <= result.keys(), f'missing keys: {required_keys - result.keys()}'

    assert type(result['latestLedger']) is int
    assert result['latestLedger'] == 3
    assert type(result['latestLedgerCloseTimestamp']) is int
    assert type(result['oldestLedger']) is int
    assert 0 <= result['oldestLedger'] <= 1
    assert type(result['oldestLedgerCloseTimestamp']) is int
    assert isinstance(result['cursor'], str)

    txs = result['transactions']
    assert isinstance(txs, list)
    # All three transactions, in chain order (ascending ledger, then application order).
    assert [tx['txHash'] for tx in txs] == [tx_hash for tx_hash, _ in sent]
    for i, tx in enumerate(txs, start=1):
        assert tx['status'] == 'SUCCESS'
        assert _is_hex64(tx['txHash'])
        assert type(tx['applicationOrder']) is int
        assert tx['applicationOrder'] == 1  # one transaction per ledger on this node
        assert tx['feeBump'] is False
        assert type(tx['ledger']) is int
        assert tx['ledger'] == i
        # Upstream quirk: createdAt is a JSON number here (int64 without `,string` in Go),
        # unlike getTransaction (singular) where it is a string.
        assert type(tx['createdAt']) is int
        assert before <= tx['createdAt'] <= after
        assert tx['envelopeXdr'] == sent[i - 1][1]
        # omitempty: XDR fields carry real base64 XDR or are absent — never empty strings.
        for optional in ('resultXdr', 'resultMetaXdr'):
            if optional in tx:
                assert isinstance(tx[optional], str) and tx[optional] != ''

    # xdrFormat: 'base64' is the default and must be accepted; unknown values are rejected.
    with_format = _rpc_result(server.port(), 'getTransactions', {'startLedger': 1, 'xdrFormat': 'base64'})
    assert [tx['txHash'] for tx in with_format['transactions']] == [tx_hash for tx_hash, _ in sent]
    assert _rpc(server.port(), 'getTransactions', {'startLedger': 1, 'xdrFormat': 'bogus'})['error']['code'] == -32602

    # The limit for getTransactions ranges from 1 to 200.
    bad_limit = _rpc(server.port(), 'getTransactions', {'startLedger': 1, 'pagination': {'limit': 201}})
    assert bad_limit['error']['code'] == -32602


def test_get_transactions_pagination(server: StellarRpcServer) -> None:
    """A limited page returns a cursor from which the next page resumes without overlap."""
    sent = _send_create_accounts(server, 3)

    page1 = _rpc_result(server.port(), 'getTransactions', {'startLedger': 1, 'pagination': {'limit': 2}})
    assert [tx['txHash'] for tx in page1['transactions']] == [sent[0][0], sent[1][0]]
    cursor = page1['cursor']
    assert isinstance(cursor, str) and cursor != ''

    # Resume from the cursor; startLedger must be omitted on cursor requests.
    page2 = _rpc_result(server.port(), 'getTransactions', {'pagination': {'cursor': cursor, 'limit': 2}})
    assert [tx['txHash'] for tx in page2['transactions']] == [sent[2][0]]


def test_get_transactions_invalid_params(server: StellarRpcServer) -> None:
    port = server.port()
    # startLedger beyond the latest ledger (0 on a fresh chain) is out of retention range.
    assert _rpc(port, 'getTransactions', {'startLedger': 999})['error']['code'] == -32602
    # startLedger and cursor are mutually exclusive.
    both = _rpc(port, 'getTransactions', {'startLedger': 1, 'pagination': {'cursor': '1'}})
    assert both['error']['code'] == -32602
    # startLedger must be a number.
    assert _rpc(port, 'getTransactions', {'startLedger': 'one'})['error']['code'] == -32602


def test_get_ledgers_spec_shape(server: StellarRpcServer) -> None:
    """getLedgers returns the response shape of GetLedgersResponse (Go SDK)."""
    _send_create_accounts(server, 2)

    result = _rpc_result(server.port(), 'getLedgers', {'startLedger': 1})

    required_keys = {
        'ledgers',
        'latestLedger',
        'latestLedgerCloseTime',
        'oldestLedger',
        'oldestLedgerCloseTime',
        'cursor',
    }
    assert required_keys <= result.keys(), f'missing keys: {required_keys - result.keys()}'

    assert type(result['latestLedger']) is int
    assert result['latestLedger'] == 2
    assert type(result['latestLedgerCloseTime']) is int
    assert type(result['oldestLedger']) is int
    assert 0 <= result['oldestLedger'] <= 1
    assert type(result['oldestLedgerCloseTime']) is int
    assert isinstance(result['cursor'], str)

    ledgers = result['ledgers']
    assert isinstance(ledgers, list)
    assert [ledger['sequence'] for ledger in ledgers] == [1, 2]
    hashes = set()
    for ledger in ledgers:
        assert type(ledger['sequence']) is int
        assert _is_hex64(ledger['hash'])
        hashes.add(ledger['hash'])
        # Per-ledger close time is a STRING containing a decimal number (Go int64 `,string`).
        assert isinstance(ledger['ledgerCloseTime'], str)
        assert ledger['ledgerCloseTime'].isdigit()
        # headerXdr is a base64 LedgerHeaderHistoryEntry for this ledger.
        header = xdr.LedgerHeaderHistoryEntry.from_xdr(ledger['headerXdr'])
        assert header.header.ledger_seq.uint32 == ledger['sequence']
        # metadataXdr is a base64 LedgerCloseMeta union for this ledger.
        meta = xdr.LedgerCloseMeta.from_xdr(ledger['metadataXdr'])
        meta_header = getattr(meta, f'v{meta.v}').ledger_header
        assert meta_header.header.ledger_seq.uint32 == ledger['sequence']
    # Ledger hashes identify ledgers and must be unique.
    assert len(hashes) == 2

    # The limit for getLedgers ranges from 1 to 200.
    bad_limit = _rpc(server.port(), 'getLedgers', {'startLedger': 1, 'pagination': {'limit': 201}})
    assert bad_limit['error']['code'] == -32602


def test_get_ledgers_pagination(server: StellarRpcServer) -> None:
    """A limited page returns a cursor from which the next page resumes without overlap."""
    _send_create_accounts(server, 2)

    page1 = _rpc_result(server.port(), 'getLedgers', {'startLedger': 1, 'pagination': {'limit': 1}})
    assert [ledger['sequence'] for ledger in page1['ledgers']] == [1]
    cursor = page1['cursor']
    assert isinstance(cursor, str) and cursor != ''

    page2 = _rpc_result(server.port(), 'getLedgers', {'pagination': {'cursor': cursor, 'limit': 1}})
    assert [ledger['sequence'] for ledger in page2['ledgers']] == [2]


def test_get_ledgers_invalid_params(server: StellarRpcServer) -> None:
    port = server.port()
    # startLedger beyond the latest ledger (0 on a fresh chain) is out of retention range.
    assert _rpc(port, 'getLedgers', {'startLedger': 999})['error']['code'] == -32602
    # startLedger and cursor are mutually exclusive.
    both = _rpc(port, 'getLedgers', {'startLedger': 1, 'pagination': {'cursor': '1'}})
    assert both['error']['code'] == -32602


def test_get_version_info(server: StellarRpcServer) -> None:
    """getVersionInfo returns exactly the five spec fields with the right JSON types.

    Real stellar-rpc (protocol 22+) emits camelCase keys only; the deprecated snake_case
    aliases (``commit_hash``, ...) were removed, so an exact key-set check covers both the
    required fields and the absence of the legacy ones. ``protocolVersion`` is a Go uint32,
    i.e. a JSON number, not a string.
    """
    resp = _rpc(server.port(), 'getVersionInfo', {})
    assert 'error' not in resp, resp
    result = resp['result']

    assert set(result) == {'version', 'commitHash', 'buildTimestamp', 'captiveCoreVersion', 'protocolVersion'}
    # komet-node reports its own package version as the RPC server version.
    assert result['version'] == importlib.metadata.version('komet-node')
    assert type(result['commitHash']) is str
    assert type(result['buildTimestamp']) is str
    assert type(result['captiveCoreVersion']) is str
    assert type(result['protocolVersion']) is int  # `is int` also rejects booleans
    assert result['protocolVersion'] == 22


def test_get_version_info_accepts_omitted_params(server: StellarRpcServer) -> None:
    """getVersionInfo takes no parameters; a request without a params member must succeed."""
    resp = _post(server.port(), b'{"jsonrpc": "2.0", "id": 1, "method": "getVersionInfo"}')
    assert 'error' not in resp, resp
    assert resp['result']['protocolVersion'] == 22


# Every FeeDistribution field except ledgerCount is an unsigned integer serialised with Go's
# `,string` option, i.e. a JSON string holding a decimal number (see the getFeeStats spec
# example: `"transactionCount": "10"` but `"ledgerCount": 50`).
_FEE_DISTRIBUTION_STRING_FIELDS = (
    'max',
    'min',
    'mode',
    'p10',
    'p20',
    'p30',
    'p40',
    'p50',
    'p60',
    'p70',
    'p80',
    'p90',
    'p95',
    'p99',
    'transactionCount',
)


def _assert_fee_distribution(dist: dict[str, Any]) -> None:
    """Check one FeeDistribution object against the stellar-rpc wire format."""
    assert set(dist) == {*_FEE_DISTRIBUTION_STRING_FIELDS, 'ledgerCount'}
    for field in _FEE_DISTRIBUTION_STRING_FIELDS:
        value = dist[field]
        assert type(value) is str, f'{field} must be a JSON string, got {type(value).__name__}'
        assert value.isdigit(), f'{field} must hold a decimal number, got {value!r}'
    assert type(dist['ledgerCount']) is int, 'ledgerCount must be a JSON number'
    # The distribution must at least be internally consistent.
    assert int(dist['min']) <= int(dist['p50']) <= int(dist['max'])


def test_get_fee_stats(server: StellarRpcServer) -> None:
    """getFeeStats returns both fee distributions and latestLedger with the right JSON types."""
    resp = _rpc(server.port(), 'getFeeStats', {})
    assert 'error' not in resp, resp
    result = resp['result']

    assert set(result) == {'sorobanInclusionFee', 'inclusionFee', 'latestLedger'}
    _assert_fee_distribution(result['sorobanInclusionFee'])
    _assert_fee_distribution(result['inclusionFee'])
    assert type(result['latestLedger']) is int  # a JSON number, and 0 on a fresh chain
    assert result['latestLedger'] == 0


def test_get_fee_stats_latest_ledger_tracks_chain(server: StellarRpcServer) -> None:
    """getFeeStats reports the live ledger sequence, not a constant."""
    keypair = Keypair.random()
    account = Account(keypair.public_key, sequence=0)
    envelope = (
        TransactionBuilder(account, Network.TESTNET_NETWORK_PASSPHRASE)
        .append_create_account_op(destination=keypair.public_key, starting_balance='1000')
        .set_timeout(30)
        .build()
    )
    envelope.sign(keypair)
    assert _rpc(server.port(), 'sendTransaction', {'transaction': envelope.to_xdr()})['result']['status'] == 'PENDING'

    resp = _rpc(server.port(), 'getFeeStats', {})
    assert 'error' not in resp, resp
    assert resp['result']['latestLedger'] == 1


# ----------------------------------------------------------------------
# xdrFormat parameter (getTransaction / sendTransaction)
#
# Real stellar-rpc accepts an optional `xdrFormat` param on both methods
# (protocols/rpc: GetTransactionRequest.Format, SendTransactionRequest.Format)
# and rejects invalid values with InvalidParams (-32602). komet-node supports
# only 'base64' (the default); 'json' is rejected with a clear -32602 error.
# ----------------------------------------------------------------------


def test_xdr_format_base64_behaves_as_default(server: StellarRpcServer) -> None:
    """xdrFormat 'base64' is the explicit spelling of the default on both methods."""
    keypair = Keypair.random()
    account = Account(keypair.public_key, sequence=0)
    send_result = _rpc(
        server.port(),
        'sendTransaction',
        {'transaction': _create_account_xdr(keypair, account), 'xdrFormat': 'base64'},
    )
    assert send_result['result']['status'] == 'PENDING'
    tx_hash = send_result['result']['hash']

    get_result = _rpc(server.port(), 'getTransaction', {'hash': tx_hash, 'xdrFormat': 'base64'})
    assert get_result['result']['status'] == 'SUCCESS'


def test_get_transaction_xdr_format_json_returns_invalid_params(server: StellarRpcServer) -> None:
    """komet-node does not support the JSON XDR format: reject with a clear -32602 error."""
    result = _rpc(server.port(), 'getTransaction', {'hash': '0' * 64, 'xdrFormat': 'json'})
    assert result['error']['code'] == -32602
    assert 'json' in result['error']['message'].lower()


def test_get_transaction_xdr_format_invalid_value_returns_invalid_params(server: StellarRpcServer) -> None:
    result = _rpc(server.port(), 'getTransaction', {'hash': '0' * 64, 'xdrFormat': 'yaml'})
    assert result['error']['code'] == -32602


def test_get_transaction_xdr_format_non_string_returns_invalid_params(server: StellarRpcServer) -> None:
    result = _rpc(server.port(), 'getTransaction', {'hash': '0' * 64, 'xdrFormat': 42})
    assert result['error']['code'] == -32602


def test_send_transaction_xdr_format_json_returns_invalid_params_without_executing(server: StellarRpcServer) -> None:
    """An unsupported xdrFormat is rejected before the transaction runs: no state change."""
    keypair = Keypair.random()
    account = Account(keypair.public_key, sequence=0)
    result = _rpc(
        server.port(),
        'sendTransaction',
        {'transaction': _create_account_xdr(keypair, account), 'xdrFormat': 'json'},
    )
    assert result['error']['code'] == -32602
    assert 'json' in result['error']['message'].lower()

    # The transaction must not have executed: no receipt written, ledger not advanced.
    assert list((server.io_dir / 'receipts').iterdir()) == []
    assert _rpc(server.port(), 'getLatestLedger', {})['result']['sequence'] == 0


def test_send_transaction_xdr_format_invalid_value_returns_invalid_params(server: StellarRpcServer) -> None:
    keypair = Keypair.random()
    account = Account(keypair.public_key, sequence=0)
    result = _rpc(
        server.port(),
        'sendTransaction',
        {'transaction': _create_account_xdr(keypair, account), 'xdrFormat': 'yaml'},
    )
    assert result['error']['code'] == -32602
    assert list((server.io_dir / 'receipts').iterdir()) == []


# ----------------------------------------------------------------------
# JSON-RPC 2.0 batch requests
#
# Per JSON-RPC 2.0 section 6: an array of request objects yields an array of
# response objects (matched by id, order not significant); an empty array is a
# single Invalid Request error; invalid batch elements each yield an Invalid
# Request error with id null; notifications (no id) get no response, and a
# batch of only notifications yields no response body at all.
# ----------------------------------------------------------------------


def _batch(port: int, requests: list[dict[str, Any]]) -> Any:
    return _post(port, json.dumps(requests).encode())


def test_batch_request_returns_array_of_responses(server: StellarRpcServer) -> None:
    responses = _batch(
        server.port(),
        [
            {'jsonrpc': '2.0', 'id': 1, 'method': 'getHealth', 'params': {}},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'getLatestLedger', 'params': {}},
        ],
    )
    assert isinstance(responses, list)
    assert len(responses) == 2
    by_id = {response['id']: response for response in responses}
    assert set(by_id) == {1, 2}
    for response in responses:
        assert response['jsonrpc'] == '2.0'
    assert by_id[1]['result']['status'] == 'healthy'
    assert by_id[2]['result']['sequence'] == 0


def test_empty_batch_returns_single_invalid_request(server: StellarRpcServer) -> None:
    """An empty array is not a valid batch: one Invalid Request error object, not an array."""
    result = _post(server.port(), b'[]')
    assert isinstance(result, dict)
    assert result['error']['code'] == -32600
    assert result['id'] is None


def test_batch_of_invalid_elements_returns_error_per_element(server: StellarRpcServer) -> None:
    """rpc call with an invalid batch: one Invalid Request response per element, id null."""
    responses = _post(server.port(), b'[1, 2, 3]')
    assert isinstance(responses, list)
    assert len(responses) == 3
    for response in responses:
        assert response['jsonrpc'] == '2.0'
        assert response['error']['code'] == -32600
        assert response['id'] is None


def test_batch_mixed_valid_and_invalid_elements(server: StellarRpcServer) -> None:
    responses = _batch(
        server.port(),
        [
            {'jsonrpc': '2.0', 'id': 1, 'method': 'getHealth', 'params': {}},
            {'foo': 'boo'},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'noSuchMethod', 'params': {}},
        ],
    )
    assert isinstance(responses, list)
    assert len(responses) == 3
    by_id = {response['id']: response for response in responses}
    assert by_id[1]['result']['status'] == 'healthy'
    assert by_id[None]['error']['code'] == -32600
    assert by_id[2]['error']['code'] == -32601


def test_batch_notification_gets_no_response(server: StellarRpcServer) -> None:
    """A request without an id is a notification: it is executed but not answered."""
    responses = _batch(
        server.port(),
        [
            {'jsonrpc': '2.0', 'id': 1, 'method': 'getHealth', 'params': {}},
            {'jsonrpc': '2.0', 'method': 'getHealth', 'params': {}},  # notification
        ],
    )
    assert isinstance(responses, list)
    assert len(responses) == 1
    assert responses[0]['id'] == 1
    assert responses[0]['result']['status'] == 'healthy'


def test_batch_of_only_notifications_returns_nothing(server: StellarRpcServer) -> None:
    """If every batch element is a notification, the server must not return an empty array."""
    body = json.dumps([{'jsonrpc': '2.0', 'method': 'getHealth', 'params': {}}]).encode()
    raw = _post_raw(server.port(), body)
    assert raw.strip() == b''

from __future__ import annotations

import importlib.metadata
import json
import shutil
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from stellar_sdk import Account, Address, Asset, Keypair, Network, StrKey, TransactionBuilder, xdr
from stellar_sdk.utils import sha256
from stellar_sdk.xdr.sc_val_type import SCValType

from komet_node.__main__ import build_server
from komet_node.scval import scval_from_json

from .conftest import (
    PASSPHRASE,
    _is_hex64,
    _is_int_string,
    _is_number,
    _post,
    _post_raw,
    _rpc,
    contract_address_from_deployer,
    deploy_and_get_invoker,
    deploy_contract,
    fund_account,
    make_invoker,
    send_tx,
    wat_to_wasm,
)

if TYPE_CHECKING:
    from stellar_sdk import TransactionEnvelope

    from komet_node.server import StellarRpcServer

EMPTY_CONTRACT_WAT = (Path(__file__).parent / 'data' / 'wasm' / 'empty.wat').resolve(strict=True)
ARGS_CONTRACT_WAT = (Path(__file__).parent / 'data' / 'wasm' / 'args.wat').resolve(strict=True)
ADDER_CONTRACT_WAT = (Path(__file__).parent / 'data' / 'wasm' / 'adder.wat').resolve(strict=True)
BYTES_CONTRACT_WAT = (Path(__file__).parent / 'data' / 'wasm' / 'bytes.wat').resolve(strict=True)
STORAGE_CONTRACT_WAT = (Path(__file__).parent / 'data' / 'wasm' / 'storage.wat').resolve(strict=True)


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


def test_default_io_dir_is_a_fresh_temp_dir() -> None:
    """With no io_dir, the composition root provisions a fresh temporary directory and seeds it."""
    srv = build_server(port=0)
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
        tb = TransactionBuilder(account, PASSPHRASE).append_create_account_op(
            destination=keypair.public_key, starting_balance='1000'
        )
        send_tx(server, keypair, tb)

    send_create_account()
    assert _rpc(server.port(), 'getLatestLedger', {})['result']['sequence'] == 1

    send_create_account()
    assert _rpc(server.port(), 'getLatestLedger', {})['result']['sequence'] == 2


def test_full_lifecycle_over_http(server: StellarRpcServer) -> None:
    """Full contract lifecycle through the HTTP server: account → upload → deploy → invoke.

    Each step asserts SUCCESS inside the shared helpers; this is the end-to-end smoke test
    that the whole pipeline works over HTTP, independent of any trace/return-value assertions.
    """
    invoke = deploy_and_get_invoker(server, EMPTY_CONTRACT_WAT)
    invoke('foo')


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
    # wasm instructions, so the stored trace is an empty array (resolved, not null/NOT_FOUND).
    trace = _rpc(server.port(), 'traceTransaction', {'hash': send_result['hash']})['result']
    assert trace == []


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


def test_trace_transaction_returns_full_instruction_trace_for_foo(server: StellarRpcServer) -> None:
    """traceTransaction returns the complete, ordered trace of an invocation: a ``callContract``
    entry frame, the executed WebAssembly instructions, and an ``endWasm`` exit frame.

    empty.wat's ``foo()`` body is a single ``i64.const 2`` (the Void return); the three leading
    instruction records are the contract's global initialisation and the ``block`` is the
    function frame. The instruction records are asserted record-for-record (the exact trace
    shown in the README) so any drift in format, ordering, or the array-vs-string shape of the
    result is caught. The entry/exit frames carry per-run contract and account ids, so they are
    checked structurally rather than by value.
    """
    invoke = deploy_and_get_invoker(server, EMPTY_CONTRACT_WAT)
    tx_hash = invoke('foo')

    trace = _rpc(server.port(), 'traceTransaction', {'hash': tx_hash})['result']

    # A callContract entry frame opens the trace: the account calls foo() on the contract with
    # no arguments at call depth 1.
    entry = trace[0]
    assert entry['instr'] == ['callContract']
    assert entry['function'] == 'foo'
    assert entry['args'] == []
    assert entry['depth'] == 1
    assert entry['from']['addrType'] == 'account'
    assert entry['to']['addrType'] == 'contract'

    # The executed WebAssembly instructions, exactly as shown in the README.
    assert trace[1:-1] == [
        {'pos': 3, 'instr': ['const', 'i32', 1048576], 'stack': [], 'locals': {}, 'mem': None},
        {'pos': 11, 'instr': ['const', 'i32', 1048576], 'stack': [], 'locals': {}, 'mem': None},
        {'pos': 19, 'instr': ['const', 'i32', 1048576], 'stack': [], 'locals': {}, 'mem': None},
        {'pos': None, 'instr': ['block'], 'stack': [], 'locals': {}, 'mem': None},
        {'pos': 3, 'instr': ['const', 'i64', 2], 'stack': [], 'locals': {}, 'mem': None},
    ]

    # An endWasm exit frame closes the trace: the call succeeded and returned Void.
    exit_frame = trace[-1]
    assert exit_frame['instr'] == ['endWasm']
    assert exit_frame['success'] is True
    assert exit_frame['result'] == {'type': 'void'}
    assert exit_frame['depth'] == 1


def test_trace_records_have_expected_structure_and_reflect_arguments(server: StellarRpcServer) -> None:
    """The trace opens with a ``callContract`` frame that echoes the decoded arguments, and each
    WebAssembly instruction record is a ``{pos, instr, stack, locals}`` object. For a call that
    takes arguments the arguments are bound as locals while intermediate values build up on the
    stack — exercising a richer trace than the argument-less ``foo()`` case.
    """
    invoke = deploy_and_get_invoker(server, ARGS_CONTRACT_WAT)
    tx_hash = invoke(
        'test_integers',
        [
            xdr.SCVal(type=SCValType.SCV_U32, u32=xdr.Uint32(42)),
            xdr.SCVal(type=SCValType.SCV_I32, i32=xdr.Int32(-7)),
            xdr.SCVal(type=SCValType.SCV_U64, u64=xdr.Uint64(100)),
            xdr.SCVal(type=SCValType.SCV_I64, i64=xdr.Int64(-200)),
        ],
    )

    trace = _rpc(server.port(), 'traceTransaction', {'hash': tx_hash})['result']

    assert isinstance(trace, list)
    assert len(trace) > 0

    # The callContract entry frame echoes the call target and its decoded arguments.
    entry = trace[0]
    assert entry['instr'] == ['callContract']
    assert entry['function'] == 'test_integers'
    assert entry['args'] == [
        {'type': 'u32', 'value': 42},
        {'type': 'i32', 'value': -7},
        {'type': 'u64', 'value': 100},
        {'type': 'i64', 'value': -200},
    ]

    # The instruction records (everything between the call-boundary frames) share one shape.
    instr_records = [record for record in trace if 'locals' in record]
    assert instr_records
    for record in instr_records:
        assert set(record) == {'pos', 'instr', 'stack', 'locals', 'mem'}
        assert record['pos'] is None or isinstance(record['pos'], int)
        # mem is null when linear memory is unchanged since the previous record, else a list of runs.
        assert record['mem'] is None or isinstance(record['mem'], list)
        assert isinstance(record['instr'], list) and record['instr']
        assert isinstance(record['instr'][0], str)  # opcode mnemonic
        # stack and locals hold [type, value] pairs.
        assert isinstance(record['stack'], list)
        assert all(isinstance(e, list) and len(e) == 2 and isinstance(e[0], str) for e in record['stack'])
        assert isinstance(record['locals'], dict)
        assert all(isinstance(e, list) and len(e) == 2 and isinstance(e[0], str) for e in record['locals'].values())

    # The four call arguments are bound as locals 0..3 by the time the body runs.
    locals_seen = {key for record in instr_records for key in record['locals']}
    assert {'0', '1', '2', '3'} <= locals_seen
    # Intermediate computation puts values on the stack at some point.
    assert any(record['stack'] for record in instr_records)
    # The function body returns Void: the final instruction pushes the i64 constant 2.
    assert instr_records[-1]['instr'] == ['const', 'i64', 2]


def test_call_tx_with_args(server: StellarRpcServer) -> None:
    """The scval_to_json / #decodeArg pipeline decodes each supported SCVal arg type correctly.

    Uses a minimal contract (args.wat) whose functions accept various arg types and return
    Void. For each call the arguments echoed in the trace's ``callContract`` frame must
    round-trip back to the exact SCVals that were sent — so a decoding bug is caught even
    when the transaction still succeeds. Covers: bool, u32, i32, u64, i64, u128, i128, symbol.
    """
    invoke = deploy_and_get_invoker(server, ARGS_CONTRACT_WAT)

    def assert_args_round_trip(func: str, args: list[xdr.SCVal]) -> None:
        tx_hash = invoke(func, args)
        entry = _rpc(server.port(), 'traceTransaction', {'hash': tx_hash})['result'][0]
        assert entry['function'] == func
        assert [scval_from_json(arg) for arg in entry['args']] == args

    assert_args_round_trip('test_bool', [xdr.SCVal(type=SCValType.SCV_BOOL, b=True)])
    assert_args_round_trip(
        'test_integers',
        [
            xdr.SCVal(type=SCValType.SCV_U32, u32=xdr.Uint32(42)),
            xdr.SCVal(type=SCValType.SCV_I32, i32=xdr.Int32(-7)),
            xdr.SCVal(type=SCValType.SCV_U64, u64=xdr.Uint64(100)),
            xdr.SCVal(type=SCValType.SCV_I64, i64=xdr.Int64(-200)),
        ],
    )
    assert_args_round_trip(
        'test_wide_integers',
        [
            xdr.SCVal(type=SCValType.SCV_U128, u128=xdr.UInt128Parts(hi=xdr.Uint64(0), lo=xdr.Uint64(999))),
            xdr.SCVal(type=SCValType.SCV_I128, i128=xdr.Int128Parts(hi=xdr.Int64(0), lo=xdr.Uint64(888))),
        ],
    )
    assert_args_round_trip('test_symbol', [xdr.SCVal(type=SCValType.SCV_SYMBOL, sym=xdr.SCSymbol(sc_symbol=b'hello'))])


def test_call_tx_with_composite_args(server: StellarRpcServer) -> None:
    """The scval_to_json / #decodeArg pipeline decodes composite (vec / map) call args.

    Regression test for the composite-argument blocker: komet-node used to decode only
    scalar SCVals in call arguments (``scval_to_json`` raised on SCV_VEC/SCV_MAP, and the
    ``#decodeArg`` rules had no vec/map cases), so a Vec/Map argument was rejected at
    admission and never ran. Both sides now recurse, so a contract call carrying vec and
    map arguments reaches SUCCESS (asserted by ``invoke``) and — like ``test_call_tx_with_args``
    — the arguments echoed in the trace's ``callContract`` frame round-trip back to the exact
    SCVals sent, so a decoding bug is caught even when the transaction still succeeds.

    User enums, structs, and tuples all reduce to vec/map at the XDR level, so the nested
    ``Vec<(enum, i128)>`` case below (with an Address-carrying variant and a negative i128)
    stands in for the real ``Vec<(AssetKey, i128)>`` motivating argument.
    """
    invoke = deploy_and_get_invoker(server, ARGS_CONTRACT_WAT)

    def assert_args_round_trip(func: str, args: list[xdr.SCVal]) -> None:
        tx_hash = invoke(func, args)
        trace = _rpc(server.port(), 'traceTransaction', {'hash': tx_hash})['result']
        # A composite argument is allocated as a host object first, so the callContract
        # frame is not necessarily trace[0] (unlike the scalar-only case): find it.
        entry = next(record for record in trace if record.get('instr') == ['callContract'])
        assert entry['function'] == func
        assert [scval_from_json(arg) for arg in entry['args']] == args

    def sym(name: str) -> xdr.SCVal:
        return xdr.SCVal(type=SCValType.SCV_SYMBOL, sym=xdr.SCSymbol(sc_symbol=name.encode()))

    def i128(value: int) -> xdr.SCVal:
        # Two's-complement split into (hi: signed int64, lo: unsigned int64) so negative
        # and high-bit values round-trip, not just small positive ones.
        unsigned = value & ((1 << 128) - 1)
        hi = unsigned >> 64
        lo = unsigned & ((1 << 64) - 1)
        if hi >= (1 << 63):
            hi -= 1 << 64
        return xdr.SCVal(type=SCValType.SCV_I128, i128=xdr.Int128Parts(hi=xdr.Int64(hi), lo=xdr.Uint64(lo)))

    def u32(value: int) -> xdr.SCVal:
        return xdr.SCVal(type=SCValType.SCV_U32, u32=xdr.Uint32(value))

    def vec(elems: list[xdr.SCVal]) -> xdr.SCVal:
        return xdr.SCVal(type=SCValType.SCV_VEC, vec=xdr.SCVec(elems))

    def mp(entries: list[tuple[xdr.SCVal, xdr.SCVal]]) -> xdr.SCVal:
        return xdr.SCVal(type=SCValType.SCV_MAP, map=xdr.SCMap([xdr.SCMapEntry(key=k, val=v) for k, v in entries]))

    address = Address(Keypair.random().public_key).to_xdr_sc_val()

    # A flat vec of scalars.
    assert_args_round_trip('test_vec', [vec([u32(1), u32(2), u32(3)])])

    # The nested motivating case: Vec<(enum, i128)> mirroring Vec<(AssetKey, i128)> — a unit
    # variant (Native), an Address-carrying variant (Stellar(addr)), and a positive and a
    # negative i128, exercising SCV_ADDRESS nested in a composite and the full signed i128 range.
    assert_args_round_trip(
        'test_vec',
        [
            vec(
                [
                    vec([vec([sym('Native')]), i128(1000)]),
                    vec([vec([sym('Stellar'), address]), i128(-5)]),
                ]
            )
        ],
    )

    # A map from symbol keys to scalar values (a struct at the XDR level). Keys are sent in
    # sorted order ('amount' < 'nonce') to match the canonical SCMap ordering the trace echoes.
    assert_args_round_trip('test_map', [mp([(sym('amount'), i128(500)), (sym('nonce'), u32(7))])])

    # A map nested inside a vec — composites compose in both directions.
    assert_args_round_trip('test_vec', [vec([mp([(sym('k'), u32(1))])])])


def test_call_tx_with_return_value(server: StellarRpcServer) -> None:
    """A contract invocation that returns a non-Void value succeeds.

    Regression test: transactions used to be decoded into ``callTx(..., Void)``, which
    asserts the call returns Void. Invoking ``add(2, 3)`` (returning U32(5)) therefore got
    stuck in the semantics and was recorded as FAILED. ``uncheckedCallTx`` drops the return
    value check.
    """
    keypair, account = fund_account(server)  # ledger 1
    deployed = deploy_contract(server, keypair, account, ADDER_CONTRACT_WAT)  # ledgers 2, 3
    invoke = make_invoker(server, keypair, account, deployed.address)

    # add(2, 3) returns U32(5), not Void — make_invoker asserts the call reaches SUCCESS.
    invoke(
        'add',
        [
            xdr.SCVal(type=SCValType.SCV_U32, u32=xdr.Uint32(2)),
            xdr.SCVal(type=SCValType.SCV_U32, u32=xdr.Uint32(3)),
        ],
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


def test_get_ledger_entries_account(server: StellarRpcServer) -> None:
    """An ACCOUNT ledger key resolves to an AccountEntry; unknown keys are silently dropped."""
    keypair = Keypair.random()
    account = Account(keypair.public_key, sequence=0)
    send_tx(
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
    # Set up: create account, upload storage.wat, deploy, invoke store() which writes the
    # persistent storage entry U32(7) -> U32(42).
    keypair, account = fund_account(server)
    deployed = deploy_contract(server, keypair, account, STORAGE_CONTRACT_WAT)
    wasm_hash, wasm_bytecode, contract_address = deployed.wasm_hash, deployed.wasm_bytecode, deployed.address
    make_invoker(server, keypair, account, contract_address)('store')

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

    Takes the caller's keypair/account (the simulate tests reuse the account's sequence to
    build a follow-up unsigned invocation). Submits three transactions, so the ledger
    sequence afterwards is 3.
    """
    tb = TransactionBuilder(account, PASSPHRASE).append_create_account_op(keypair.public_key, '1000')
    send_tx(server, keypair, tb)
    return deploy_contract(server, keypair, account, ADDER_CONTRACT_WAT).address


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


# ---------------------------------------------------------------------------
# getTransaction: resultXdr / resultMetaXdr contents.
#
# Per the official spec, `resultXdr` is "a base64 encoded string of the raw
# TransactionResult XDR struct" and `resultMetaXdr` the raw TransactionMeta, present
# when status is SUCCESS or FAILED. Fields are either omitted or valid XDR — an
# empty-string stub is neither.
# ---------------------------------------------------------------------------


def _tx_result_of(get_result: dict[str, Any]) -> xdr.TransactionResult:
    """Decode a receipt's resultXdr, asserting it is real base64 XDR (not an empty stub)."""
    result_xdr = get_result.get('resultXdr')
    assert isinstance(result_xdr, str), f'resultXdr missing or not a string: {get_result}'
    assert result_xdr != '', 'resultXdr must be real XDR or omitted, not an empty string'
    return xdr.TransactionResult.from_xdr(result_xdr)


def _tx_meta_of(get_result: dict[str, Any]) -> xdr.TransactionMeta:
    """Decode a receipt's resultMetaXdr, asserting it is real base64 XDR (not an empty stub)."""
    meta_xdr = get_result.get('resultMetaXdr')
    assert isinstance(meta_xdr, str), f'resultMetaXdr missing or not a string: {get_result}'
    assert meta_xdr != '', 'resultMetaXdr must be real XDR or omitted, not an empty string'
    return xdr.TransactionMeta.from_xdr(meta_xdr)


def _soroban_return_value(meta: xdr.TransactionMeta) -> xdr.SCVal:
    """Extract sorobanMeta.returnValue from a TransactionMeta (v3 at protocol version 22)."""
    assert meta.v == 3, f'expected TransactionMeta v3 at protocol version 22, got v{meta.v}'
    assert meta.v3 is not None and meta.v3.soroban_meta is not None
    return meta.v3.soroban_meta.return_value


def test_get_transaction_success_returns_decodable_result_xdr(server: StellarRpcServer) -> None:
    """A SUCCESS receipt carries a real TransactionResult and TransactionMeta.

    The TransactionResult must decode from base64 and report code txSUCCESS; the
    TransactionMeta must decode as well.
    """
    keypair = Keypair.random()
    account = Account(keypair.public_key, sequence=0)
    envelope = (
        TransactionBuilder(account, Network.TESTNET_NETWORK_PASSPHRASE)
        .append_create_account_op(destination=keypair.public_key, starting_balance='1000')
        .set_timeout(30)
        .build()
    )
    envelope.sign(keypair)
    tx_hash = _rpc(server.port(), 'sendTransaction', {'transaction': envelope.to_xdr()})['result']['hash']

    get_result = _rpc(server.port(), 'getTransaction', {'hash': tx_hash})['result']
    assert get_result['status'] == 'SUCCESS'
    # The K-side receipt's internal returnValue field must never reach RPC clients: the
    # server rewrites it into resultXdr/resultMetaXdr before the receipt is ever served.
    assert 'returnValue' not in get_result

    tx_result = _tx_result_of(get_result)
    assert tx_result.result.code == xdr.TransactionResultCode.txSUCCESS

    _tx_meta_of(get_result)  # must decode as TransactionMeta


def test_get_transaction_invocation_reports_return_value(server: StellarRpcServer) -> None:
    """A successful contract invocation surfaces its return value in resultMetaXdr.

    ``add(2, 3)`` returns U32(5). Real stellar-rpc reports the invocation's return value
    as ``sorobanMeta.returnValue`` inside the TransactionMeta, and the TransactionResult
    carries a successful InvokeHostFunction operation result. Every receipt along the
    lifecycle (create account, upload, deploy) must carry decodable result XDR too.
    """
    keypair = Keypair.random()
    account = Account(keypair.public_key, sequence=0)

    def builder() -> TransactionBuilder:
        return TransactionBuilder(account, PASSPHRASE)

    def send(tb: TransactionBuilder) -> dict[str, Any]:
        _, get_res = send_tx(server, keypair, tb)
        # The internal K-side returnValue field must never leak into getTransaction responses.
        assert 'returnValue' not in get_res
        # Every successful receipt, whatever the operation, has a decodable txSUCCESS result.
        assert _tx_result_of(get_res).result.code == xdr.TransactionResultCode.txSUCCESS
        return get_res

    # Set up: create account, upload adder.wat, deploy contract
    send(builder().append_create_account_op(keypair.public_key, '1000'))

    wasm_bytecode = wat_to_wasm(ADDER_CONTRACT_WAT)
    send(builder().append_upload_contract_wasm_op(wasm_bytecode))

    wasm_hash = sha256(wasm_bytecode)
    salt = b'\x00' * 32
    send(builder().append_create_contract_op(wasm_hash, keypair.public_key, None, salt))

    contract_address = contract_address_from_deployer(keypair.public_key, salt)
    invoke_result = send(
        builder().append_invoke_contract_function_op(
            contract_address,
            'add',
            [
                xdr.SCVal(type=SCValType.SCV_U32, u32=xdr.Uint32(2)),
                xdr.SCVal(type=SCValType.SCV_U32, u32=xdr.Uint32(3)),
            ],
        )
    )

    # The TransactionResult reports the successful InvokeHostFunction operation.
    tx_result = _tx_result_of(invoke_result)
    op_results = tx_result.result.results
    assert op_results, 'TransactionResult must carry the InvokeHostFunction operation result'
    assert op_results[0].tr is not None
    invoke_op_result = op_results[0].tr.invoke_host_function_result
    assert invoke_op_result is not None
    assert invoke_op_result.code == xdr.InvokeHostFunctionResultCode.INVOKE_HOST_FUNCTION_SUCCESS

    # The TransactionMeta reports the call's return value: U32(5).
    return_value = _soroban_return_value(_tx_meta_of(invoke_result))
    assert return_value.type == SCValType.SCV_U32
    assert return_value.u32 == xdr.Uint32(5)


def test_get_transaction_reports_empty_bytes_return_value(server: StellarRpcServer) -> None:
    """A contract returning empty Bytes yields SCV_BYTES(b'') in sorobanMeta.returnValue.

    Regression test for the zero-length edge of the receipt's hex encoding: an empty
    byte string round-trips through the K-side JSON encoding as an empty hex string, not
    as ``"0"`` (an odd-length non-hex value that breaks the XDR rewrite and would leak
    the internal ``returnValue`` field to clients).
    """
    keypair, account = fund_account(server)
    deployed = deploy_contract(server, keypair, account, BYTES_CONTRACT_WAT)

    invoke_tx_hash = make_invoker(server, keypair, account, deployed.address)('empty_bytes')
    invoke_result = _rpc(server.port(), 'getTransaction', {'hash': invoke_tx_hash})['result']
    assert 'returnValue' not in invoke_result  # internal receipt field, must never be served

    return_value = _soroban_return_value(_tx_meta_of(invoke_result))
    assert return_value.type == SCValType.SCV_BYTES
    assert return_value.bytes is not None
    assert return_value.bytes.sc_bytes == b''


def test_get_transaction_failed_reports_error_result_xdr(server: StellarRpcServer) -> None:
    """A FAILED receipt carries a real error TransactionResult, not an empty stub.

    Invoking a never-deployed contract fails. The spec requires `resultXdr` (a base64
    TransactionResult, here with code txFAILED) when status is FAILED, and `ledger`/
    `createdAt` to be reported with the same shape as on SUCCESS receipts.
    """
    keypair = Keypair.random()
    account = Account(keypair.public_key, sequence=0)

    def builder() -> TransactionBuilder:
        return TransactionBuilder(account, PASSPHRASE)

    # A successful reference transaction first, to compare receipt shapes across statuses.
    _, ok_result = send_tx(server, keypair, builder().append_create_account_op(keypair.public_key, '1000'))
    assert ok_result['status'] == 'SUCCESS'

    # A failing invocation of a never-deployed contract — submitted directly, since send_tx
    # asserts SUCCESS and this transaction is expected to end up FAILED.
    missing_contract = StrKey.encode_contract(b'\x22' * 32)  # valid C-strkey, never deployed
    env = builder().append_invoke_contract_function_op(missing_contract, 'foo', []).set_timeout(30).build()
    env.sign(keypair)
    bad_hash = _rpc(server.port(), 'sendTransaction', {'transaction': env.to_xdr()})['result']['hash']

    get_result = _rpc(server.port(), 'getTransaction', {'hash': bad_hash})['result']
    assert get_result['status'] == 'FAILED'
    assert 'returnValue' not in get_result  # internal receipt field, must never be served

    tx_result = _tx_result_of(get_result)
    assert tx_result.result.code == xdr.TransactionResultCode.txFAILED

    # resultMetaXdr may be omitted on a failed transaction, but must never be an empty stub.
    if 'resultMetaXdr' in get_result:
        _tx_meta_of(get_result)

    # `ledger`/`createdAt` are present on FAILED receipts, with the same JSON types as on
    # SUCCESS receipts (their exact number-vs-string encoding is covered elsewhere).
    for field in ('ledger', 'createdAt'):
        assert field in get_result, f'FAILED receipt must report {field}'
        assert type(get_result[field]) is type(ok_result[field]), f'{field} type differs across statuses'
    # createdAt in getTransaction (singular) is an int64 rendered as a decimal string.
    assert isinstance(get_result['createdAt'], str) and get_result['createdAt'].isdigit()


def test_get_transaction_not_found_omits_transaction_fields(server: StellarRpcServer) -> None:
    """A NOT_FOUND response carries no transaction details (omitted, not empty/null)."""
    get_result = _rpc(server.port(), 'getTransaction', {'hash': 'b' * 64})['result']
    assert get_result['status'] == 'NOT_FOUND'
    for field in ('ledger', 'createdAt', 'envelopeXdr', 'resultXdr', 'resultMetaXdr', 'returnValue'):
        assert field not in get_result, f'NOT_FOUND response must omit {field}'

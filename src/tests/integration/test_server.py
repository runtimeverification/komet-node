from __future__ import annotations

import json
import socket
import subprocess
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

import pytest
from stellar_sdk import Account, Keypair, Network, TransactionBuilder, xdr
from stellar_sdk.xdr.sc_val_type import SCValType

from komet_node.server import StellarRpcServer

EMPTY_CONTRACT_WAT = (Path(__file__).parent / 'data' / 'wasm' / 'empty.wat').resolve(strict=True)
ARGS_CONTRACT_WAT = (Path(__file__).parent / 'data' / 'wasm' / 'args.wat').resolve(strict=True)


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
        state_file=tmp_path / 'state.kore',
        network_passphrase=Network.TESTNET_NETWORK_PASSPHRASE,
    )
    thread = threading.Thread(target=srv.serve, daemon=True)
    thread.start()
    _wait_for_server('localhost', port)
    yield srv
    srv.shutdown()


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

    # since krun runs synchronously, the result is already stored
    get_result = _rpc(server.port(), 'getTransaction', {'hash': tx_hash})
    assert get_result['result']['status'] == 'SUCCESS'
    assert get_result['result']['envelopeXdr'] == xdr_str


def test_ledger_seq_increments(server: StellarRpcServer) -> None:
    """ledger_seq increments by 1 for each successful transaction."""
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


def test_trace_transaction_returns_result_directly(server: StellarRpcServer) -> None:
    """traceTransaction returns status/ledger/trace immediately, not PENDING."""
    keypair = Keypair.random()
    account = Account(keypair.public_key, sequence=0)

    envelope = (
        TransactionBuilder(account, Network.TESTNET_NETWORK_PASSPHRASE)
        .append_create_account_op(destination=keypair.public_key, starting_balance='1000')
        .set_timeout(30)
        .build()
    )
    envelope.sign(keypair)

    result = _rpc(server.port(), 'traceTransaction', {'transaction': envelope.to_xdr()})['result']

    assert result['status'] == 'SUCCESS'
    assert result['hash'] == envelope.hash_hex()
    assert 'ledger' in result
    assert 'trace' in result
    assert 'latestLedger' in result


def test_trace_transaction_stored_for_get_transaction(server: StellarRpcServer) -> None:
    """traceTransaction stores the result so getTransaction can retrieve it."""
    keypair = Keypair.random()
    account = Account(keypair.public_key, sequence=0)

    envelope = (
        TransactionBuilder(account, Network.TESTNET_NETWORK_PASSPHRASE)
        .append_create_account_op(destination=keypair.public_key, starting_balance='1000')
        .set_timeout(30)
        .build()
    )
    envelope.sign(keypair)

    trace_result = _rpc(server.port(), 'traceTransaction', {'transaction': envelope.to_xdr()})['result']
    get_result = _rpc(server.port(), 'getTransaction', {'hash': trace_result['hash']})['result']

    assert get_result['status'] == 'SUCCESS'
    assert get_result['envelopeXdr'] == envelope.to_xdr()


def test_trace_transaction_produces_trace_on_contract_invocation(server: StellarRpcServer) -> None:
    """traceTransaction returns non-empty trace JSONL when a contract function is invoked."""
    keypair = Keypair.random()
    account = Account(keypair.public_key, sequence=0)

    def builder() -> TransactionBuilder:
        return TransactionBuilder(account, Network.TESTNET_NETWORK_PASSPHRASE)

    def sign_and_xdr(tb: TransactionBuilder) -> str:
        env = tb.set_timeout(30).build()
        env.sign(keypair)
        return env.to_xdr()

    def send(xdr: str) -> None:
        res = _rpc(server.port(), 'sendTransaction', {'transaction': xdr})
        assert res['result']['status'] == 'PENDING'
        tx_hash = res['result']['hash']
        assert _rpc(server.port(), 'getTransaction', {'hash': tx_hash})['result']['status'] == 'SUCCESS'

    # Set up: create account, upload wasm, deploy contract
    send(sign_and_xdr(builder().append_create_account_op(keypair.public_key, '1000')))

    wasm_bytecode = wat_to_wasm(EMPTY_CONTRACT_WAT)
    send(sign_and_xdr(builder().append_upload_contract_wasm_op(wasm_bytecode)))

    from stellar_sdk.utils import sha256

    wasm_hash = sha256(wasm_bytecode)
    salt = b'\x00' * 32
    send(sign_and_xdr(builder().append_create_contract_op(wasm_hash, keypair.public_key, None, salt)))

    # Invoke via traceTransaction and check trace content
    contract_address = server.encoder.contract_address_from_deployer_address(keypair.public_key, salt)
    invoke_xdr = sign_and_xdr(builder().append_invoke_contract_function_op(contract_address, 'foo', []))
    result = _rpc(server.port(), 'traceTransaction', {'transaction': invoke_xdr})['result']

    assert result['status'] == 'SUCCESS'
    assert result['trace'] is not None
    # Trace is newline-separated JSON records; verify each line parses as JSON
    lines = [line for line in result['trace'].splitlines() if line.strip()]
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

"""Shared fixtures and helpers for the integration tests.

The integration tests drive a live ``StellarRpcServer`` over HTTP. This module centralises
everything they build on: the server fixture, the JSON-RPC / HTTP plumbing, wat compilation,
the JSON-shape predicates used to assert the stellar-rpc wire format, and the transaction
submit / deploy helpers. Keeping these in one place stops each feature's test file from
carrying its own drifting copy.
"""

from __future__ import annotations

import json
import re
import socket
import subprocess
import threading
import time
import urllib.request
from typing import TYPE_CHECKING, Any, NamedTuple

import pytest
from stellar_sdk import Account, Keypair, Network, TransactionBuilder
from stellar_sdk.utils import sha256

from komet_node.server import StellarRpcServer

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from stellar_sdk import xdr

PASSPHRASE = Network.TESTNET_NETWORK_PASSPHRASE


# ---------------------------------------------------------------------------
# HTTP / JSON-RPC plumbing
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Spec-shape predicates
#
# The official serialization rules come from the Go structs in stellar/go-stellar-sdk
# protocols/rpc (what real stellar-rpc emits): ledger sequence numbers and protocolVersion
# are JSON numbers; the close-time fields on the singular methods are int64 with Go's
# `,string` encoding, i.e. JSON strings holding a decimal integer; hashes are 64 lowercase
# hex characters.
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Server fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def server(tmp_path: Path) -> Iterator[StellarRpcServer]:
    port = _find_free_port()
    srv = StellarRpcServer(
        host='localhost',
        port=port,
        io_dir=tmp_path,
        network_passphrase=PASSPHRASE,
    )
    thread = threading.Thread(target=srv.serve, daemon=True)
    thread.start()
    _wait_for_server('localhost', port)
    yield srv
    srv.shutdown()


# ---------------------------------------------------------------------------
# Transaction submit / deploy helpers
#
# Every lifecycle test builds on the same primitives: submit a signed transaction and
# confirm it reaches SUCCESS, fund a fresh account, deploy a contract from a .wat file, and
# invoke functions on it. They compose — ``deploy_and_get_invoker`` is just the three-step
# account -> upload -> deploy setup wrapped around ``make_invoker``.
# ---------------------------------------------------------------------------


class Deployed(NamedTuple):
    """A deployed contract instance: its C-strkey address, wasm hash, and wasm bytecode."""

    address: str
    wasm_hash: bytes
    wasm_bytecode: bytes


def send_tx(server: StellarRpcServer, keypair: Keypair, tb: TransactionBuilder) -> tuple[str, dict[str, Any]]:
    """Sign, submit, and confirm a transaction.

    Asserts the submission is accepted (PENDING) and the transaction executes to SUCCESS.
    Returns ``(tx_hash, getTransaction_result)``.
    """
    env = tb.set_timeout(30).build()
    env.sign(keypair)
    res = _rpc(server.port(), 'sendTransaction', {'transaction': env.to_xdr()})
    assert res['result']['status'] == 'PENDING'
    tx_hash = res['result']['hash']
    get_res = _rpc(server.port(), 'getTransaction', {'hash': tx_hash})['result']
    assert get_res['status'] == 'SUCCESS', f'Transaction failed: {get_res}'
    return tx_hash, get_res


def fund_account(server: StellarRpcServer) -> tuple[Keypair, Account]:
    """Create a fresh, funded account; return its keypair and sequence-tracking ``Account``."""
    keypair = Keypair.random()
    account = Account(keypair.public_key, sequence=0)
    tb = TransactionBuilder(account, PASSPHRASE).append_create_account_op(keypair.public_key, '1000')
    send_tx(server, keypair, tb)
    return keypair, account


def deploy_contract(server: StellarRpcServer, keypair: Keypair, account: Account, wat_path: Path) -> Deployed:
    """Upload ``wat_path`` and deploy an instance from it, signed by ``keypair``/``account``.

    The account must already exist. Submits two transactions (upload + create) and returns
    the deployed contract's address, wasm hash, and wasm bytecode.
    """

    def builder() -> TransactionBuilder:
        return TransactionBuilder(account, PASSPHRASE)

    wasm_bytecode = wat_to_wasm(wat_path)
    send_tx(server, keypair, builder().append_upload_contract_wasm_op(wasm_bytecode))
    wasm_hash = sha256(wasm_bytecode)
    salt = b'\x00' * 32
    send_tx(server, keypair, builder().append_create_contract_op(wasm_hash, keypair.public_key, None, salt))
    address = server.encoder.contract_address_from_deployer_address(keypair.public_key, salt)
    return Deployed(address=address, wasm_hash=wasm_hash, wasm_bytecode=wasm_bytecode)


def make_invoker(
    server: StellarRpcServer, keypair: Keypair, account: Account, contract_address: str
) -> Callable[..., str]:
    """Return an ``invoke(func, args=None) -> tx_hash`` callable that asserts each call SUCCEEDs."""

    def invoke(func: str, args: list[xdr.SCVal] | None = None) -> str:
        tb = TransactionBuilder(account, PASSPHRASE).append_invoke_contract_function_op(
            contract_address, func, args or []
        )
        tx_hash, _ = send_tx(server, keypair, tb)
        return tx_hash

    return invoke


def deploy_and_get_invoker(server: StellarRpcServer, wat_path: Path) -> Callable[..., str]:
    """Fund an account, upload ``wat_path``, and deploy a contract instance from it.

    Returns an ``invoke(func, args=None)`` callable that runs a contract function and returns
    the executed transaction's hash, asserting the whole setup and each call reaches SUCCESS.
    """
    keypair, account = fund_account(server)
    deployed = deploy_contract(server, keypair, account, wat_path)
    return make_invoker(server, keypair, account, deployed.address)

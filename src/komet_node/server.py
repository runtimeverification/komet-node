from __future__ import annotations

import json
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from stellar_sdk import Network

from komet_node.interpreter import NodeInterpreter
from komet_node.transaction import TransactionEncoder

if TYPE_CHECKING:
    from http.server import HTTPServer as HTTPServerType

_PROTOCOL_VERSION: Final = '22'

_TX_METHODS: Final = ('sendTransaction', 'traceTransaction')


class StellarRpcServer:
    """
    Thin HTTP/JSON-RPC shim in front of the K node semantics.

    It decodes the Stellar XDR envelope (:class:`TransactionEncoder`), then runs the
    request envelope through the semantics (:class:`NodeInterpreter`). All RPC dispatch,
    the transaction store, ledger accounting and response formatting are performed in K
    (``node.md``). The persistent state lives in ``io_dir``:

      - ``state.kore``        — the KORE world-state configuration (accounts, contracts, wasm)
      - ``metadata.json``     — ``{"latest_ledger": N}``
      - ``transactions.json`` — map from tx hash to stored receipt
    """

    interpreter: NodeInterpreter
    encoder: TransactionEncoder
    state_file: Path
    io_dir: Path

    def __init__(
        self,
        host: str = 'localhost',
        port: int = 8000,
        state_file: Path = Path('state.kore'),
        network_passphrase: str = Network.TESTNET_NETWORK_PASSPHRASE,
        trace: bool = False,
    ) -> None:
        self.host = host
        self._port = port
        self.interpreter = NodeInterpreter()
        self.encoder = TransactionEncoder(network_passphrase, trace=trace)
        self.state_file = state_file.resolve()
        self.io_dir = self.state_file.parent
        self.io_dir.mkdir(parents=True, exist_ok=True)
        self._httpd: HTTPServerType | None = None

        fresh = not self.state_file.exists()
        if fresh:
            self.state_file.write_text(self.interpreter.empty_config())
        metadata_file = self.io_dir / 'metadata.json'
        if fresh or not metadata_file.exists():
            metadata_file.write_text(json.dumps({'latest_ledger': 0}))
        transactions_file = self.io_dir / 'transactions.json'
        if fresh or not transactions_file.exists():
            transactions_file.write_text(json.dumps({}))

    def serve(self) -> None:
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get('Content-Length', 0))
                body = self.rfile.read(length)
                response = server._handle(body)
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(response)))
                self.end_headers()
                self.wfile.write(response)

            def log_message(self, *args: Any) -> None:  # silence per-request logging
                pass

        self._httpd = HTTPServer((self.host, int(self._port)), Handler)
        self._httpd.serve_forever()

    def port(self) -> int:
        if self._httpd is not None:
            return self._httpd.server_port
        return int(self._port)

    def shutdown(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()  # release the listening socket so the port is freed

    # ------------------------------------------------------------------
    # Request handling
    # ------------------------------------------------------------------

    def _handle(self, body: bytes) -> bytes:
        """Parse a raw JSON-RPC body and return the response bytes (the HTTP entry point)."""
        try:
            req = json.loads(body.decode('utf-8'))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return _error_bytes(None, -32700, 'Parse error')
        if not isinstance(req, dict):
            return _error_bytes(None, -32600, 'Invalid Request')
        request_id = req.get('id')
        params = req.get('params')
        try:
            return self.handle_rpc(req.get('method'), params if isinstance(params, dict) else {}, request_id).encode(
                'utf-8'
            )
        except Exception:
            # A malformed request must never take down the server thread.
            return _error_bytes(request_id, -32603, 'Internal error')

    def handle_rpc(self, method: str | None, params: dict[str, Any], request_id: Any = None) -> str:
        """Dispatch a single JSON-RPC call and return the response envelope as a JSON string.

        Usable without the HTTP layer (e.g. from scripts and tests).
        """
        now = str(int(time.time()))

        if method in _TX_METHODS:
            transaction = params.get('transaction')
            if not isinstance(transaction, str):
                return _error_str(request_id, -32602, "Invalid params: 'transaction' (XDR string) is required")
            try:
                envelope, program_steps = self.encoder.build_tx_request(
                    method, request_id, transaction, now, force_trace=(method == 'traceTransaction')
                )
            except Exception as err:
                return _error_str(request_id, -32602, f'Invalid params: could not decode transaction XDR ({err})')
            response = self.interpreter.run(self.state_file, self.io_dir, envelope, program_steps)
            if response is None:
                return json.dumps(self._failure_response(method, request_id, envelope, now))
            return response

        read_only_envelope = self._read_only_envelope(method, params, request_id, now)
        if isinstance(read_only_envelope, str):  # a pre-formatted JSON-RPC error
            return read_only_envelope
        if read_only_envelope is None:
            return _error_str(request_id, -32601, 'Method not found')
        response = self.interpreter.run(self.state_file, self.io_dir, read_only_envelope, None)
        if response is None:
            return _error_str(request_id, -32603, 'Internal error')
        return response

    def _read_only_envelope(
        self, method: str | None, params: dict[str, Any], request_id: Any, now: str
    ) -> dict[str, Any] | str | None:
        """Build the request envelope for a read-only method.

        Returns the envelope dict, ``None`` if the method is unknown, or a pre-formatted
        JSON-RPC error string for a recognised method with invalid params.
        """
        base = {'method': method, 'id': request_id, 'now': now}
        if method == 'getHealth':
            return base
        if method == 'getNetwork':
            return {**base, 'passphrase': self.encoder.network_passphrase, 'protocolVersion': _PROTOCOL_VERSION}
        if method == 'getLatestLedger':
            return {**base, 'protocolVersion': _PROTOCOL_VERSION}
        if method == 'getTransaction':
            tx_hash = params.get('hash')
            if not isinstance(tx_hash, str):
                return _error_str(request_id, -32602, "Invalid params: 'hash' (string) is required")
            return {**base, 'hash': tx_hash}
        return None

    def _failure_response(self, method: str, rpc_id: Any, envelope: dict[str, Any], now: str) -> dict[str, Any]:
        """Synthesise the response for a transaction that got stuck (failed) in the semantics.

        The K run does not produce a ``response.json`` for a failed transaction and the
        world state is left unchanged. We record a FAILED receipt so a later getTransaction
        finds it, without bumping the ledger.
        """
        metadata = json.loads((self.io_dir / 'metadata.json').read_text())
        ledger = metadata.get('latest_ledger', 0)
        tx_hash = envelope['txHash']

        receipt = {
            'status': 'FAILED',
            'ledger': str(ledger),
            'createdAt': now,
            'envelopeXdr': envelope['envelopeXdr'],
            'resultXdr': '',
            'resultMetaXdr': '',
            'trace': None,
        }
        transactions_file = self.io_dir / 'transactions.json'
        transactions = json.loads(transactions_file.read_text())
        transactions[tx_hash] = receipt
        transactions_file.write_text(json.dumps(transactions))

        if method == 'sendTransaction':
            result = {
                'hash': tx_hash,
                'status': 'PENDING',
                'latestLedger': str(ledger),
                'latestLedgerCloseTime': now,
            }
        else:
            result = {
                'hash': tx_hash,
                'status': 'FAILED',
                'ledger': str(ledger),
                'trace': None,
                'latestLedger': str(ledger),
                'latestLedgerCloseTime': now,
            }
        return {'jsonrpc': '2.0', 'id': rpc_id, 'result': result}


def _error_str(rpc_id: Any, code: int, message: str) -> str:
    return json.dumps({'jsonrpc': '2.0', 'id': rpc_id, 'error': {'code': code, 'message': message}})


def _error_bytes(rpc_id: Any, code: int, message: str) -> bytes:
    return _error_str(rpc_id, code, message).encode('utf-8')

from __future__ import annotations

import json
import logging
import re
import sys
import tempfile
import time
import traceback
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from stellar_sdk import Network, TransactionEnvelope, xdr

from komet_node.interpreter import NodeInterpreter
from komet_node.scval import json_to_scval
from komet_node.transaction import SimulationRejected, TransactionEncoder, malformed_tx_result_xdr

if TYPE_CHECKING:
    from http.server import HTTPServer as HTTPServerType

_PROTOCOL_VERSION: Final = 22

# Transaction hashes are 64 lowercase hex characters (the spec's Hash schema:
# ^[a-f\d]{64}$). Anything else is rejected as Invalid params before reaching K.
_TX_HASH_RE: Final = re.compile(r'[0-9a-f]{64}')

# Synthetic minimum resource fee (in stroops) reported by simulateTransaction. The K
# semantics do not meter resources, so any plausible constant is as honest as another;
# clients only need a stringified number they can add on top of the inclusion fee.
_MIN_RESOURCE_FEE: Final = '100000'


def _empty_transaction_data() -> str:
    """Minimal valid ``SorobanTransactionData``: empty footprint, all-zero resources.

    komet-node does not compute ledger footprints or resource usage, so all-zero values
    are the honest choice; the field exists because clients expect to XDR-decode it.
    """
    data = xdr.SorobanTransactionData(
        ext=xdr.SorobanTransactionDataExt(0),
        resources=xdr.SorobanResources(
            footprint=xdr.LedgerFootprint(read_only=[], read_write=[]),
            instructions=xdr.Uint32(0),
            disk_read_bytes=xdr.Uint32(0),
            write_bytes=xdr.Uint32(0),
        ),
        resource_fee=xdr.Int64(0),
    )
    return data.to_xdr()


_TRANSACTION_DATA: Final = _empty_transaction_data()

# Only sendTransaction executes and commits a transaction. simulateTransaction also executes
# one, but as a dry run with its own dispatch path (see _handle_simulate); traceTransaction
# is a read-only lookup of the trace stored on a previously executed transaction's receipt
# (see _read_only_envelope).
_TX_METHODS: Final = ('sendTransaction',)

_log = logging.getLogger('komet_node')


class StellarRpcServer:
    """
    Long-running HTTP/JSON-RPC server that wraps the one-shot K node semantics.

    The compiled semantics run one request per process invocation and hold no state
    between runs, so this server supplies what they lack: it keeps the HTTP socket open,
    persists state to disk, and decodes the Stellar XDR envelope (:class:`TransactionEncoder`)
    that K cannot parse. It then runs the request envelope through the semantics
    (:class:`NodeInterpreter`). All RPC dispatch, receipt bookkeeping, ledger accounting,
    and response formatting are performed in K (``node.md``). All artifacts — input and
    output — live in ``io_dir``:

      - ``state.kore``            — the KORE world-state configuration (accounts, contracts, wasm)
      - ``metadata.json``         — ``{"latest_ledger": N}``
      - ``receipts/receipt_<hash>.json`` — one stored receipt per transaction
      - ``traces/trace_<hash>.jsonl``    — one execution trace per transaction
      - ``requests/request_<n>.json``    — an archive of each incoming JSON-RPC request

    Splitting receipts, traces, and requests into per-item files keeps any single file from
    growing without bound as the chain advances.
    """

    interpreter: NodeInterpreter
    encoder: TransactionEncoder
    io_dir: Path
    state_file: Path

    def __init__(
        self,
        host: str = 'localhost',
        port: int = 8000,
        io_dir: Path | None = None,
        network_passphrase: str = Network.TESTNET_NETWORK_PASSPHRASE,
    ) -> None:
        self.host = host
        self._port = port
        self.interpreter = NodeInterpreter()
        self.encoder = TransactionEncoder(network_passphrase)
        # With no io-dir given, run against a fresh temporary directory: a throwaway chain
        # that starts empty on every launch and leaves the working directory untouched.
        self.io_dir = (Path(tempfile.mkdtemp(prefix='komet-node-')) if io_dir is None else io_dir).resolve()
        self.io_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = self.io_dir / 'state.kore'
        self._httpd: HTTPServerType | None = None

        self._fresh = not self.state_file.exists()
        if self._fresh:
            self.state_file.write_text(self.interpreter.empty_config())
        metadata_file = self.io_dir / 'metadata.json'
        if self._fresh or not metadata_file.exists():
            metadata_file.write_text(json.dumps({'latest_ledger': 0}))

        # Per-transaction receipts and traces, and per-request archives, each go in their own
        # file under these directories so no single file grows without bound. The K
        # file-system hooks open files with POSIX open(), which does not create parent
        # directories, so the directories must exist before the semantics run.
        self.receipts_dir = self.io_dir / 'receipts'
        self.traces_dir = self.io_dir / 'traces'
        self.requests_dir = self.io_dir / 'requests'
        for directory in (self.receipts_dir, self.traces_dir, self.requests_dir):
            directory.mkdir(exist_ok=True)
        # Continue the request archive numbering past anything a previous run left behind, so
        # resuming an io-dir never overwrites its earlier request files.
        self._request_count = _next_request_index(self.requests_dir)

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

            # The default BaseHTTPRequestHandler logging writes one noisy line per request to
            # stderr. We log requests ourselves (in _handle), so silence the default.
            def log_message(self, *args: Any) -> None:
                pass

        # Intentionally a single-threaded HTTPServer: requests are serialised. The K node
        # communicates through singleton files in the io dir (request.json / response.json /
        # state.kore), so two requests in flight at once would clobber each other. Do not
        # switch to ThreadingHTTPServer without reworking that file protocol.
        self._httpd = HTTPServer((self.host, int(self._port)), Handler)
        self._log_ready()
        self._httpd.serve_forever()

    def _log_ready(self) -> None:
        """Announce, once the socket is bound, where the server listens and how it started."""
        _configure_logging()
        if self._fresh:
            status = 'starting from a fresh state (empty io-dir)'
        else:
            metadata = json.loads((self.io_dir / 'metadata.json').read_text())
            status = f'resuming existing state (latest ledger {metadata.get("latest_ledger", 0)})'
        _log.info('komet-node ready — %s', status)
        _log.info('io-dir: %s', self.io_dir)
        _log.info('listening on http://%s:%d', self.host, self.port())

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

        # Validate the JSON-RPC frame before dispatch (JSON-RPC 2.0):
        #   - wrong/missing protocol version or a non-string method => Invalid Request
        #   - params, if present, must be a structured (object) value => else Invalid params
        if req.get('jsonrpc') != '2.0' or not isinstance(req.get('method'), str):
            return _error_bytes(request_id, -32600, 'Invalid Request')
        params = req.get('params')
        if params is None:
            params = {}
        elif not isinstance(params, dict):
            return _error_bytes(request_id, -32602, 'Invalid params')

        try:
            return self.handle_rpc(req['method'], params, request_id).encode('utf-8')
        except Exception:
            # An unexpected error must never take down the server thread, but it must not
            # vanish silently either — log the traceback before returning Internal error.
            traceback.print_exc()
            return _error_bytes(request_id, -32603, 'Internal error')

    def handle_rpc(self, method: str | None, params: dict[str, Any], request_id: Any = None) -> str:
        """Dispatch a single JSON-RPC call and return the response envelope as a JSON string.

        Usable without the HTTP layer (e.g. from scripts and tests).
        """
        now = str(int(time.time()))
        _log.info('request: %s (id=%r)', method, request_id)
        self._archive_request(method, params, request_id)

        if method in _TX_METHODS:
            transaction = params.get('transaction')
            if not isinstance(transaction, str):
                return _error_str(request_id, -32602, "Invalid params: 'transaction' (XDR string) is required")
            # Undecodable XDR is a JSON-RPC client error (-32602, as in real stellar-rpc);
            # a transaction that decodes but cannot be processed (e.g. an unsupported
            # operation) is instead rejected at admission time with status ERROR below.
            try:
                parsed = TransactionEnvelope.from_xdr(transaction, self.encoder.network_passphrase)
            except Exception:
                traceback.print_exc()
                return _error_str(request_id, -32602, 'Invalid params: could not decode transaction XDR')
            try:
                envelope, program_steps = self.encoder.build_tx_request(method, request_id, transaction, now)
            except Exception:
                # Log the detail, but keep the client-facing response neutral rather than
                # leaking internal exceptions.
                traceback.print_exc()
                return json.dumps(self._error_status_response(request_id, parsed.hash_hex(), now))
            # A hash that already has a receipt is a duplicate submission: the semantics
            # answer DUPLICATE without running the steps (see node.md). Steps injected into
            # the <program> cell (wasm uploads) would execute *before* dispatch, though, so
            # they must not be injected for a duplicate.
            if (self.receipts_dir / f'receipt_{envelope["txHash"]}.json').exists():
                program_steps = None
            response = self.interpreter.run(self.state_file, self.io_dir, envelope, program_steps)
            if response is None:
                return json.dumps(self._failure_response(request_id, envelope, now))
            return response

        if method == 'simulateTransaction':
            return self._handle_simulate(params, request_id, now)

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
        if method in ('getTransaction', 'traceTransaction'):
            tx_hash = params.get('hash')
            if not isinstance(tx_hash, str):
                return _error_str(request_id, -32602, "Invalid params: 'hash' (string) is required")
            if _TX_HASH_RE.fullmatch(tx_hash) is None:
                return _error_str(request_id, -32602, "Invalid params: 'hash' must be a 64-character hex string")
            return {**base, 'hash': tx_hash}
        return None

    # ------------------------------------------------------------------
    # simulateTransaction
    # ------------------------------------------------------------------

    def _handle_simulate(self, params: dict[str, Any], request_id: Any, now: str) -> str:
        """Dispatch a ``simulateTransaction`` call: a dry run that commits nothing.

        The K semantics execute the invocation against the current state and report the
        return value (see the simulateTransaction section of ``node.md``); the run is not
        persisted (``commit=False``), no receipt or trace is written, and the ledger does
        not advance. Simulation failures are reported in the result body (``{error,
        latestLedger}``), matching real stellar-rpc; only an undecodable ``transaction``
        param is a JSON-RPC error.
        """
        transaction = params.get('transaction')
        if not isinstance(transaction, str):
            return _error_str(request_id, -32602, "Invalid params: 'transaction' (XDR string) is required")
        try:
            envelope = self.encoder.build_simulate_request(request_id, transaction, now)
        except SimulationRejected as err:
            return json.dumps(self._simulate_error_response(request_id, str(err)))
        except Exception:
            traceback.print_exc()
            return _error_str(request_id, -32602, 'Invalid params: could not process transaction')

        response = self.interpreter.run(self.state_file, self.io_dir, envelope, None, commit=False)
        if response is None:
            # The run got stuck: the invocation could not be executed at all (e.g. the
            # target contract does not exist). Report it as a simulation error.
            return json.dumps(self._simulate_error_response(request_id, 'transaction simulation failed'))
        return json.dumps(self._simulate_response(request_id, json.loads(response)['result']))

    def _simulate_response(self, rpc_id: Any, k_result: dict[str, Any]) -> dict[str, Any]:
        """Map the internal K simulation result to the spec response shape.

        This is the one read path where Python builds response content: the spec requires
        the return value as base64 SCVal XDR and ``transactionData`` as base64
        ``SorobanTransactionData``, and K can construct neither (no XDR encoder, no base64
        hook). ``latestLedger`` and the error/success split come from K unchanged.
        """
        if 'error' in k_result:
            result: dict[str, Any] = {'error': k_result['error'], 'latestLedger': k_result['latestLedger']}
        else:
            result = {
                'latestLedger': k_result['latestLedger'],
                'minResourceFee': _MIN_RESOURCE_FEE,
                'results': [{'xdr': json_to_scval(k_result['returnValue']).to_xdr(), 'auth': []}],
                'transactionData': _TRANSACTION_DATA,
            }
        return {'jsonrpc': '2.0', 'id': rpc_id, 'result': result}

    def _simulate_error_response(self, rpc_id: Any, message: str) -> dict[str, Any]:
        """Build the ``{error, latestLedger}`` result for a simulation that never ran in K."""
        metadata = json.loads((self.io_dir / 'metadata.json').read_text())
        result = {'error': message, 'latestLedger': metadata.get('latest_ledger', 0)}
        return {'jsonrpc': '2.0', 'id': rpc_id, 'result': result}

    def _archive_request(self, method: str | None, params: dict[str, Any], request_id: Any) -> None:
        """Write each incoming JSON-RPC call to its own ``requests/request_<n>.json`` file.

        This is an audit trail for the developer; the canonical ``request.json`` the semantics
        consume is written separately by :class:`NodeInterpreter`. The server is single-threaded
        (requests are serialised), so the counter needs no locking.
        """
        archive = {'jsonrpc': '2.0', 'id': request_id, 'method': method, 'params': params}
        (self.requests_dir / f'request_{self._request_count}.json').write_text(json.dumps(archive))
        self._request_count += 1

    def _error_status_response(self, rpc_id: Any, tx_hash: str, now: str) -> dict[str, Any]:
        """Synthesise the sendTransaction response for a transaction rejected at admission.

        Mirrors real stellar-rpc: a transaction that decodes as XDR but cannot be processed
        gets status ``ERROR`` with a ``txMALFORMED`` ``errorResultXdr``. It never reaches
        the ledger, so no receipt is stored (``getTransaction`` stays ``NOT_FOUND``) and the
        ledger does not advance.
        """
        metadata = json.loads((self.io_dir / 'metadata.json').read_text())
        result = {
            'hash': tx_hash,
            'status': 'ERROR',
            'errorResultXdr': malformed_tx_result_xdr(),
            'latestLedger': metadata.get('latest_ledger', 0),
            'latestLedgerCloseTime': now,
        }
        return {'jsonrpc': '2.0', 'id': rpc_id, 'result': result}

    def _failure_response(self, rpc_id: Any, envelope: dict[str, Any], now: str) -> dict[str, Any]:
        """Synthesise the sendTransaction response for a transaction that got stuck (failed).

        The K run does not produce a ``response.json`` for a failed transaction and the
        world state is left unchanged. We record a FAILED receipt so a later getTransaction
        finds it, without bumping the ledger.
        """
        metadata = json.loads((self.io_dir / 'metadata.json').read_text())
        ledger = metadata.get('latest_ledger', 0)
        tx_hash = envelope['txHash']

        # This FAILED receipt mirrors the SUCCESS receipt the semantics build in
        # `#txReceipt` (kdist/node.md): keep the field set in sync with that rule (ledger a
        # JSON number, createdAt an int64-as-string). Like the success path, the receipt
        # carries no trace — any trace lives in its own file.
        receipt = {
            'status': 'FAILED',
            'applicationOrder': 1,
            'feeBump': False,
            'envelopeXdr': envelope['envelopeXdr'],
            'resultXdr': '',
            'resultMetaXdr': '',
            'ledger': ledger,
            'createdAt': now,
        }
        (self.receipts_dir / f'receipt_{tx_hash}.json').write_text(json.dumps(receipt))

        result = {
            'hash': tx_hash,
            'status': 'PENDING',
            'latestLedger': ledger,
            'latestLedgerCloseTime': now,
        }
        return {'jsonrpc': '2.0', 'id': rpc_id, 'result': result}


def _next_request_index(requests_dir: Path) -> int:
    """Return the next free index for ``requests/request_<n>.json``.

    One past the highest index already present, so resuming an io-dir continues the archive
    rather than overwriting it; 0 when the directory holds no request files yet.
    """
    highest = -1
    for path in requests_dir.glob('request_*.json'):
        try:
            highest = max(highest, int(path.stem.removeprefix('request_')))
        except ValueError:
            continue  # ignore files that don't match the request_<int> pattern
    return highest + 1


def _configure_logging() -> None:
    """Attach a stderr handler to the komet_node logger once, if nothing else has.

    Logs go to stderr so they never interleave with anything a client reads from stdout.
    Idempotent: calling it more than once (e.g. server restarted in-process) is a no-op.
    """
    if _log.handlers:
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    _log.addHandler(handler)
    _log.setLevel(logging.INFO)


def _error_str(rpc_id: Any, code: int, message: str) -> str:
    return json.dumps({'jsonrpc': '2.0', 'id': rpc_id, 'error': {'code': code, 'message': message}})


def _error_bytes(rpc_id: Any, code: int, message: str) -> bytes:
    return _error_str(rpc_id, code, message).encode('utf-8')

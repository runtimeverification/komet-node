from __future__ import annotations

import importlib.metadata
import json
import logging
import re
import sys
import tempfile
import time
import traceback
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, TypedDict

from stellar_sdk import Network, StrKey, TransactionEnvelope, xdr

from komet_node.errors import RpcError
from komet_node.interpreter import NodeInterpreter
from komet_node.ledger import build_ledger_artifacts
from komet_node.ledger_entries import InvalidParamsError, format_ledger_entries_response, ledger_key_descriptors
from komet_node.result_xdr import transaction_meta_xdr, transaction_result_xdr
from komet_node.scval import scval_from_json
from komet_node.store import ChainStore, EventRecord, LedgerRecord
from komet_node.transaction import SimulationRejected, TransactionEncoder, malformed_tx_result_xdr

if TYPE_CHECKING:
    from collections.abc import Mapping
    from http.server import HTTPServer as HTTPServerType

_PROTOCOL_VERSION: Final = 22

# Transaction hashes are 64 lowercase hex characters (the spec's Hash schema:
# ^[a-f\d]{64}$). Anything else is rejected as Invalid params before reaching K.
_TX_HASH_RE: Final = re.compile(r'[0-9a-f]{64}')

# Pagination bounds for the history methods (getTransactions/getLedgers), per the spec:
# limit ranges from 1 to 200 and defaults to 50.
_MAX_PAGE_LIMIT: Final = 200
_DEFAULT_PAGE_LIMIT: Final = 50
# getTransactions cursors are TOID-style: (ledger sequence << 32) | (application order << 12).
_TOID_LEDGER_SHIFT: Final = 32

# getEvents cursors and event ids are TOID-style: a 19-digit zero-padded TOID, a hyphen,
# and a 10-digit zero-padded event index (SEP-35).
_EVENT_CURSOR_RE: Final = re.compile(r'\d{19}-\d{10}')

# getEvents limits from the spec: at most 5 filters, 5 topic filters each, 1-4 segment
# matchers per topic filter (a trailing '**' does not count), and a page limit of 1-10000
# (default 100).
_MAX_EVENT_FILTERS: Final = 5
_MAX_TOPIC_FILTERS: Final = 5
_MAX_TOPIC_SEGMENTS: Final = 4
_DEFAULT_EVENTS_LIMIT: Final = 100
_MAX_EVENTS_LIMIT: Final = 10000

# getVersionInfo fields. komet-node is a Python package, not a Go binary, so there is no
# commit hash or build timestamp baked in at compile time; report all-zeros / epoch
# placeholders with the correct spec types instead. The "Captive Core" of komet-node is the
# komet package (the K semantics of Soroban that execute the transactions).
_VERSION: Final = importlib.metadata.version('komet-node')
_COMMIT_HASH: Final = '0' * 40
_BUILD_TIMESTAMP: Final = '1970-01-01T00:00:00'
_CAPTIVE_CORE_VERSION: Final = f'komet {importlib.metadata.version("komet")} (K semantics of Soroban)'

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

# Methods whose spec accepts an optional `xdrFormat` param (protocols/rpc:
# GetTransactionRequest.Format, SendTransactionRequest.Format). komet-node supports only
# the default 'base64' format; see _require_supported_xdr_format.
_XDR_FORMAT_METHODS: Final = ('getTransaction', 'sendTransaction')

_log = logging.getLogger('komet_node')

# The internal simulateTransaction result K emits (node.md): always ``latestLedger``, then
# either ``error`` (a failed simulation) or ``returnValue`` (a JSON SCVal for a successful
# one). ``total=False`` — the two outcomes are mutually exclusive.
SimulateResult = TypedDict(
    'SimulateResult',
    {'latestLedger': int, 'error': str, 'returnValue': Any},
    total=False,
)


class StellarRpcServer:
    """
    Long-running HTTP/JSON-RPC server that wraps the one-shot K node semantics.

    The compiled semantics run one request per process invocation and hold no state
    between runs, so this server supplies what they lack: it keeps the HTTP socket open,
    persists state to disk, and decodes the Stellar XDR envelope (:class:`TransactionEncoder`)
    that K cannot parse. It then runs the request envelope through the semantics
    (:class:`NodeInterpreter`). All RPC dispatch, receipt bookkeeping, ledger accounting,
    and response formatting are performed in K (``node.md``). The on-disk artifacts — input
    and output — belong to the :class:`~komet_node.store.ChainStore`, which owns the io-dir
    layout; this server holds one and asks it for receipts, ledgers, events, and the ledger
    counter rather than touching paths itself.
    """

    interpreter: NodeInterpreter
    encoder: TransactionEncoder
    store: ChainStore
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
        self._httpd: HTTPServerType | None = None

        # With no io-dir given, run against a fresh temporary directory: a throwaway chain
        # that starts empty on every launch and leaves the working directory untouched.
        self.store = ChainStore(Path(tempfile.mkdtemp(prefix='komet-node-')) if io_dir is None else io_dir)
        self._fresh = self.store.initialize(self.interpreter.empty_config)
        # state_file and io_dir are the paths the interpreter subprocess resolves against; kept
        # as attributes so callers (and tests) can reach the io-dir root directly.
        self.io_dir = self.store.root
        self.state_file = self.store.state_file

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
            status = f'resuming existing state (latest ledger {self.store.latest_ledger()})'
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
        """Parse a raw JSON-RPC body and return the response bytes (the HTTP entry point).

        An array body is a JSON-RPC 2.0 batch; anything else is a single call. The result
        may be empty (no response body) when every request was a notification.
        """
        try:
            req = json.loads(body.decode('utf-8'))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return _error_bytes(None, -32700, 'Parse error')
        if isinstance(req, list):
            return self._handle_batch(req)
        response = self._handle_single(req)
        return b'' if response is None else response.encode('utf-8')

    def _handle_batch(self, batch: list[Any]) -> bytes:
        """Answer a JSON-RPC 2.0 batch call: one response per element, in an array.

        Per section 6 of the spec: an empty array is itself a single Invalid Request error;
        invalid elements each get their own error response; notifications get no response
        entry, and a batch of only notifications gets no response body at all. Elements run
        sequentially (the server is single-threaded by design, see serve()).
        """
        if not batch:
            return _error_bytes(None, -32600, 'Invalid Request')
        responses = [response for element in batch if (response := self._handle_single(element)) is not None]
        if not responses:
            return b''
        return ('[' + ','.join(responses) + ']').encode('utf-8')

    def _handle_single(self, req: Any) -> str | None:
        """Validate one JSON-RPC request frame and dispatch it.

        Returns the response as a JSON string, or ``None`` for a notification — a valid
        request frame without an ``id`` member, which per JSON-RPC 2.0 is executed but
        never answered, not even with an error.
        """
        if not isinstance(req, dict):
            return _error_str(None, -32600, 'Invalid Request')
        request_id = req.get('id')

        # Validate the JSON-RPC frame before dispatch (JSON-RPC 2.0):
        #   - wrong/missing protocol version or a non-string method => Invalid Request
        #   - params, if present, must be a structured (object) value => else Invalid params
        if req.get('jsonrpc') != '2.0' or not isinstance(req.get('method'), str):
            return _error_str(request_id, -32600, 'Invalid Request')
        params = req.get('params')
        if params is None:
            params = {}

        if not isinstance(params, dict):
            response = _error_str(request_id, -32602, 'Invalid params')
        else:
            try:
                response = self.handle_rpc(req['method'], params, request_id)
            except Exception:
                # An unexpected error must never take down the server thread, but it must
                # not vanish silently either — log the traceback, return Internal error.
                traceback.print_exc()
                response = _error_str(request_id, -32603, 'Internal error')
        return None if 'id' not in req else response

    def handle_rpc(self, method: str | None, params: dict[str, Any], request_id: Any = None) -> str:
        """Dispatch a single JSON-RPC call and return the response envelope as a JSON string.

        Usable without the HTTP layer (e.g. from scripts and tests).
        """
        now = str(int(time.time()))
        _log.info('request: %s (id=%r)', method, request_id)
        self.store.archive_request(method, params, request_id)
        try:
            return self._dispatch(method, params, request_id, now)
        except RpcError as err:
            return _error_str(request_id, err.code, err.message)

    def _dispatch(self, method: str | None, params: dict[str, Any], request_id: Any, now: str) -> str:
        """Route a validated JSON-RPC call to its handler, returning the response JSON string.

        Client-facing problems (bad params, unknown method, a stuck read-only run) are
        raised as :class:`RpcError`; :meth:`handle_rpc` turns them into the error envelope.
        """
        # Reject an unsupported xdrFormat up front, before the request does anything —
        # in particular before a sendTransaction executes and commits state.
        if method in _XDR_FORMAT_METHODS:
            _require_supported_xdr_format(params)

        if method in _TX_METHODS:
            return self._handle_send_transaction(method, params, request_id, now)
        if method == 'simulateTransaction':
            return self._handle_simulate(params, request_id, now)
        if method == 'getLedgerEntries':
            return self._get_ledger_entries(params, request_id, now)

        envelope = self._read_only_envelope(method, params, request_id, now)
        response = self.interpreter.run(self.state_file, self.io_dir, envelope, None)
        if response is None:
            raise RpcError.internal()
        return response

    def _handle_send_transaction(self, method: str, params: dict[str, Any], request_id: Any, now: str) -> str:
        """Execute and commit a ``sendTransaction`` call, returning the response JSON string.

        Decodes the transaction XDR, runs it through the semantics, and — on the success
        path — finalises the receipt's result XDR, materialises any staged events, persists
        uploaded wasm bytes, and records the closed ledger. Undecodable XDR is a JSON-RPC
        client error (-32602); a transaction that decodes but cannot be admitted gets an
        ERROR status response; one that gets stuck mid-run gets a FAILED receipt.
        """
        transaction = params.get('transaction')
        if not isinstance(transaction, str):
            raise RpcError.invalid_params("'transaction' (XDR string) is required")
        try:
            parsed = TransactionEnvelope.from_xdr(transaction, self.encoder.network_passphrase)
        except Exception:
            traceback.print_exc()
            raise RpcError.invalid_params('could not decode transaction XDR') from None
        try:
            envelope, program_steps, uploaded_wasms = self.encoder.build_tx_request(
                method, request_id, transaction, now
            )
        except Exception:
            # Log the detail, but keep the client-facing response neutral rather than
            # leaking internal exceptions.
            traceback.print_exc()
            return self._error_status_response(request_id, parsed.hash_hex(), now)
        # A hash that already has a receipt is a duplicate submission: the semantics
        # answer DUPLICATE without running the steps (see node.md). Steps injected into
        # the <program> cell (wasm uploads) would execute *before* dispatch, though, so
        # they must not be injected for a duplicate.
        is_duplicate = self.store.has_receipt(envelope['txHash'])
        if is_duplicate:
            program_steps = None
        # Stale staged events (e.g. from a previously failed transaction) must not leak
        # into this transaction's event records.
        self.store.clear_staged_events()
        response = self.interpreter.run(self.state_file, self.io_dir, envelope, program_steps)
        if response is None:
            self.store.clear_staged_events()
            return self._failure_response(request_id, envelope, now)
        # Rewrite the freshly written SUCCESS receipt's internal `returnValue` into real
        # resultXdr/resultMetaXdr. Skip it for a duplicate: that receipt was already
        # finalised on first submission and no longer carries `returnValue`, so
        # reprocessing it would overwrite the stored result with an empty one.
        if not is_duplicate:
            self._attach_result_xdr(envelope['txHash'])
        # Turn any events the run staged into events/events_<ledger>.json. A duplicate
        # runs no steps and stages nothing, so this is a no-op for it.
        self._finalize_events(envelope['txHash'], now)
        # Keep the raw bytes of successfully uploaded wasm modules: the K configuration
        # stores modules parsed, and getLedgerEntries CONTRACT_CODE entries must return
        # the original bytes.
        for wasm_hash, wasm in uploaded_wasms.items():
            self.store.write_wasm(wasm_hash, wasm)
        self._record_closed_ledger(envelope, now)
        return response

    def _get_ledger_entries(self, params: dict[str, Any], request_id: Any, now: str) -> str:
        """Handle getLedgerEntries: decode the LedgerKey XDR, run the K lookup, re-encode.

        Unlike the other read-only methods, the semantics' response is an intermediate
        shape here: K performs the state lookups and emits per-kind JSON payloads, and
        this side re-encodes them as base64 ``LedgerEntryData`` XDR (which K cannot
        produce) before returning the final response. See ``ledger_entries.py``.
        """
        try:
            descriptors = ledger_key_descriptors(params)
        except InvalidParamsError as err:
            raise RpcError.invalid_params(str(err)) from err
        envelope = {'method': 'getLedgerEntries', 'id': request_id, 'now': now, 'keys': descriptors}
        response = self.interpreter.run(self.state_file, self.io_dir, envelope, None)
        if response is None:
            raise RpcError.internal()
        return format_ledger_entries_response(response, self.store.wasms_dir)

    def _read_only_envelope(
        self, method: str | None, params: dict[str, Any], request_id: Any, now: str
    ) -> dict[str, Any]:
        """Build the request envelope for a read-only method.

        Returns the envelope dict, or raises :class:`RpcError` — ``method_not_found`` for an
        unknown method, ``invalid_params`` for a recognised method with bad params.
        """
        base = {'method': method, 'id': request_id, 'now': now}
        if method == 'getHealth':
            return base
        if method == 'getNetwork':
            return {**base, 'passphrase': self.encoder.network_passphrase, 'protocolVersion': _PROTOCOL_VERSION}
        if method == 'getLatestLedger':
            return {**base, 'protocolVersion': _PROTOCOL_VERSION}
        if method == 'getVersionInfo':
            # protocolVersion is a JSON number here (a Go uint32 in stellar-rpc), unlike the
            # string the older methods still emit.
            return {
                **base,
                'version': _VERSION,
                'commitHash': _COMMIT_HASH,
                'buildTimestamp': _BUILD_TIMESTAMP,
                'captiveCoreVersion': _CAPTIVE_CORE_VERSION,
                'protocolVersion': int(_PROTOCOL_VERSION),
            }
        if method == 'getFeeStats':
            return base
        if method in ('getTransaction', 'traceTransaction'):
            tx_hash = params.get('hash')
            if not isinstance(tx_hash, str):
                raise RpcError.invalid_params("'hash' (string) is required")
            if _TX_HASH_RE.fullmatch(tx_hash) is None:
                raise RpcError.invalid_params("'hash' must be a 64-character hex string")
            return {**base, 'hash': tx_hash}
        if method in ('getTransactions', 'getLedgers'):
            return self._history_envelope(method, params, base)
        if method == 'getEvents':
            return self._get_events_envelope(base, params)
        raise RpcError.method_not_found()

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
            raise RpcError.invalid_params("'transaction' (XDR string) is required")
        try:
            envelope = self.encoder.build_simulate_request(request_id, transaction, now)
        except SimulationRejected as err:
            return self._simulate_error_response(request_id, str(err))
        except Exception:
            traceback.print_exc()
            raise RpcError.invalid_params('could not process transaction') from None

        response = self.interpreter.run(self.state_file, self.io_dir, envelope, None, commit=False)
        if response is None:
            # The run got stuck: the invocation could not be executed at all (e.g. the
            # target contract does not exist). Report it as a simulation error.
            return self._simulate_error_response(request_id, 'transaction simulation failed')
        return self._simulate_response(request_id, json.loads(response)['result'])

    def _simulate_response(self, rpc_id: Any, k_result: SimulateResult) -> str:
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
                'results': [{'xdr': scval_from_json(k_result['returnValue']).to_xdr(), 'auth': []}],
                'transactionData': _TRANSACTION_DATA,
            }
        return _result_str(rpc_id, result)

    def _simulate_error_response(self, rpc_id: Any, message: str) -> str:
        """Build the ``{error, latestLedger}`` result for a simulation that never ran in K."""
        return _result_str(rpc_id, {'error': message, 'latestLedger': self.store.latest_ledger()})

    def _history_envelope(self, method: str, params: dict[str, Any], base: dict[str, Any]) -> dict[str, Any]:
        """Validate getTransactions/getLedgers params and build the request envelope.

        Parameter validation lives here rather than in K because it needs the JSON-RPC
        error path and the latest-ledger bound, both of which the server already owns for
        the other methods. The envelope carries the resolved first ledger sequence to
        serve (``startSeq``) and the page ``limit``; the semantics collect the records
        and format the response. Bad params raise :class:`RpcError`.
        """
        xdr_format_error = _unsupported_xdr_format(params)
        if xdr_format_error is not None:
            raise RpcError.invalid_params(xdr_format_error)

        pagination = params.get('pagination') or {}
        if not isinstance(pagination, dict):
            raise RpcError.invalid_params("'pagination' must be an object")
        limit = pagination.get('limit', _DEFAULT_PAGE_LIMIT)
        if not _is_int(limit) or not 1 <= limit <= _MAX_PAGE_LIMIT:
            raise RpcError.invalid_params(f"'limit' must be an integer between 1 and {_MAX_PAGE_LIMIT}")

        cursor = pagination.get('cursor')
        start_ledger = params.get('startLedger')
        if cursor is not None:
            if start_ledger is not None:
                raise RpcError.invalid_params("'startLedger' and 'cursor' are mutually exclusive")
            if not isinstance(cursor, str) or not cursor.isdigit():
                raise RpcError.invalid_params("'cursor' must be a cursor string from a previous response")
            # The cursor names the last record already returned: a TOID for transactions
            # (ledger << 32 | order << 12; one transaction per ledger here), the plain
            # ledger sequence for ledgers. Either way the next page starts one ledger on.
            position = int(cursor)
            if method == 'getTransactions':
                position >>= _TOID_LEDGER_SHIFT
            start_seq = position + 1
        else:
            if start_ledger is None:
                start_ledger = 0  # like real stellar-rpc: unset means "from the oldest ledger"
            if not _is_int(start_ledger):
                raise RpcError.invalid_params("'startLedger' must be a number")
            latest_ledger = self.store.latest_ledger()
            if start_ledger != 0 and not 0 <= start_ledger <= latest_ledger:
                raise RpcError.invalid_params(
                    f"'startLedger' must be between the oldest ledger 0 and the latest ledger {latest_ledger}"
                )
            start_seq = start_ledger
        return {**base, 'startSeq': start_seq, 'limit': limit}

    def _attach_result_xdr(self, tx_hash: str) -> None:
        """Rewrite a fresh SUCCESS receipt's internal ``returnValue`` into real result XDR.

        The K semantics record the transaction's contract-call return value in the receipt
        as a JSON-encoded SCVal (``null`` when the transaction made no call), because K
        cannot construct XDR. Replace it with the spec-mandated ``resultXdr``/
        ``resultMetaXdr`` (base64 TransactionResult / TransactionMeta), so getTransaction
        can serve the stored receipt as-is.
        """
        receipt = self.store.read_receipt(tx_hash)
        return_value_json = receipt.pop('returnValue', None)
        try:
            return_value = scval_from_json(return_value_json) if return_value_json is not None else None
            tx_envelope = TransactionEnvelope.from_xdr(receipt['envelopeXdr'], self.encoder.network_passphrase)
            receipt['resultXdr'] = transaction_result_xdr(tx_envelope, return_value, success=True)
            receipt['resultMetaXdr'] = transaction_meta_xdr(tx_envelope, return_value)
        finally:
            # Persist the receipt even when the rewrite fails (e.g. a return value this
            # encoder cannot decode): the transaction has already committed, and the stored
            # receipt must never keep the K-internal `returnValue` field. A receipt with
            # `resultXdr` omitted is spec-legal; a leaked internal field is not.
            self.store.write_receipt(tx_hash, receipt)

    def _record_closed_ledger(self, envelope: Mapping[str, Any], now: str) -> None:
        """Materialise ``ledgers/ledger_<seq>.json`` for the ledger a transaction just closed.

        The K semantics own the ledger counter and receipts; this per-ledger record
        additionally carries the ledger-header artifacts (``hash``, ``headerXdr``,
        ``metadataXdr``) that only Python can construct (XDR), and is what the semantics
        read to serve getTransactions and getLedgers. Written only on the success path —
        a failed transaction closes no ledger.
        """
        sequence = self.store.latest_ledger()  # the semantics bumped it just before responding
        previous = self.store.read_ledger(sequence - 1)
        previous_hash = bytes.fromhex(previous['hash']) if previous is not None else b'\x00' * 32  # genesis hash
        ledger_hash, header_xdr, metadata_xdr = build_ledger_artifacts(
            sequence, int(now), previous_hash, envelope['envelopeXdr']
        )
        record: LedgerRecord = {
            'sequence': sequence,
            'txHash': envelope['txHash'],
            'closedAt': int(now),
            'hash': ledger_hash,
            'headerXdr': header_xdr,
            'metadataXdr': metadata_xdr,
        }
        self.store.write_ledger(record)

    def _get_events_envelope(self, base: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
        """Validate the getEvents params and build the request envelope.

        Everything structural is checked here so the K rules (node.md) only handle
        well-typed envelopes; the one state-dependent check (the window against the chain
        tip) lives in K. Bad params raise :class:`RpcError`.
        """
        xdr_format_error = _unsupported_xdr_format(params)
        if xdr_format_error is not None:
            raise RpcError.invalid_params(xdr_format_error)

        pagination = params.get('pagination') or {}
        if not isinstance(pagination, dict):
            raise RpcError.invalid_params("'pagination' must be an object")
        cursor = pagination.get('cursor')
        if cursor is not None and not (isinstance(cursor, str) and _EVENT_CURSOR_RE.fullmatch(cursor)):
            raise RpcError.invalid_params("'cursor' is malformed")
        limit = pagination.get('limit')
        if limit is None:
            limit = _DEFAULT_EVENTS_LIMIT
        if isinstance(limit, float) and limit.is_integer():
            limit = int(limit)
        if not _is_int(limit) or not 1 <= limit <= _MAX_EVENTS_LIMIT:
            raise RpcError.invalid_params(f'limit must be between 1 and {_MAX_EVENTS_LIMIT}')

        start_ledger = params.get('startLedger')
        end_ledger = params.get('endLedger')
        if cursor is not None and (start_ledger is not None or end_ledger is not None):
            raise RpcError.invalid_request('startLedger and endLedger must be omitted when a cursor is set')
        if cursor is None and start_ledger is None:
            raise RpcError.invalid_request('startLedger or a pagination cursor is required')
        for name, value in (('startLedger', start_ledger), ('endLedger', end_ledger)):
            if value is not None and not _is_int(value):
                raise RpcError.invalid_params(f'{name} must be a ledger sequence number')
        if start_ledger is not None and start_ledger < 1:
            raise RpcError.invalid_request('startLedger must be positive')

        filters = params.get('filters') or []
        error = _validate_event_filters(filters)
        if error is not None:
            raise RpcError.invalid_params(error)

        return {
            **base,
            'startLedger': start_ledger,
            'endLedger': end_ledger,
            'filters': filters,
            'cursor': cursor,
            'limit': limit,
        }

    def _finalize_events(self, tx_hash: str, now: str) -> None:
        """Convert the events staged by the K run into ``events/events_<ledger>.json``.

        The semantics stage one JSON record per contract event (K-side ScVal JSON, hex
        contract id — see "Event capture" in node.md); this turns each into the spec's
        Event shape — base64 SCVal XDR topics/value, strkey contract id, TOID-style id —
        which K cannot build itself. Runs after every successful sendTransaction; with
        nothing staged it does nothing.
        """
        lines = self.store.read_staged_event_lines()
        self.store.clear_staged_events()
        if not lines:
            return
        ledger = self.store.latest_ledger()
        closed_at = datetime.fromtimestamp(int(now), tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        # One transaction per ledger: TOID with application order 1, operation index 0.
        toid = (ledger << 32) | (1 << 12)
        events: list[EventRecord] = []
        for index, line in enumerate(lines):
            staged = json.loads(line)
            try:
                topic = [scval_from_json(t).to_xdr() for t in staged['topics']]
                value = scval_from_json(staged['data']).to_xdr()
            except NotImplementedError as err:
                _log.warning('dropping contract event with unsupported value: %s', err)
                continue
            events.append(
                {
                    'type': 'contract',
                    'ledger': ledger,
                    'ledgerClosedAt': closed_at,
                    'contractId': StrKey.encode_contract(bytes.fromhex(staged['contractId'])),
                    'id': f'{toid:019d}-{index:010d}',
                    'inSuccessfulContractCall': True,
                    'txHash': tx_hash,
                    'topic': topic,
                    'value': value,
                }
            )
        if events:
            self.store.write_events(ledger, events)

    def _error_status_response(self, rpc_id: Any, tx_hash: str, now: str) -> str:
        """Synthesise the sendTransaction response for a transaction rejected at admission.

        Mirrors real stellar-rpc: a transaction that decodes as XDR but cannot be processed
        gets status ``ERROR`` with a ``txMALFORMED`` ``errorResultXdr``. It never reaches
        the ledger, so no receipt is stored (``getTransaction`` stays ``NOT_FOUND``) and the
        ledger does not advance.
        """
        return _result_str(
            rpc_id,
            {
                'hash': tx_hash,
                'status': 'ERROR',
                'errorResultXdr': malformed_tx_result_xdr(),
                'latestLedger': self.store.latest_ledger(),
                'latestLedgerCloseTime': now,
            },
        )

    def _failure_response(self, rpc_id: Any, envelope: Mapping[str, Any], now: str) -> str:
        """Synthesise the sendTransaction response for a transaction that got stuck (failed).

        The K run does not produce a ``response.json`` for a failed transaction and the
        world state is left unchanged. We record a FAILED receipt so a later getTransaction
        finds it, without bumping the ledger.
        """
        ledger = self.store.latest_ledger()
        tx_hash = envelope['txHash']
        tx_envelope = TransactionEnvelope.from_xdr(envelope['envelopeXdr'], self.encoder.network_passphrase)

        # This FAILED receipt mirrors the SUCCESS receipt the semantics build in
        # `#txReceipt` (kdist/node.md) after `_attach_result_xdr` finalises it: keep the
        # field set in sync (ledger a JSON number, createdAt an int64-as-string). A failed
        # transaction never commits, so the chain does not advance: `ledger` pins the latest
        # ledger at failure time. `resultXdr` carries a real `txFAILED` result;
        # `resultMetaXdr` is omitted — the spec allows that, and no meta exists for a
        # rolled-back run. Like the success path, the receipt carries no trace — any trace
        # lives in its own file.
        receipt = {
            'status': 'FAILED',
            'applicationOrder': 1,
            'feeBump': False,
            'envelopeXdr': envelope['envelopeXdr'],
            'resultXdr': transaction_result_xdr(tx_envelope, None, success=False),
            'ledger': ledger,
            'createdAt': now,
        }
        self.store.write_receipt(tx_hash, receipt)

        return _result_str(
            rpc_id,
            {
                'hash': tx_hash,
                'status': 'PENDING',
                'latestLedger': ledger,
                'latestLedgerCloseTime': now,
            },
        )


def _is_int(value: Any) -> bool:
    """True for actual JSON numbers only — bool is an int subclass in Python, so exclude it."""
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_event_filters(filters: Any) -> str | None:
    """Check a getEvents ``filters`` param against the EventFilters schema.

    Returns a human-readable problem description, or ``None`` if the filters are valid.
    """
    if not isinstance(filters, list):
        return "'filters' must be an array"
    if len(filters) > _MAX_EVENT_FILTERS:
        return f'maximum {_MAX_EVENT_FILTERS} filters per request'
    for event_filter in filters:
        if not isinstance(event_filter, dict):
            return 'each filter must be an object'
        event_type = event_filter.get('type')
        if event_type is not None and event_type not in ('contract', 'system'):
            return "filter 'type' must be 'contract' or 'system'"
        contract_ids = event_filter.get('contractIds')
        if contract_ids is not None:
            if not isinstance(contract_ids, list):
                return "'contractIds' must be an array"
            if not all(isinstance(c, str) and StrKey.is_valid_contract(c) for c in contract_ids):
                return "each entry of 'contractIds' must be a contract address"
        topics = event_filter.get('topics')
        if topics is not None:
            if not isinstance(topics, list) or len(topics) > _MAX_TOPIC_FILTERS:
                return f"'topics' must be an array of at most {_MAX_TOPIC_FILTERS} topic filters"
            for topic_filter in topics:
                if not isinstance(topic_filter, list) or not all(isinstance(s, str) for s in topic_filter):
                    return 'each topic filter must be an array of segment matchers'
                # A trailing '**' matches any remaining segments and does not count
                # towards the 1-4 segment matcher limit.
                segments = topic_filter[:-1] if topic_filter and topic_filter[-1] == '**' else topic_filter
                if not 1 <= len(segments) <= _MAX_TOPIC_SEGMENTS or '**' in segments:
                    return f'each topic filter must hold 1 to {_MAX_TOPIC_SEGMENTS} segment matchers'
    return None


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


def _unsupported_xdr_format(params: dict[str, Any]) -> str | None:
    """Describe why the request's ``xdrFormat`` is unsupported, or ``None`` if it is fine.

    komet-node implements only ``'base64'`` (the spec default); an absent, null, or empty
    value means the default, so it is also fine. The spec's alternative ``'json'`` format is
    not implemented and gets a dedicated message; any other value is reported as unknown.
    Callers turn the returned message into their own Invalid params (-32602) error, matching
    real stellar-rpc's handling of a bad format value.
    """
    xdr_format = params.get('xdrFormat') or 'base64'
    if xdr_format == 'base64':
        return None
    if xdr_format == 'json':
        return "xdrFormat 'json' is not supported, use 'base64'"
    return "unknown xdrFormat, expected 'base64'"


def _require_supported_xdr_format(params: dict[str, Any]) -> None:
    """Raise :class:`RpcError` if the request's ``xdrFormat`` is unsupported.

    A thin wrapper over :func:`_unsupported_xdr_format` for the raise-based dispatch path.
    """
    message = _unsupported_xdr_format(params)
    if message is not None:
        raise RpcError.invalid_params(message)


def _error_str(rpc_id: Any, code: int, message: str) -> str:
    return json.dumps({'jsonrpc': '2.0', 'id': rpc_id, 'error': {'code': code, 'message': message}})


def _result_str(rpc_id: Any, result: dict[str, Any]) -> str:
    return json.dumps({'jsonrpc': '2.0', 'id': rpc_id, 'result': result})


def _error_bytes(rpc_id: Any, code: int, message: str) -> bytes:
    return _error_str(rpc_id, code, message).encode('utf-8')

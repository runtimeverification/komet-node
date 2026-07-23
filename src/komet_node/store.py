"""The on-disk layout of a komet-node io-dir, behind one object.

The compiled K semantics are a one-shot interpreter with no state between runs, so the
chain state lives on disk in the *io-dir*. :class:`ChainStore` owns that directory's
schema: it knows the paths, the file-naming conventions, and the JSON shapes, and it is the
only place that reads or writes them. The server holds a :class:`ChainStore` and asks it for
receipts, ledgers, events, and the ledger counter rather than reaching into paths itself.

Splitting receipts, traces, ledgers, and requests into per-item files keeps any single file
from growing without bound as the chain advances. The layout::

    state.kore                    the KORE world-state configuration
    metadata.json                 {"latest_ledger": N}
    events_staged.jsonl           events of the in-flight transaction (written by K)
    receipts/receipt_<hash>.json  one stored receipt per transaction
    traces/trace_<hash>.jsonl     one execution trace per transaction (written by K)
    ledgers/ledger_<seq>.json     one record per closed ledger
    events/events_<ledger>.json   one finished event array per ledger
    requests/request_<n>.json     an archive of each incoming JSON-RPC request
    wasms/<hash>.wasm             raw bytes of each uploaded wasm module
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, TypedDict

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


# The ``ledgers/ledger_<seq>.json`` record for one closed ledger. Carries the ledger-header
# XDR artifacts (hash/headerXdr/metadataXdr) that only Python can build; the semantics read
# them back to serve getTransactions/getLedgers. The functional TypedDict form is used because
# the keys are the spec's camelCase JSON wire names, not Python identifiers.
LedgerRecord = TypedDict(
    'LedgerRecord',
    {
        'sequence': int,
        'txHash': str,
        'closedAt': int,
        'hash': str,
        'headerXdr': str,
        'metadataXdr': str,
    },
)

# One entry of an ``events/events_<ledger>.json`` array, in the getEvents Event shape.
EventRecord = TypedDict(
    'EventRecord',
    {
        'type': str,
        'ledger': int,
        'ledgerClosedAt': str,
        'contractId': str,
        'id': str,
        'inSuccessfulContractCall': bool,
        'txHash': str,
        'topic': list[str],
        'value': str,
    },
)


# Where the K semantics stage the contract events of the currently executing transaction
# (one JSON record per line, appended by the `contract_event` interception in node.md).
_EVENTS_STAGING = 'events_staged.jsonl'


class ChainStore:
    """Reads and writes the files of a single komet-node io-dir."""

    def __init__(self, io_dir: Path) -> None:
        self.root = io_dir.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.state_file = self.root / 'state.kore'
        self._metadata_file = self.root / 'metadata.json'
        self._staging_file = self.root / _EVENTS_STAGING

        self.receipts_dir = self.root / 'receipts'
        self.traces_dir = self.root / 'traces'
        self.ledgers_dir = self.root / 'ledgers'
        self.requests_dir = self.root / 'requests'
        self.wasms_dir = self.root / 'wasms'
        self.events_dir = self.root / 'events'
        # The K file-system hooks open files with POSIX open(), which does not create parent
        # directories, so the directories must exist before the semantics run.
        for directory in (
            self.receipts_dir,
            self.traces_dir,
            self.ledgers_dir,
            self.requests_dir,
            self.wasms_dir,
            self.events_dir,
        ):
            directory.mkdir(exist_ok=True)
        # Continue the request archive numbering past anything a previous run left behind, so
        # resuming an io-dir never overwrites its earlier request files.
        self._request_count = self._next_request_index()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def initialize(self, empty_config: Callable[[], str]) -> bool:
        """Seed a fresh io-dir; return whether it *was* fresh (had no ``state.kore``).

        ``empty_config`` is the (expensive) initial-configuration builder; it is invoked only
        when the dir is fresh. An existing ``state.kore`` is left untouched so an io-dir can
        be resumed. ``metadata.json`` is created if missing either way.
        """
        fresh = not self.state_file.exists()
        if fresh:
            self.state_file.write_text(empty_config())
        if fresh or not self._metadata_file.exists():
            self._metadata_file.write_text(json.dumps({'latest_ledger': 0}))
        return fresh

    # ------------------------------------------------------------------
    # Ledger counter
    # ------------------------------------------------------------------

    def latest_ledger(self) -> int:
        return int(json.loads(self._metadata_file.read_text()).get('latest_ledger', 0))

    # ------------------------------------------------------------------
    # Receipts
    # ------------------------------------------------------------------

    def has_receipt(self, tx_hash: str) -> bool:
        return self._receipt_file(tx_hash).exists()

    def read_receipt(self, tx_hash: str) -> dict[str, Any]:
        return json.loads(self._receipt_file(tx_hash).read_text())

    def write_receipt(self, tx_hash: str, receipt: dict[str, Any]) -> None:
        self._receipt_file(tx_hash).write_text(json.dumps(receipt))

    def _receipt_file(self, tx_hash: str) -> Path:
        return self.receipts_dir / f'receipt_{tx_hash}.json'

    # ------------------------------------------------------------------
    # Ledgers
    # ------------------------------------------------------------------

    def read_ledger(self, sequence: int) -> LedgerRecord | None:
        ledger_file = self.ledgers_dir / f'ledger_{sequence}.json'
        if not ledger_file.exists():
            return None
        return json.loads(ledger_file.read_text())

    def write_ledger(self, record: LedgerRecord) -> None:
        (self.ledgers_dir / f'ledger_{record["sequence"]}.json').write_text(json.dumps(record))

    # ------------------------------------------------------------------
    # Wasm side store
    # ------------------------------------------------------------------

    def write_wasm(self, wasm_hash: str, wasm: bytes) -> None:
        (self.wasms_dir / f'{wasm_hash}.wasm').write_bytes(wasm)

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    def read_staged_event_lines(self) -> list[str]:
        """The non-blank lines the K run staged for the in-flight transaction (empty if none)."""
        if not self._staging_file.exists():
            return []
        return [line for line in self._staging_file.read_text().splitlines() if line.strip()]

    def clear_staged_events(self) -> None:
        """Discard any staged events, so a later transaction cannot inherit them."""
        self._staging_file.unlink(missing_ok=True)

    def write_events(self, ledger: int, events: list[EventRecord]) -> None:
        (self.events_dir / f'events_{ledger}.json').write_text(json.dumps(events))

    # ------------------------------------------------------------------
    # Request archive
    # ------------------------------------------------------------------

    def archive_request(self, method: str | None, params: dict[str, Any], request_id: Any) -> None:
        """Write an incoming JSON-RPC call to its own ``requests/request_<n>.json`` file.

        An audit trail for the developer; the canonical ``request.json`` the semantics consume
        is written separately by the interpreter. The server is single-threaded (requests are
        serialised), so the counter needs no locking.
        """
        archive = {'jsonrpc': '2.0', 'id': request_id, 'method': method, 'params': params}
        (self.requests_dir / f'request_{self._request_count}.json').write_text(json.dumps(archive))
        self._request_count += 1

    def _next_request_index(self) -> int:
        """One past the highest ``request_<n>.json`` index present; 0 when there are none."""
        highest = -1
        for path in self.requests_dir.glob('request_*.json'):
            try:
                highest = max(highest, int(path.stem.removeprefix('request_')))
            except ValueError:
                continue  # ignore files that don't match the request_<int> pattern
        return highest + 1

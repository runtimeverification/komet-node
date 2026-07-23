"""The abstractions the server depends on: the shared data shapes and the collaborator protocols.

:class:`~komet_node.server.StellarRpcServer` is written against these, not against the
concrete `NodeInterpreter` / `TransactionEncoder` / `ChainStore`, so the concretes can be
injected (and substituted in tests) — the dependency-inversion boundary. The concrete classes
declare that they implement the protocols, and a composition root wires them together (see
``build_server`` in ``__main__.py``).

Two kinds of thing live here so both the protocols and the concretes can share them without
either importing the other:

- the request/response and disk data shapes (``TypedDict``);
- the ``Interpreter`` / ``Encoder`` / ``Store`` protocols.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, TypedDict

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

    from pyk.kast.inner import KInner


# ----------------------------------------------------------------------
# Data shapes (functional TypedDict form: the keys are JSON wire / disk
# names, some camelCase, so they cannot be class attributes).
# ----------------------------------------------------------------------

# The request envelopes consumed by node.md. `steps` is the JSON-encoded operation list; for a
# wasm upload it is empty and the steps ride in the <program> cell instead. Key *order* also
# matters to K's JSON matcher — which a TypedDict cannot express, only the fields and types.
TxRequest = TypedDict(
    'TxRequest',
    {'method': str, 'id': Any, 'now': str, 'txHash': str, 'envelopeXdr': str, 'steps': list},
)
SimulateRequest = TypedDict(
    'SimulateRequest',
    {'method': str, 'id': Any, 'now': str, 'steps': list},
)

# The internal simulateTransaction result K emits: always ``latestLedger``, then either
# ``error`` (a failed simulation) or ``returnValue`` (a JSON SCVal). total=False — mutually
# exclusive outcomes.
SimulateResult = TypedDict(
    'SimulateResult',
    {'latestLedger': int, 'error': str, 'returnValue': Any},
    total=False,
)

# The ``ledgers/ledger_<seq>.json`` record for one closed ledger; carries the header XDR
# artifacts (hash/headerXdr/metadataXdr) that only Python can build.
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


# ----------------------------------------------------------------------
# Collaborator protocols
# ----------------------------------------------------------------------


class Interpreter(Protocol):
    """Runs RPC request envelopes through the K node semantics (see ``NodeInterpreter``)."""

    def empty_config(self) -> str: ...

    def run(
        self,
        state_file: Path,
        io_dir: Path,
        request: Mapping[str, Any],
        program_steps: list[KInner] | None = ...,
        *,
        commit: bool = ...,
    ) -> str | None: ...


class Encoder(Protocol):
    """Decodes Stellar XDR transactions into node request envelopes (see ``TransactionEncoder``)."""

    network_passphrase: str

    def build_tx_request(
        self, method: str, rpc_id: Any, transaction_xdr: str, now: str
    ) -> tuple[TxRequest, list[KInner] | None, dict[str, bytes]]: ...

    def build_simulate_request(self, rpc_id: Any, transaction_xdr: str, now: str) -> SimulateRequest: ...


class Store(Protocol):
    """Reads and writes the files of a komet-node io-dir (see ``ChainStore``)."""

    root: Path
    state_file: Path
    wasms_dir: Path

    def initialize(self, empty_config: Callable[[], str]) -> bool: ...

    def latest_ledger(self) -> int: ...

    def has_receipt(self, tx_hash: str) -> bool: ...

    def read_receipt(self, tx_hash: str) -> dict[str, Any]: ...

    def write_receipt(self, tx_hash: str, receipt: dict[str, Any]) -> None: ...

    def read_ledger(self, sequence: int) -> LedgerRecord | None: ...

    def write_ledger(self, record: LedgerRecord) -> None: ...

    def write_wasm(self, wasm_hash: str, wasm: bytes) -> None: ...

    def read_staged_event_lines(self) -> list[str]: ...

    def clear_staged_events(self) -> None: ...

    def write_events(self, ledger: int, events: list[EventRecord]) -> None: ...

    def archive_request(self, method: str | None, params: dict[str, Any], request_id: Any) -> None: ...

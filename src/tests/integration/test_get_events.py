"""Integration tests for the ``getEvents`` JSON-RPC method.

The expected behavior follows the official stellar-rpc OpenRPC spec (getEvents.json and the
Event / EventFilters / Cursor schemas): a request selects a ledger range with ``startLedger``
(inclusive) / ``endLedger`` (exclusive) or resumes from a pagination ``cursor``, optionally
narrowed by up to five filters (event type, contract ids, topic matchers). The result carries
``events`` (array), ``latestLedger`` (JSON number) and ``cursor`` (string).

The event-emitting contract lives in ``data/wasm/events.wat``: its ``emit`` function publishes
one contract event with topics ``[Symbol("transfer")]`` and data ``U32(42)`` via the
``contract_event`` host function (module "x", name "1").
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from stellar_sdk import StrKey, TransactionBuilder, xdr
from stellar_sdk.xdr.sc_val_type import SCValType

from .conftest import PASSPHRASE, _is_number, _rpc, deploy_contract, fund_account, send_tx

if TYPE_CHECKING:
    from stellar_sdk import Account, Keypair

    from komet_node.server import StellarRpcServer

EVENTS_CONTRACT_WAT = (Path(__file__).parent / 'data' / 'wasm' / 'events.wat').resolve(strict=True)

# Base64 SCVal XDR of the topic and data emitted by events.wat's `emit` function.
TRANSFER_TOPIC_XDR = xdr.SCVal(type=SCValType.SCV_SYMBOL, sym=xdr.SCSymbol(sc_symbol=b'transfer')).to_xdr()
MINT_TOPIC_XDR = xdr.SCVal(type=SCValType.SCV_SYMBOL, sym=xdr.SCSymbol(sc_symbol=b'mint')).to_xdr()
U32_42_XDR = xdr.SCVal(type=SCValType.SCV_U32, u32=xdr.Uint32(42)).to_xdr()

# TOID-style event id: 19-digit zero-padded TOID, hyphen, 10-digit zero-padded event index
# (see the EventId schema / SEP-35).
EVENT_ID_RE = re.compile(r'\d{19}-\d{10}')


# ---------------------------------------------------------------------------
# Helpers (the server fixture, RPC plumbing, and deploy helpers live in conftest.py)
# ---------------------------------------------------------------------------


def _deploy_events_contract(server: StellarRpcServer) -> tuple[Keypair, Account, str]:
    """Create an account, upload events.wat, and deploy it (ledgers 1-3).

    Returns the funding keypair, its (mutated) sequence-tracking account, and the C-strkey
    address of the deployed contract.
    """
    keypair, account = fund_account(server)
    deployed = deploy_contract(server, keypair, account, EVENTS_CONTRACT_WAT)
    return keypair, account, deployed.address


def _emit_event(server: StellarRpcServer, keypair: Keypair, account: Account, contract_address: str) -> str:
    """Invoke the contract's `emit` function; return the transaction hash."""
    tb = TransactionBuilder(account, PASSPHRASE).append_invoke_contract_function_op(contract_address, 'emit', [])
    tx_hash, _ = send_tx(server, keypair, tb)
    return tx_hash


def _assert_request_error(response: dict[str, Any]) -> None:
    """The call must be rejected with a JSON-RPC request/params error.

    The OpenRPC spec documents which requests are invalid but not the numeric code; real
    stellar-rpc uses -32600 (Invalid Request) for getEvents request validation, and -32602
    (Invalid params) is the JSON-RPC code for bad params. Accept either, but nothing else.
    """
    assert 'error' in response, f'expected an error response, got: {response}'
    assert response['error']['code'] in (-32600, -32602), response['error']


# ---------------------------------------------------------------------------
# Request validation
# ---------------------------------------------------------------------------


def test_get_events_requires_start_ledger_or_cursor(server: StellarRpcServer) -> None:
    """Neither startLedger nor a pagination cursor: the request is invalid (per StartLedger)."""
    _assert_request_error(_rpc(server.port(), 'getEvents', {}))


def test_get_events_rejects_start_ledger_beyond_latest(server: StellarRpcServer) -> None:
    """startLedger greater than the latest ledger seen by the node is an error (per StartLedger)."""
    _assert_request_error(_rpc(server.port(), 'getEvents', {'startLedger': 999999}))


def test_get_events_rejects_cursor_combined_with_start_ledger(server: StellarRpcServer) -> None:
    """If a cursor is included, startLedger must be omitted (per StartLedger/EndLedger)."""
    response = _rpc(
        server.port(),
        'getEvents',
        {'startLedger': 1, 'pagination': {'cursor': '0000000004294967296-0000000000'}},
    )
    _assert_request_error(response)


def test_get_events_rejects_more_than_five_filters(server: StellarRpcServer) -> None:
    """EventFilters allows at most 5 filters per request."""
    filters = [{'type': 'contract'}] * 6
    _assert_request_error(_rpc(server.port(), 'getEvents', {'startLedger': 1, 'filters': filters}))


def test_get_events_rejects_topic_filter_with_five_segments(server: StellarRpcServer) -> None:
    """A TopicFilter holds 1 to 4 SegmentMatchers."""
    filters = [{'topics': [[TRANSFER_TOPIC_XDR] * 5]}]
    _assert_request_error(_rpc(server.port(), 'getEvents', {'startLedger': 1, 'filters': filters}))


def test_get_events_rejects_invalid_xdr_format(server: StellarRpcServer) -> None:
    """xdrFormat only admits 'base64' and 'json'; anything else is invalid params.

    komet-node does not implement the JSON XDR representation, so 'json' is also rejected
    with -32602 (documented limitation, consistent with the other methods).
    """
    response = _rpc(server.port(), 'getEvents', {'startLedger': 1, 'xdrFormat': 'bogus'})
    assert 'error' in response, f'expected an error response, got: {response}'
    assert response['error']['code'] == -32602

    response = _rpc(server.port(), 'getEvents', {'startLedger': 1, 'xdrFormat': 'json'})
    assert 'error' in response, f'expected an error response, got: {response}'
    assert response['error']['code'] == -32602


# ---------------------------------------------------------------------------
# Result shape
# ---------------------------------------------------------------------------


def test_get_events_no_events_returns_empty_array(server: StellarRpcServer) -> None:
    """A range that contains no contract events yields an empty events array, not null."""
    fund_account(server)

    result = _rpc(server.port(), 'getEvents', {'startLedger': 1})['result']
    assert result['events'] == []
    assert _is_number(result['latestLedger'])
    assert result['latestLedger'] == 1
    # Real stellar-rpc always populates the cursor (the position to resume scanning from).
    assert isinstance(result['cursor'], str)


def test_get_events_returns_emitted_contract_event(server: StellarRpcServer) -> None:
    """An event published via contract_event is returned with the exact spec shape."""
    keypair, account, contract_address = _deploy_events_contract(server)
    tx_hash = _emit_event(server, keypair, account, contract_address)  # ledger 4

    result = _rpc(server.port(), 'getEvents', {'startLedger': 1})['result']
    assert _is_number(result['latestLedger'])
    assert result['latestLedger'] == 4
    assert isinstance(result['cursor'], str)

    assert isinstance(result['events'], list)
    assert len(result['events']) == 1, f'expected exactly one event: {result["events"]}'
    event = result['events'][0]

    assert event['type'] == 'contract'
    assert _is_number(event['ledger'])
    assert event['ledger'] == 4
    # ledgerClosedAt is an ISO-8601 timestamp string.
    assert isinstance(event['ledgerClosedAt'], str)
    datetime.fromisoformat(event['ledgerClosedAt'].replace('Z', '+00:00'))
    assert event['contractId'] == contract_address
    assert StrKey.is_valid_contract(event['contractId'])
    # The id is TOID-style: 19-digit TOID, hyphen, 10-digit event index; the TOID's high
    # 32 bits are the ledger sequence.
    assert EVENT_ID_RE.fullmatch(event['id']), event['id']
    assert int(event['id'].split('-')[0]) >> 32 == 4
    assert event['txHash'] == tx_hash
    # Topics and value are base64-encoded SCVal XDR.
    assert event['topic'] == [TRANSFER_TOPIC_XDR]
    assert event['value'] == U32_42_XDR
    # Deprecated but still emitted by stellar-rpc v22; if present it must be a true bool
    # (the transaction succeeded).
    if 'inSuccessfulContractCall' in event:
        assert event['inSuccessfulContractCall'] is True

    # endLedger is exclusive: a window that ends at the event's ledger does not include it.
    result = _rpc(server.port(), 'getEvents', {'startLedger': 1, 'endLedger': 4})['result']
    assert result['events'] == []

    # xdrFormat 'base64' is the default and must be accepted explicitly as well.
    result = _rpc(server.port(), 'getEvents', {'startLedger': 1, 'xdrFormat': 'base64'})['result']
    assert len(result['events']) == 1


# ---------------------------------------------------------------------------
# Filters and pagination
# ---------------------------------------------------------------------------


def test_get_events_filtering_and_pagination(server: StellarRpcServer) -> None:
    """Filters narrow by contract id, type, and topics; pagination resumes from the cursor."""
    keypair, account, contract_address = _deploy_events_contract(server)
    _emit_event(server, keypair, account, contract_address)  # ledger 4
    _emit_event(server, keypair, account, contract_address)  # ledger 5

    def get_events(params: dict[str, Any]) -> dict[str, Any]:
        response = _rpc(server.port(), 'getEvents', params)
        assert 'result' in response, f'expected a result, got: {response}'
        return response['result']

    # No filters: both events, in order.
    result = get_events({'startLedger': 1})
    assert [e['ledger'] for e in result['events']] == [4, 5]

    # A matching contractIds filter keeps the events.
    result = get_events({'startLedger': 1, 'filters': [{'contractIds': [contract_address]}]})
    assert len(result['events']) == 2

    # A non-matching contract id filters everything out.
    other_contract = StrKey.encode_contract(b'\x11' * 32)
    result = get_events({'startLedger': 1, 'filters': [{'contractIds': [other_contract]}]})
    assert result['events'] == []

    # Type filter: both events are contract events; no system events were emitted.
    result = get_events({'startLedger': 1, 'filters': [{'type': 'contract'}]})
    assert len(result['events']) == 2
    result = get_events({'startLedger': 1, 'filters': [{'type': 'system'}]})
    assert result['events'] == []

    # Topic filters: exact segment match, single-segment wildcard, and a non-matching topic.
    result = get_events({'startLedger': 1, 'filters': [{'topics': [[TRANSFER_TOPIC_XDR]]}]})
    assert len(result['events']) == 2
    result = get_events({'startLedger': 1, 'filters': [{'topics': [['*']]}]})
    assert len(result['events']) == 2
    result = get_events({'startLedger': 1, 'filters': [{'topics': [[MINT_TOPIC_XDR]]}]})
    assert result['events'] == []

    # Pagination: limit the first page to one event, then resume from the cursor (without
    # startLedger, per the StartLedger descriptor) to fetch the second.
    first_page = get_events({'startLedger': 1, 'pagination': {'limit': 1}})
    assert len(first_page['events']) == 1
    assert first_page['events'][0]['ledger'] == 4
    cursor = first_page['cursor']
    assert isinstance(cursor, str) and cursor != ''

    second_page = get_events({'pagination': {'cursor': cursor, 'limit': 1}})
    assert len(second_page['events']) == 1
    assert second_page['events'][0]['ledger'] == 5
    assert second_page['events'][0]['id'] != first_page['events'][0]['id']

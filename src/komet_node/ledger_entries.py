"""XDR translation for the ``getLedgerEntries`` RPC method.

The K semantics (``node.md``) own the actual state lookup and response dispatch. This
module performs only the two steps K cannot: decoding the base64 ``LedgerKey`` XDR of the
request into the JSON *key descriptors* the semantics consume, and re-encoding the
intermediate entries the semantics found as base64 ``LedgerEntryData`` XDR.

Supported ledger-entry types — the ones the K world state tracks — are ``ACCOUNT``,
``CONTRACT_DATA`` (both the ``SCV_LEDGER_KEY_CONTRACT_INSTANCE`` entry and
persistent/temporary storage), and ``CONTRACT_CODE``. Any other well-formed key is mapped
to an ``unsupported`` descriptor, which the semantics never resolve: per the spec, an
unknown key is not an error, it is simply absent from ``entries``.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from stellar_sdk import xdr
from stellar_sdk.xdr.sc_val_type import SCValType

from komet_node.scval import scval_from_json, scval_to_json

if TYPE_CHECKING:
    from pathlib import Path

_log = logging.getLogger('komet_node')

# The spec caps a single getLedgerEntries request at 200 ledger keys.
KEY_LIMIT = 200


class InvalidParamsError(Exception):
    """Raised when getLedgerEntries params fail validation (JSON-RPC error -32602)."""


def ledger_key_descriptors(params: dict[str, Any]) -> list[dict[str, Any]]:
    """Validate getLedgerEntries params and build the key descriptors for the K envelope.

    Key order within each descriptor is significant: the ``#ledgerEntries`` rules in
    ``node.md`` pattern-match the JSON objects positionally.
    """
    xdr_format = params.get('xdrFormat', 'base64')
    if xdr_format == 'json':
        raise InvalidParamsError("xdrFormat 'json' is not supported by komet-node; use 'base64'")
    if xdr_format != 'base64':
        raise InvalidParamsError(f"unknown xdrFormat {xdr_format!r}; only 'base64' is supported")
    keys = params.get('keys')
    if not isinstance(keys, list) or not all(isinstance(key, str) for key in keys):
        raise InvalidParamsError("'keys' (array of base64-encoded LedgerKey strings) is required")
    if len(keys) > KEY_LIMIT:
        raise InvalidParamsError(f'key count ({len(keys)}) exceeds maximum supported ({KEY_LIMIT})')

    descriptors = []
    for key in keys:
        try:
            ledger_key = xdr.LedgerKey.from_xdr(key)
        except Exception as err:
            raise InvalidParamsError(f'cannot unmarshal key value {key!r}') from err
        if ledger_key.to_xdr() != key:
            # stellar_sdk's from_xdr ignores trailing bytes; real stellar-rpc (Go
            # xdr.SafeUnmarshal) rejects them. The round-trip check restores that.
            raise InvalidParamsError(f'cannot unmarshal key value {key!r}')
        descriptors.append(_descriptor(key, ledger_key))
    return descriptors


def format_ledger_entries_response(response: str, wasms_dir: Path) -> str:
    """Rewrite the semantics' intermediate getLedgerEntries response into the final one.

    The intermediate ``entries`` array (per-kind JSON payloads built in K) is replaced by
    spec-shaped entries with base64 ``LedgerEntryData`` in ``xdr``; ``latestLedger``
    passes through as the JSON number K emitted.
    """
    envelope = json.loads(response)
    result = envelope.get('result')
    if not isinstance(result, dict):
        return response  # not a result envelope; pass through untouched
    latest_ledger = result['latestLedger']
    entries = []
    for entry in result.get('entries', []):
        encoded = _entry_result(entry, latest_ledger, wasms_dir)
        if encoded is not None:
            entries.append(encoded)
    envelope['result'] = {'entries': entries, 'latestLedger': latest_ledger}
    return json.dumps(envelope)


# ----------------------------------------------------------------------
# Request: LedgerKey -> key descriptor
# ----------------------------------------------------------------------


def _descriptor(key: str, ledger_key: xdr.LedgerKey) -> dict[str, Any]:
    unsupported = {'kind': 'unsupported', 'key': key}
    match ledger_key.type:
        case xdr.LedgerEntryType.ACCOUNT:
            assert ledger_key.account is not None
            ed25519 = ledger_key.account.account_id.account_id.ed25519
            if ed25519 is None:
                return unsupported
            return {'kind': 'account', 'key': key, 'accountId': ed25519.uint256.hex()}

        case xdr.LedgerEntryType.CONTRACT_CODE:
            assert ledger_key.contract_code is not None
            return {'kind': 'contractCode', 'key': key, 'hash': ledger_key.contract_code.hash.hash.hex()}

        case xdr.LedgerEntryType.CONTRACT_DATA:
            assert ledger_key.contract_data is not None
            contract_data = ledger_key.contract_data
            if contract_data.contract.contract_id is None:
                return unsupported  # contract data lives under contract addresses only
            contract_hex = contract_data.contract.contract_id.contract_id.hash.hex()

            if contract_data.key.type == SCValType.SCV_LEDGER_KEY_CONTRACT_INSTANCE:
                if contract_data.durability != xdr.ContractDataDurability.PERSISTENT:
                    return unsupported  # instance entries are always persistent
                return {'kind': 'contractInstance', 'key': key, 'contract': contract_hex}

            match contract_data.durability:
                case xdr.ContractDataDurability.PERSISTENT:
                    durability = 'persistent'
                case xdr.ContractDataDurability.TEMPORARY:
                    durability = 'temporary'
                case _:
                    return unsupported
            try:
                sc_key = scval_to_json(contract_data.key)
            except NotImplementedError:
                return unsupported  # key ScVal outside the JSON-encodable subset
            return {
                'kind': 'contractData',
                'key': key,
                'contract': contract_hex,
                'durability': durability,
                'scKey': sc_key,
            }

        case _:
            return unsupported


# ----------------------------------------------------------------------
# Response: intermediate entry -> spec entry with LedgerEntryData XDR
# ----------------------------------------------------------------------


class _UnrepresentableEntryError(Exception):
    """An entry the semantics found cannot be re-encoded as XDR (dropped with a warning)."""


def _entry_result(entry: dict[str, Any], latest_ledger: int, wasms_dir: Path) -> dict[str, Any] | None:
    key = entry['key']
    try:
        data, live_until = _entry_data(entry, xdr.LedgerKey.from_xdr(key), wasms_dir)
    except _UnrepresentableEntryError as err:
        _log.warning('getLedgerEntries: dropping entry for key %s: %s', key, err)
        return None
    # The semantics do not track per-entry modification ledgers, so the current ledger is
    # reported for lastModifiedLedgerSeq. Both ledger fields are JSON numbers per the spec.
    result: dict[str, Any] = {'key': key, 'xdr': data.to_xdr(), 'lastModifiedLedgerSeq': latest_ledger}
    if live_until is not None:
        result['liveUntilLedgerSeq'] = live_until
    return result


def _entry_data(
    entry: dict[str, Any], ledger_key: xdr.LedgerKey, wasms_dir: Path
) -> tuple[xdr.LedgerEntryData, int | None]:
    """Build the LedgerEntryData for one intermediate entry, plus its TTL (None = no TTL)."""
    match entry['kind']:
        case 'account':
            assert ledger_key.account is not None
            return _account_entry_data(ledger_key.account.account_id, entry['balance']), None

        case 'contractCode':
            # The K configuration stores uploaded wasm as a parsed ModuleDecl, so the raw
            # bytes come from the server's side store written at upload time.
            wasm_file = wasms_dir / f'{entry["hash"]}.wasm'
            if not wasm_file.exists():
                raise _UnrepresentableEntryError(
                    'uploaded wasm bytes are not on disk (io-dir predates the wasm store?)'
                )
            data = xdr.LedgerEntryData(
                type=xdr.LedgerEntryType.CONTRACT_CODE,
                contract_code=xdr.ContractCodeEntry(
                    ext=xdr.ContractCodeEntryExt(0),
                    hash=xdr.Hash(bytes.fromhex(entry['hash'])),
                    code=wasm_file.read_bytes(),
                ),
            )
            return data, entry['liveUntil']

        case 'contractInstance':
            assert ledger_key.contract_data is not None
            instance = xdr.SCContractInstance(
                executable=xdr.ContractExecutable(
                    xdr.ContractExecutableType.CONTRACT_EXECUTABLE_WASM,
                    wasm_hash=xdr.Hash(bytes.fromhex(entry['wasmHash'])),
                ),
                storage=_instance_storage(entry['storage']),
            )
            val = xdr.SCVal(type=SCValType.SCV_CONTRACT_INSTANCE, instance=instance)
            return _contract_data_entry_data(ledger_key.contract_data, val), entry['liveUntil']

        case 'contractData':
            assert ledger_key.contract_data is not None
            try:
                val = scval_from_json(entry['val'])
            except NotImplementedError as err:
                raise _UnrepresentableEntryError(str(err)) from err
            return _contract_data_entry_data(ledger_key.contract_data, val), entry['liveUntil']

    raise _UnrepresentableEntryError(f'unknown entry kind {entry["kind"]!r}')


def _account_entry_data(account_id: xdr.AccountID, balance: int) -> xdr.LedgerEntryData:
    # The semantics track only the account's balance; the remaining (required)
    # AccountEntry fields are fixed, plausible values for a fresh account.
    return xdr.LedgerEntryData(
        type=xdr.LedgerEntryType.ACCOUNT,
        account=xdr.AccountEntry(
            account_id=account_id,
            balance=xdr.Int64(balance),
            seq_num=xdr.SequenceNumber(xdr.Int64(0)),
            num_sub_entries=xdr.Uint32(0),
            inflation_dest=None,
            flags=xdr.Uint32(0),
            home_domain=xdr.String32(b''),
            thresholds=xdr.Thresholds(bytes([1, 0, 0, 0])),
            signers=[],
            ext=xdr.AccountEntryExt(0),
        ),
    )


def _contract_data_entry_data(key: xdr.LedgerKeyContractData, val: xdr.SCVal) -> xdr.LedgerEntryData:
    return xdr.LedgerEntryData(
        type=xdr.LedgerEntryType.CONTRACT_DATA,
        contract_data=xdr.ContractDataEntry(
            ext=xdr.ExtensionPoint(0),
            contract=key.contract,
            key=key.key,
            durability=key.durability,
            val=val,
        ),
    )


def _instance_storage(pairs: list[dict[str, Any]]) -> xdr.SCMap | None:
    """Rebuild a contract's instance-storage map; None when the instance stores nothing."""
    if not pairs:
        return None
    entries = []
    for pair in pairs:
        if not {'key', 'val'} <= set(pair):
            raise _UnrepresentableEntryError(f'instance storage pair has no JSON form: {pair!r}')
        try:
            entries.append(xdr.SCMapEntry(key=scval_from_json(pair['key']), val=scval_from_json(pair['val'])))
        except NotImplementedError as err:
            raise _UnrepresentableEntryError(str(err)) from err
    return xdr.SCMap(entries)

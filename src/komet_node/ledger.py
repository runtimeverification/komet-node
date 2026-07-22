"""Construction of the per-ledger XDR artifacts served by ``getLedgers``.

K cannot build XDR, so when a transaction closes a ledger the server materialises the
ledger's header artifacts here and stores them in ``ledgers/ledger_<seq>.json`` for the
history methods (``getLedgers``/``getTransactions``) to read back.
"""

from __future__ import annotations

import hashlib
from typing import Final

from stellar_sdk import xdr

_PROTOCOL_VERSION: Final = 22
_ZERO_HASH: Final = b'\x00' * 32


def build_ledger_artifacts(
    sequence: int,
    close_time: int,
    previous_hash: bytes,
    envelope_xdr: str,
) -> tuple[str, str, str]:
    """Build the ``(hash, headerXdr, metadataXdr)`` artifacts for a newly closed ledger.

    ``headerXdr`` is a base64 ``LedgerHeaderHistoryEntry`` and ``metadataXdr`` a base64
    ``LedgerCloseMeta`` (v0), per the getLedgers spec. komet-node has no consensus,
    buckets, or fee pool, so every header field except the sequence, close time,
    previous-ledger hash, and the transaction set is zeroed. The ledger hash is the
    SHA-256 of the header XDR — unique per ledger and chained to the previous ledger
    through ``previous_ledger_hash`` — which is enough for clients that treat it as an
    opaque identifier.
    """
    header = xdr.LedgerHeader(
        ledger_version=xdr.Uint32(_PROTOCOL_VERSION),
        previous_ledger_hash=xdr.Hash(previous_hash),
        scp_value=xdr.StellarValue(
            tx_set_hash=xdr.Hash(_ZERO_HASH),
            close_time=xdr.TimePoint(xdr.Uint64(close_time)),
            upgrades=[],
            ext=xdr.StellarValueExt(v=xdr.StellarValueType.STELLAR_VALUE_BASIC),
        ),
        tx_set_result_hash=xdr.Hash(_ZERO_HASH),
        bucket_list_hash=xdr.Hash(_ZERO_HASH),
        ledger_seq=xdr.Uint32(sequence),
        total_coins=xdr.Int64(0),
        fee_pool=xdr.Int64(0),
        inflation_seq=xdr.Uint32(0),
        id_pool=xdr.Uint64(0),
        base_fee=xdr.Uint32(100),
        base_reserve=xdr.Uint32(5_000_000),
        max_tx_set_size=xdr.Uint32(1),
        skip_list=[xdr.Hash(_ZERO_HASH)] * 4,
        ext=xdr.LedgerHeaderExt(v=0),
    )
    ledger_hash = hashlib.sha256(header.to_xdr_bytes()).digest()
    entry = xdr.LedgerHeaderHistoryEntry(
        hash=xdr.Hash(ledger_hash),
        header=header,
        ext=xdr.LedgerHeaderHistoryEntryExt(v=0),
    )
    meta = xdr.LedgerCloseMeta(
        v=0,
        v0=xdr.LedgerCloseMetaV0(
            ledger_header=entry,
            tx_set=xdr.TransactionSet(
                previous_ledger_hash=xdr.Hash(previous_hash),
                txs=[xdr.TransactionEnvelope.from_xdr(envelope_xdr)],
            ),
            tx_processing=[],
            upgrades_processing=[],
            scp_info=[],
        ),
    )
    return ledger_hash.hex(), entry.to_xdr(), meta.to_xdr()

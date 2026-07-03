from __future__ import annotations

from typing import TYPE_CHECKING

from komet.scval import (
    SCI32,
    SCI64,
    SCI128,
    SCI256,
    SCU32,
    SCU64,
    SCU128,
    SCU256,
    AccountId,
    ContractId,
    SCAddress,
    SCBool,
    SCBytes,
    SCMap,
    SCSymbol,
    SCVec,
)
from stellar_sdk import xdr as stellar_xdr
from stellar_sdk.xdr.sc_address_type import SCAddressType
from stellar_sdk.xdr.sc_val_type import SCValType

_UINT64_MASK = (1 << 64) - 1

if TYPE_CHECKING:
    from komet.scval import SCValue
    from stellar_sdk.xdr.sc_address import SCAddress as XDRSCAddress
    from stellar_sdk.xdr.sc_val import SCVal


def scval_to_json(scval: SCVal) -> dict:
    """Encode a Stellar XDR SCVal as a JSON-serialisable dict for the node request envelope.

    Key ordering matters: K pattern-matches on JSON key order, so these dicts must be
    produced with keys in the same order as the ``#decodeArg`` rules in ``node.md``.
    """
    match scval.type:
        case SCValType.SCV_BOOL:
            assert scval.b is not None
            return {'type': 'bool', 'value': scval.b}
        case SCValType.SCV_I32:
            assert scval.i32 is not None
            return {'type': 'i32', 'value': scval.i32.int32}
        case SCValType.SCV_U32:
            assert scval.u32 is not None
            return {'type': 'u32', 'value': scval.u32.uint32}
        case SCValType.SCV_I64:
            assert scval.i64 is not None
            return {'type': 'i64', 'value': scval.i64.int64}
        case SCValType.SCV_U64:
            assert scval.u64 is not None
            return {'type': 'u64', 'value': scval.u64.uint64}
        case SCValType.SCV_I128:
            assert scval.i128 is not None
            val = (scval.i128.hi.int64 << 64) | scval.i128.lo.uint64
            return {'type': 'i128', 'value': val}
        case SCValType.SCV_U128:
            assert scval.u128 is not None
            val = (scval.u128.hi.uint64 << 64) | scval.u128.lo.uint64
            return {'type': 'u128', 'value': val}
        case SCValType.SCV_SYMBOL:
            assert scval.sym is not None
            return {'type': 'symbol', 'value': scval.sym.sc_symbol.decode()}
        case SCValType.SCV_BYTES:
            assert scval.bytes is not None
            return {'type': 'bytes', 'value': scval.bytes.sc_bytes.hex()}
        case SCValType.SCV_ADDRESS:
            assert scval.address is not None
            addr = scval.address
            if addr.type == SCAddressType.SC_ADDRESS_TYPE_ACCOUNT:
                assert addr.account_id is not None
                assert addr.account_id.account_id.ed25519 is not None
                raw = addr.account_id.account_id.ed25519.uint256
                return {'type': 'address', 'addrType': 'account', 'value': raw.hex()}
            assert addr.contract_id is not None
            return {'type': 'address', 'addrType': 'contract', 'value': addr.contract_id.contract_id.hash.hex()}
        case _:
            raise NotImplementedError(f'Unsupported SCVal type for JSON encoding: {scval.type}')


def json_to_scval(value: dict) -> stellar_xdr.SCVal:
    """Decode the JSON encoding of an ScVal produced by K (``#scValJSON`` in ``node.md``)
    into a Stellar XDR SCVal.

    This is the K-to-Python direction (contract *return values*, e.g. from
    ``simulateTransaction``), the inverse of :func:`scval_to_json` — keep the two and the
    ``#scValJSON`` rules in sync.
    """
    match value['type']:
        case 'bool':
            return stellar_xdr.SCVal(SCValType.SCV_BOOL, b=value['value'])
        case 'void':
            return stellar_xdr.SCVal(SCValType.SCV_VOID)
        case 'u32':
            return stellar_xdr.SCVal(SCValType.SCV_U32, u32=stellar_xdr.Uint32(value['value']))
        case 'i32':
            return stellar_xdr.SCVal(SCValType.SCV_I32, i32=stellar_xdr.Int32(value['value']))
        case 'u64':
            return stellar_xdr.SCVal(SCValType.SCV_U64, u64=stellar_xdr.Uint64(value['value']))
        case 'i64':
            return stellar_xdr.SCVal(SCValType.SCV_I64, i64=stellar_xdr.Int64(value['value']))
        case 'u128':
            val = value['value']
            parts = stellar_xdr.UInt128Parts(
                hi=stellar_xdr.Uint64(val >> 64), lo=stellar_xdr.Uint64(val & _UINT64_MASK)
            )
            return stellar_xdr.SCVal(SCValType.SCV_U128, u128=parts)
        case 'i128':
            val = value['value']
            # Arithmetic right shift keeps the sign in hi; lo is the unsigned low word.
            iparts = stellar_xdr.Int128Parts(hi=stellar_xdr.Int64(val >> 64), lo=stellar_xdr.Uint64(val & _UINT64_MASK))
            return stellar_xdr.SCVal(SCValType.SCV_I128, i128=iparts)
        case 'symbol':
            return stellar_xdr.SCVal(SCValType.SCV_SYMBOL, sym=stellar_xdr.SCSymbol(value['value'].encode('ascii')))
        case 'string':
            return stellar_xdr.SCVal(SCValType.SCV_STRING, str=stellar_xdr.SCString(value['value'].encode('utf-8')))
        case 'bytes':
            return stellar_xdr.SCVal(SCValType.SCV_BYTES, bytes=stellar_xdr.SCBytes(bytes.fromhex(value['value'])))
        case 'address':
            return stellar_xdr.SCVal(SCValType.SCV_ADDRESS, address=_json_to_sc_address(value))
        case 'vec':
            vec = stellar_xdr.SCVec([json_to_scval(item) for item in value['value']])
            return stellar_xdr.SCVal(SCValType.SCV_VEC, vec=vec)
        case other:
            raise NotImplementedError(f'Unsupported ScVal JSON type: {other}')


def _json_to_sc_address(value: dict) -> XDRSCAddress:
    raw = bytes.fromhex(value['value'])
    if value['addrType'] == 'account':
        account_id = stellar_xdr.AccountID(
            stellar_xdr.PublicKey(stellar_xdr.PublicKeyType.PUBLIC_KEY_TYPE_ED25519, ed25519=stellar_xdr.Uint256(raw))
        )
        return stellar_xdr.SCAddress(SCAddressType.SC_ADDRESS_TYPE_ACCOUNT, account_id=account_id)
    assert value['addrType'] == 'contract'
    return stellar_xdr.SCAddress(
        SCAddressType.SC_ADDRESS_TYPE_CONTRACT,
        contract_id=stellar_xdr.ContractID(stellar_xdr.Hash(raw)),
    )


def sc_address_from_xdr(xdr: XDRSCAddress) -> SCAddress:
    """Convert an XDR SCAddress to a Komet SCAddress."""
    match xdr.type:
        case SCAddressType.SC_ADDRESS_TYPE_ACCOUNT:
            assert xdr.account_id is not None
            # The account_id is a PublicKey XDR — extract the raw 32-byte ed25519 key
            assert xdr.account_id.account_id.ed25519 is not None
            raw_bytes = xdr.account_id.account_id.ed25519.uint256
            return SCAddress(AccountId(raw_bytes))
        case SCAddressType.SC_ADDRESS_TYPE_CONTRACT:
            assert xdr.contract_id is not None
            return SCAddress(ContractId(xdr.contract_id.contract_id.hash))
        case _:
            raise NotImplementedError(f'Unsupported SCAddress type: {xdr.type}')


def scvalue_from_xdr(xdr: SCVal) -> SCValue:
    """
    Convert a Stellar XDR SCVal to a Komet SCValue.

    The XDR SCVal is a large union type — each case maps directly to one of
    the Komet SCValue dataclasses. Unsupported types (void, error, timepoint,
    duration, contract instance, ledger keys) raise NotImplementedError.
    """
    match xdr.type:

        case SCValType.SCV_BOOL:
            assert xdr.b is not None
            return SCBool(xdr.b)

        case SCValType.SCV_I32:
            assert xdr.i32 is not None
            return SCI32(xdr.i32.int32)

        case SCValType.SCV_I64:
            assert xdr.i64 is not None
            return SCI64(xdr.i64.int64)

        case SCValType.SCV_I128:
            assert xdr.i128 is not None
            # i128 is stored as (hi: int64, lo: uint64) parts
            val = (xdr.i128.hi.int64 << 64) | xdr.i128.lo.uint64
            return SCI128(val)

        case SCValType.SCV_I256:
            assert xdr.i256 is not None
            # i256 is stored as (hi_hi, hi_lo, lo_hi, lo_lo) parts
            val = (
                (xdr.i256.hi_hi.int64 << 192)
                | (xdr.i256.hi_lo.uint64 << 128)
                | (xdr.i256.lo_hi.uint64 << 64)
                | xdr.i256.lo_lo.uint64
            )
            return SCI256(val)

        case SCValType.SCV_U32:
            assert xdr.u32 is not None
            return SCU32(xdr.u32.uint32)

        case SCValType.SCV_U64:
            assert xdr.u64 is not None
            return SCU64(xdr.u64.uint64)

        case SCValType.SCV_U128:
            assert xdr.u128 is not None
            val = (xdr.u128.hi.uint64 << 64) | xdr.u128.lo.uint64
            return SCU128(val)

        case SCValType.SCV_U256:
            assert xdr.u256 is not None
            val = (
                (xdr.u256.hi_hi.uint64 << 192)
                | (xdr.u256.hi_lo.uint64 << 128)
                | (xdr.u256.lo_hi.uint64 << 64)
                | xdr.u256.lo_lo.uint64
            )
            return SCU256(val)

        case SCValType.SCV_SYMBOL:
            assert xdr.sym is not None
            return SCSymbol(xdr.sym.sc_symbol.decode())

        case SCValType.SCV_BYTES:
            assert xdr.bytes is not None
            return SCBytes(xdr.bytes.sc_bytes)

        case SCValType.SCV_ADDRESS:
            assert xdr.address is not None
            return sc_address_from_xdr(xdr.address)

        case SCValType.SCV_VEC:
            assert xdr.vec is not None
            # komet annotates SCVec.val as tuple[SCValue] instead of tuple[SCValue, ...]
            return SCVec(tuple(scvalue_from_xdr(v) for v in xdr.vec.sc_vec))  # type: ignore[arg-type]

        case SCValType.SCV_MAP:
            assert xdr.map is not None
            return SCMap({scvalue_from_xdr(entry.key): scvalue_from_xdr(entry.val) for entry in xdr.map.sc_map})

        case _:
            raise NotImplementedError(f'Unsupported SCVal type: {xdr.type}')

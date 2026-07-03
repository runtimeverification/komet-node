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

if TYPE_CHECKING:
    from komet.scval import SCValue
    from stellar_sdk.xdr.sc_address import SCAddress as XDRSCAddress
    from stellar_sdk.xdr.sc_val import SCVal

_UINT64_MASK = (1 << 64) - 1


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


def scval_from_json(value: dict) -> SCVal:
    """Decode the JSON ScVal encoding emitted by the semantics back into an XDR SCVal.

    Inverse of :func:`scval_to_json`, extended with the value-only types the semantics can
    hold in contract storage but that never appear as call arguments (``void``, ``string``,
    ``u256``, ``vec``, ``map``) — see ``#scVal2JSON`` in ``node.md``. Raises
    ``NotImplementedError`` for values with no JSON form (``{"type": "unsupported"}``).
    """
    match value.get('type'):
        case 'bool':
            return stellar_xdr.SCVal(type=SCValType.SCV_BOOL, b=bool(value['value']))
        case 'i32':
            return stellar_xdr.SCVal(type=SCValType.SCV_I32, i32=stellar_xdr.Int32(value['value']))
        case 'u32':
            return stellar_xdr.SCVal(type=SCValType.SCV_U32, u32=stellar_xdr.Uint32(value['value']))
        case 'i64':
            return stellar_xdr.SCVal(type=SCValType.SCV_I64, i64=stellar_xdr.Int64(value['value']))
        case 'u64':
            return stellar_xdr.SCVal(type=SCValType.SCV_U64, u64=stellar_xdr.Uint64(value['value']))
        case 'i128':
            val = value['value']
            parts = stellar_xdr.Int128Parts(hi=stellar_xdr.Int64(val >> 64), lo=stellar_xdr.Uint64(val & _UINT64_MASK))
            return stellar_xdr.SCVal(type=SCValType.SCV_I128, i128=parts)
        case 'u128':
            val = value['value']
            parts128 = stellar_xdr.UInt128Parts(
                hi=stellar_xdr.Uint64(val >> 64), lo=stellar_xdr.Uint64(val & _UINT64_MASK)
            )
            return stellar_xdr.SCVal(type=SCValType.SCV_U128, u128=parts128)
        case 'u256':
            val = value['value']
            parts256 = stellar_xdr.UInt256Parts(
                hi_hi=stellar_xdr.Uint64(val >> 192),
                hi_lo=stellar_xdr.Uint64((val >> 128) & _UINT64_MASK),
                lo_hi=stellar_xdr.Uint64((val >> 64) & _UINT64_MASK),
                lo_lo=stellar_xdr.Uint64(val & _UINT64_MASK),
            )
            return stellar_xdr.SCVal(type=SCValType.SCV_U256, u256=parts256)
        case 'symbol':
            return stellar_xdr.SCVal(type=SCValType.SCV_SYMBOL, sym=stellar_xdr.SCSymbol(value['value'].encode()))
        case 'string':
            return stellar_xdr.SCVal(type=SCValType.SCV_STRING, str=stellar_xdr.SCString(value['value'].encode()))
        case 'bytes':
            return stellar_xdr.SCVal(type=SCValType.SCV_BYTES, bytes=stellar_xdr.SCBytes(bytes.fromhex(value['value'])))
        case 'void':
            return stellar_xdr.SCVal(type=SCValType.SCV_VOID)
        case 'address':
            raw = bytes.fromhex(value['value'])
            if value['addrType'] == 'account':
                address = stellar_xdr.SCAddress(
                    type=SCAddressType.SC_ADDRESS_TYPE_ACCOUNT,
                    account_id=stellar_xdr.AccountID(
                        stellar_xdr.PublicKey(
                            stellar_xdr.PublicKeyType.PUBLIC_KEY_TYPE_ED25519,
                            ed25519=stellar_xdr.Uint256(raw),
                        )
                    ),
                )
            else:
                address = stellar_xdr.SCAddress(
                    type=SCAddressType.SC_ADDRESS_TYPE_CONTRACT,
                    contract_id=stellar_xdr.ContractID(stellar_xdr.Hash(raw)),
                )
            return stellar_xdr.SCVal(type=SCValType.SCV_ADDRESS, address=address)
        case 'vec':
            items = [scval_from_json(item) for item in value['value']]
            return stellar_xdr.SCVal(type=SCValType.SCV_VEC, vec=stellar_xdr.SCVec(items))
        case 'map':
            if not all({'key', 'val'} <= set(pair) for pair in value['value']):
                raise NotImplementedError(f'Unsupported SCMap entry in JSON encoding: {value!r}')
            entries = [
                stellar_xdr.SCMapEntry(key=scval_from_json(pair['key']), val=scval_from_json(pair['val']))
                for pair in value['value']
            ]
            return stellar_xdr.SCVal(type=SCValType.SCV_MAP, map=stellar_xdr.SCMap(entries))
        case _:
            raise NotImplementedError(f'Unsupported SCVal JSON encoding: {value!r}')


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
            return SCVec(tuple(scvalue_from_xdr(v) for v in xdr.vec.sc_vec))

        case SCValType.SCV_MAP:
            assert xdr.map is not None
            return SCMap({scvalue_from_xdr(entry.key): scvalue_from_xdr(entry.val) for entry in xdr.map.sc_map})

        case _:
            raise NotImplementedError(f'Unsupported SCVal type: {xdr.type}')

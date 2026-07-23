from __future__ import annotations

from typing import TYPE_CHECKING

from stellar_sdk import xdr as stellar_xdr
from stellar_sdk.xdr.sc_address_type import SCAddressType
from stellar_sdk.xdr.sc_val_type import SCValType

_UINT64_MASK = (1 << 64) - 1

if TYPE_CHECKING:
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


def scval_from_json(value: dict) -> SCVal:
    """Decode the JSON ScVal encoding emitted by the semantics back into an XDR SCVal.

    Inverse of :func:`scval_to_json`, extended with the value-only types the semantics can
    hold in contract storage or return from a contract call but that never appear as call
    arguments (``void``, ``string``, ``u256``, ``vec``, ``map``). Covers all three K-side
    encoders (``#scVal2JSON``, ``#scValJSON``, ``#scValToJSON`` in ``node.md``); the ``map``
    case accepts both entry shapes they emit — ``{"key": ..., "val": ...}`` objects and
    ``[key, val]`` pairs — so keep the encoders and this decoder in sync. Raises
    ``NotImplementedError`` for values with no JSON form (``{"type": "unsupported"}`` or a
    non-object such as the ``null`` the event capture stages for an unrepresentable value).
    """
    if not isinstance(value, dict):
        raise NotImplementedError(f'Unsupported SCVal JSON encoding: {value!r}')
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
            entries = [
                stellar_xdr.SCMapEntry(key=scval_from_json(key), val=scval_from_json(val))
                for key, val in (_map_entry(pair) for pair in value['value'])
            ]
            return stellar_xdr.SCVal(type=SCValType.SCV_MAP, map=stellar_xdr.SCMap(entries))
        case _:
            raise NotImplementedError(f'Unsupported SCVal JSON encoding: {value!r}')


def _map_entry(pair: object) -> tuple[dict, dict]:
    """Unpack one SCMap entry from either JSON shape the K encoders emit.

    ``#scVal2JSON`` emits ``{"key": ..., "val": ...}`` objects; ``#scValToJSON`` emits
    ``[key, val]`` two-element arrays. Both decode to the same ``(key, val)`` pair.
    """
    if isinstance(pair, dict) and {'key', 'val'} <= set(pair):
        return pair['key'], pair['val']
    if isinstance(pair, (list, tuple)) and len(pair) == 2:
        return pair[0], pair[1]
    raise NotImplementedError(f'Unsupported SCMap entry in JSON encoding: {pair!r}')

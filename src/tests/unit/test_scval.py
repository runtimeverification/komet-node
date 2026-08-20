"""Unit tests for ``scval_to_json`` — the SCVal -> request-envelope JSON encoder —
and for the 256-bit cases of its inverse, ``scval_from_json``.

These are pure-Python tests (no K, no kdist build). They pin two things:

* the JSON *shape* the K ``#decodeArg`` rules pattern-match on for composite
  (vec / map) call arguments and for the scalar types that used to be rejected as
  arguments (``void``, ``string``, ``u256``, ``i256``) — key order is significant,
  so the expected dicts are compared verbatim; and
* that encoding a deeply nested composite value does not blow Python's default
  recursion limit (blocker #2). ``scval_to_json`` recurses with the value's
  structure, so a deep value is a deterministic proxy for the large-real-contract
  recursion that komet-node previously died on.
"""

from __future__ import annotations

import json

from stellar_sdk import xdr
from stellar_sdk.xdr.sc_val_type import SCValType

from komet_node.scval import scval_from_json, scval_to_json


def _sym(name: str) -> xdr.SCVal:
    return xdr.SCVal(type=SCValType.SCV_SYMBOL, sym=xdr.SCSymbol(sc_symbol=name.encode()))


def _i128(value: int) -> xdr.SCVal:
    return xdr.SCVal(type=SCValType.SCV_I128, i128=xdr.Int128Parts(hi=xdr.Int64(0), lo=xdr.Uint64(value)))


def _u32(value: int) -> xdr.SCVal:
    return xdr.SCVal(type=SCValType.SCV_U32, u32=xdr.Uint32(value))


def _vec(elems: list[xdr.SCVal]) -> xdr.SCVal:
    return xdr.SCVal(type=SCValType.SCV_VEC, vec=xdr.SCVec(elems))


def _map(entries: list[tuple[xdr.SCVal, xdr.SCVal]]) -> xdr.SCVal:
    return xdr.SCVal(
        type=SCValType.SCV_MAP,
        map=xdr.SCMap([xdr.SCMapEntry(key=k, val=v) for k, v in entries]),
    )


def test_scval_to_json_vec_of_scalars() -> None:
    """A vec encodes as ``{'type': 'vec', 'value': [<elem>, ...]}``.

    Key *order* is significant: the K ``#decodeArg`` rules pattern-match on JSON
    member order, so this pins the exact serialization (a dict ``==`` compare is
    order-insensitive and would not catch a reordering), not just the key/values.
    """
    encoded = scval_to_json(_vec([_sym('Native'), _i128(1000)]))
    assert encoded == {
        'type': 'vec',
        'value': [
            {'type': 'symbol', 'value': 'Native'},
            {'type': 'i128', 'value': 1000},
        ],
    }
    assert json.dumps(encoded) == (
        '{"type": "vec", "value": [{"type": "symbol", "value": "Native"}, ' '{"type": "i128", "value": 1000}]}'
    )


def test_scval_to_json_empty_vec() -> None:
    assert scval_to_json(_vec([])) == {'type': 'vec', 'value': []}


def test_scval_to_json_map() -> None:
    """A map encodes as ``{'type': 'map', 'value': [{'key': .., 'val': ..}, ..]}``."""
    encoded = scval_to_json(_map([(_sym('amount'), _u32(7))]))
    assert encoded == {
        'type': 'map',
        'value': [
            {'key': {'type': 'symbol', 'value': 'amount'}, 'val': {'type': 'u32', 'value': 7}},
        ],
    }
    # Order-sensitive check: 'type' before 'value', and 'key' before 'val'.
    assert json.dumps(encoded) == (
        '{"type": "map", "value": [{"key": {"type": "symbol", "value": "amount"}, '
        '"val": {"type": "u32", "value": 7}}]}'
    )


def test_scval_to_json_empty_map() -> None:
    assert scval_to_json(_map([])) == {'type': 'map', 'value': []}


def test_scval_to_json_nested_composite_supply_shape() -> None:
    """The real motivating case: ``Vec<(AssetKey, i128)>`` with a unit-enum variant.

    A unit enum variant (``AssetKey::Native``) is itself a single-element vec of a
    symbol at the XDR level, and a tuple is a vec — so the whole argument is nested
    vecs bottoming out in scalars. Encoding must recurse through every level.
    """
    request = _vec([_vec([_vec([_sym('Native')]), _i128(1000)])])
    assert scval_to_json(request) == {
        'type': 'vec',
        'value': [
            {
                'type': 'vec',
                'value': [
                    {'type': 'vec', 'value': [{'type': 'symbol', 'value': 'Native'}]},
                    {'type': 'i128', 'value': 1000},
                ],
            },
        ],
    }


def test_scval_to_json_deeply_nested_vec_survives_recursion_limit() -> None:
    """Encoding a deeply nested value must not raise ``RecursionError`` (blocker #2).

    ``scval_to_json`` recurses with the value's depth. Python's default recursion
    limit (1000) is well below what a large real contract's values reach, so
    komet-node raises the limit at import time. A 2000-deep vec is a deterministic
    proxy: it exceeds the default limit but stays within the process stack. Without
    the raised limit this raises ``RecursionError``; with it, it encodes cleanly.
    """
    depth = 2000
    value = _sym('leaf')
    for _ in range(depth):
        value = _vec([value])

    encoded = scval_to_json(value)

    # Peel the encoded structure back down and confirm it is intact to the leaf.
    for _ in range(depth):
        assert encoded['type'] == 'vec'
        assert len(encoded['value']) == 1
        encoded = encoded['value'][0]
    assert encoded == {'type': 'symbol', 'value': 'leaf'}


def test_scval_from_json_u256() -> None:
    """The semantics emit 256-bit values as a single integer; the parts are rebuilt here."""
    value = (1 << 192) | (2 << 128) | (3 << 64) | 4
    decoded = scval_from_json({'type': 'u256', 'value': value})
    assert decoded.type == SCValType.SCV_U256
    assert decoded.u256 is not None
    assert (decoded.u256.hi_hi.uint64, decoded.u256.hi_lo.uint64) == (1, 2)
    assert (decoded.u256.lo_hi.uint64, decoded.u256.lo_lo.uint64) == (3, 4)


def test_scval_from_json_i256_positive() -> None:
    value = (1 << 192) | (2 << 128) | (3 << 64) | 4
    decoded = scval_from_json({'type': 'i256', 'value': value})
    assert decoded.type == SCValType.SCV_I256
    assert decoded.i256 is not None
    assert (decoded.i256.hi_hi.int64, decoded.i256.hi_lo.uint64) == (1, 2)
    assert (decoded.i256.lo_hi.uint64, decoded.i256.lo_lo.uint64) == (3, 4)


def test_scval_from_json_i256_negative() -> None:
    """Only the i256 case can carry a negative value: the words are its two's complement."""
    mask = (1 << 64) - 1
    decoded = scval_from_json({'type': 'i256', 'value': -1})
    assert decoded.type == SCValType.SCV_I256
    assert decoded.i256 is not None
    assert decoded.i256.hi_hi.int64 == -1
    assert decoded.i256.hi_lo.uint64 == mask
    assert decoded.i256.lo_hi.uint64 == mask
    assert decoded.i256.lo_lo.uint64 == mask


def test_scval_from_json_i256_min() -> None:
    decoded = scval_from_json({'type': 'i256', 'value': -(2**255)})
    assert decoded.i256 is not None
    assert decoded.i256.hi_hi.int64 == -(2**63)
    assert (decoded.i256.hi_lo.uint64, decoded.i256.lo_hi.uint64, decoded.i256.lo_lo.uint64) == (0, 0, 0)


def _u256(value: int) -> xdr.SCVal:
    mask = (1 << 64) - 1
    parts = xdr.UInt256Parts(
        hi_hi=xdr.Uint64(value >> 192),
        hi_lo=xdr.Uint64((value >> 128) & mask),
        lo_hi=xdr.Uint64((value >> 64) & mask),
        lo_lo=xdr.Uint64(value & mask),
    )
    return xdr.SCVal(type=SCValType.SCV_U256, u256=parts)


def _i256(value: int) -> xdr.SCVal:
    mask = (1 << 64) - 1
    parts = xdr.Int256Parts(
        hi_hi=xdr.Int64(value >> 192),
        hi_lo=xdr.Uint64((value >> 128) & mask),
        lo_hi=xdr.Uint64((value >> 64) & mask),
        lo_lo=xdr.Uint64(value & mask),
    )
    return xdr.SCVal(type=SCValType.SCV_I256, i256=parts)


def test_scval_to_json_void() -> None:
    """Void carries no payload, so it encodes to the single-key object #decodeArg matches."""
    encoded = scval_to_json(xdr.SCVal(type=SCValType.SCV_VOID))
    assert encoded == {'type': 'void'}
    assert json.dumps(encoded) == '{"type": "void"}'


def test_scval_to_json_string() -> None:
    """A String argument (e.g. hello_world's `to: String`) encodes like a symbol does."""
    encoded = scval_to_json(xdr.SCVal(type=SCValType.SCV_STRING, str=xdr.SCString(sc_string=b'Soroban')))
    assert encoded == {'type': 'string', 'value': 'Soroban'}
    assert json.dumps(encoded) == '{"type": "string", "value": "Soroban"}'


def test_scval_to_json_string_empty_and_non_ascii() -> None:
    assert scval_to_json(xdr.SCVal(type=SCValType.SCV_STRING, str=xdr.SCString(sc_string=b''))) == {
        'type': 'string',
        'value': '',
    }
    assert scval_to_json(
        xdr.SCVal(type=SCValType.SCV_STRING, str=xdr.SCString(sc_string='üñî'.encode()))
    ) == {'type': 'string', 'value': 'üñî'}


def test_scval_to_json_u256() -> None:
    """The four words recombine into the single integer the semantics expect."""
    value = (1 << 192) | (2 << 128) | (3 << 64) | 4
    assert scval_to_json(_u256(value)) == {'type': 'u256', 'value': value}
    assert scval_to_json(_u256(2**256 - 1)) == {'type': 'u256', 'value': 2**256 - 1}


def test_scval_to_json_i256_round_trips_through_from_json() -> None:
    """Negative and extreme i256 values survive the encode/decode pair unchanged."""
    for value in (0, 33, -1, -(2**255), 2**255 - 1, -(1 << 192) | 7):
        assert scval_to_json(_i256(value)) == {'type': 'i256', 'value': value}
        decoded = scval_from_json(scval_to_json(_i256(value)))
        assert decoded == _i256(value)

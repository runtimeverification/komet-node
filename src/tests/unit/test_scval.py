"""Unit tests for ``scval_to_json`` — the SCVal -> request-envelope JSON encoder.

These are pure-Python tests (no K, no kdist build). They pin two things:

* the JSON *shape* the K ``#decodeArg`` rules pattern-match on for composite
  (vec / map) call arguments — key order is significant, so the expected dicts
  are compared verbatim; and
* that encoding a deeply nested composite value does not blow Python's default
  recursion limit (blocker #2). ``scval_to_json`` recurses with the value's
  structure, so a deep value is a deterministic proxy for the large-real-contract
  recursion that komet-node previously died on.
"""

from __future__ import annotations

import json

from stellar_sdk import xdr
from stellar_sdk.xdr.sc_val_type import SCValType

from komet_node.scval import scval_to_json


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

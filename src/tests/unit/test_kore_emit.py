"""Unit tests for the structural half of the fast KAST-to-KORE emitter's guard.

The emitter only handles *plain* terms — trees of ``KApply`` and ``KToken`` with no
sequences, variables, rewrites, ML connectives, or cells. Those exclusions are what make
the generic pipeline's normalization passes provably inapplicable, so the emitter can skip
straight to text. This module covers the definition-free part of that check; the part that
needs a ``KDefinition`` (sort parameters already resolved) is covered in the integration
tests, along with byte-equality against ``kast_to_kore``.
"""

from __future__ import annotations

from pyk.kast.inner import KApply, KRewrite, KSequence, KSort, KToken, KVariable
from pyk.kast.prelude.utils import token

from komet_node.kore_emit import has_only_plain_nodes


def test_plain_tree_of_applies_and_tokens_is_plain() -> None:
    term = KApply('uploadWasm', [token(b'\x01'), KApply('moduleDecl', [token(7)])])

    assert has_only_plain_nodes(term)


def test_a_bare_token_is_plain() -> None:
    assert has_only_plain_nodes(token(3))


def test_a_childless_apply_is_plain() -> None:
    assert has_only_plain_nodes(KApply('emptyModule'))


def test_a_variable_is_not_plain() -> None:
    # `sort_vars` exists to rewrite variables, so a term containing one is not a term the
    # normalization passes can be skipped for.
    assert not has_only_plain_nodes(KApply('f', [KVariable('X', KSort('Int'))]))


def test_a_ksequence_is_not_plain() -> None:
    # Two of the skipped passes exist purely to rewrite K sequences.
    assert not has_only_plain_nodes(KApply('f', [KSequence([KApply('a'), KApply('b')])]))


def test_a_rewrite_is_not_plain() -> None:
    assert not has_only_plain_nodes(KRewrite(KApply('a'), KApply('b')))


def test_an_ml_connective_is_not_plain() -> None:
    # ML patterns become \and, \equals, ... in KORE, with their own arity and sort rules.
    assert not has_only_plain_nodes(KApply('#And', [KApply('a'), KApply('b')]))


def test_an_ml_quantifier_is_not_plain() -> None:
    assert not has_only_plain_nodes(KApply('#Exists', [KVariable('X'), KApply('a')]))


def test_a_cell_is_not_plain() -> None:
    # `add_cell_map_items` rewrites collection items inside cells.
    assert not has_only_plain_nodes(KApply('<program>', [KApply('a')]))


def test_nesting_is_checked_all_the_way_down() -> None:
    deep = KApply('f', [KApply('g', [KApply('h', [KSequence([KApply('a')])])])])

    assert not has_only_plain_nodes(deep)


def test_a_token_of_every_sort_is_plain() -> None:
    for value in (1, 'text', b'\x00\xff', True):
        assert has_only_plain_nodes(KApply('f', [token(value)]))


def test_plainness_does_not_depend_on_label_spelling() -> None:
    # Only the specific exclusions matter; an ordinary label with punctuation is fine.
    assert has_only_plain_nodes(KApply('_+Int_', [token(1), token(2)]))


def test_a_lone_angle_bracket_label_is_still_treated_as_a_cell() -> None:
    assert not has_only_plain_nodes(KApply('<k>', [KToken('.K', KSort('K'))]))

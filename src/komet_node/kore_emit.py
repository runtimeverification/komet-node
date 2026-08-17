"""A fast KAST-to-KORE-text conversion for plain terms.

``pyk.konvert.kast_to_kore`` is general: it normalizes the term (six whole-term passes),
builds a KORE term, and the caller then serializes that. Every stage rebuilds every node, so
converting an uploaded wasm module — half a million subterms for an unoptimized build with
debug info — took ~40s, of which ~20s was passes that provably could not change it, plus a
million uncached ``resolve_sorts`` calls over a few hundred distinct labels.

:func:`kast_to_kore_text` does the same job for *plain* terms in one pass, writing KORE text
straight into a buffer with every definition lookup memoized by label, sort, or token. It is
~17x faster on a contract module and produces byte-identical output; anything not plain falls
back to ``kast_to_kore``.

A *plain* term is a tree of ``KApply`` and ``KToken`` with no K sequences, variables,
rewrites, ML connectives or quantifiers, or cells, and with every parametric label's sort
parameters already resolved. Those exclusions are exactly the features the normalization
passes exist to rewrite, which is what makes skipping them sound rather than merely faster.
Terms built by ``pykwasm``'s ``wasm2kast`` are plain.

Nothing here is Soroban- or wasm-specific: this is generic ``pyk.konvert`` material and
belongs upstream in pyk, where it would speed up every K tool. It lives here until it does.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from pyk.kast.inner import KApply, KToken
from pyk.konvert import kast_to_kore
from pyk.konvert._kast_to_kore import ML_PATTERN_LABELS, _ktoken_to_kore, _label_to_kore

if TYPE_CHECKING:
    from pyk.kast.inner import KInner, KSort
    from pyk.kast.outer import KDefinition

# Cell labels (`<k>`, `<program>`, ...) are excluded because `add_cell_map_items` rewrites
# collection items inside them.
_CELL_PREFIX: Final = '<'


def has_only_plain_nodes(term: KInner) -> bool:
    """True when every node of ``term`` is a non-cell, non-ML ``KApply`` or a ``KToken``.

    The definition-free half of the plainness check: it rules out the node kinds and labels
    the emitter cannot render (sequences, variables, rewrites, ML patterns) and the ones
    whose presence would make a skipped normalization pass meaningful (cells).
    """
    stack = [term]
    while stack:
        node = stack.pop()
        if isinstance(node, KToken):
            continue
        if not isinstance(node, KApply):
            return False
        name = node.label.name
        if name.startswith(_CELL_PREFIX) or name in ML_PATTERN_LABELS:
            return False
        stack.extend(node.args)
    return True


def _sort_params_resolved(definition: KDefinition, term: KInner) -> bool:
    """True when every label in ``term`` already carries its production's sort parameters.

    This is what makes ``add_sort_params`` a no-op for the term. A label whose parameters are
    missing (or that the definition does not know at all) sends the term down the generic
    path rather than into a ``resolve_sorts`` failure.
    """
    arity: dict[str, int] = {}
    stack = [term]
    while stack:
        node = stack.pop()
        if not isinstance(node, KApply):
            continue
        name = node.label.name
        expected = arity.get(name)
        if expected is None:
            production = definition.symbols.get(name)
            if production is None:
                return False
            expected = arity[name] = len(production.params)
        if len(node.label.params) != expected:
            return False
        stack.extend(node.args)
    return True


def is_plain_kast(definition: KDefinition, term: KInner) -> bool:
    """True when ``term`` can be converted by :func:`emit_kore_text`."""
    return has_only_plain_nodes(term) and _sort_params_resolved(definition, term)


def kast_to_kore_text(definition: KDefinition, term: KInner, sort: KSort) -> str:
    """``kast_to_kore(definition, term, sort).text``, taking the fast path when it applies."""
    if is_plain_kast(definition, term):
        return emit_kore_text(definition, term, sort)
    return kast_to_kore(definition, term, sort).text


def emit_kore_text(definition: KDefinition, term: KInner, sort: KSort) -> str:
    """Serialize a plain ``term`` to KORE text in a single pass.

    Caller must have established :func:`is_plain_kast`. The walk keeps its own stack (a
    module nests far deeper than Python's recursion limit allows) of pending items: either a
    subterm paired with the sort it must be injected to, or a literal chunk to append. The
    stack is untyped for the same reason pyk's own conversion loops are — the two entry
    shapes are discriminated by ``isinstance`` at the top of the loop.

    Every lookup is memoized: by ``KLabel`` for sorts and the opening text, by
    (sort, literal) for tokens, and by sort pair for injections. Half a million subterms use
    only a few hundred distinct labels, so the definition is consulted a few hundred times
    rather than a million.
    """
    chunks: list[str] = []
    resolved: dict[object, tuple[KSort, tuple[KSort, ...]]] = {}
    openers: dict[object, str] = {}
    tokens: dict[tuple[str, str], str] = {}
    injections: dict[tuple[str, str], str] = {}
    subsorts: dict[str, frozenset] = {}

    stack: list = [(term, sort)]
    while stack:
        node, target = stack.pop()
        if isinstance(node, str):
            chunks.append(node)
            continue

        if isinstance(node, KToken):
            actual = node.sort
        else:
            label = node.label
            sorts = resolved.get(label)
            if sorts is None:
                sorts = resolved[label] = definition.resolve_sorts(label)
            actual, argument_sorts = sorts

        inject = actual != target
        if inject:
            key = (actual.name, target.name)
            wrapper = injections.get(key)
            if wrapper is None:
                allowed = subsorts.get(target.name)
                if allowed is None:
                    allowed = subsorts[target.name] = definition.subsorts(target)
                if actual not in allowed:
                    raise ValueError(f'Sort {actual.name} is not a subsort of {target.name}: {node}')
                wrapper = injections[key] = f'inj{{Sort{actual.name}{{}}, Sort{target.name}{{}}}}('
            chunks.append(wrapper)

        if isinstance(node, KToken):
            token_key = (actual.name, node.token)
            text = tokens.get(token_key)
            if text is None:
                text = tokens[token_key] = _ktoken_to_kore(node).text
            chunks.append(text)
            if inject:
                chunks.append(')')
            continue

        opener = openers.get(label)
        if opener is None:
            params = ', '.join(f'Sort{p.name}{{}}' for p in label.params)
            opener = openers[label] = f'{_label_to_kore(label.name)}{{{params}}}('
        chunks.append(opener)

        # Pushed in reverse so arguments come off the stack left to right, followed by the
        # closing paren of this application and of its injection wrapper, if any.
        if inject:
            stack.append((')', None))
        stack.append((')', None))
        arguments = node.args
        for index in range(len(arguments) - 1, -1, -1):
            stack.append((arguments[index], argument_sorts[index]))
            if index:
                stack.append((', ', None))

    return ''.join(chunks)

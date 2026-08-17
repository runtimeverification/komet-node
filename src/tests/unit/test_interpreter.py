"""Unit tests for the interpreter's pure helpers.

``NodeInterpreter.run`` hands ``state.kore`` to the LLVM interpreter as a file path and
writes its stdout straight back, so the world state never becomes a Python ``Pattern``.
The two things that still need Python are covered here: splicing the ``<program>`` cell
textually (for wasm uploads), and deriving the cache key that lets a module's KORE be
reused instead of re-converted.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from komet.kast.syntax import set_exit_code, upload_wasm
from pyk.kast.inner import KApply
from pyk.kast.prelude.utils import token

from komet_node.errors import NodeInterpreterError
from komet_node.interpreter import EMPTY_PROGRAM_KORE, splice_program, upload_steps_cache_key

if TYPE_CHECKING:
    from pyk.kast.inner import KInner

# A stand-in for a serialized configuration: the empty <program> cell surrounded by
# unrelated cells. The real thing is megabytes of the same shape.
_CONFIG = f"Lbl'-LT-'generatedTop'-GT-'{{}}({EMPTY_PROGRAM_KORE}, Lbl'-LT-'k'-GT-'{{}}(dotk{{}}()))"


# ---------------------------------------------------------------------------
# splice_program
# ---------------------------------------------------------------------------


def test_splice_program_replaces_the_empty_program_cell() -> None:
    spliced = splice_program(_CONFIG, 'STEPS')

    assert "Lbl'-LT-'program'-GT-'{}(STEPS)" in spliced
    # The idle .Steps token is gone; nothing else about the configuration moved.
    assert EMPTY_PROGRAM_KORE not in spliced
    assert "Lbl'-LT-'k'-GT-'{}(dotk{}())" in spliced


def test_splice_program_leaves_the_rest_of_the_configuration_byte_identical() -> None:
    spliced = splice_program(_CONFIG, 'STEPS')

    # Splicing is a single substitution: undoing it must recover the original exactly.
    assert spliced.replace("Lbl'-LT-'program'-GT-'{}(STEPS)", EMPTY_PROGRAM_KORE) == _CONFIG


def test_splice_program_rejects_a_configuration_with_no_empty_program_cell() -> None:
    # A configuration whose <program> cell is not the idle .Steps cannot be spliced into
    # blindly — silently returning it unchanged would drop the uploaded module.
    with pytest.raises(NodeInterpreterError):
        splice_program("Lbl'-LT-'k'-GT-'{}(dotk{}())", 'STEPS')


def test_splice_program_rejects_an_ambiguous_configuration() -> None:
    # Two candidate sites means we cannot tell which one is the real <program> cell.
    with pytest.raises(NodeInterpreterError):
        splice_program(_CONFIG + _CONFIG, 'STEPS')


# ---------------------------------------------------------------------------
# upload_steps_cache_key
# ---------------------------------------------------------------------------


def _upload(wasm_hash: bytes, module: str = 'module') -> KInner:
    return upload_wasm(wasm_hash, KApply(module))


def test_upload_steps_cache_key_is_stable_for_the_same_wasm() -> None:
    assert upload_steps_cache_key([_upload(b'\x01\x02')]) == upload_steps_cache_key([_upload(b'\x01\x02')])


def test_upload_steps_cache_key_distinguishes_different_wasm() -> None:
    assert upload_steps_cache_key([_upload(b'\x01\x02')]) != upload_steps_cache_key([_upload(b'\x03\x04')])


def test_upload_steps_cache_key_distinguishes_order() -> None:
    a, b = _upload(b'\x01'), _upload(b'\x02')

    assert upload_steps_cache_key([a, b]) != upload_steps_cache_key([b, a])


def test_upload_steps_cache_key_distinguishes_count() -> None:
    a = _upload(b'\x01')

    assert upload_steps_cache_key([a]) != upload_steps_cache_key([a, a])


def test_upload_steps_cache_key_declines_non_upload_steps() -> None:
    # Only uploadWasm steps are content-addressed by their first argument; anything else
    # must not be cached, since we have no key that determines its KORE.
    assert upload_steps_cache_key([set_exit_code(0)]) is None


def test_upload_steps_cache_key_declines_a_mixed_step_list() -> None:
    assert upload_steps_cache_key([_upload(b'\x01'), set_exit_code(0)]) is None


def test_upload_steps_cache_key_declines_a_malformed_upload() -> None:
    # A hash argument that is not a literal token gives us nothing to key on.
    assert upload_steps_cache_key([KApply('uploadWasm', [KApply('notAToken'), KApply('module')])]) is None


def test_upload_steps_cache_key_declines_an_empty_step_list() -> None:
    assert upload_steps_cache_key([]) is None


def test_upload_steps_cache_key_ignores_the_module_argument() -> None:
    """The key is the declared wasm hash, because the module is a function of it.

    ``TransactionEncoder._upload_steps`` builds every step as
    ``upload_wasm(sha256(wasm), wasm2kast(wasm))`` — both arguments derive from the same
    bytes, so the hash alone determines the whole step. This test pins the assumption; the
    integration test ``test_upload_step_hash_is_the_wasm_content_hash`` checks that the
    encoder really does construct steps that way.
    """
    assert upload_steps_cache_key([_upload(b'\x01', 'moduleA')]) == upload_steps_cache_key(
        [_upload(b'\x01', 'moduleB')]
    )


def test_upload_steps_cache_key_is_filename_safe() -> None:
    key = upload_steps_cache_key([_upload(b'\x00\xff/\\')])

    assert key is not None
    assert key.isalnum()


def test_token_shape_assumption() -> None:
    """`upload_wasm` puts the hash in a KToken, which is what the key reads."""
    step = upload_wasm(b'\x01\x02', KApply('module'))

    assert isinstance(step, KApply)
    assert step.args[0] == token(b'\x01\x02')

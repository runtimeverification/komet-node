"""Interpreter-level integration tests.

The full contract lifecycle (create account -> upload wasm -> deploy -> invoke) is covered
end-to-end through the HTTP server in ``test_server.py`` (``test_full_lifecycle_over_http``).
This module holds the lower-level checks that drive ``NodeInterpreter`` directly.
"""

from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING

from komet.kast.syntax import steps_of, upload_wasm
from pyk.kast.inner import KApply, KSort
from pyk.kast.prelude.utils import token
from pyk.konvert import kast_to_kore
from pyk.kore.parser import KoreParser
from pyk.kore.syntax import App
from pykwasm.wasm2kast import wasm2kast
from stellar_sdk import Account, TransactionBuilder
from stellar_sdk.utils import sha256

from komet_node.interpreter import EMPTY_PROGRAM_KORE, NodeInterpreter, splice_program

from .conftest import PASSPHRASE, wat_to_wasm

if TYPE_CHECKING:
    import pytest
    from pyk.kast.inner import KInner
    from pyk.kore.syntax import Pattern

EMPTY_CONTRACT_WAT = (Path(__file__).parent / 'data' / 'wasm' / 'empty.wat').resolve(strict=True)
ADDER_CONTRACT_WAT = (Path(__file__).parent / 'data' / 'wasm' / 'adder.wat').resolve(strict=True)

# The reference implementation of the <program> edit: parse the whole configuration and
# replace the cell's child. This is what `splice_program` has to agree with, and what it
# replaced in production — correct, but its cost scales with the whole world state.
_PROGRAM_CELL = "Lbl'-LT-'program'-GT-'"


def _set_cell(pattern: Pattern, cell_symbol: str, value: Pattern) -> Pattern:
    if isinstance(pattern, App):
        if pattern.symbol == cell_symbol:
            return App(pattern.symbol, pattern.sorts, (value,))
        return App(pattern.symbol, pattern.sorts, tuple(_set_cell(arg, cell_symbol, value) for arg in pattern.args))
    return pattern


def test_empty_config_ignores_stray_request_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """empty_config() must produce a clean idle state regardless of the process cwd.

    The idle config's empty <k>/<program> cells are the precondition for the
    insert-handleRequestFile rule, so a stray request.json in the cwd could otherwise hijack
    idle-config generation. empty_config() isolates itself in a temp dir to prevent this.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / 'request.json').write_text(json.dumps({'method': 'getHealth', 'id': 1, 'now': '1700000000'}))

    config = NodeInterpreter().empty_config()

    # The stray request is left untouched and no response is produced — the run was isolated.
    assert (tmp_path / 'request.json').exists()
    assert not (tmp_path / 'response.json').exists()
    assert 'healthy' not in config


# ---------------------------------------------------------------------------
# The <program> splice
#
# ``run`` never parses the world state: for a wasm upload it puts the module into the
# <program> cell by substituting text, and for everything else it hands ``state.kore`` to
# the interpreter untouched. These tests check the splice against the real idle
# configuration, and against what a whole-configuration KORE edit would have produced.
# ---------------------------------------------------------------------------


def _upload_steps_kore(interpreter: NodeInterpreter, wasm: bytes) -> str:
    steps = [upload_wasm(sha256(wasm), wasm2kast(BytesIO(wasm)))]
    return kast_to_kore(interpreter.definition.kdefinition, steps_of(steps), KSort('Steps')).text


def test_idle_config_has_exactly_one_empty_program_cell() -> None:
    """The premise of the textual splice, checked against the real idle configuration.

    ``state.kore`` is always saved in the idle state, whose <program> cell holds ``.Steps``.
    If the serializer ever renders that cell differently, the splice must fail loudly rather
    than silently drop an uploaded module — so pin the exact marker here.
    """
    config = NodeInterpreter().empty_config()

    assert config.count(EMPTY_PROGRAM_KORE) == 1


def test_splice_program_matches_a_whole_configuration_kore_edit(tmp_path: Path) -> None:
    """Splicing text must produce the same KORE term as editing the parsed configuration.

    This is the correctness claim the fast path rests on: the cheap substitution and the
    expensive parse-edit-serialize round trip are the same edit.
    """
    interpreter = NodeInterpreter()
    config = interpreter.empty_config()
    steps_kore = _upload_steps_kore(interpreter, wat_to_wasm(EMPTY_CONTRACT_WAT))

    spliced = KoreParser(splice_program(config, steps_kore)).pattern()
    reference = _set_cell(KoreParser(config).pattern(), _PROGRAM_CELL, KoreParser(steps_kore).pattern())

    assert spliced == reference


def test_upload_step_hash_is_the_wasm_content_hash() -> None:
    """The invariant the module cache is keyed on, checked against the encoder.

    ``upload_steps_cache_key`` keys on the step's declared hash alone, which is only sound
    because the encoder derives both the hash and the module from the same bytes. Checked
    here against the encoder's own output rather than a hand-built step.
    """
    from komet_node.transaction import TransactionEncoder

    wasm = wat_to_wasm(EMPTY_CONTRACT_WAT)
    account = Account('GDIIXPI2CDPBXRI3WEF7UPVZEOBZRMI2ZQASKYDLN5ENWYS73OSG6FKO', sequence=0)
    builder = TransactionBuilder(account, PASSPHRASE).append_upload_contract_wasm_op(wasm)
    transaction = builder.set_timeout(30).build().transaction

    steps, uploaded = TransactionEncoder(PASSPHRASE)._upload_steps(transaction)

    (step,) = steps
    assert isinstance(step, KApply)
    assert step.label.name == 'uploadWasm'
    assert step.args[0] == token(sha256(wasm))
    assert uploaded == {sha256(wasm).hex(): wasm}


# ---------------------------------------------------------------------------
# The module-KORE cache
# ---------------------------------------------------------------------------


def test_upload_steps_kore_is_cached_across_interpreters(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Converting a module to KORE is the one remaining term-scale Python cost.

    It is a pure function of the wasm bytes, so a second upload of the same contract — the
    common case when a debug session is relaunched — must read the cache instead of
    converting again.
    """
    monkeypatch.setenv('KOMET_NODE_CACHE_DIR', str(tmp_path / 'cache'))
    wasm = wat_to_wasm(ADDER_CONTRACT_WAT)
    steps = [upload_wasm(sha256(wasm), wasm2kast(BytesIO(wasm)))]

    first = NodeInterpreter().steps_kore_text(steps)

    conversions = 0
    real = NodeInterpreter._convert_steps

    def counting(self: NodeInterpreter, steps: list[KInner]) -> str:
        nonlocal conversions
        conversions += 1
        return real(self, steps)

    monkeypatch.setattr(NodeInterpreter, '_convert_steps', counting)
    second = NodeInterpreter().steps_kore_text(steps)

    assert second == first
    assert conversions == 0, 'the second conversion should have come from the cache'


def test_upload_steps_cache_is_keyed_to_the_definition(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A rebuilt semantics must not be served stale KORE from a previous build."""
    monkeypatch.setenv('KOMET_NODE_CACHE_DIR', str(tmp_path / 'cache'))
    wasm = wat_to_wasm(ADDER_CONTRACT_WAT)
    steps = [upload_wasm(sha256(wasm), wasm2kast(BytesIO(wasm)))]

    interpreter = NodeInterpreter()
    interpreter.steps_kore_text(steps)
    cached = list((tmp_path / 'cache').iterdir())
    assert len(cached) == 1

    monkeypatch.setattr(NodeInterpreter, '_definition_stamp', property(lambda self: 'a-different-build'))
    NodeInterpreter().steps_kore_text(steps)

    assert len(list((tmp_path / 'cache').iterdir())) == 2


def test_upload_steps_cache_survives_a_corrupt_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A truncated or empty cache file must be recomputed, not served."""
    monkeypatch.setenv('KOMET_NODE_CACHE_DIR', str(tmp_path / 'cache'))
    wasm = wat_to_wasm(ADDER_CONTRACT_WAT)
    steps = [upload_wasm(sha256(wasm), wasm2kast(BytesIO(wasm)))]

    expected = NodeInterpreter().steps_kore_text(steps)
    (entry,) = (tmp_path / 'cache').iterdir()
    entry.write_text('')

    assert NodeInterpreter().steps_kore_text(steps) == expected

from __future__ import annotations

import json
import tempfile
from subprocess import CalledProcessError
from typing import TYPE_CHECKING, Final

from komet.kast.syntax import steps_of
from pyk.kast.inner import KSort
from pyk.konvert import kast_to_kore
from pyk.kore.parser import KoreParser
from pyk.kore.prelude import SORT_K_ITEM, inj, int_dv, str_dv, top_cell_initializer
from pyk.kore.syntax import App, SortApp
from pyk.utils import check_file_path, run_process_2

from .utils import simbolik_definition

if TYPE_CHECKING:
    from pathlib import Path
    from typing import Any

    from pyk.kast.inner import KInner
    from pyk.kore.syntax import Pattern

    from .utils import SimbolikDefinition


def _llvm_interpret(definition_dir: Path, pattern: Pattern, *, cwd: str | Path | None = None) -> Pattern:
    """Run the LLVM interpreter binary on a KORE pattern, optionally in ``cwd``.

    This mirrors pyk's ``llvm_interpret`` but runs the interpreter *subprocess* with its
    working directory set to ``cwd`` (rather than ``os.chdir``-ing this process). The K
    file-system hooks resolve their relative paths against the subprocess cwd, so the io-dir
    files are found without mutating the parent process's global cwd — which would otherwise
    race other threads (e.g. the server runs in a background thread in the tests).

    The interpreter is run with ``check=True``: both a successful request and a failed
    (stuck) transaction exit 0 — failure is signalled by the absence of ``response.json``,
    not by the exit code — so a non-zero exit can only mean a genuine interpreter error,
    which we surface rather than silently parsing whatever it emitted.
    """
    interpreter_file = definition_dir / 'interpreter'
    check_file_path(interpreter_file)
    args = [str(interpreter_file), '/dev/stdin', '-1', '/dev/stdout']
    try:
        res = run_process_2(args, input=pattern.text, cwd=cwd, check=True)
    except CalledProcessError as err:
        raise NodeInterpreterError(f'Interpreter failed with status {err.returncode}: {err.stderr}', err) from err
    if not res.stdout:
        raise NodeInterpreterError(f'Interpreter produced no output: {res.stderr}', res)
    return KoreParser(res.stdout).pattern()


# KORE building blocks, used to construct the initial configuration and the <program>
# cell directly in KORE — this avoids the multi-second, configuration-size-scaling
# kast<->kore round-trips that whole-config conversions incur.
_SORT_STEPS: Final = SortApp('SortSteps')
_SORT_STRING: Final = SortApp('SortString')
_PROGRAM_CELL: Final = "Lbl'-LT-'program'-GT-'"
_DOT_STEPS: Final = App("Lbl'Stop'List'LBraQuot'kasmerSteps'QuotRBra'")


def _steps_kore(steps: tuple[Pattern, ...]) -> Pattern:
    """Build a KORE ``Steps`` term (a ``kasmerSteps`` cons list) from step patterns."""
    result: Pattern = _DOT_STEPS
    for step in reversed(steps):
        result = App('LblkasmerSteps', (), (step, result))
    return result


def _set_cell(pattern: Pattern, cell_symbol: str, value: Pattern) -> Pattern:
    """Replace the (single) child of the named cell in a KORE configuration pattern."""
    if isinstance(pattern, App):
        if pattern.symbol == cell_symbol:
            return App(pattern.symbol, pattern.sorts, (value,))
        return App(pattern.symbol, pattern.sorts, tuple(_set_cell(arg, cell_symbol, value) for arg in pattern.args))
    return pattern


class NodeInterpreter:
    """
    Runs the K node semantics against a saved KORE world-state configuration.

    Its sole responsibility is K execution: it builds the initial configuration, runs RPC
    request envelopes through the LLVM interpreter, and persists the resulting state. It
    knows nothing about Stellar — XDR decoding lives in :class:`TransactionEncoder`, and RPC
    dispatch / bookkeeping / response formatting live in ``node.md``.

    The world state (accounts, contracts, uploaded wasm) round-trips through the KORE
    configuration (``state.kore``); the RPC bookkeeping (per-transaction receipts, ledger
    counter) is persisted as files in the working directory, read and written by the semantics.
    """

    definition: SimbolikDefinition

    def __init__(self) -> None:
        self.definition = simbolik_definition()

    def empty_config(self) -> str:
        """Return the initial idle K configuration as KORE.

        Built entirely in KORE (no kast conversion, no krun subprocess): the configuration
        is seeded with ``$PGM = setExitCode(0)`` and an empty ``$TRACE``, then run to its
        idle state by the LLVM interpreter.

        The run happens in an isolated empty directory: the idle config ends with empty
        ``<k>``/``<instrs>``/``<program>`` cells, which is exactly the precondition for the
        ``insert-handleRequestFile`` rule. Were a stray ``request.json`` present in the
        process's cwd, that rule would fire and dispatch it instead of stopping at the idle
        state — corrupting the configuration we are about to persist as ``state.kore``.
        """
        config = top_cell_initializer(
            {
                '$PGM': inj(_SORT_STEPS, SORT_K_ITEM, _steps_kore((App('LblsetExitCode', (), (int_dv(0),)),))),
                '$TRACE': inj(_SORT_STRING, SORT_K_ITEM, str_dv('')),
            }
        )
        with tempfile.TemporaryDirectory() as isolated_dir:
            return _llvm_interpret(self.definition.path, config, cwd=isolated_dir).text

    def run(
        self,
        state_file: Path,
        io_dir: Path,
        request: dict[str, Any],
        program_steps: list[KInner] | None = None,
    ) -> str | None:
        """
        Run a single RPC request envelope against the saved KORE configuration.

        Writes ``request.json`` into ``io_dir``, runs the LLVM interpreter on the current
        ``state.kore`` (with the interpreter subprocess's working directory set to ``io_dir``
        so the K file-system hooks resolve the relative paths), and returns the contents of
        ``response.json``.

        On success the node writes ``response.json`` and removes ``request.json``; we then
        persist the new configuration to ``state.kore``. If ``response.json`` was not
        produced the request got stuck (a failed transaction) — we keep the previous
        ``state.kore`` and return ``None`` so the caller can synthesise a failure response.
        """
        state_file = state_file.resolve()
        io_dir = io_dir.resolve()

        (io_dir / 'request.json').write_text(json.dumps(request))
        response_file = io_dir / 'response.json'
        if response_file.exists():
            response_file.unlink()

        pattern = KoreParser(state_file.read_text()).pattern()
        if program_steps:
            pattern = self._inject_program(pattern, program_steps)

        result = _llvm_interpret(self.definition.path, pattern, cwd=io_dir)

        if response_file.exists():
            state_file.write_text(result.text)
            return response_file.read_text()
        return None

    def _inject_program(self, pattern: Pattern, steps: list[KInner]) -> Pattern:
        """Embed kasmer steps into the ``<program>`` cell of a KORE configuration.

        Used for transactions that upload wasm: the resulting ``ModuleDecl`` cannot be
        JSON-encoded, so the steps are injected directly into the configuration.

        We convert only the (small) steps term to KORE and splice it into the ``<program>``
        cell of the already-parsed configuration. We deliberately avoid a whole-config
        ``kore_to_kast``/``kast_to_kore`` round-trip, whose cost scales with the (ever
        growing) configuration size. The remaining ``kast_to_kore`` here is bounded by the
        size of the uploaded wasm module — the one thing that can only originate as KAST
        (``wasm2kast``), since the semantics have no wasm binary decoder — and is
        independent of the accumulated world state.
        """
        steps_kore = kast_to_kore(self.definition.kdefinition, steps_of(steps), KSort('Steps'))
        return _set_cell(pattern, _PROGRAM_CELL, steps_kore)


class NodeInterpreterError(RuntimeError):
    pass

from __future__ import annotations

import json
import os
import tempfile
from hashlib import sha256
from pathlib import Path
from subprocess import CalledProcessError
from typing import TYPE_CHECKING, Final

from komet.kast.syntax import steps_of
from pyk.kast.inner import KApply, KSort, KToken
from pyk.konvert import kast_to_kore
from pyk.kore.prelude import SORT_K_ITEM, inj, int_dv, str_dv, top_cell_initializer
from pyk.kore.syntax import App, SortApp
from pyk.utils import check_file_path, run_process_2

from .errors import NodeInterpreterError
from .interfaces import Interpreter
from .utils import simbolik_definition

if TYPE_CHECKING:
    from collections.abc import Mapping
    from typing import Any

    from pyk.kast.inner import KInner
    from pyk.kore.syntax import Pattern

    from .utils import SimbolikDefinition


def _run_interpreter(definition_dir: Path, config: Path | str, *, cwd: str | Path | None = None) -> str:
    """Run the LLVM interpreter binary and return its output configuration as KORE text.

    This mirrors pyk's ``llvm_interpret`` but differs from it in two ways.

    It runs the interpreter *subprocess* with its working directory set to ``cwd`` (rather
    than ``os.chdir``-ing this process). The K file-system hooks resolve their relative paths
    against the subprocess cwd, so the io-dir files are found without mutating the parent
    process's global cwd — which would otherwise race other threads (e.g. the server runs in
    a background thread in the tests).

    And it exchanges KORE as *text*, never as a parsed ``Pattern``. A ``Path`` config is
    handed to the interpreter to read itself; only a ``str`` config is fed on stdin. Parsing
    the world state into Python objects and immediately re-serializing it dominated every
    request — pyk's KORE parser needed ~1.8s for a 3MB state where the interpreter needs
    ~0.5s, and it was paid twice per call (once in, once out) no matter how trivial the
    request. Nothing here inspects the configuration, so nothing here parses it.

    The interpreter is run with ``check=True``: both a successful request and a failed
    (stuck) transaction exit 0 — failure is signalled by the absence of ``response.json``,
    not by the exit code — so a non-zero exit can only mean a genuine interpreter error,
    which we surface rather than silently returning whatever it emitted.
    """
    interpreter_file = definition_dir / 'interpreter'
    check_file_path(interpreter_file)
    config_arg = str(config) if isinstance(config, Path) else '/dev/stdin'
    args = [str(interpreter_file), config_arg, '-1', '/dev/stdout']
    try:
        res = run_process_2(args, input=None if isinstance(config, Path) else config, cwd=cwd, check=True)
    except CalledProcessError as err:
        raise NodeInterpreterError(f'Interpreter failed with status {err.returncode}: {err.stderr}', err) from err
    if not res.stdout:
        raise NodeInterpreterError(f'Interpreter produced no output: {res.stderr}', res)
    return res.stdout


# KORE building blocks, used to construct the initial configuration directly in KORE — this
# avoids the multi-second, configuration-size-scaling kast<->kore round-trips that
# whole-config conversions incur.
_SORT_STEPS: Final = SortApp('SortSteps')
_SORT_STRING: Final = SortApp('SortString')
_DOT_STEPS: Final = App("Lbl'Stop'List'LBraQuot'kasmerSteps'QuotRBra'")

# The serialized form of an idle ``<program>`` cell: the cell wrapping the empty
# ``kasmerSteps`` list. ``state.kore`` is only ever saved in the idle state, so this appears
# in it exactly once, which is what makes the textual splice below unambiguous.
EMPTY_PROGRAM_KORE: Final = "Lbl'-LT-'program'-GT-'{}(Lbl'Stop'List'LBraQuot'kasmerSteps'QuotRBra'{}())"


def _steps_kore(steps: tuple[Pattern, ...]) -> Pattern:
    """Build a KORE ``Steps`` term (a ``kasmerSteps`` cons list) from step patterns."""
    result: Pattern = _DOT_STEPS
    for step in reversed(steps):
        result = App('LblkasmerSteps', (), (step, result))
    return result


def splice_program(config_text: str, steps_kore: str) -> str:
    """Put ``steps_kore`` into the ``<program>`` cell of a serialized configuration.

    A textual substitution rather than a parse-edit-serialize round trip: the cost of the
    latter scales with the whole accumulated world state, while this scales with a single
    scan. It is unambiguous because a saved configuration is always idle, and an idle
    ``<program>`` cell is exactly :data:`EMPTY_PROGRAM_KORE`.

    Anything else is an error rather than a no-op — returning the configuration unspliced
    would silently drop the uploaded module and leave the transaction to fail obscurely.
    """
    occurrences = config_text.count(EMPTY_PROGRAM_KORE)
    if occurrences != 1:
        raise NodeInterpreterError(
            f'Expected exactly one idle <program> cell in the configuration, found {occurrences}. '
            'The configuration is not in the idle state, or the serializer changed.'
        )
    return config_text.replace(EMPTY_PROGRAM_KORE, f"Lbl'-LT-'program'-GT-'{{}}({steps_kore})")


def upload_steps_cache_key(steps: list[KInner]) -> str | None:
    """A content-address for an all-``uploadWasm`` step list, or ``None`` if it is not one.

    ``TransactionEncoder._upload_steps`` builds each step as
    ``upload_wasm(sha256(wasm), wasm2kast(wasm))``, so both arguments derive from the same
    bytes and the declared hash alone determines the whole step — and therefore the KORE it
    converts to. Any other kind of step has no such key, so it is never cached.
    """
    if not steps:
        return None
    hashes = []
    for step in steps:
        if not isinstance(step, KApply) or step.label.name != 'uploadWasm' or len(step.args) != 2:
            return None
        wasm_hash = step.args[0]
        if not isinstance(wasm_hash, KToken):
            return None
        hashes.append(wasm_hash.token)
    return sha256('\x00'.join(hashes).encode()).hexdigest()


class NodeInterpreter(Interpreter):
    """
    Runs the K node semantics against a saved KORE world-state configuration.

    Its sole responsibility is K execution: it builds the initial configuration, runs RPC
    request envelopes through the LLVM interpreter, and persists the resulting state. It
    knows nothing about Stellar — XDR decoding lives in :class:`TransactionEncoder`, and RPC
    dispatch / bookkeeping / response formatting live in ``node.md``.

    The world state (accounts, contracts, uploaded wasm) round-trips through the KORE
    configuration (``state.kore``); the RPC bookkeeping (per-transaction receipts, ledger
    counter) is persisted as files in the working directory, read and written by the semantics.

    That round trip happens entirely as *text*: ``state.kore`` is handed to the interpreter
    as a file path and its output is written straight back. The world state is never parsed
    into Python, so the per-request cost no longer scales with how much contract code the
    chain has accumulated.
    """

    definition: SimbolikDefinition

    def __init__(self) -> None:
        self.definition = simbolik_definition()

    # ------------------------------------------------------------------
    # Module KORE cache
    #
    # Converting an uploaded module to KORE is the one remaining Python cost that scales
    # with the size of a contract, and it is a pure function of the wasm bytes. Caching it
    # on disk makes re-uploading an unchanged contract — what every debug-session relaunch
    # does — a file read instead of a fresh sort-inference pass over the whole module.
    # ------------------------------------------------------------------

    @property
    def _definition_stamp(self) -> str:
        """Identity of the compiled semantics, so a rebuild cannot be served stale KORE."""
        compiled = self.definition.path / 'compiled.json'
        stat = compiled.stat()
        return sha256(f'{compiled}:{stat.st_mtime_ns}:{stat.st_size}'.encode()).hexdigest()[:16]

    @property
    def _cache_dir(self) -> Path:
        """Where cached module KORE lives.

        Deliberately outside the io-dir: that is a fresh temporary directory per debug
        session, so a cache inside it would never see a second hit.
        """
        override = os.environ.get('KOMET_NODE_CACHE_DIR')
        if override:
            return Path(override)
        xdg = os.environ.get('XDG_CACHE_HOME')
        return (Path(xdg) if xdg else Path.home() / '.cache') / 'komet-node' / 'steps'

    def steps_kore_text(self, steps: list[KInner]) -> str:
        """The KORE text for kasmer ``steps``, converted only if not already cached."""
        key = upload_steps_cache_key(steps)
        if key is None:
            return self._convert_steps(steps)
        entry = self._cache_dir / f'{self._definition_stamp}-{key}.kore'
        try:
            cached = entry.read_text()
        except OSError:
            cached = ''
        if cached:
            return cached
        text = self._convert_steps(steps)
        self._write_cache_entry(entry, text)
        return text

    def _convert_steps(self, steps: list[KInner]) -> str:
        return kast_to_kore(self.definition.kdefinition, steps_of(steps), KSort('Steps')).text

    @staticmethod
    def _write_cache_entry(entry: Path, text: str) -> None:
        """Populate a cache entry atomically. Failing to cache must never fail the run."""
        try:
            entry.parent.mkdir(parents=True, exist_ok=True)
            tmp = entry.with_name(f'{entry.name}.{os.getpid()}.tmp')
            tmp.write_text(text)
            tmp.replace(entry)
        except OSError:
            pass

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
            return _run_interpreter(self.definition.path, config.text, cwd=isolated_dir)

    def run(
        self,
        state_file: Path,
        io_dir: Path,
        request: Mapping[str, Any],
        program_steps: list[KInner] | None = None,
        *,
        commit: bool = True,
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

        With ``commit=False`` the resulting configuration is discarded even on success:
        the run executes against the current state but never writes ``state.kore`` back.
        This is what makes ``simulateTransaction`` a dry run.

        The state file itself is handed to the interpreter, and its output written straight
        back, so a request that needs no configuration edit costs no configuration parse.
        """
        state_file = state_file.resolve()
        io_dir = io_dir.resolve()

        (io_dir / 'request.json').write_text(json.dumps(request))
        response_file = io_dir / 'response.json'
        if response_file.exists():
            response_file.unlink()

        if program_steps:
            result = self._run_with_program(state_file, io_dir, program_steps)
        else:
            result = _run_interpreter(self.definition.path, state_file, cwd=io_dir)

        if response_file.exists():
            if commit:
                state_file.write_text(result)
            return response_file.read_text()
        return None

    def _run_with_program(self, state_file: Path, io_dir: Path, steps: list[KInner]) -> str:
        """Run with kasmer ``steps`` embedded in the ``<program>`` cell.

        Used for transactions that upload wasm: the resulting ``ModuleDecl`` cannot be
        JSON-encoded, so it cannot ride in ``request.json`` like every other request's
        operations and has to go into the configuration instead.

        Only the steps are converted to KORE (cached by wasm hash, since that conversion is
        the expensive part); splicing them into the configuration is textual, so the cost
        stays bounded by the uploaded module rather than by the accumulated world state. The
        spliced configuration goes to a temporary file — not into the io-dir, which may sit
        on a slow shared mount.
        """
        spliced = splice_program(state_file.read_text(), self.steps_kore_text(steps))
        handle, name = tempfile.mkstemp(suffix='.kore')
        config_file = Path(name)
        try:
            with os.fdopen(handle, 'w') as f:
                f.write(spliced)
            return _run_interpreter(self.definition.path, config_file, cwd=io_dir)
        finally:
            config_file.unlink(missing_ok=True)

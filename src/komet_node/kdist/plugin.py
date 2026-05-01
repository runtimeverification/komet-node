from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from komet.kdist.plugin import KompileTarget
from pyk.ktool.kompile import kompile

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from typing import Any, Final


NODE_KSRC_DIR: Final = Path(__file__).parent.resolve(strict=True)


class NodeKompileTarget(KompileTarget):
    _kompile_args: Callable[[Path], Mapping[str, Any]]

    def __init__(self, kompile_args: Callable[[Path], Mapping[str, Any]]):
        self._kompile_args = kompile_args

    def build(self, output_dir: Path, deps: dict[str, Path], args: dict[str, Any], verbose: bool) -> None:
        kompile_args = self._kompile_args(deps['soroban-semantics.source'])
        kompile(output_dir=output_dir, verbose=verbose, **kompile_args)

    def deps(self) -> tuple[str]:
        return super().deps()

    def source(self):
        return (NODE_KSRC_DIR,)


__TARGETS__: Final = {
    'simbolik': NodeKompileTarget(
        lambda soroban_src_dir: {
            'backend': 'llvm',
            'main_file': NODE_KSRC_DIR / 'node.md',
            'syntax_module': 'NODE-SYNTAX',
            'include_dirs': [soroban_src_dir, NODE_KSRC_DIR],
            'md_selector': 'k | k-tracing',
            'warnings_to_errors': True,
        },
    ),
}

from __future__ import annotations

import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from komet.utils import SorobanDefinition
from pyk.kdist import kdist

if TYPE_CHECKING:
    from collections.abc import Generator


@contextmanager
def temp_working_directory() -> Generator[Path, None, None]:
    original = os.getcwd()
    with tempfile.TemporaryDirectory() as tmp_dir:
        try:
            os.chdir(tmp_dir)
            yield Path(tmp_dir)
        finally:
            os.chdir(original)


class SimbolikDefinition(SorobanDefinition): ...


def simbolik_definition() -> SimbolikDefinition:
    return SimbolikDefinition(kdist.get('komet-node.simbolik'))

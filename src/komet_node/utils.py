from __future__ import annotations

from komet.utils import SorobanDefinition
from pyk.kdist import kdist


class SimbolikDefinition(SorobanDefinition): ...


def simbolik_definition() -> SimbolikDefinition:
    return SimbolikDefinition(kdist.get('komet-node.simbolik'))

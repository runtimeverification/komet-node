"""Interpreter-level integration tests.

The full contract lifecycle (create account -> upload wasm -> deploy -> invoke) is covered
end-to-end through the HTTP server in ``test_server.py`` (``test_full_lifecycle_over_http``).
This module holds the lower-level checks that drive ``NodeInterpreter`` directly.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from komet_node.hello import hello
from komet_node.interpreter import NodeInterpreter

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def test_hello() -> None:
    assert hello('World') == 'Hello, World!'


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

import pytest

from komet_node.hello import hello
from komet_node.interpreter import NodeInterpreterError
from komet_node.transaction import _xlm_to_stroops


def test_hello() -> None:
    assert hello('World') == 'Hello, World!'


def test_xlm_to_stroops_whole() -> None:
    assert _xlm_to_stroops('1000') == 10_000_000_000


def test_xlm_to_stroops_max_precision() -> None:
    # 7 decimal places is exactly one stroop — the finest representable amount
    assert _xlm_to_stroops('0.0000001') == 1


def test_xlm_to_stroops_rejects_sub_stroop() -> None:
    # 8 decimal places cannot be represented exactly; must be rejected, not truncated
    with pytest.raises(NodeInterpreterError):
        _xlm_to_stroops('0.00000001')

"""The exceptions komet-node raises internally.

Collected in one module so no layer has to reach into another purely for an error type
(the encoder, for instance, must not depend on the interpreter just to signal a bad input).

- :class:`NodeInterpreterError` — the K interpreter subprocess failed.
- :class:`TransactionEncodingError` — a Stellar transaction could not be encoded.
- :class:`RpcError` — a JSON-RPC error to hand back to the client.
"""

from __future__ import annotations


class NodeError(RuntimeError):
    """Base class for komet-node's own errors."""


class NodeInterpreterError(NodeError):
    """The K interpreter subprocess failed or produced no usable output."""


class TransactionEncodingError(NodeError):
    """A Stellar transaction could not be encoded into a node request envelope.

    Raised by :class:`~komet_node.transaction.TransactionEncoder` for inputs it cannot
    translate (sub-stroop XLM amounts, malformed strkey addresses). On the admission path
    the server catches it and turns it into a ``txMALFORMED`` status response.
    """


class RpcError(Exception):
    """A JSON-RPC error to return to the client, carrying its spec code and message.

    Raised by request validation and dispatch in the server so the envelope builders can
    return a single value type instead of threading a pre-formatted error string back
    through their return type. :meth:`~komet_node.server.StellarRpcServer.handle_rpc`
    catches it and formats the error envelope. The classmethods name the JSON-RPC 2.0
    error codes; ``invalid_params`` prepends the conventional ``Invalid params:`` prefix.
    """

    code: int
    message: str

    def __init__(self, code: int, message: str) -> None:
        super().__init__(code, message)
        self.code = code
        self.message = message

    @classmethod
    def invalid_request(cls, message: str) -> RpcError:
        return cls(-32600, message)

    @classmethod
    def invalid_params(cls, detail: str) -> RpcError:
        return cls(-32602, f'Invalid params: {detail}')

    @classmethod
    def method_not_found(cls, message: str = 'Method not found') -> RpcError:
        return cls(-32601, message)

    @classmethod
    def internal(cls, message: str = 'Internal error') -> RpcError:
        return cls(-32603, message)

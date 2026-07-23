from __future__ import annotations

import argparse
import tempfile
from pathlib import Path

from stellar_sdk import Network

from komet_node.interpreter import NodeInterpreter
from komet_node.server import StellarRpcServer
from komet_node.store import ChainStore
from komet_node.transaction import TransactionEncoder

_DESCRIPTION = 'Komet Node — a local Stellar testnet backed by the K semantics of Soroban.'

_EPILOG = """\
examples:
  komet-node                     serve on localhost:8000 in a fresh temp dir
  komet-node --port 9000         use a custom port
  komet-node --io-dir ./chain    keep all artifacts under ./chain (persistent)
  komet-node --host 0.0.0.0      accept connections from outside localhost
"""


def main() -> None:
    parser = argparse.ArgumentParser(
        prog='komet-node',
        description=_DESCRIPTION,
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('--host', default='localhost', help='bind address (default: localhost)')
    parser.add_argument('--port', type=int, default=8000, help='port to listen on (default: 8000)')
    parser.add_argument(
        '--io-dir',
        type=Path,
        default=None,
        help='directory for all input/output artifacts (default: a fresh temporary directory)',
    )
    args = parser.parse_args()

    server = build_server(io_dir=args.io_dir, host=args.host, port=args.port)
    server.serve()


def build_server(
    *,
    io_dir: Path | None = None,
    network_passphrase: str = Network.TESTNET_NETWORK_PASSPHRASE,
    host: str = 'localhost',
    port: int = 8000,
) -> StellarRpcServer:
    """Composition root: build the concrete collaborators and wire up the server.

    With no ``io_dir`` the chain runs against a fresh temporary directory — a throwaway chain
    that starts empty on every launch and leaves the working directory untouched.
    """
    resolved_io_dir = Path(tempfile.mkdtemp(prefix='komet-node-')) if io_dir is None else io_dir
    return StellarRpcServer(
        interpreter=NodeInterpreter(),
        encoder=TransactionEncoder(network_passphrase),
        store=ChainStore(resolved_io_dir),
        host=host,
        port=port,
    )


if __name__ == '__main__':
    main()

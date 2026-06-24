from __future__ import annotations

import argparse
from pathlib import Path

from stellar_sdk import Network

from komet_node.server import StellarRpcServer

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

    server = StellarRpcServer(
        host=args.host,
        port=args.port,
        io_dir=args.io_dir,
        network_passphrase=Network.TESTNET_NETWORK_PASSPHRASE,
    )
    server.serve()


if __name__ == '__main__':
    main()

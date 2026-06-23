from __future__ import annotations

import argparse
from pathlib import Path

from stellar_sdk import Network

from komet_node.server import StellarRpcServer


def main() -> None:
    parser = argparse.ArgumentParser(description='Komet Node — local Stellar testnet backed by K semantics')
    parser.add_argument('--host', default='localhost', help='Bind address (default: localhost)')
    parser.add_argument('--port', type=int, default=8000, help='Port to listen on (default: 8000)')
    parser.add_argument(
        '--state-file', type=Path, default=Path('state.kore'), help='State file path (default: state.kore)'
    )
    parser.add_argument(
        '--trace', action='store_true', default=False, help='Enable instruction-level execution tracing'
    )
    args = parser.parse_args()

    server = StellarRpcServer(
        host=args.host,
        port=args.port,
        state_file=args.state_file,
        network_passphrase=Network.TESTNET_NETWORK_PASSPHRASE,
        trace=args.trace,
    )
    server.serve()


if __name__ == '__main__':
    main()

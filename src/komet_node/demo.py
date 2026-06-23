"""
Komet Node demo — deploy a Soroban contract and invoke it through the K semantics.

See the link below for more details about Stellar RPC:
    https://developers.stellar.org/docs/data/apis/rpc/api-reference/methods/sendTransaction

Prerequisites:
  - K semantics compiled:  make kdist-build
  - wat2wasm on PATH:      apt install wabt  (or brew install wabt)

Usage:
  uv run python -m komet_node.demo <contract.wat> [--out-dir <dir>]

Example:
  uv run python -m komet_node.demo src/tests/integration/data/wasm/empty.wat

Output (written to --out-dir, default ./out):
  state.kore               latest blockchain state in KORE format (input for the next step)
  state_0_initial.pretty   K configuration after each step in human-readable format
  state_1_create_account.pretty
  state_2_upload_wasm.pretty
  state_3_create_contract.pretty
  state_4_call_foo.pretty
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

from stellar_sdk import Account, Keypair, Network, TransactionBuilder
from stellar_sdk.utils import sha256

from komet_node.server import StellarRpcServer


def wat_to_wasm(wat_path: Path) -> bytes:
    proc_res = subprocess.run(['wat2wasm', str(wat_path), '--output=/dev/stdout'], check=True, capture_output=True)
    return proc_res.stdout


def main(wasm_wat: Path, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    state_file = out_dir / 'state.kore'

    root_keypair = Keypair.random()
    root_account = Account(root_keypair.public_key, sequence=0)

    # The server owns the io_dir lifecycle (state.kore, metadata.json, transactions.json);
    # we drive it directly via handle_rpc, no HTTP server needed.
    server = StellarRpcServer(state_file=state_file)

    step = 0

    def save(label: str) -> None:
        nonlocal step
        pretty_file = out_dir / f'state_{step}_{label}.pretty'
        pretty_file.write_text(server.interpreter.pretty_print(state_file.read_text()))
        print(f'[{step}] {label} -> {pretty_file.name}')
        step += 1

    def run(builder: TransactionBuilder, label: str) -> None:
        envelope = builder.set_timeout(30).build()
        envelope.sign(root_keypair)
        server.handle_rpc('sendTransaction', {'transaction': envelope.to_xdr()})
        save(label)

    def builder() -> TransactionBuilder:
        return TransactionBuilder(root_account, Network.TESTNET_NETWORK_PASSPHRASE, base_fee=100)

    # 0. The server initialised state.kore to the idle configuration; record it.
    save('initial')

    # 1. Create root account
    run(
        builder().append_create_account_op(destination=root_keypair.public_key, starting_balance='1000'),
        'create_account',
    )

    # 2. Upload wasm bytecode to the ledger (stores code keyed by its sha256 hash)
    wasm_bytecode = wat_to_wasm(wasm_wat)
    run(builder().append_upload_contract_wasm_op(wasm_bytecode), 'upload_wasm')

    # 3. Deploy a contract instance from the uploaded wasm
    wasm_hash = sha256(wasm_bytecode)
    salt = b'\x00' * 32
    run(builder().append_create_contract_op(wasm_hash, root_keypair.public_key, None, salt), 'create_contract')

    # 4. Invoke foo() — takes no args, returns Void (i64 value 2 in Soroban encoding)
    contract_address = server.encoder.contract_address_from_deployer_address(root_keypair.public_key, salt)
    run(builder().append_invoke_contract_function_op(contract_address, 'foo', []), 'call_foo')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Deploy and invoke a Soroban contract through the Komet semantics.')
    parser.add_argument('wasm_wat', type=Path, help='Path to the .wat contract source file')
    parser.add_argument(
        '--out-dir', type=Path, default=Path('out'), help='Output directory for state files (default: ./out)'
    )
    args = parser.parse_args()
    main(args.wasm_wat, args.out_dir)

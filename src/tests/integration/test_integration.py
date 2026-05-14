import subprocess
from pathlib import Path

from stellar_sdk import Account, Keypair, Network, TransactionBuilder, xdr
from stellar_sdk.utils import sha256
from stellar_sdk.xdr.sc_val_type import SCValType

from komet_node.hello import hello
from komet_node.interpreter import NodeInterpreter

EMPTY_CONTRACT_WAT = (Path(__file__).parent / 'data' / 'wasm' / 'empty.wat').resolve(strict=True)
ARGS_CONTRACT_WAT = (Path(__file__).parent / 'data' / 'wasm' / 'args.wat').resolve(strict=True)


def test_hello() -> None:
    assert hello('World') == 'Hello, World!'


def wat_to_wasm(wat_path: Path) -> bytes:
    proc_res = subprocess.run(['wat2wasm', str(wat_path), '--output=/dev/stdout'], check=True, capture_output=True)
    return proc_res.stdout



def test_full_lifecycle(tmp_path: Path) -> None:
    """
    Full testnet lifecycle:
      1. Create root account
      2. Upload wasm compiled from empty.wat
      3. Deploy a contract instance from the uploaded wasm
      4. Call foo() on the deployed contract
    """
    root_keypair = Keypair.random()
    root_account = Account(root_keypair.public_key, sequence=0)

    interpreter = NodeInterpreter()
    state_file = tmp_path / 'state.kore'

    def run(tx: object) -> None:
        result = interpreter.run_transaction(state_file, tx)  # type: ignore[arg-type]
        state_file.write_text(result.final_kore)

    def builder() -> TransactionBuilder:
        return TransactionBuilder(root_account, Network.TESTNET_NETWORK_PASSPHRASE)

    # 0. Generate initial (empty) K configuration and save it as the starting state
    state_file.write_text(interpreter.empty_config())

    # 1. Create root account
    run(
        builder()
        .append_create_account_op(destination=root_keypair.public_key, starting_balance='1000')
        .set_timeout(30)
        .build()
        .transaction
    )

    # 2. Upload wasm bytecode to the ledger (stores code keyed by its sha256 hash)
    wasm_bytecode = wat_to_wasm(EMPTY_CONTRACT_WAT)
    run(builder().append_upload_contract_wasm_op(wasm_bytecode).set_timeout(30).build().transaction)

    # 3. Deploy a contract instance from the uploaded wasm
    wasm_hash = sha256(wasm_bytecode)
    salt = b'\x00' * 32
    run(
        builder()
        .append_create_contract_op(wasm_hash, root_keypair.public_key, None, salt)
        .set_timeout(30)
        .build()
        .transaction
    )

    # 4. Invoke foo() — takes no args, returns Void (i64 value 2 in Soroban encoding)
    contract_address = interpreter.contract_address_from_deployer_address(root_keypair.public_key, salt)
    run(builder().append_invoke_contract_function_op(contract_address, 'foo', []).set_timeout(30).build().transaction)


def test_callTx_with_args(tmp_path: Path) -> None:
    """
    Exercise the _encode_scval / #decodeArg pipeline for each supported SCVal type.
    Uses a minimal contract (args.wat) whose functions accept various arg types and return Void.
    Covers: bool, u32, i32, u64, i64, u128, i128, symbol.
    """
    root_keypair = Keypair.random()
    root_account = Account(root_keypair.public_key, sequence=0)

    interpreter = NodeInterpreter()
    state_file = tmp_path / 'state.kore'

    def run(tx: object) -> None:
        result = interpreter.run_transaction(state_file, tx)  # type: ignore[arg-type]
        state_file.write_text(result.final_kore)

    def builder() -> TransactionBuilder:
        return TransactionBuilder(root_account, Network.TESTNET_NETWORK_PASSPHRASE)

    state_file.write_text(interpreter.empty_config())

    run(builder().append_create_account_op(root_keypair.public_key, '1000').set_timeout(30).build().transaction)

    wasm_bytecode = wat_to_wasm(ARGS_CONTRACT_WAT)
    run(builder().append_upload_contract_wasm_op(wasm_bytecode).set_timeout(30).build().transaction)

    wasm_hash = sha256(wasm_bytecode)
    salt = b'\x00' * 32
    run(builder().append_create_contract_op(wasm_hash, root_keypair.public_key, None, salt).set_timeout(30).build().transaction)

    contract_address = interpreter.contract_address_from_deployer_address(root_keypair.public_key, salt)

    run(builder().append_invoke_contract_function_op(
        contract_address,
        'test_bool',
        [xdr.SCVal(type=SCValType.SCV_BOOL, b=True)],
    ).set_timeout(30).build().transaction)

    run(builder().append_invoke_contract_function_op(
        contract_address,
        'test_integers',
        [
            xdr.SCVal(type=SCValType.SCV_U32, u32=xdr.Uint32(42)),
            xdr.SCVal(type=SCValType.SCV_I32, i32=xdr.Int32(-7)),
            xdr.SCVal(type=SCValType.SCV_U64, u64=xdr.Uint64(100)),
            xdr.SCVal(type=SCValType.SCV_I64, i64=xdr.Int64(-200)),
        ],
    ).set_timeout(30).build().transaction)

    run(builder().append_invoke_contract_function_op(
        contract_address,
        'test_wide_integers',
        [
            xdr.SCVal(type=SCValType.SCV_U128, u128=xdr.UInt128Parts(hi=xdr.Uint64(0), lo=xdr.Uint64(999))),
            xdr.SCVal(type=SCValType.SCV_I128, i128=xdr.Int128Parts(hi=xdr.Int64(0), lo=xdr.Uint64(888))),
        ],
    ).set_timeout(30).build().transaction)

    run(builder().append_invoke_contract_function_op(
        contract_address,
        'test_symbol',
        [xdr.SCVal(type=SCValType.SCV_SYMBOL, sym=xdr.SCSymbol(sc_symbol=b'hello'))],
    ).set_timeout(30).build().transaction)

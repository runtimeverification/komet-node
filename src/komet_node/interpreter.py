from __future__ import annotations

import hashlib
from decimal import Decimal
from io import BytesIO
from typing import TYPE_CHECKING, NamedTuple

from komet.kast.syntax import (
    SC_VOID,
    account_id,
    call_tx,
    contract_id,
    deploy_contract,
    set_account,
    set_exit_code,
    steps_of,
    upload_wasm,
)
from pyk.kast.inner import KSort
from pyk.kast.manip import Subst, split_config_from
from pyk.konvert import kast_to_kore, kore_to_kast
from pyk.kore.parser import KoreParser
from pyk.ktool.krun import KRunOutput, _krun
from pykwasm.wasm2kast import wasm2kast
from stellar_sdk import Network, StrKey, xdr
from stellar_sdk.operation import CreateAccount, InvokeHostFunction
from stellar_sdk.utils import sha256

from komet_node.scval import scvalue_from_xdr

from .utils import simbolik_definition, temp_working_directory

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from pyk.kast.inner import KInner
    from stellar_sdk import MuxedAccount, Transaction
    from stellar_sdk.operation import Operation

    from .utils import SimbolikDefinition


REQUEST_FILE = 'request.json'


class InterpreterResponse(NamedTuple):
    final_kore: str


class NodeInterpreter:
    definition: SimbolikDefinition
    network_passphrase: str

    def __init__(self, network_passphrase: str = Network.TESTNET_NETWORK_PASSPHRASE) -> None:
        self.definition = simbolik_definition()
        self.network_passphrase = network_passphrase

    def empty_config(self) -> str:
        """
        Return the initial K configuration with an empty program cell.
        This is used as the base state before any steps are injected.
        """
        res = self.definition.krun_with_kast(
            pgm=steps_of([set_exit_code(0)]),
            sort=KSort('Steps'),
            output=KRunOutput.KORE,
        )
        res.check_returncode()
        return res.stdout

    @staticmethod
    def decode_contract_id(addr: str) -> bytes:
        if addr.startswith('C'):
            return StrKey.decode_contract(addr)
        raise NodeInterpreterError(f'Invalid strkey prefix. Expected "C" got {addr[0]}')

    @staticmethod
    def decode_account_id(addr: str) -> bytes:
        if addr.startswith('G'):
            return StrKey.decode_ed25519_public_key(addr)
        raise NodeInterpreterError(f'Invalid strkey prefix. Expected "G" got {addr[0]}')

    @staticmethod
    def address_to_kast(addr: str) -> KInner:
        if addr.startswith('G'):
            return account_id(StrKey.decode_ed25519_public_key(addr))
        if addr.startswith('C'):
            return contract_id(StrKey.decode_contract(addr))
        raise NodeInterpreterError(f'Unknown strkey prefix: {addr[0]}')

    def pretty_print(self, kore_str: str) -> str:
        """Pretty-print a KORE configuration string using the K definition."""
        with temp_working_directory() as root:
            kore_file = root / 'input.kore'
            kore_file.write_text(kore_str)
            res = _krun(
                input_file=kore_file,
                definition_dir=self.definition.path,
                parser='cat',
                term=True,
                output=KRunOutput.PRETTY,
                check=False,
                depth=0,
            )
            if res.returncode:
                raise NodeInterpreterError('Failed to pretty-print kore', res)
            return res.stdout

    def contract_id_from_preimage(self, contract_id_preimage: xdr.ContractIDPreimage) -> bytes:
        network_id_hash = xdr.Hash(hashlib.sha256(self.network_passphrase.encode()).digest())

        preimage = xdr.HashIDPreimage(
            xdr.EnvelopeType.ENVELOPE_TYPE_CONTRACT_ID,
            contract_id=xdr.HashIDPreimageContractID(
                network_id=network_id_hash,
                contract_id_preimage=contract_id_preimage,
            ),
        )

        return hashlib.sha256(preimage.to_xdr_bytes()).digest()

    def contract_address_from_deployer_address(self, deployer_public_key: str, salt: bytes) -> str:
        """
        Compute the C-strkey contract address that CREATE_CONTRACT will assign when
        deploying from an account address with the given salt.
        """
        preimage = xdr.ContractIDPreimage(
            xdr.ContractIDPreimageType.CONTRACT_ID_PREIMAGE_FROM_ADDRESS,
            from_address=xdr.ContractIDPreimageFromAddress(
                address=xdr.SCAddress(
                    xdr.SCAddressType.SC_ADDRESS_TYPE_ACCOUNT,
                    account_id=xdr.AccountID(
                        xdr.PublicKey(
                            xdr.PublicKeyType.PUBLIC_KEY_TYPE_ED25519,
                            ed25519=xdr.Uint256(StrKey.decode_ed25519_public_key(deployer_public_key)),
                        )
                    ),
                ),
                salt=xdr.Uint256(salt),
            ),
        )
        return StrKey.encode_contract(self.contract_id_from_preimage(preimage))

    def run_transaction(self, input_file: Path, transaction: Transaction) -> InterpreterResponse:

        def operation_to_steps(op: Operation) -> list[KInner]:
            match op:

                case CreateAccount(destination=dest, starting_balance=balance):
                    # starting_balance is XLM (may have decimals); convert to stroops (1 XLM = 10^7 stroops).
                    # We ignore the source account for now.
                    balance_stroops = int(Decimal(str(balance)) * Decimal('10000000'))
                    return self.make_set_account_step(dest, balance_stroops)

                case InvokeHostFunction(host_function=hf) if (
                    hf.type == xdr.HostFunctionType.HOST_FUNCTION_TYPE_UPLOAD_CONTRACT_WASM
                ):
                    # Upload Wasm bytecode to the ledger.
                    # This does not create a contract instance, it just stores the code.
                    # The hash of the bytecode is later used to deploy contract instances.
                    assert hf.wasm is not None
                    return self.make_upload_wasm_step(hf.wasm)

                case InvokeHostFunction(host_function=hf) if hf.type in (
                    xdr.HostFunctionType.HOST_FUNCTION_TYPE_CREATE_CONTRACT,
                    xdr.HostFunctionType.HOST_FUNCTION_TYPE_CREATE_CONTRACT_V2,
                ):
                    # Deploy a contract instance from a previously uploaded Wasm hash.
                    # Both V1 and V2 are handled identically here
                    # TODO: V2 supports constructor arguments, which the semantics do not yet handle.
                    create = hf.create_contract or hf.create_contract_v2
                    assert create is not None

                    preimage = create.contract_id_preimage
                    wasm_hash = create.executable.wasm_hash

                    # Only Wasm contracts are supported. Stellar Asset Contracts
                    # (CONTRACT_EXECUTABLE_STELLAR_ASSET) have no Wasm hash and
                    # require special host-level handling we do not implement.
                    assert (
                        create.executable.type == xdr.ContractExecutableType.CONTRACT_EXECUTABLE_WASM
                    ), f'Only WASM contracts are supported, got {create.executable.type}'
                    assert wasm_hash is not None  # should never happen if type is WASM

                    return self.make_deploy_contract(transaction.source, preimage, wasm_hash.hash)

                case InvokeHostFunction(host_function=hf) if (
                    hf.type == xdr.HostFunctionType.HOST_FUNCTION_TYPE_INVOKE_CONTRACT
                ):
                    invoke = hf.invoke_contract
                    assert invoke is not None
                    return self.make_call_tx(transaction.source, invoke)

                case _:
                    raise NotImplementedError(f'Unsupported operation type: {type(op)}')

        # When all operations can be expressed as JSON (i.e. no wasm upload), we could
        # skip the Kore round-trip and run through run_request_file instead.
        # needs_kast_execution = any(
        #     isinstance(op, InvokeHostFunction)
        #     and op.host_function.type == xdr.HostFunctionType.HOST_FUNCTION_TYPE_UPLOAD_CONTRACT_WASM
        #     for op in transaction.operations
        # )
        # if not needs_kast_execution:
        #     request_str = ...  # TODO: encode transaction as JSON
        #     return self.run_request_file(input_file, request_str)

        steps: list[KInner] = []
        for op in transaction.operations:
            steps.extend(operation_to_steps(op))
        return self.run_steps(input_file, steps)

    def make_set_account_step(self, destination: str, balance: int) -> list[KInner]:
        acct_id = StrKey.decode_ed25519_public_key(destination)
        return [set_account(acct_id, balance)]

    def make_upload_wasm_step(self, wasm_bytecode: bytes) -> list[KInner]:
        wasm_module_kast = wasm2kast(BytesIO(wasm_bytecode))
        wasm_hash = sha256(wasm_bytecode)
        return [upload_wasm(wasm_hash, wasm_module_kast)]

    def make_deploy_contract(
        self, deployer: MuxedAccount, contract_id_preimage: xdr.ContractIDPreimage, wasm_hash: bytes
    ) -> list[KInner]:
        from_addr = self.decode_account_id(deployer.universal_account_id)
        address = self.contract_id_from_preimage(contract_id_preimage)
        return [deploy_contract(from_addr, address, wasm_hash)]

    def make_call_tx(self, caller: MuxedAccount, invoke: xdr.InvokeContractArgs) -> list[KInner]:

        dest_contract_id = invoke.contract_address.contract_id
        assert dest_contract_id is not None, f'Contract address is None in invoke operation: {invoke}'
        callee_addr = contract_id(dest_contract_id.contract_id.hash)

        step = call_tx(
            from_addr=self.address_to_kast(caller.universal_account_id),
            to_addr=callee_addr,
            func=str(invoke.function_name),
            args=[scvalue_from_xdr(a).to_kast() for a in invoke.args],
            result=SC_VOID,
        )
        return [step]

    def run_request_file(self, input_file: Path, request_str: str) -> InterpreterResponse:
        """
        Run a request against a saved K configuration by writing request.json to the
        working directory and invoking krun on the idle kore state.

        node.md's insert-handleRequestFile rule fires when the k and program cells are
        empty and request.json exists, reads the file, dispatches #handleRequest, then
        removes the file and halts — leaving the updated idle state as output.
        """
        with temp_working_directory() as root:
            (root / REQUEST_FILE).write_text(request_str)

            res = _krun(
                input_file=input_file,
                definition_dir=self.definition.path,
                parser='cat',
                term=True,
                output=KRunOutput.KORE,
                check=False,
            )

            if res.returncode:
                raise NodeInterpreterError(f'krun failed for request: {request_str}', res)

            return InterpreterResponse(final_kore=res.stdout)

    def run_steps(self, input_file: Path, steps: Iterable[KInner]) -> InterpreterResponse:
        input_state_kore = KoreParser(input_file.read_text()).pattern()
        input_state_kast = kore_to_kast(self.definition.kdefinition, input_state_kore)

        conf, subst = split_config_from(input_state_kast)
        subst['PROGRAM_CELL'] = steps_of(
            (
                set_exit_code(1),
                *steps,
                set_exit_code(0),
            )
        )
        conf_with_pgm = Subst(subst).apply(conf)
        conf_with_pgm_kore = kast_to_kore(self.definition.kdefinition, conf_with_pgm, KSort('GeneratedTopCell'))

        with temp_working_directory():
            res = self.definition.krun.run_process(pgm=conf_with_pgm_kore)

            if res.returncode:
                raise NodeInterpreterError('Failed to krun program', res)

            return InterpreterResponse(final_kore=res.stdout)


class NodeInterpreterError(RuntimeError):
    pass

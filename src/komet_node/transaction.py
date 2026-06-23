from __future__ import annotations

import hashlib
from decimal import Decimal
from io import BytesIO
from typing import TYPE_CHECKING, Any

from komet.kast.syntax import upload_wasm
from pykwasm.wasm2kast import wasm2kast
from stellar_sdk import Network, StrKey, TransactionEnvelope, xdr
from stellar_sdk.operation import CreateAccount, InvokeHostFunction
from stellar_sdk.utils import sha256

from komet_node.scval import scval_to_json

from .interpreter import NodeInterpreterError

if TYPE_CHECKING:
    from pyk.kast.inner import KInner
    from stellar_sdk import MuxedAccount, Transaction
    from stellar_sdk.operation import Operation

_STROOPS_PER_XLM = Decimal('10000000')


def _xlm_to_stroops(balance: object) -> int:
    """Convert an XLM amount (which may carry up to 7 decimals) to integer stroops.

    Stellar balances are denominated in stroops (1 XLM = 10^7 stroops), so an amount with
    more than 7 decimal places cannot be represented exactly. Reject it rather than silently
    truncating toward zero, which would put an incorrect balance on the ledger.
    """
    stroops = Decimal(str(balance)) * _STROOPS_PER_XLM
    if stroops != stroops.to_integral_value():
        raise NodeInterpreterError(f'XLM amount has sub-stroop precision: {balance!r}')
    return int(stroops)


class TransactionEncoder:
    """
    Decodes Stellar XDR transactions into the request envelope consumed by ``node.md``.

    It performs the work K cannot do itself: parsing the XDR envelope, computing the
    transaction hash and contract ids, and (for wasm uploads) parsing the bytecode into a
    ``ModuleDecl`` via ``wasm2kast``. It produces a JSON request envelope and, only for the
    wasm-upload case, the kasmer steps to inject into the ``<program>`` cell.
    """

    network_passphrase: str
    trace: bool

    def __init__(self, network_passphrase: str = Network.TESTNET_NETWORK_PASSPHRASE, trace: bool = False) -> None:
        self.network_passphrase = network_passphrase
        self.trace = trace

    def build_tx_request(
        self,
        method: str,
        rpc_id: Any,
        transaction_xdr: str,
        now: str,
        force_trace: bool,
    ) -> tuple[dict[str, Any], list[KInner] | None]:
        """
        Decode a transaction XDR envelope into a request envelope for the K semantics.

        Returns the envelope dict plus, for the wasm-upload path, the kasmer steps to embed
        in the ``<program>`` cell (``None`` for the common JSON-steps path).
        """
        envelope = TransactionEnvelope.from_xdr(transaction_xdr, self.network_passphrase)
        transaction = envelope.transaction

        json_steps = self._encode_steps(transaction)
        request: dict[str, Any] = {
            'method': method,
            'id': rpc_id,
            'now': now,
            'txHash': envelope.hash_hex(),
            'envelopeXdr': transaction_xdr,
            'trace': bool(force_trace or self.trace),
            'steps': json_steps if json_steps is not None else [],
        }

        # A transaction that uploads wasm cannot be expressed as JSON (the resulting
        # ModuleDecl has no JSON form). Soroban allows only a single host-function
        # operation per transaction, so such a transaction is exactly one upload op, whose
        # step we build in K-AST form for direct injection into the <program> cell.
        if json_steps is not None:
            return request, None
        return request, self._upload_steps(transaction)

    def _encode_steps(self, transaction: Transaction) -> list[dict] | None:
        """Encode each operation as a JSON step dict, or ``None`` if any op needs the wasm path.

        Key ordering in each step dict is significant: it must match the ``#decodeStep``
        patterns in ``node.md`` exactly, because K's JSON sort is ordered.
        """
        steps = []
        for op in transaction.operations:
            encoded = self._encode_operation(op, transaction.source)
            if encoded is None:
                return None
            steps.append(encoded)
        return steps

    def _encode_operation(self, op: Operation, source: MuxedAccount) -> dict | None:
        match op:
            case CreateAccount(destination=dest, starting_balance=balance):
                return {
                    'op': 'setAccount',
                    'account': StrKey.decode_ed25519_public_key(dest).hex(),
                    'balance': _xlm_to_stroops(balance),
                }

            case InvokeHostFunction(host_function=hf) if (
                hf.type == xdr.HostFunctionType.HOST_FUNCTION_TYPE_UPLOAD_CONTRACT_WASM
            ):
                return None  # requires embedding a ModuleDecl in the K AST

            case InvokeHostFunction(host_function=hf) if hf.type in (
                xdr.HostFunctionType.HOST_FUNCTION_TYPE_CREATE_CONTRACT,
                xdr.HostFunctionType.HOST_FUNCTION_TYPE_CREATE_CONTRACT_V2,
            ):
                create = _wasm_create_contract(hf)
                assert create.executable.wasm_hash is not None
                return {
                    'op': 'deployContract',
                    'from': self.decode_account_id(source.universal_account_id).hex(),
                    'address': self.contract_id_from_preimage(create.contract_id_preimage).hex(),
                    'wasmHash': create.executable.wasm_hash.hash.hex(),
                }

            case InvokeHostFunction(host_function=hf) if (
                hf.type == xdr.HostFunctionType.HOST_FUNCTION_TYPE_INVOKE_CONTRACT
            ):
                invoke = hf.invoke_contract
                assert invoke is not None
                from_str = source.universal_account_id
                from_is_contract = from_str.startswith('C')
                from_bytes = self.decode_contract_id(from_str) if from_is_contract else self.decode_account_id(from_str)
                assert invoke.contract_address.contract_id is not None
                return {
                    'op': 'callTx',
                    'from': from_bytes.hex(),
                    'fromIsContract': from_is_contract,
                    'func': invoke.function_name.sc_symbol.decode('ascii'),
                    'to': invoke.contract_address.contract_id.contract_id.hash.hex(),
                    'args': [scval_to_json(a) for a in invoke.args],
                }

            case _:
                return None

    def _upload_steps(self, transaction: Transaction) -> list[KInner]:
        """Build the kasmer ``uploadWasm`` step(s) for a wasm-upload transaction."""
        steps: list[KInner] = []
        for op in transaction.operations:
            match op:
                case InvokeHostFunction(host_function=hf) if (
                    hf.type == xdr.HostFunctionType.HOST_FUNCTION_TYPE_UPLOAD_CONTRACT_WASM
                ):
                    assert hf.wasm is not None
                    steps.append(upload_wasm(sha256(hf.wasm), wasm2kast(BytesIO(hf.wasm))))
                case _:
                    raise NotImplementedError(f'Unexpected operation in wasm-upload transaction: {type(op)}')
        return steps

    # ------------------------------------------------------------------
    # Address / contract-id helpers
    # ------------------------------------------------------------------

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


def _wasm_create_contract(hf: xdr.HostFunction) -> xdr.CreateContractArgs | xdr.CreateContractArgsV2:
    """Extract and validate the create-contract args from a host function (V1 or V2)."""
    create = hf.create_contract or hf.create_contract_v2
    assert create is not None
    assert (
        create.executable.type == xdr.ContractExecutableType.CONTRACT_EXECUTABLE_WASM
    ), f'Only WASM contracts are supported, got {create.executable.type}'
    return create

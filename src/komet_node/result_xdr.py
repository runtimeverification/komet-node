"""Builders for the ``resultXdr`` / ``resultMetaXdr`` receipt fields.

Per the RPC spec, a SUCCESS or FAILED getTransaction response carries the transaction's
outcome as base64-encoded ``TransactionResult`` and ``TransactionMeta`` XDR structs. The K
semantics record the outcome (status and, for contract calls, the return value) but cannot
construct XDR, so the server synthesises these structs from the transaction envelope and the
recorded return value.

Being a mock chain, komet-node does not track fees or ledger-entry changes, so those parts
of the structs are empty/zero: ``feeCharged`` is 0 and the meta carries no entry changes.
The meta is emitted as ``TransactionMeta`` v3, the protocol-22 format (v4 is protocol 23+).
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from stellar_sdk import xdr
from stellar_sdk.operation import CreateAccount, InvokeHostFunction

if TYPE_CHECKING:
    from stellar_sdk import TransactionEnvelope
    from stellar_sdk.operation import Operation


def transaction_result_xdr(envelope: TransactionEnvelope, return_value: xdr.SCVal | None, *, success: bool) -> str:
    """Build the base64 ``TransactionResult`` XDR for a transaction's receipt.

    On success every operation reports its success code (with the InvokeHostFunction result
    carrying a hash derived from the return value). On failure the code is ``txFAILED``; a
    single trapped InvokeHostFunction operation is reported when the transaction is a plain
    contract invocation, otherwise the per-operation detail is left empty — the semantics
    only report that the run got stuck, not which operation trapped.
    """
    operations = envelope.transaction.operations
    if success:
        code = xdr.TransactionResultCode.txSUCCESS
        results = [_op_success_result(op, return_value) for op in operations]
    else:
        code = xdr.TransactionResultCode.txFAILED
        results = _op_failure_results(operations)
    result = xdr.TransactionResult(
        fee_charged=xdr.Int64(0),
        result=xdr.TransactionResultResult(code=code, results=results),
        ext=xdr.TransactionResultExt(0),
    )
    return result.to_xdr()


def transaction_meta_xdr(envelope: TransactionEnvelope, return_value: xdr.SCVal | None) -> str:
    """Build the base64 ``TransactionMeta`` (v3) XDR for a successful transaction's receipt.

    When the transaction made a contract call, its return value is reported as
    ``sorobanMeta.returnValue`` — this is where clients read an invocation's result. Other
    soroban transactions (upload, deploy) carry no soroban meta; ledger-entry change sets
    are empty because komet-node does not track them.
    """
    soroban_meta = None
    if return_value is not None:
        soroban_meta = xdr.SorobanTransactionMeta(
            ext=xdr.SorobanTransactionMetaExt(0),
            events=[],
            return_value=return_value,
            diagnostic_events=[],
        )
    meta = xdr.TransactionMeta(
        v=3,
        v3=xdr.TransactionMetaV3(
            ext=xdr.ExtensionPoint(0),
            tx_changes_before=xdr.LedgerEntryChanges([]),
            operations=[xdr.OperationMeta(xdr.LedgerEntryChanges([])) for _ in envelope.transaction.operations],
            tx_changes_after=xdr.LedgerEntryChanges([]),
            soroban_meta=soroban_meta,
        ),
    )
    return meta.to_xdr()


def _op_success_result(op: Operation, return_value: xdr.SCVal | None) -> xdr.OperationResult:
    """The success ``OperationResult`` for one operation of a committed transaction."""
    if isinstance(op, CreateAccount):
        tr = xdr.OperationResultTr(
            xdr.OperationType.CREATE_ACCOUNT,
            create_account_result=xdr.CreateAccountResult(xdr.CreateAccountResultCode.CREATE_ACCOUNT_SUCCESS),
        )
    elif isinstance(op, InvokeHostFunction):
        # The real network puts SHA-256(InvokeHostFunctionSuccessPreImage) here — a hash over
        # the emitted events and the return value. komet-node does not capture events, so the
        # hash is derived from the return value alone (empty for upload/deploy operations).
        payload = return_value.to_xdr_bytes() if return_value is not None else b''
        tr = xdr.OperationResultTr(
            xdr.OperationType.INVOKE_HOST_FUNCTION,
            invoke_host_function_result=xdr.InvokeHostFunctionResult(
                xdr.InvokeHostFunctionResultCode.INVOKE_HOST_FUNCTION_SUCCESS,
                success=xdr.Hash(hashlib.sha256(payload).digest()),
            ),
        )
    else:
        # TransactionEncoder only admits CreateAccount and InvokeHostFunction operations, so
        # a committed transaction cannot contain anything else.
        raise NotImplementedError(f'No result encoding for operation type: {type(op).__name__}')
    return xdr.OperationResult(xdr.OperationResultCode.opINNER, tr=tr)


def _op_failure_results(operations: list[Operation]) -> list[xdr.OperationResult]:
    """The per-operation results for a failed (stuck) transaction."""
    if len(operations) == 1 and isinstance(operations[0], InvokeHostFunction):
        tr = xdr.OperationResultTr(
            xdr.OperationType.INVOKE_HOST_FUNCTION,
            invoke_host_function_result=xdr.InvokeHostFunctionResult(
                xdr.InvokeHostFunctionResultCode.INVOKE_HOST_FUNCTION_TRAPPED
            ),
        )
        return [xdr.OperationResult(xdr.OperationResultCode.opINNER, tr=tr)]
    return []

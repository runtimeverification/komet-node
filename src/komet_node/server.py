from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from pyk.rpc.rpc import JsonRpcServer, ServeRpcOptions
from stellar_sdk import Network, TransactionEnvelope

from komet_node.interpreter import NodeInterpreter, NodeInterpreterError

if TYPE_CHECKING:
    pass

_PROTOCOL_VERSION: Final = '22'


class StellarRpcServer(JsonRpcServer):
    interpreter: NodeInterpreter
    state_file: Path
    ledger_seq: int
    _transactions: dict[str, dict[str, Any]]

    def __init__(
        self,
        host: str = 'localhost',
        port: int = 8000,
        state_file: Path = Path('state.kore'),
        network_passphrase: str = Network.TESTNET_NETWORK_PASSPHRASE,
        trace: bool = False,
    ) -> None:
        super().__init__(ServeRpcOptions({'addr': host, 'port': port, 'definition_dir': None}))
        self.interpreter = NodeInterpreter(network_passphrase, trace=trace)
        self.state_file = state_file
        self.ledger_seq = 0
        self._transactions = {}

        if not self.state_file.exists():
            self.state_file.write_text(self.interpreter.empty_config())

        self._register_stellar_methods()

    def _register_stellar_methods(self) -> None:
        for name, fn in {
            'getHealth': self.exec_get_health,
            'getNetwork': self.exec_get_network,
            'getLatestLedger': self.exec_get_latest_ledger,
            'sendTransaction': self.exec_send_transaction,
            'getTransaction': self.exec_get_transaction,
            'traceTransaction': self.exec_trace_transaction,
        }.items():
            self.register_method(name, fn)

    def exec_get_health(self) -> dict[str, Any]:
        return {'status': 'healthy'}

    def exec_get_network(self) -> dict[str, Any]:
        return {
            'friendbotUrl': None,
            'passphrase': self.interpreter.network_passphrase,
            'protocolVersion': _PROTOCOL_VERSION,
        }

    def exec_get_latest_ledger(self) -> dict[str, Any]:
        return {
            'id': '0' * 64,
            'protocolVersion': _PROTOCOL_VERSION,
            'sequence': self.ledger_seq,
        }

    def exec_send_transaction(self, transaction: str) -> dict[str, Any]:
        now = str(int(time.time()))
        envelope = TransactionEnvelope.from_xdr(transaction, self.interpreter.network_passphrase)
        tx_hash = envelope.hash_hex()

        try:
            result = self.interpreter.run_transaction(self.state_file, envelope.transaction, self.ledger_seq)
            self.state_file.write_text(result.final_kore)
            self.ledger_seq += 1
            self._transactions[tx_hash] = {
                'status': 'SUCCESS',
                'ledger': str(self.ledger_seq),
                'createdAt': now,
                'envelopeXdr': transaction,
                'resultXdr': '',
                'resultMetaXdr': '',
                'trace': result.trace,
            }
        except NodeInterpreterError:
            self._transactions[tx_hash] = {
                'status': 'FAILED',
                'ledger': str(self.ledger_seq),
                'createdAt': now,
                'envelopeXdr': transaction,
                'resultXdr': '',
                'resultMetaXdr': '',
                'trace': None,
            }

        return {
            'hash': tx_hash,
            'status': 'PENDING',
            'latestLedger': str(self.ledger_seq),
            'latestLedgerCloseTime': now,
        }

    def exec_trace_transaction(self, transaction: str) -> dict[str, Any]:
        now = str(int(time.time()))
        envelope = TransactionEnvelope.from_xdr(transaction, self.interpreter.network_passphrase)
        tx_hash = envelope.hash_hex()

        try:
            result = self.interpreter.run_transaction_with_trace(
                self.state_file, envelope.transaction, self.ledger_seq
            )
            self.state_file.write_text(result.final_kore)
            self.ledger_seq += 1
            self._transactions[tx_hash] = {
                'status': 'SUCCESS',
                'ledger': str(self.ledger_seq),
                'createdAt': now,
                'envelopeXdr': transaction,
                'resultXdr': '',
                'resultMetaXdr': '',
                'trace': result.trace,
            }
        except NodeInterpreterError:
            self._transactions[tx_hash] = {
                'status': 'FAILED',
                'ledger': str(self.ledger_seq),
                'createdAt': now,
                'envelopeXdr': transaction,
                'resultXdr': '',
                'resultMetaXdr': '',
                'trace': None,
            }

        return {
            'hash': tx_hash,
            'status': self._transactions[tx_hash]['status'],
            'ledger': self._transactions[tx_hash]['ledger'],
            'trace': self._transactions[tx_hash]['trace'],
            'latestLedger': str(self.ledger_seq),
            'latestLedgerCloseTime': now,
        }

    def exec_get_transaction(self, hash: str) -> dict[str, Any]:
        now = str(int(time.time()))
        result = self._transactions.get(hash)

        if result is None:
            return {
                'status': 'NOT_FOUND',
                'latestLedger': str(self.ledger_seq),
                'latestLedgerCloseTime': now,
            }

        return result | {
            'latestLedger': str(self.ledger_seq),
            'latestLedgerCloseTime': now,
        }

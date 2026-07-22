
This module implements the komet-node JSON-RPC request lifecycle in K.

The Python server wraps this semantics in a long-running process: it decodes the Stellar
XDR envelope (which K cannot parse), builds a JSON *request envelope* describing the RPC
call, writes it to `request.json`, runs this semantics against the saved KORE
configuration, and reads back `response.json`.

All RPC dispatch, transaction bookkeeping, ledger-sequence accounting and JSON-RPC
response formatting live here in K:
  - The semantic world state (accounts, contracts, uploaded wasm) round-trips through the
    KORE configuration (`state.kore`), because uploaded wasm is a `ModuleDecl` that cannot
    be reconstructed from bytes by the semantics.
  - The latest-ledger counter is persisted as a small JSON file (`metadata.json`) in the
    working directory. Each transaction's receipt and execution trace are persisted as their
    own files under `receipts/` and `traces/`, keyed by tx hash, so no single file grows
    without bound. All are read and written by these rules via the file-system hooks.

Lifecycle: on each invocation, if `request.json` exists, read it, dispatch on the
`method` field, write `response.json`, remove `request.json`, and halt. The empty `<k>`,
`<instrs>`, and `<program>` cells with no `request.json` present represent the idle/ready
state that is saved and reused for the next request.

```k
requires "soroban-semantics/kasmer.md"
requires "fs.md"
requires "json.md"

module NODE-SYNTAX
    imports KASMER-SYNTAX
endmodule

module NODE
    imports KASMER
    imports FILE-OPERATIONS
    imports JSON
    imports BYTES
    imports K-EQUAL
    imports STRING

    // Allow parenthesising JSON and JSONs (needed to group JSONs as a single
    // argument to the helper functions below).
    syntax JSON  ::= "(" JSON  ")" [bracket]
    syntax JSONs ::= "(" JSONs ")" [bracket]

    // Internal control-flow items for the node request lifecycle.
    syntax KItem ::= "#handleRequestFile"
                   | #dispatch( JSON )
                   | #dispatchMethod( String, JSON )
                   | #runTx( JSON )
                   | #finalizeTx( JSON )
                   | #recordAndRespond( JSON, Int )
                   | #respondTx( JSON, String, Int )
                   | #enableTrace( String )
                   | #getTxResult( String, String, JSON, Int )
                   | #respondTrace( JSON, String )
                   | #respond( JSON, JSON )
                   | #respondError( JSON, Int, String )
                   | #respondHealth( JSON, String, Int )
                   | #respondLatestLedger( JSON, Int, Int )

    syntax Step ::= setLedgerSequence(Int)    [symbol(setLedgerSequence)]
 // ----------------------------------------------------------------------
    rule [setLedgerSequence]:
        <k> setLedgerSequence(SEQ) => .K ... </k>
        <ledgerSequenceNumber> _ => SEQ </ledgerSequenceNumber>
```

`HexBytes` decodes a lowercase hex string to Bytes (big-endian, with length = hex length / 2).
It relies on K's String2Base hook (base 16) and on Int2Bytes with an explicit byte count, so
that leading zero bytes are preserved.

```k
    syntax Bytes ::= HexBytes(String) [function]
    rule HexBytes("") => .Bytes
    rule HexBytes(S)  => Int2Bytes(lengthString(S) /Int 2, String2Base(S, 16), BE)
      requires lengthString(S) >Int 0
```

`string2WasmToken` wraps a plain K String (for example, "foo") in double-quote delimiters and
produces a WasmStringToken using K's generic string-to-token hook.

```k
    syntax WasmStringToken ::= string2WasmToken(String) [function, hook(STRING.string2token)]
```

###############################################################################
# JSON helpers

These rules provide order-independent accessors over JSON objects, ported from kontrol-node's
`json-utils.md`.

```k
    syntax JSON ::= #getJSON( JSONKey, JSON )       [function, symbol(getJSON)]
                  | #getJSON( JSONKey, JSON, JSON ) [function, symbol(getJSONDefault)]
 // ----------------------------------------------------------------------------------
    rule #getJSON( KEY, { KEY  : J, _    }, _   ) => J
    rule #getJSON(   _, { .JSONs         }, DEF ) => DEF
    rule #getJSON( KEY, { KEY2 : _, REST }, DEF ) => #getJSON( KEY, { REST }, DEF )
      requires KEY =/=K KEY2
    rule #getJSON( KEY, J ) => #getJSON( KEY, J, null )

    syntax String ::= #getString( JSONKey, JSON ) [function, symbol(getString)]
 // ---------------------------------------------------------------------------
    rule #getString( KEY, J ) => {#getJSON( KEY, J )}:>String

    syntax Int ::= #getInt( JSONKey, JSON ) [function, symbol(getInt)]
 // -----------------------------------------------------------------
    rule #getInt( KEY, J ) => {#getJSON( KEY, J )}:>Int

    // The per-hash file that holds a transaction's receipt / execution trace. Python and K
    // both build these paths the same way (see server.py), and the directories are created
    // by the server before the semantics run (the file-system hooks do not create them).
    syntax String ::= #receiptFile( String ) [function, symbol(receiptFile)]
                    | #traceFile( String )   [function, symbol(traceFile)]
 // ----------------------------------------------------------------------
    rule #receiptFile( HASH ) => "receipts/receipt_" +String HASH +String ".json"
    rule #traceFile( HASH )   => "traces/trace_"     +String HASH +String ".jsonl"

    // Append the entries of TAIL after the entries of HEAD.
    syntax JSONs ::= #concatJSONs( JSONs, JSONs ) [function, symbol(concatJSONs)]
 // ----------------------------------------------------------------------------
    rule #concatJSONs( .JSONs, TAIL ) => TAIL
    rule #concatJSONs( ( J, REST ), TAIL ) => ( J , #concatJSONs( REST, TAIL ) )
```

###############################################################################
# Request lifecycle

insert-handleRequestFile fires when the `<k>`, `<instrs>` and `<program>` cells are empty
and `request.json` is present (the initial/idle state). If `request.json` does not exist,
this rule does not fire and execution halts — this is the idle state the node saves for
reuse.

For transactions that carry uploaded wasm, the Python server injects the kasmer steps into
the `<program>` cell directly (the wasm `ModuleDecl` cannot be JSON-encoded). Those steps
run first via KASMER's `load-program` rule (which requires a non-empty `<program>`); once
`<program>` drains to `.Steps`, this rule fires and the request envelope drives the
bookkeeping.

```k
    rule [insert-handleRequestFile]:
        <k> .K => #handleRequestFile </k>
        <instrs> .K </instrs>
        <program> .Steps </program>
      requires #fileExists("request.json")

    rule [handleRequestFile]:
        <k> #handleRequestFile
         => #dispatch( String2JSON( {#readFile("request.json")}:>String ) )
            ...
        </k>

    // KASMER's steps-empty requires <k> .Steps </k> exactly (no frame).
    // When steps are injected into <k> with a continuation, we need this rule
    // to consume .Steps and let the continuation proceed.
    rule [steps-done]:
        <k> .Steps => .K ... </k>
        <instrs> .K </instrs>
```

#dispatch reads the `method` field of the request envelope and routes to a per-method
rule. `#respond(ID, RESULT)` writes the JSON-RPC envelope to `response.json`, removes
`request.json`, and marks the run successful (exit code 0). `#respondError(ID, CODE, MSG)`
is its error-response counterpart: it writes a JSON-RPC error envelope (with an `error`
member instead of `result`, per JSON-RPC 2.0) and otherwise behaves the same.

```k
    rule <k> #dispatch( REQ ) => #dispatchMethod( #getString( "method", REQ ), REQ ) ... </k>

    rule <k> #respond( ID, RESULT )
          => #writeFile( "response.json", JSON2String({
                 "jsonrpc" : "2.0",
                 "id"      : ID,
                 "result"  : RESULT
             }))
          ~> #remove( "request.json" )
             ...
         </k>
         <exitCode> _ => 0 </exitCode>

    rule <k> #respondError( ID, CODE, MSG )
          => #writeFile( "response.json", JSON2String({
                 "jsonrpc" : "2.0",
                 "id"      : ID,
                 "error"   : { "code" : CODE, "message" : MSG }
             }))
          ~> #remove( "request.json" )
             ...
         </k>
         <exitCode> _ => 0 </exitCode>
```

###############################################################################
## Read-only methods

The response shapes follow real stellar-rpc's serialization: ledger sequence numbers,
`protocolVersion`, and `ledgerRetentionWindow` are JSON numbers, while close-time fields
are int64s that Go serializes with `,string` — JSON strings holding a decimal integer.
`friendbotUrl` is an `omitempty` field and this node runs no friendbot, so it is omitted
entirely (not `null`).

This node retains every ledger since genesis, so `getHealth` reports `oldestLedger` 0 and a
retention window covering all ledgers so far. Close times are not recorded per ledger: the
latest close time is approximated by the request time (`now`, as everywhere else in this
module) and the oldest by the epoch (`"0"`).

```k
    rule <k> #dispatchMethod( "getHealth", REQ )
          => #respondHealth(
                 #getJSON( "id", REQ ),
                 #getString( "now", REQ ),
                 #getInt( "latest_ledger", String2JSON( {#readFile("metadata.json")}:>String ) )
             )
             ...
         </k>

    rule <k> #respondHealth( ID, NOW, LL )
          => #respond( ID, {
                 "status"                : "healthy",
                 "latestLedger"          : LL,
                 "latestLedgerCloseTime" : NOW,
                 "oldestLedger"          : 0,
                 "oldestLedgerCloseTime" : "0",
                 "ledgerRetentionWindow" : LL +Int 1
             })
             ...
         </k>

    rule <k> #dispatchMethod( "getNetwork", REQ )
          => #respond( #getJSON( "id", REQ ), {
                 "passphrase"      : #getString( "passphrase", REQ ),
                 "protocolVersion" : #getInt( "protocolVersion", REQ )
             })
             ...
         </k>

    rule <k> #dispatchMethod( "getLatestLedger", REQ )
          => #respondLatestLedger(
                 #getJSON( "id", REQ ),
                 #getInt( "protocolVersion", REQ ),
                 #getInt( "latest_ledger", String2JSON( {#readFile("metadata.json")}:>String ) )
             )
             ...
         </k>

    rule <k> #respondLatestLedger( ID, PV, SEQ )
          => #respond( ID, {
                 "id"              : #ledgerId( SEQ ),
                 "protocolVersion" : PV,
                 "sequence"        : SEQ
             })
             ...
         </k>
```

`#ledgerId` derives the ledger's `id` (the hash of the ledger header on a real chain) from
its sequence number. This node has no ledger headers to hash — and the semantics have no
hash hooks — so the id is a deterministic stand-in: the sequence (offset by one so ledger 0
is not the all-zeros hash) multiplied by a fixed odd 256-bit constant, mod 2^256, printed as
64 lowercase hex characters. Multiplication by an odd constant is injective mod a power of
two, so every ledger gets a distinct id. The constant is ⌊2^256/φ⌋ rounded to odd
(0x9e3779b97f4a7c15... — the golden-ratio constant, chosen only to spread the bits).

```k
    syntax String ::= #ledgerId( Int ) [function, total, symbol(ledgerId)]
 // ----------------------------------------------------------------------
    rule #ledgerId( SEQ )
      => #padLeftZeros(
             Base2String(
                 ((SEQ +Int 1) *Int 71563446777022297856526126342750658392501306254664949883333486863006233104021)
                     modInt (2 ^Int 256),
                 16
             ),
             64
         )

    syntax String ::= #padLeftZeros( String, Int ) [function, total, symbol(padLeftZeros)]
 // --------------------------------------------------------------------------------------
    rule #padLeftZeros( S, N ) => S requires lengthString(S) >=Int N
    rule #padLeftZeros( S, N ) => #padLeftZeros( "0" +String S, N ) requires lengthString(S) <Int N
```

## getTransaction

Look up the stored receipt by hash in its `receipts/receipt_<hash>.json` file. If the file
exists, return its contents merged with the ledger-range fields; otherwise return
`NOT_FOUND`. The ledger range (`latestLedger`/`latestLedgerCloseTime`/`oldestLedger`/
`oldestLedgerCloseTime`) is required on every `getTransaction` response, whatever the
status. Ledger sequences are JSON numbers; the close times use the int64-as-string
encoding (see the read-only methods above) — the oldest ledger is always 0 on this node
and its close time is not recorded, so the constant `"0"` stands in.

```k
    rule <k> #dispatchMethod( "getTransaction", REQ )
          => #getTxResult(
                 #getString( "hash", REQ ),
                 #getString( "now", REQ ),
                 #getJSON( "id", REQ ),
                 #getInt( "latest_ledger", String2JSON( {#readFile("metadata.json")}:>String ) )
             )
             ...
         </k>

    rule <k> #getTxResult( HASH, NOW, ID, LL )
          => #respond( ID, { #concatJSONs(
                 #recordOf( String2JSON( {#readFile( #receiptFile( HASH ) )}:>String ) ),
                 ( "latestLedger"          : LL ,
                   "latestLedgerCloseTime" : NOW ,
                   "oldestLedger"          : 0 ,
                   "oldestLedgerCloseTime" : "0" ,
                   .JSONs )
             )})
             ...
         </k>
      requires #fileExists( #receiptFile( HASH ) )

    rule <k> #getTxResult( HASH, NOW, ID, LL )
          => #respond( ID, {
                 "status"                : "NOT_FOUND",
                 "latestLedger"          : LL,
                 "latestLedgerCloseTime" : NOW,
                 "oldestLedger"          : 0,
                 "oldestLedgerCloseTime" : "0"
             })
             ...
         </k>
      requires notBool #fileExists( #receiptFile( HASH ) )

    // Extract the entries of a stored receipt object so they can be concatenated.
    syntax JSONs ::= #recordOf( JSON ) [function, symbol(recordOf)]
 // --------------------------------------------------------------
    rule #recordOf( { OBJ } ) => OBJ
```

###############################################################################
## sendTransaction

`sendTransaction` runs the decoded steps, records a receipt, bumps the ledger, and responds
with `PENDING`. Instruction tracing is always on: the executing steps append to the
transaction's own `traces/trace_<hash>.jsonl` file, which `traceTransaction` (below) later
retrieves by hash. The receipt itself does not carry the trace.

A transaction whose hash already has a receipt is a duplicate submission: it is answered
with status `DUPLICATE` and is **not** re-executed — the ledger does not advance and the
stored receipt is left untouched. (On the wasm-upload path the Python server suppresses the
`<program>` injection for duplicates, so nothing runs before dispatch either; see
server.py.)

The steps come either from the `steps` array of the request envelope (the common path) or
from the `<program>` cell (the wasm-upload path, where they were pre-injected and have
already run by the time we get here, leaving `steps` empty).

```k
    rule <k> #dispatchMethod( "sendTransaction", REQ ) => #runTx( REQ ) ... </k>
      requires notBool #fileExists( #receiptFile( #getString( "txHash", REQ ) ) )

    rule <k> #dispatchMethod( "sendTransaction", REQ )
          => #respondTx( REQ, "DUPLICATE",
                 #getInt( "latest_ledger", String2JSON( {#readFile("metadata.json")}:>String ) ) )
             ...
         </k>
      requires #fileExists( #receiptFile( #getString( "txHash", REQ ) ) )

    // Unknown method — the JSON-RPC "Method not found" error. (The Python server filters
    // unknown methods before they reach the semantics, so this is a safety net for
    // envelopes injected directly.)
    rule <k> #dispatchMethod( _, REQ )
          => #respondError( #getJSON( "id", REQ ), -32601, "Method not found" )
             ...
         </k> [owise]

    rule <k> #runTx( REQ )
          => #enableTrace( #traceFile( #getString( "txHash", REQ ) ) )
          ~> setLedgerSequence( #getInt( "latest_ledger", String2JSON( {#readFile("metadata.json")}:>String ) ) )
          ~> #decodeSteps( #stepsJSONs( #getJSON( "steps", REQ, [ .JSONs ] ) ) )
          ~> #finalizeTx( REQ )
             ...
         </k>

    syntax JSONs ::= #stepsJSONs( JSON ) [function, symbol(stepsJSONs)]
 // ------------------------------------------------------------------
    rule #stepsJSONs( [ SS ] ) => SS
    rule #stepsJSONs( _ )      => .JSONs [owise]
```

Tracing is always enabled: clear the transaction's trace file and point the trace `<ioDir>`
at it so the executing steps append their records to it.

```k
    rule <k> #enableTrace( PATH ) => #writeFile( PATH, "" ) ... </k>
         <ioDir> _ => PATH </ioDir>
```

After the steps run, record the receipt, write the new ledger counter, and respond. The trace
was already written to its own file during execution, so we only reset `<ioDir>`. Reaching
this point means the steps completed without getting stuck, so the status is `SUCCESS`.

The receipt holds the per-transaction half of a `getTransaction` response, with the
serialization getTransaction requires: `ledger` is a JSON number, `createdAt` an
int64-as-string. Every ledger on this node contains exactly one transaction, so
`applicationOrder` is always 1, and fee-bump envelopes are not supported, so `feeBump` is
always false. (The Python failure fallback in server.py writes a `FAILED` receipt with the
same field set — keep the two in sync.)

```k
    rule <k> #finalizeTx( REQ )
          => #recordAndRespond(
                 REQ,
                 #getInt( "latest_ledger", String2JSON( {#readFile("metadata.json")}:>String ) )
             )
             ...
         </k>
         <ioDir> _ => "" </ioDir>

    rule <k> #recordAndRespond( REQ, L )
          => #writeFile( "metadata.json", JSON2String({ "latest_ledger" : L +Int 1 }) )
          ~> #writeFile( #receiptFile( #getString( "txHash", REQ ) ),
                 JSON2String( #txReceipt( REQ, L +Int 1 ) ) )
          ~> #respondTx( REQ, "PENDING", L +Int 1 )
             ...
         </k>

    syntax JSON ::= #txReceipt( JSON, Int ) [function, symbol(txReceipt)]
 // ---------------------------------------------------------------------
    rule #txReceipt( REQ, NEWL ) => {
            "status"           : "SUCCESS",
            "applicationOrder" : 1,
            "feeBump"          : false,
            "envelopeXdr"      : #getString( "envelopeXdr", REQ ),
            "resultXdr"        : "",
            "resultMetaXdr"    : "",
            "ledger"           : NEWL,
            "createdAt"        : #getString( "now", REQ )
        }

    rule <k> #respondTx( REQ, STATUS, LL )
          => #respond( #getJSON( "id", REQ ), {
                 "hash"                  : #getString( "txHash", REQ ),
                 "status"                : STATUS,
                 "latestLedger"          : LL,
                 "latestLedgerCloseTime" : #getString( "now", REQ )
             })
             ...
         </k>
```

## traceTransaction

Retrieve the execution trace of a previously submitted transaction, looked up by `hash` (the
same parameter `getTransaction` takes). The trace was written to `traces/trace_<hash>.jsonl`
by `sendTransaction`. Responds with the trace file's contents, or `null` when no trace file
exists for that hash.

```k
    rule <k> #dispatchMethod( "traceTransaction", REQ )
          => #respondTrace( #getJSON( "id", REQ ), #getString( "hash", REQ ) )
             ...
         </k>

    rule <k> #respondTrace( ID, HASH ) => #respond( ID, {#readFile( #traceFile( HASH ) )}:>String ) ... </k>
      requires #fileExists( #traceFile( HASH ) )
    rule <k> #respondTrace( ID, HASH ) => #respond( ID, null ) ... </k>
      requires notBool #fileExists( #traceFile( HASH ) )
```

###############################################################################
## getTransactions / getLedgers

The history methods are served from the per-ledger index files `ledgers/ledger_<seq>.json`
that the Python server writes whenever a transaction closes a ledger. Each file carries the
ledger's `sequence`, the `txHash` of the transaction that closed it, its `closedAt` unix
time, and the ledger-header artifacts (`hash`, `headerXdr`, `metadataXdr`) — the latter are
XDR, which only Python can construct. Parameter validation (limit range, `startLedger`
bounds, `cursor`/`startLedger` exclusivity, resolving the cursor) also happens in the
server, because it needs the JSON-RPC error path; the envelope arrives here with a
validated `startSeq` (first ledger sequence to serve) and `limit`, and these rules only
collect the records and format the response.

Serialization notes, matching real stellar-rpc (the Go protocol structs win over the
OpenRPC doc):
  - ledger sequences and both top-level close-time fields are JSON numbers, and the
    close-time keys differ between the methods (`latestLedgerCloseTimestamp` here,
    `latestLedgerCloseTime` on getLedgers) — an upstream quirk, kept as is;
  - per-transaction `createdAt` is a JSON number, unlike the singular getTransaction;
  - per-ledger `ledgerCloseTime` is a string holding a decimal number (Go `,string`);
  - `resultXdr`/`resultMetaXdr` are `omitempty`: omitted while receipts carry empty stubs;
  - `cursor` names the page's last record when the page is full (a TOID for transactions:
    `ledger << 32 | applicationOrder << 12`), and is empty otherwise.

```k
    rule <k> #dispatchMethod( "getTransactions", REQ )
          => #respond( #getJSON( "id", REQ ), #getTransactionsResult(
                 #getInt( "startSeq", REQ ),
                 #getInt( "limit", REQ ),
                 #getInt( "latest_ledger", #readJSONFile( "metadata.json" ) )
             ))
             ...
         </k>

    rule <k> #dispatchMethod( "getLedgers", REQ )
          => #respond( #getJSON( "id", REQ ), #getLedgersResult(
                 #getInt( "startSeq", REQ ),
                 #getInt( "limit", REQ ),
                 #getInt( "latest_ledger", #readJSONFile( "metadata.json" ) )
             ))
             ...
         </k>

    syntax JSON ::= #getTransactionsResult( Int, Int, Int ) [function, symbol(getTransactionsResult)]
                  | #txHistoryPage( JSONs, Int, Int )       [function, symbol(txHistoryPage)]
 // -------------------------------------------------------------------------------------------------
    rule #getTransactionsResult( START, LIMIT, LL ) => #txHistoryPage( #txInfos( START, LIMIT, LL ), LIMIT, LL )

    rule #txHistoryPage( TXS, LIMIT, LL ) => {
            "transactions"               : [ TXS ],
            "latestLedger"               : LL,
            "latestLedgerCloseTimestamp" : #ledgerCloseTimeOf( LL ),
            "oldestLedger"               : 0,
            "oldestLedgerCloseTimestamp" : 0,
            "cursor"                     : #pageCursor( TXS, LIMIT, "ledger", 4294967296, 4096 )
        }

    syntax JSON ::= #getLedgersResult( Int, Int, Int )    [function, symbol(getLedgersResult)]
                  | #ledgerHistoryPage( JSONs, Int, Int ) [function, symbol(ledgerHistoryPage)]
 // -------------------------------------------------------------------------------------------
    rule #getLedgersResult( START, LIMIT, LL ) => #ledgerHistoryPage( #ledgerInfos( START, LIMIT, LL ), LIMIT, LL )

    rule #ledgerHistoryPage( LS, LIMIT, LL ) => {
            "ledgers"               : [ LS ],
            "latestLedger"          : LL,
            "latestLedgerCloseTime" : #ledgerCloseTimeOf( LL ),
            "oldestLedger"          : 0,
            "oldestLedgerCloseTime" : 0,
            "cursor"                : #pageCursor( LS, LIMIT, "sequence", 1, 0 )
        }
```

Both collectors walk the ledger sequence upwards from `startSeq` to the latest ledger,
taking at most `limit` records. Ledgers without an index file are skipped: the genesis
ledger 0 never has one, and neither do ledgers closed by io-dirs predating the index.
Failed transactions never closed a ledger, so they do not appear in the history.

```k
    syntax JSONs ::= #txInfos( Int, Int, Int ) [function, symbol(txInfos)]
 // ----------------------------------------------------------------------
    rule #txInfos( _, LIMIT, _ )    => .JSONs requires LIMIT <=Int 0
    rule #txInfos( SEQ, LIMIT, LL ) => .JSONs requires LIMIT >Int 0 andBool SEQ >Int LL
    rule #txInfos( SEQ, LIMIT, LL ) => #txInfos( SEQ +Int 1, LIMIT, LL )
      requires LIMIT >Int 0 andBool SEQ <=Int LL andBool notBool #fileExists( #ledgerFile( SEQ ) )
    rule #txInfos( SEQ, LIMIT, LL )
      => ( #txInfoOf( SEQ, #getString( "txHash", #readJSONFile( #ledgerFile( SEQ ) ) ) )
         , #txInfos( SEQ +Int 1, LIMIT -Int 1, LL ) )
      requires LIMIT >Int 0 andBool SEQ <=Int LL andBool #fileExists( #ledgerFile( SEQ ) )

    syntax JSONs ::= #ledgerInfos( Int, Int, Int ) [function, symbol(ledgerInfos)]
 // ------------------------------------------------------------------------------
    rule #ledgerInfos( _, LIMIT, _ )    => .JSONs requires LIMIT <=Int 0
    rule #ledgerInfos( SEQ, LIMIT, LL ) => .JSONs requires LIMIT >Int 0 andBool SEQ >Int LL
    rule #ledgerInfos( SEQ, LIMIT, LL ) => #ledgerInfos( SEQ +Int 1, LIMIT, LL )
      requires LIMIT >Int 0 andBool SEQ <=Int LL andBool notBool #fileExists( #ledgerFile( SEQ ) )
    rule #ledgerInfos( SEQ, LIMIT, LL )
      => ( #ledgerInfoOf( #readJSONFile( #ledgerFile( SEQ ) ) )
         , #ledgerInfos( SEQ +Int 1, LIMIT -Int 1, LL ) )
      requires LIMIT >Int 0 andBool SEQ <=Int LL andBool #fileExists( #ledgerFile( SEQ ) )
```

One transaction record, in the field set and order of the Go `TransactionInfo` struct.
The receipt provides the status, the envelope, and the creation time; the ledger sequence
comes from the index. Each ledger holds exactly one transaction on this node, so
`applicationOrder` is always 1, and fee-bump envelopes are not supported, so `feeBump` is
always false.

```k
    syntax JSON ::= #txInfoOf( Int, String )         [function, symbol(txInfoOf)]
                  | #txInfoFrom( Int, String, JSON ) [function, symbol(txInfoFrom)]
 // -------------------------------------------------------------------------------
    rule #txInfoOf( SEQ, HASH ) => #txInfoFrom( SEQ, HASH, #readJSONFile( #receiptFile( HASH ) ) )

    rule #txInfoFrom( SEQ, HASH, RCPT ) => { #concatJSONs(
            ( "status"           : #getJSON( "status", RCPT ),
              "txHash"           : HASH,
              "applicationOrder" : 1,
              "feeBump"          : false,
              "envelopeXdr"      : #getJSON( "envelopeXdr", RCPT ),
              .JSONs ),
            #concatJSONs(
                #optXdrEntry( "resultXdr", #getJSON( "resultXdr", RCPT, "" ) ),
                #concatJSONs(
                    #optXdrEntry( "resultMetaXdr", #getJSON( "resultMetaXdr", RCPT, "" ) ),
                    ( "ledger"    : SEQ,
                      "createdAt" : #asInt( #getJSON( "createdAt", RCPT ) ),
                      .JSONs )
                )
            )
        )}

    // An entry for an optional (`omitempty`) XDR field: omitted when the stored value is
    // an empty stub or the receipt predates the field.
    syntax JSONs ::= #optXdrEntry( JSONKey, JSON ) [function, symbol(optXdrEntry)]
 // ------------------------------------------------------------------------------
    rule #optXdrEntry( _, ""   ) => .JSONs
    rule #optXdrEntry( _, null ) => .JSONs
    rule #optXdrEntry( KEY, V  ) => ( KEY : V, .JSONs ) [owise]

    // One ledger record, straight from the index file.
    syntax JSON ::= #ledgerInfoOf( JSON ) [function, symbol(ledgerInfoOf)]
 // ----------------------------------------------------------------------
    rule #ledgerInfoOf( L ) => {
            "hash"            : #getJSON( "hash", L ),
            "sequence"        : #asInt( #getJSON( "sequence", L ) ),
            "ledgerCloseTime" : Int2String( #asInt( #getJSON( "closedAt", L ) ) ),
            "headerXdr"       : #getJSON( "headerXdr", L ),
            "metadataXdr"     : #getJSON( "metadataXdr", L )
        }
```

Shared helpers for the history methods.

```k
    syntax String ::= #ledgerFile( Int ) [function, symbol(ledgerFile)]
 // -------------------------------------------------------------------
    rule #ledgerFile( SEQ ) => "ledgers/ledger_" +String Int2String(SEQ) +String ".json"

    syntax JSON ::= #readJSONFile( String ) [function, symbol(readJSONFile)]
 // ------------------------------------------------------------------------
    rule #readJSONFile( FILE ) => String2JSON( {#readFile( FILE )}:>String )

    // Coerce a JSON number-or-decimal-string to Int (receipts store some ints as strings).
    syntax Int ::= #asInt( JSON ) [function, symbol(asInt)]
 // -------------------------------------------------------
    rule #asInt( I:Int )    => I
    rule #asInt( S:String ) => String2Int( S )

    // The close time recorded for a ledger, or 0 when it has no index file (genesis).
    syntax Int ::= #ledgerCloseTimeOf( Int ) [function, symbol(ledgerCloseTimeOf)]
 // ------------------------------------------------------------------------------
    rule #ledgerCloseTimeOf( SEQ ) => #asInt( #getJSON( "closedAt", #readJSONFile( #ledgerFile( SEQ ) ) ) )
      requires #fileExists( #ledgerFile( SEQ ) )
    rule #ledgerCloseTimeOf( SEQ ) => 0
      requires notBool #fileExists( #ledgerFile( SEQ ) )

    // The paging token to return: the position of the page's last record when the page is
    // full (more records may follow), the empty string otherwise. The position is the value
    // of KEY in the last record, scaled as POS *Int MULT +Int OFFSET — the TOID encoding
    // for getTransactions (MULT 2^32, OFFSET 1 << 12), the plain sequence for getLedgers
    // (MULT 1, OFFSET 0). The server resolves cursors back to a start sequence.
    syntax JSON ::= #pageCursor( JSONs, Int, JSONKey, Int, Int ) [function, symbol(pageCursor)]
 // -------------------------------------------------------------------------------------------
    rule #pageCursor( RECORDS, LIMIT, KEY, MULT, OFFSET )
      => Int2String( #lastIntIn( KEY, RECORDS ) *Int MULT +Int OFFSET )
      requires #lengthJSONs( RECORDS ) ==Int LIMIT
    rule #pageCursor( RECORDS, LIMIT, _, _, _ ) => ""
      requires #lengthJSONs( RECORDS ) =/=Int LIMIT

    syntax Int ::= #lengthJSONs( JSONs ) [function, symbol(lengthJSONs)]
 // --------------------------------------------------------------------
    rule #lengthJSONs( .JSONs )      => 0
    rule #lengthJSONs( ( _, REST ) ) => 1 +Int #lengthJSONs( REST )

    // The value of KEY in the last object of a non-empty list, as an Int.
    syntax Int ::= #lastIntIn( JSONKey, JSONs ) [function, symbol(lastIntIn)]
 // -------------------------------------------------------------------------
    rule #lastIntIn( KEY, ( J:JSON, .JSONs ) )             => #getInt( KEY, J )
    rule #lastIntIn( KEY, ( _:JSON, J:JSON, REST:JSONs ) ) => #lastIntIn( KEY, ( J, REST ) )
```

###############################################################################
# Step decoding

Each step of a transaction is decoded from JSON into a kasmer `Step`. Key order is
significant — it must match the Python encoders in `transaction.py` (`TransactionEncoder`)
and `scval.py` (`scval_to_json`, for the `callTx` args).

  { "op": "setLedgerSequence", "sequence": <int> }
  { "op": "setAccount",        "account": "<hex32>", "balance": <int> }
  { "op": "deployContract",    "from": "<hex32>", "address": "<hex32>", "wasmHash": "<hex32>" }
  { "op": "callTx",            "from": "<hex32>", "fromIsContract": <bool>,
                                "func": "<name>", "to": "<hex32>", "args": [ <scval>, ... ] }

SCVal arg encoding (key order also significant):

  { "type": "bool",    "value": <bool>   }
  { "type": "i32",     "value": <int>    }
  { "type": "u32",     "value": <int>    }
  { "type": "i64",     "value": <int>    }
  { "type": "u64",     "value": <int>    }
  { "type": "i128",    "value": <int>    }
  { "type": "u128",    "value": <int>    }
  { "type": "symbol",  "value": "<str>"  }
  { "type": "bytes",   "value": "<hex>"  }
  { "type": "address", "addrType": "account"|"contract", "value": "<hex32>" }

```k
    syntax Steps ::= #decodeSteps(JSONs)   [function]
    syntax Step  ::= #decodeStep(JSON)     [function]

    rule #decodeSteps(.JSONs)                     => .Steps
    rule #decodeSteps(S:JSON, SS:JSONs)           => #decodeStep(S) #decodeSteps(SS)

    rule #decodeStep({ "op" : "setLedgerSequence" , "sequence" : SEQ:Int })
        => setLedgerSequence(SEQ)

    rule #decodeStep({ "op" : "setAccount" , "account" : ACCT:String , "balance" : BAL:Int })
        => setAccount(Account(HexBytes(ACCT)), BAL)

    rule #decodeStep({ "op" : "deployContract" , "from" : FROM:String , "address" : ADDR:String , "wasmHash" : HASH:String })
        => deployContract(Account(HexBytes(FROM)), Contract(HexBytes(ADDR)), HexBytes(HASH))

    rule #decodeStep({ "op" : "callTx" , "from" : FROM:String , "fromIsContract" : false , "func" : FUNC:String , "to" : TO:String , "args" : [ARGS:JSONs] })
        => uncheckedCallTx(Account(HexBytes(FROM)), Contract(HexBytes(TO)), string2WasmToken("\"" +String FUNC +String "\""), #decodeArgList(ARGS))

    rule #decodeStep({ "op" : "callTx" , "from" : FROM:String , "fromIsContract" : true , "func" : FUNC:String , "to" : TO:String , "args" : [ARGS:JSONs] })
        => uncheckedCallTx(Contract(HexBytes(FROM)), Contract(HexBytes(TO)), string2WasmToken("\"" +String FUNC +String "\""), #decodeArgList(ARGS))

    syntax List  ::= #decodeArgList(JSONs) [function]
    syntax ScVal ::= #decodeArg(JSON)      [function]

    rule #decodeArgList(.JSONs)           => .List
    rule #decodeArgList(A:JSON, AS:JSONs) => ListItem(#decodeArg(A)) #decodeArgList(AS)

    rule #decodeArg({ "type" : "bool"    , "value" : V:Bool   }) => SCBool(V)
    rule #decodeArg({ "type" : "i32"     , "value" : V:Int    }) => I32(V)
    rule #decodeArg({ "type" : "u32"     , "value" : V:Int    }) => U32(V)
    rule #decodeArg({ "type" : "i64"     , "value" : V:Int    }) => I64(V)
    rule #decodeArg({ "type" : "u64"     , "value" : V:Int    }) => U64(V)
    rule #decodeArg({ "type" : "i128"    , "value" : V:Int    }) => I128(V)
    rule #decodeArg({ "type" : "u128"    , "value" : V:Int    }) => U128(V)
    rule #decodeArg({ "type" : "symbol"  , "value" : V:String }) => Symbol(V)
    rule #decodeArg({ "type" : "bytes"   , "value" : V:String }) => ScBytes(HexBytes(V))
    rule #decodeArg({ "type" : "address" , "addrType" : "account"  , "value" : V:String }) => ScAddress(Account(HexBytes(V)))
    rule #decodeArg({ "type" : "address" , "addrType" : "contract" , "value" : V:String }) => ScAddress(Contract(HexBytes(V)))
```

`uncheckedCallTx` is like komet's `callTx` but it does not entail a return value check.


```k
    syntax Step ::= uncheckedCallTx( from: Address, to: Address, func: WasmString, args: List)     [symbol(uncheckedCallTx)]

    rule [uncheckedCallTx]:
        <k> uncheckedCallTx(FROM, TO, FUNC, ARGS)
         => allocObjects(ARGS)
         ~> callContractFromStack(FROM, TO, FUNC)
         ~> #resetHost
            ...
        </k>
        // clear the host cell before contract calls
        (_:HostCell => <host> <hostStack> .HostStack </hostStack> ... </host>)

endmodule
```

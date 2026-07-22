
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
                   | #simulateStep( JSON )
                   | #simulateRespond( JSON )
                   | #respondError( JSON, Int, String )
                   | #respondHealth( JSON, String, Int )
                   | #respondLatestLedger( JSON, Int, Int )
                   | #getEvents( JSON, Int )
                   | #respondEvents( JSON, Int, JSONs )

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

The inverse direction — Bytes to a lowercase, zero-padded hex string — is K's built-in
`Bytes2Hex` (hook `BYTES.bytes2hex`), used by the getLedgerEntries rules below.

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

`getVersionInfo` echoes the version strings the Python server put into the request envelope
(the package versions live in Python's package metadata, which K cannot read). Its
`protocolVersion` is a JSON number, per the spec and real stellar-rpc.

```k
    rule <k> #dispatchMethod( "getVersionInfo", REQ )
          => #respond( #getJSON( "id", REQ ), {
                 "version"            : #getString( "version", REQ ),
                 "commitHash"         : #getString( "commitHash", REQ ),
                 "buildTimestamp"     : #getString( "buildTimestamp", REQ ),
                 "captiveCoreVersion" : #getString( "captiveCoreVersion", REQ ),
                 "protocolVersion"    : #getInt( "protocolVersion", REQ )
             })
             ...
         </k>
```

`getFeeStats` reports a constant fee distribution: komet-node has no fee market (transactions
execute immediately and pay no fees), so every statistic is the network minimum inclusion fee
of 100 stroops over an empty sample. Real stellar-rpc serialises every distribution field
except `ledgerCount` with Go's `,string` option, so those are JSON strings holding decimal
numbers, while `ledgerCount` and `latestLedger` are JSON numbers. `latestLedger` is read live
from `metadata.json`.

```k
    rule <k> #dispatchMethod( "getFeeStats", REQ )
          => #respond( #getJSON( "id", REQ ), {
                 "sorobanInclusionFee" : #feeDistribution,
                 "inclusionFee"        : #feeDistribution,
                 "latestLedger"        : #getInt( "latest_ledger", String2JSON( {#readFile("metadata.json")}:>String ) )
             })
             ...
         </k>

    syntax JSON ::= "#feeDistribution" [function, symbol(feeDistribution)]
 // ----------------------------------------------------------------------
    rule #feeDistribution => {
            "max"  : "100",
            "min"  : "100",
            "mode" : "100",
            "p10"  : "100",
            "p20"  : "100",
            "p30"  : "100",
            "p40"  : "100",
            "p50"  : "100",
            "p60"  : "100",
            "p70"  : "100",
            "p80"  : "100",
            "p90"  : "100",
            "p95"  : "100",
            "p99"  : "100",
            "transactionCount" : "0",
            "ledgerCount"      : 0
        }
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

The receipt also carries the contract call's return value (if the transaction made one):
after `uncheckedCallTx` runs, the call's result `ScVal` is still sitting on the
`<hostStack>` (see below), so we serialise it into the receipt's internal `returnValue`
field and only then reset the host. The Python server immediately rewrites that field into
the spec-mandated `resultXdr`/`resultMetaXdr` base64 XDR fields (K cannot construct XDR), so
`returnValue` never reaches an RPC client.

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
                 JSON2String( #txReceipt( REQ, L +Int 1, #returnValueJSON( STACK ) ) ) )
          ~> #resetHost
          ~> #respondTx( REQ, "PENDING", L +Int 1 )
             ...
         </k>
         <hostStack> STACK </hostStack>

    syntax JSON ::= #txReceipt( JSON, Int, JSON ) [function, symbol(txReceipt)]
 // ---------------------------------------------------------------------------
    rule #txReceipt( REQ, NEWL, RETVAL ) => {
            "status"           : "SUCCESS",
            "applicationOrder" : 1,
            "feeBump"          : false,
            "envelopeXdr"      : #getString( "envelopeXdr", REQ ),
            "ledger"           : NEWL,
            "createdAt"        : #getString( "now", REQ ),
            "returnValue"      : RETVAL
        }

    // The return value of the transaction's contract call: the ScVal left on top of the
    // host stack, or null when the transaction made no contract call.
    syntax JSON ::= #returnValueJSON( HostStack ) [function, total, symbol(returnValueJSON)]
 // ----------------------------------------------------------------------------------------
    rule #returnValueJSON( VAL:ScVal : _ ) => #scValToJSON( VAL )
    rule #returnValueJSON( _ )             => null                 [owise]

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
by `sendTransaction`. The file is JSONL (one JSON record per executed instruction); we parse
it into a JSON array so the result is structured data rather than an opaque string. Responds
with that array — empty when the transaction ran no instructions — or `null` when no trace
file exists for that hash.

```k
    rule <k> #dispatchMethod( "traceTransaction", REQ )
          => #respondTrace( #getJSON( "id", REQ ), #getString( "hash", REQ ) )
             ...
         </k>

    rule <k> #respondTrace( ID, HASH ) => #respond( ID, [ #parseTraceLines( {#readFile( #traceFile( HASH ) )}:>String ) ] ) ... </k>
      requires #fileExists( #traceFile( HASH ) )
    rule <k> #respondTrace( ID, HASH ) => #respond( ID, null ) ... </k>
      requires notBool #fileExists( #traceFile( HASH ) )
```

`#parseTraceLines` turns the JSONL trace text into a `JSONs` list, parsing each newline-
delimited record with `String2JSON`. Empty segments (a leading/blank line, or the empty
tail after the final record's trailing newline) are skipped, so an empty file yields `.JSONs`
(an empty array).

```k
    syntax JSONs ::= #parseTraceLines( String ) [function, symbol(parseTraceLines)]
 // -------------------------------------------------------------------------------
    rule #parseTraceLines( "" ) => .JSONs

    // No more newlines: the whole remaining string is the final record.
    rule #parseTraceLines( S ) => String2JSON( S ) , .JSONs
      requires S =/=String "" andBool findString( S, "\n", 0 ) <Int 0

    // Split off the first line and recurse on the rest.
    rule #parseTraceLines( S )
      => String2JSON( substrString( S, 0, findString( S, "\n", 0 ) ) )
       , #parseTraceLines( substrString( S, findString( S, "\n", 0 ) +Int 1, lengthString( S ) ) )
      requires findString( S, "\n", 0 ) >Int 0

    // Empty leading line (the string starts with a newline): drop it and recurse.
    rule #parseTraceLines( S )
      => #parseTraceLines( substrString( S, 1, lengthString( S ) ) )
      requires findString( S, "\n", 0 ) ==Int 0
```

## simulateTransaction

Run a single contract invocation against the current world state *without committing
anything*: no receipt, no trace, no ledger bump, and the Python server does not persist the
resulting configuration (`interpreter.run(..., commit=False)`). Tracing stays disabled
because `<ioDir>` is left empty.

The request envelope carries exactly one `callTx` step (the server rejects anything else
before dispatching here). After the call, the invocation's return value sits on the
`<hostStack>` as a fully resolved `ScVal` — the one thing `sendTransaction` discards and
simulation exists to report.

The K side responds with an *internal* result, `{ "latestLedger": N, "returnValue": <scval
json> }` on success or `{ "latestLedger": N, "error": <string> }` when the call trapped.
The server maps it to the spec response shape: the return value must be serialized as
base64 SCVal XDR and `transactionData` as base64 `SorobanTransactionData`, and K can build
neither (no XDR encoder, no base64 hook), so that final serialization step lives in Python
(`server.py`). A simulation that gets stuck (e.g. calling a contract that does not exist)
produces no `response.json`, and the server synthesises the error result.

```k
    rule <k> #dispatchMethod( "simulateTransaction", REQ )
          => setLedgerSequence( #getInt( "latest_ledger", String2JSON( {#readFile("metadata.json")}:>String ) ) )
          ~> #simulateStep( #firstJSON( #getJSON( "steps", REQ, [ .JSONs ] ) ) )
          ~> #simulateRespond( #getJSON( "id", REQ ) )
             ...
         </k>

    syntax JSON ::= #firstJSON( JSON ) [function, symbol(firstJSON)]
 // ----------------------------------------------------------------
    rule #firstJSON( [ J , _ ] ) => J
```

Like komet's `callTx`, the invocation clears the `<host>` cell first, so afterwards the
stack holds exactly the call's result. Unlike `callTx` (and `uncheckedCallTx`), there is no
`#resetHost` after the call: `#simulateRespond` still needs the result, and the
configuration is thrown away when the run ends, so nothing leaks into later requests. The
step patterns must mirror the `callTx` case of `#decodeStep` (key order is significant).

```k
    rule [simulateStep-account]:
        <k> #simulateStep({ "op" : "callTx" , "from" : FROM:String , "fromIsContract" : false , "func" : FUNC:String , "to" : TO:String , "args" : [ ARGS:JSONs ] })
         => allocObjects(#decodeArgList(ARGS))
         ~> callContractFromStack(Account(HexBytes(FROM)), Contract(HexBytes(TO)), string2WasmToken("\"" +String FUNC +String "\""))
            ...
        </k>
        (_:HostCell => <host> <hostStack> .HostStack </hostStack> ... </host>)

    rule [simulateStep-contract]:
        <k> #simulateStep({ "op" : "callTx" , "from" : FROM:String , "fromIsContract" : true , "func" : FUNC:String , "to" : TO:String , "args" : [ ARGS:JSONs ] })
         => allocObjects(#decodeArgList(ARGS))
         ~> callContractFromStack(Contract(HexBytes(FROM)), Contract(HexBytes(TO)), string2WasmToken("\"" +String FUNC +String "\""))
            ...
        </k>
        (_:HostCell => <host> <hostStack> .HostStack </hostStack> ... </host>)

    rule [simulateRespond]:
        <k> #simulateRespond( ID )
          => #respond( ID, #simulateResult( SCVAL,
                 #getInt( "latest_ledger", String2JSON( {#readFile("metadata.json")}:>String ) ) ) )
             ...
         </k>
         <hostStack> SCVAL:ScVal : _ </hostStack>

    // A trapped call self-heals (the world state is rolled back by `endWasm-error`) and
    // leaves an `Error` ScVal on the stack; report it as a result-level error.
    syntax JSON ::= #simulateResult( ScVal, Int ) [function, symbol(simulateResult)]
 // --------------------------------------------------------------------------------
    rule #simulateResult( Error(T, C), LL ) => {
            "latestLedger" : LL,
            "error"        : "host function invocation failed (error type "
                             +String Int2String(ErrorType2Int(T))
                             +String ", code " +String Int2String(C) +String ")"
        }
    rule #simulateResult( SCVAL, LL ) => {
            "latestLedger" : LL,
            "returnValue"  : #scValJSON( SCVAL )
        } [owise]
```

`#scValJSON` encodes an `ScVal` return value as JSON for the Python side to XDR-serialize
(`scval_from_json` in `scval.py` is its inverse — keep the two in sync). Python reads these
objects with order-independent lookups, but the fields are emitted in the same order as the
`#decodeArg` patterns for consistency. Unsupported return types (`ScMap`, unresolved host
values, ...) have no rule: the run gets stuck and the server reports a simulation error.

```k
    syntax JSON ::= #scValJSON( ScVal ) [function, symbol(scValJSON)]
 // -----------------------------------------------------------------
    rule #scValJSON( SCBool(B)   ) => { "type" : "bool"   , "value" : B }
    rule #scValJSON( Void        ) => { "type" : "void" }
    rule #scValJSON( U32(I)      ) => { "type" : "u32"    , "value" : I }
    rule #scValJSON( I32(I)      ) => { "type" : "i32"    , "value" : I }
    rule #scValJSON( U64(I)      ) => { "type" : "u64"    , "value" : I }
    rule #scValJSON( I64(I)      ) => { "type" : "i64"    , "value" : I }
    rule #scValJSON( U128(I)     ) => { "type" : "u128"   , "value" : I }
    rule #scValJSON( I128(I)     ) => { "type" : "i128"   , "value" : I }
    rule #scValJSON( Symbol(S)   ) => { "type" : "symbol" , "value" : S }
    rule #scValJSON( ScString(S) ) => { "type" : "string" , "value" : S }
    rule #scValJSON( ScBytes(B)  ) => { "type" : "bytes"  , "value" : Bytes2Hex(B) }
    rule #scValJSON( ScAddress(Account(B))  ) => { "type" : "address" , "addrType" : "account"  , "value" : Bytes2Hex(B) }
    rule #scValJSON( ScAddress(Contract(B)) ) => { "type" : "address" , "addrType" : "contract" , "value" : Bytes2Hex(B) }
    rule #scValJSON( ScVec(L)    ) => { "type" : "vec"    , "value" : [ #scValJSONs(L) ] }

    syntax JSONs ::= #scValJSONs( List ) [function, symbol(scValJSONs)]
 // -------------------------------------------------------------------
    rule #scValJSONs( .List )                => .JSONs
    rule #scValJSONs( ListItem(V:ScVal) L )  => #scValJSON(V) , #scValJSONs(L)
```

`Bytes2Hex` (the inverse of `HexBytes`, two hex digits per byte) is K's hooked builtin from
the BYTES module; the Python decoder (`bytes.fromhex`) accepts either letter case.

## getLedgerEntries

The Python server decodes each base64 `LedgerKey` of the request (K cannot parse XDR)
into a JSON *key descriptor* and sends the list as the `keys` field of the request
envelope. The rules below look each descriptor up in the world-state cells and reply with
an intermediate JSON entry per *found* key — unknown keys are silently skipped, per the
spec. The server then re-encodes each intermediate entry as base64 `LedgerEntryData` XDR
and rebuilds the `entries` array (`ledger_entries.py`), the one step of this method that
cannot be done in K.

Descriptor shapes (key order is significant — it must match `ledger_entries.py`):

  { "kind": "account",          "key": "<b64>", "accountId": "<hex32>" }
  { "kind": "contractCode",     "key": "<b64>", "hash": "<hex32>" }
  { "kind": "contractInstance", "key": "<b64>", "contract": "<hex32>" }
  { "kind": "contractData",     "key": "<b64>", "contract": "<hex32>",
                                "durability": "persistent"|"temporary", "scKey": <scval> }
  { "kind": "unsupported",      "key": "<b64>" }

```k
    syntax KItem ::= #ledgerEntries( JSON, JSONs, JSONs )
                   | #contractDataEntry( JSON, String, StorageKey, JSONs, JSONs )

    rule <k> #dispatchMethod( "getLedgerEntries", REQ )
          => #ledgerEntries(
                 #getJSON( "id", REQ ),
                 #stepsJSONs( #getJSON( "keys", REQ, [ .JSONs ] ) ),
                 .JSONs
             )
             ...
         </k>
```

All keys processed: respond with the found entries and the current ledger.
`latestLedger` is emitted as an Int, i.e. a JSON number, as the spec requires.

```k
    rule <k> #ledgerEntries( ID, .JSONs, ENTRIES )
          => #respond( ID, {
                 "entries"      : [ ENTRIES ],
                 "latestLedger" : #getInt( "latest_ledger", String2JSON( {#readFile("metadata.json")}:>String ) )
             })
             ...
         </k>
```

ACCOUNT keys resolve against the `<accounts>` cell. The semantics track only the balance;
the remaining `AccountEntry` fields are synthesised by the server.

```k
    rule <k> #ledgerEntries( ID, ({ "kind" : "account", "key" : KEY:String, "accountId" : AID:String }, REST:JSONs), ENTRIES )
          => #ledgerEntries( ID, REST, #concatJSONs( ENTRIES,
                 ({ "kind" : "account", "key" : KEY, "balance" : BAL }, .JSONs) ) )
             ...
         </k>
         <account>
           <accountId> Account(ACCTID) </accountId>
           <balance> BAL </balance>
         </account>
      requires ACCTID ==K HexBytes(AID)
```

CONTRACT_CODE keys resolve against `<contractCodes>`. The stored wasm is a parsed
`ModuleDecl` whose original bytes cannot be recovered here, so the entry reports only the
hash and TTL; the server keeps the raw bytes (written at upload time, `wasms/<hash>.wasm`)
and reattaches them when building the XDR.

```k
    rule <k> #ledgerEntries( ID, ({ "kind" : "contractCode", "key" : KEY:String, "hash" : HASH:String }, REST:JSONs), ENTRIES )
          => #ledgerEntries( ID, REST, #concatJSONs( ENTRIES,
                 ({ "kind" : "contractCode", "key" : KEY, "hash" : HASH, "liveUntil" : LIVE }, .JSONs) ) )
             ...
         </k>
         <contractCode>
           <codeHash> CH </codeHash>
           <codeLiveUntil> LIVE </codeLiveUntil>
           ...
         </contractCode>
      requires CH ==K HexBytes(HASH)
```

A CONTRACT_DATA key whose ScVal key is `SCV_LEDGER_KEY_CONTRACT_INSTANCE` resolves
against the `<contracts>` cell: the entry carries the wasm hash the instance points at
and the contract's instance storage.

```k
    rule <k> #ledgerEntries( ID, ({ "kind" : "contractInstance", "key" : KEY:String, "contract" : CADDR:String }, REST:JSONs), ENTRIES )
          => #ledgerEntries( ID, REST, #concatJSONs( ENTRIES,
                 ({ "kind"      : "contractInstance",
                    "key"       : KEY,
                    "wasmHash"  : Bytes2Hex(WH),
                    "storage"   : [ #scMapEntries2JSONs( keys_list(ISTORE), ISTORE ) ],
                    "liveUntil" : LIVE }, .JSONs) ) )
             ...
         </k>
         <contract>
           <contractId> Contract(CID) </contractId>
           <wasmHash> WH </wasmHash>
           <instanceStorage> ISTORE </instanceStorage>
           <contractLiveUntil> LIVE </contractLiveUntil>
         </contract>
      requires CID ==K HexBytes(CADDR)
```

Other CONTRACT_DATA keys (persistent/temporary storage) resolve against the
`<contractData>` map. The storage-key ScVal is rebuilt with `#decodeArg` — the same
decoder the transaction path uses — so it matches the stored `#skey` exactly.

```k
    rule <k> #ledgerEntries( ID, ({ "kind" : "contractData", "key" : KEY:String, "contract" : CADDR:String, "durability" : DUR:String, "scKey" : SK:JSON }, REST:JSONs), ENTRIES )
          => #contractDataEntry( ID, KEY, #skey( Contract(HexBytes(CADDR)), #decodeDurability(DUR), #decodeArg(SK) ), REST, ENTRIES )
             ...
         </k>

    rule <k> #contractDataEntry( ID, KEY, SKEY, REST, ENTRIES )
          => #ledgerEntries( ID, REST, #concatJSONs( ENTRIES,
                 ({ "kind"      : "contractData",
                    "key"       : KEY,
                    "val"       : #scVal2JSON( #svalData( {CDATA[SKEY]}:>StorageVal ) ),
                    "liveUntil" : #svalLive( {CDATA[SKEY]}:>StorageVal ) }, .JSONs) ) )
             ...
         </k>
         <contractData> CDATA </contractData>
      requires SKEY in_keys(CDATA)

    rule <k> #contractDataEntry( ID, _KEY, SKEY, REST, ENTRIES ) => #ledgerEntries( ID, REST, ENTRIES ) ... </k>
         <contractData> CDATA </contractData>
      requires notBool SKEY in_keys(CDATA)
```

Any other key — an unsupported entry type, or a supported type not present in the world
state — is not an error: it is skipped ([owise] fires when no lookup rule above matched).

```k
    rule <k> #ledgerEntries( ID, (_KEY:JSON, REST:JSONs), ENTRIES ) => #ledgerEntries( ID, REST, ENTRIES ) ... </k> [owise]

    syntax Durability ::= #decodeDurability( String ) [function]
 // ------------------------------------------------------------
    rule #decodeDurability( "persistent" ) => #persistent
    rule #decodeDurability( "temporary" )  => #temporary

    syntax ScVal ::= #svalData( StorageVal ) [function]
    syntax Int   ::= #svalLive( StorageVal ) [function]
 // ---------------------------------------------------
    rule #svalData( #sval( VAL, _ ) )  => VAL
    rule #svalLive( #sval( _, LIVE ) ) => LIVE
```

`#scVal2JSON` serialises a stored ScVal back to the JSON encoding that `#decodeArg`
consumes (same shapes, same key order), extended with the value-only types that never
appear as call arguments (`void`, `string`, `u256`, `vec`, `map`). Values with no JSON
form are emitted as `{"type": "unsupported"}`; the server drops the enclosing entry.

```k
    syntax JSON ::= #scVal2JSON( ScVal ) [function, total, symbol(scVal2JSON)]
 // --------------------------------------------------------------------------
    rule #scVal2JSON( SCBool(B) )   => { "type" : "bool",   "value" : B }
    rule #scVal2JSON( I32(V) )      => { "type" : "i32",    "value" : V }
    rule #scVal2JSON( U32(V) )      => { "type" : "u32",    "value" : V }
    rule #scVal2JSON( I64(V) )      => { "type" : "i64",    "value" : V }
    rule #scVal2JSON( U64(V) )      => { "type" : "u64",    "value" : V }
    rule #scVal2JSON( I128(V) )     => { "type" : "i128",   "value" : V }
    rule #scVal2JSON( U128(V) )     => { "type" : "u128",   "value" : V }
    rule #scVal2JSON( U256(V) )     => { "type" : "u256",   "value" : V }
    rule #scVal2JSON( Symbol(S) )   => { "type" : "symbol", "value" : S }
    rule #scVal2JSON( ScBytes(B) )  => { "type" : "bytes",  "value" : Bytes2Hex(B) }
    rule #scVal2JSON( ScString(S) ) => { "type" : "string", "value" : S }
    rule #scVal2JSON( Void )        => { "type" : "void" }
    rule #scVal2JSON( ScAddress(Account(B)) )  => { "type" : "address", "addrType" : "account",  "value" : Bytes2Hex(B) }
    rule #scVal2JSON( ScAddress(Contract(B)) ) => { "type" : "address", "addrType" : "contract", "value" : Bytes2Hex(B) }
    rule #scVal2JSON( ScVec(ITEMS) ) => { "type" : "vec", "value" : [ #scValList2JSONs(ITEMS) ] }
    rule #scVal2JSON( ScMap(M) )     => { "type" : "map", "value" : [ #scMapEntries2JSONs(keys_list(M), M) ] }
    rule #scVal2JSON( _ )            => { "type" : "unsupported" } [owise]

    syntax JSONs ::= #scValList2JSONs( List ) [function, total]
 // -----------------------------------------------------------
    rule #scValList2JSONs( .List )                  => .JSONs
    rule #scValList2JSONs( ListItem(V:ScVal) REST ) => ( #scVal2JSON(V), #scValList2JSONs(REST) )
    rule #scValList2JSONs( ListItem(_) REST )       => ( { "type" : "unsupported" }, #scValList2JSONs(REST) ) [owise]

    syntax JSONs ::= #scMapEntries2JSONs( List, Map ) [function, total]
 // -------------------------------------------------------------------
    rule #scMapEntries2JSONs( .List, _ ) => .JSONs
    rule #scMapEntries2JSONs( ListItem(KEY:ScVal) REST, M )
      => ( { "key" : #scVal2JSON(KEY), "val" : #scVal2JSON( M {{ KEY }} orDefault Void ) }, #scMapEntries2JSONs(REST, M) )
    rule #scMapEntries2JSONs( ListItem(_) REST, M ) => ( { "type" : "unsupported" }, #scMapEntries2JSONs(REST, M) ) [owise]
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

Unlike `callTx`, it does not `#resetHost` after the call either: the call's return value is
left on the `<hostStack>` so `#recordAndRespond` can serialise it into the receipt. The host
is reset there instead (and each `uncheckedCallTx` clears the host cell before it runs, so a
leftover value never bleeds into a later call).

```k
    syntax Step ::= uncheckedCallTx( from: Address, to: Address, func: WasmString, args: List)     [symbol(uncheckedCallTx)]

    rule [uncheckedCallTx]:
        <k> uncheckedCallTx(FROM, TO, FUNC, ARGS)
         => allocObjects(ARGS)
         ~> callContractFromStack(FROM, TO, FUNC)
            ...
        </k>
        // clear the host cell before contract calls
        (_:HostCell => <host> <hostStack> .HostStack </hostStack> ... </host>)
```

###############################################################################
# ScVal serialisation

`#scValToJSON` renders an `ScVal` as JSON for the receipt's internal `returnValue` field.
The encoding mirrors the SCVal *argument* encoding accepted by `#decodeArg` above (and
produced by `scval_to_json` in `scval.py`), extended with the value-only cases that can come
back from a contract but never go in as arguments: `void`, `string`, `u256`, `vec`, `map`.
The Python server decodes it with `scval_from_json` (`scval.py`) — keep the three in sync.
Values with no JSON encoding (e.g. `Error`) render as `null`, i.e. as "no return value".

```k
    syntax JSON ::= #scValToJSON( ScVal ) [function, total, symbol(scValToJSON)]
 // ----------------------------------------------------------------------------
    rule #scValToJSON( SCBool(B)  ) => { "type" : "bool"   , "value" : B }
    rule #scValToJSON( Void       ) => { "type" : "void" }
    rule #scValToJSON( I32(V)     ) => { "type" : "i32"    , "value" : V }
    rule #scValToJSON( U32(V)     ) => { "type" : "u32"    , "value" : V }
    rule #scValToJSON( I64(V)     ) => { "type" : "i64"    , "value" : V }
    rule #scValToJSON( U64(V)     ) => { "type" : "u64"    , "value" : V }
    rule #scValToJSON( I128(V)    ) => { "type" : "i128"   , "value" : V }
    rule #scValToJSON( U128(V)    ) => { "type" : "u128"   , "value" : V }
    rule #scValToJSON( U256(V)    ) => { "type" : "u256"   , "value" : V }
    rule #scValToJSON( Symbol(S)  ) => { "type" : "symbol" , "value" : S }
    rule #scValToJSON( ScString(S)) => { "type" : "string" , "value" : S }
    rule #scValToJSON( ScBytes(B) ) => { "type" : "bytes"  , "value" : #bytesToHex(B) }
    rule #scValToJSON( ScAddress(Account(B))  ) => { "type" : "address" , "addrType" : "account"  , "value" : #bytesToHex(B) }
    rule #scValToJSON( ScAddress(Contract(B)) ) => { "type" : "address" , "addrType" : "contract" , "value" : #bytesToHex(B) }
    rule #scValToJSON( ScVec(L)   ) => { "type" : "vec"    , "value" : [ #scValsToJSONs(L) ] }
    rule #scValToJSON( ScMap(M)   ) => { "type" : "map"    , "value" : [ #mapToJSONs(keys_list(M), M) ] }
    rule #scValToJSON( _          ) => null                                     [owise]

    syntax JSONs ::= #scValsToJSONs( List ) [function, symbol(scValsToJSONs)]
 // -------------------------------------------------------------------------
    rule #scValsToJSONs( .List ) => .JSONs
    rule #scValsToJSONs( ListItem(V) REST ) => #scValToJSON({V}:>ScVal) , #scValsToJSONs(REST)

    // Each map entry becomes a two-element [key, value] array, in key order.
    syntax JSONs ::= #mapToJSONs( List, Map ) [function, symbol(mapToJSONs)]
 // ------------------------------------------------------------------------
    rule #mapToJSONs( .List, _ ) => .JSONs
    rule #mapToJSONs( ListItem(KEY) REST, M )
      => [ #scValToJSON({KEY}:>ScVal) , #scValToJSON({M[KEY]}:>ScVal) , .JSONs ] , #mapToJSONs(REST, M)
```

`#bytesToHex` is the inverse of `HexBytes`: lowercase hex, two digits per byte (zero-padded,
since Base2String drops leading zeroes). Empty bytes encode as the empty string — the
general rule would yield `"0"` (Base2String of 0), which `#padZeros` cannot trim.

```k
    syntax String ::= #bytesToHex( Bytes )      [function, total, symbol(bytesToHex)]
                    | #padZeros( String, Int )  [function, total, symbol(padZeros)]
 // --------------------------------------------------------------------------------
    rule #bytesToHex( B ) => "" requires lengthBytes(B) ==Int 0
    rule #bytesToHex( B ) => #padZeros( Base2String( Bytes2Int(B, BE, Unsigned), 16 ), 2 *Int lengthBytes(B) )
      requires lengthBytes(B) >Int 0

    rule #padZeros( S, N ) => #padZeros( "0" +String S, N ) requires lengthString(S) <Int N
    rule #padZeros( S, _ ) => S                              [owise]
```

## getEvents

Contract events are captured during `sendTransaction` (see "Event capture" below) and
persisted by the Python server as one finished JSON array per ledger in
`events/events_<ledger>.json` — each entry already in the spec's Event shape (base64 SCVal
XDR topics/value, strkey contract id, TOID-style id). `getEvents` scans the requested
ledger window, applies the request's filters, and paginates.

The Python server validates the request parameters (types, filter/topic counts, cursor
format, `xdrFormat`) and guarantees the envelope carries: `startLedger` (Int or null),
`endLedger` (Int or null), `filters` (array), `cursor` (String or null, exclusive with
`startLedger`/`endLedger`), and `limit` (Int ≥ 1). Only the state-dependent check — the
requested window against the chain tip — is performed here.

```k
    rule <k> #dispatchMethod( "getEvents", REQ )
          => #getEvents( REQ, #getInt( "latest_ledger", String2JSON( {#readFile("metadata.json")}:>String ) ) )
             ...
         </k>

    rule [getEvents-start-beyond-latest]:
        <k> #getEvents( REQ, LL )
         => #respondError( #getJSON( "id", REQ ), -32600,
                "startLedger must be within the ledger range: 1 - " +String Int2String( LL ) )
            ...
        </k>
      requires #getJSON( "cursor", REQ ) ==K null
       andBool #getInt( "startLedger", REQ ) >Int LL

    rule [getEvents-scan]:
        <k> #getEvents( REQ, LL )
         => #respondEvents( REQ, LL,
                #matchingEvents( #scanStart( REQ ), #scanEnd( REQ, LL ), #afterId( REQ ), #getJSON( "filters", REQ ) ) )
            ...
        </k>
      [owise]

    rule <k> #respondEvents( REQ, LL, MATCHED )
          => #respond( #getJSON( "id", REQ ), {
                 "latestLedger" : LL,
                 "events"       : [ #takeJSONs( #getInt( "limit", REQ ), MATCHED ) ],
                 "cursor"       : #eventsPageCursor( MATCHED, #getInt( "limit", REQ ), #scanEnd( REQ, LL ) )
             })
             ...
         </k>
```

The scan window: `startLedger` is inclusive, `endLedger` exclusive, both capped at the
latest ledger. A pagination cursor replaces `startLedger` — scanning resumes at the
cursor's ledger (its TOID's high 32 bits) and only events with an id strictly greater than
the cursor are returned. Event ids are fixed-width zero-padded, so id order is plain string
order.

```k
    syntax Int ::= #scanStart( JSON ) [function]
 // --------------------------------------------
    rule #scanStart( REQ ) => #cursorLedger( #getString( "cursor", REQ ) )
      requires #getJSON( "cursor", REQ ) =/=K null
    rule #scanStart( REQ ) => #getInt( "startLedger", REQ ) [owise]

    syntax Int ::= #cursorLedger( String ) [function]
 // -------------------------------------------------
    rule #cursorLedger( C ) => String2Int( substrString( C, 0, 19 ) ) >>Int 32

    syntax Int ::= #scanEnd( JSON, Int ) [function]   // exclusive end of the scan window
 // -------------------------------------------------------------------------------------
    rule #scanEnd( REQ, LL ) => minInt( #getInt( "endLedger", REQ ), LL +Int 1 )
      requires #getJSON( "endLedger", REQ ) =/=K null
    rule #scanEnd( _, LL ) => LL +Int 1 [owise]

    syntax String ::= #afterId( JSON ) [function]   // only events with id > this are returned
 // ------------------------------------------------------------------------------------------
    rule #afterId( REQ ) => #getString( "cursor", REQ )
      requires #getJSON( "cursor", REQ ) =/=K null
    rule #afterId( _ ) => "" [owise]

    syntax String ::= #eventsFile( Int ) [function, symbol(eventsFile)]
 // -------------------------------------------------------------------
    rule #eventsFile( L ) => "events/events_" +String Int2String( L ) +String ".json"

    syntax JSONs ::= #matchingEvents( Int, Int, String, JSON ) [function]
 // ---------------------------------------------------------------------
    rule #matchingEvents( L, END, _, _ ) => .JSONs requires L >=Int END
    rule #matchingEvents( L, END, AFTER, FILTERS )
      => #concatJSONs( #filterEvents( #ledgerEvents( L ), AFTER, FILTERS ),
                       #matchingEvents( L +Int 1, END, AFTER, FILTERS ) )
      [owise]

    syntax JSONs ::= #ledgerEvents( Int ) [function]
 // ------------------------------------------------
    rule #ledgerEvents( L ) => #arrayJSONs( String2JSON( {#readFile( #eventsFile( L ) )}:>String ) )
      requires #fileExists( #eventsFile( L ) )
    rule #ledgerEvents( _ ) => .JSONs [owise]

    syntax JSONs ::= #arrayJSONs( JSON ) [function]
 // -----------------------------------------------
    rule #arrayJSONs( [ ES ] ) => ES
    rule #arrayJSONs( _ )      => .JSONs [owise]

    syntax JSONs ::= #filterEvents( JSONs, String, JSON ) [function]
 // ----------------------------------------------------------------
    rule #filterEvents( .JSONs, _, _ ) => .JSONs
    rule #filterEvents( ( E, ES ), AFTER, FILTERS ) => ( E, #filterEvents( ES, AFTER, FILTERS ) )
      requires AFTER <String #getString( "id", E ) andBool #matchesFilters( E, FILTERS )
    rule #filterEvents( ( _, ES ), AFTER, FILTERS ) => #filterEvents( ES, AFTER, FILTERS ) [owise]
```

An event passes if any filter matches (or the filter list is empty); a filter matches if
all of its present criteria — `type`, `contractIds`, `topics` — match.

```k
    syntax Bool ::= #matchesFilters( JSON, JSON ) [function]
 // --------------------------------------------------------
    rule #matchesFilters( _, null )       => true
    rule #matchesFilters( _, [ .JSONs ] ) => true
    rule #matchesFilters( E, [ FS ] )     => #matchesAnyFilter( E, FS ) [owise]

    syntax Bool ::= #matchesAnyFilter( JSON, JSONs ) [function]
 // -----------------------------------------------------------
    rule #matchesAnyFilter( _, .JSONs )     => false
    rule #matchesAnyFilter( E, ( F, FS ) )  => #matchesFilter( E, F ) orBool #matchesAnyFilter( E, FS )

    syntax Bool ::= #matchesFilter( JSON, JSON ) [function]
 // -------------------------------------------------------
    rule #matchesFilter( E, F )
      => #matchesType( #getString( "type", E ), #getJSON( "type", F ) )
         andBool #matchesContractIds( #getString( "contractId", E ), #getJSON( "contractIds", F ) )
         andBool #matchesTopicFilters( #getJSON( "topic", E ), #getJSON( "topics", F ) )

    syntax Bool ::= #matchesType( String, JSON ) [function]
 // -------------------------------------------------------
    rule #matchesType( _, null )     => true
    rule #matchesType( T, F:String ) => T ==String F
    rule #matchesType( _, _ )        => false [owise]

    syntax Bool ::= #matchesContractIds( String, JSON ) [function]
 // --------------------------------------------------------------
    rule #matchesContractIds( _, null )            => true
    rule #matchesContractIds( _, [ .JSONs ] )      => true
    rule #matchesContractIds( C, [ I0:JSON, IS ] ) => #containsString( C, ( I0, IS ) )
    rule #matchesContractIds( _, _ )               => false [owise]

    syntax Bool ::= #containsString( String, JSONs ) [function]
 // -----------------------------------------------------------
    rule #containsString( _, .JSONs )           => false
    rule #containsString( S, ( S2:String, SS ) ) => S ==String S2 orBool #containsString( S, SS )
    rule #containsString( S, ( _, SS ) )         => #containsString( S, SS ) [owise]
```

Topic filters: each filter is a list of segment matchers compared pairwise against the
event's topics — a base64 SCVal XDR string for an exact match, `"*"` for exactly one
segment, and a final `"**"` for zero or more remaining segments.

```k
    syntax Bool ::= #matchesTopicFilters( JSON, JSON ) [function]
 // -------------------------------------------------------------
    rule #matchesTopicFilters( _, null )              => true
    rule #matchesTopicFilters( _, [ .JSONs ] )        => true
    rule #matchesTopicFilters( T, [ TF:JSON, TFS ] )  => #anyTopicFilter( T, ( TF, TFS ) )
    rule #matchesTopicFilters( _, _ )                 => false [owise]

    syntax Bool ::= #anyTopicFilter( JSON, JSONs ) [function]
 // ---------------------------------------------------------
    rule #anyTopicFilter( _, .JSONs )      => false
    rule #anyTopicFilter( T, ( TF, TFS ) ) => #matchesTopicFilter( T, TF ) orBool #anyTopicFilter( T, TFS )

    syntax Bool ::= #matchesTopicFilter( JSON, JSON ) [function]
 // ------------------------------------------------------------
    rule #matchesTopicFilter( [ TS ], [ MS ] ) => #matchSegments( TS, MS )
    rule #matchesTopicFilter( _, _ )           => false [owise]

    syntax Bool ::= #matchSegments( JSONs, JSONs ) [function]
 // ---------------------------------------------------------
    rule #matchSegments( _, ( "**", .JSONs ) )              => true
    rule #matchSegments( .JSONs, .JSONs )                   => true
    rule #matchSegments( ( _:JSON, TS ), ( "*", MS ) )      => #matchSegments( TS, MS )
    rule #matchSegments( ( T:String, TS ), ( M:String, MS ) )
      => T ==String M andBool #matchSegments( TS, MS )
      requires M =/=String "*" andBool M =/=String "**"
    rule #matchSegments( _, _ )                             => false [owise]
```

Pagination: at most `limit` events are returned. The response cursor resumes the scan — the
id of the last returned event when the page filled up, otherwise a position just past the
scanned window (the TOID of the window's exclusive end ledger, which every later event id
exceeds).

```k
    syntax JSONs ::= #takeJSONs( Int, JSONs ) [function, total]
 // -----------------------------------------------------------
    rule #takeJSONs( N, _ )          => .JSONs requires N <=Int 0
    rule #takeJSONs( N, .JSONs )     => .JSONs requires N >Int 0
    rule #takeJSONs( N, ( E, ES ) )  => ( E, #takeJSONs( N -Int 1, ES ) ) requires N >Int 0

    syntax String ::= #lastEventId( JSONs ) [function]
 // --------------------------------------------------
    rule #lastEventId( ( E, .JSONs ) ) => #getString( "id", E )
    rule #lastEventId( ( _, ES ) )     => #lastEventId( ES ) [owise]

    syntax String ::= #eventsPageCursor( JSONs, Int, Int ) [function]
 // -----------------------------------------------------------
    rule #eventsPageCursor( MATCHED, LIMIT, ENDEXCL ) => #ledgerCursor( ENDEXCL )
      requires #lengthJSONs( MATCHED ) <=Int LIMIT
    rule #eventsPageCursor( MATCHED, LIMIT, _ ) => #lastEventId( #takeJSONs( LIMIT, MATCHED ) ) [owise]

    syntax String ::= #ledgerCursor( Int ) [function]
 // -------------------------------------------------
    rule #ledgerCursor( L ) => #padLeft( Int2String( L <<Int 32 ), 19 ) +String "-" +String #padLeft( "0", 10 )
```

###############################################################################
# Event capture

The upstream soroban semantics implement the `contract_event` host function ("x"/"1") as a
no-op that drops the topics and data. This rule shadows it (priority 40, ahead of the
upstream rule's default 50): it resolves the topics vector and the data value from the host
objects and appends one JSON record per event to `events_staged.jsonl` in the io-dir —
mirroring how the tracer appends to the trace file — before yielding the same `Void` result
as upstream. The Python server deletes the staging file before each transaction runs and,
after a successful `sendTransaction`, converts the staged records into the finished
`events/events_<ledger>.json` that `getEvents` serves (base64 SCVal XDR and strkey ids are
XDR work that K cannot do).

```k
    rule [node-contract-event]:
        <instrs> hostCall ( "x" , "1" , [ i64  i64  .ValTypes ] -> [ i64  .ValTypes ] )
              => #stageEvent(HostVal(TOPICS), HostVal(DATA))
              ~> toSmall(Void)
                 ...
        </instrs>
        <locals>
            0 |-> <i64> TOPICS
            1 |-> <i64> DATA
        </locals>
      [priority(40)]

    syntax InternalInstr ::= #stageEvent( HostVal, HostVal )   [symbol(stageEvent)]
 // -------------------------------------------------------------------------------
    rule [stageEvent]:
        <instrs> #stageEvent(TOPICS, DATA)
              => #appendFile( "events_staged.jsonl",
                     JSON2String({
                         "contractId" : #bytesHex(ADDR),
                         "topics"     : [ #topicsJSONs(HostVal2ScVal(TOPICS, OBJS, RELS)) ],
                         "data"       : #eventScValJSON(HostVal2ScVal(DATA, OBJS, RELS))
                     }) +String "\n" )
                 ...
        </instrs>
        <callee> Contract(ADDR) </callee>
        <hostObjects> OBJS </hostObjects>
        <relativeObjects> RELS </relativeObjects>
```

`#eventScValJSON` serialises a resolved `ScVal` in the same `{"type": ..., "value": ...}` scheme
that `#decodeArg` consumes (see `scval.py`), extended with `vec`, `string`, and `void`.
Values with no JSON representation here (maps, errors, 256-bit ints) become `null`; the
Python side skips such events with a warning rather than fabricating XDR for them.

```k
    syntax JSONs ::= #topicsJSONs( ScVal ) [function, total]
 // --------------------------------------------------------
    rule #topicsJSONs( ScVec(L) ) => #eventScValsJSONs(L)
    rule #topicsJSONs( _ )        => .JSONs [owise]

    syntax JSONs ::= #eventScValsJSONs( List ) [function, total]
 // -------------------------------------------------------
    rule #eventScValsJSONs( .List )                 => .JSONs
    rule #eventScValsJSONs( ListItem(V:ScVal) L )   => ( #eventScValJSON(V), #eventScValsJSONs(L) )
    rule #eventScValsJSONs( ListItem(_) L )         => ( null, #eventScValsJSONs(L) ) [owise]

    syntax JSON ::= #eventScValJSON( ScVal ) [function, total]
 // -----------------------------------------------------
    rule #eventScValJSON( SCBool(B) )   => { "type" : "bool"  , "value" : B }
    rule #eventScValJSON( Void )        => { "type" : "void" }
    rule #eventScValJSON( U32(I) )      => { "type" : "u32"   , "value" : I }
    rule #eventScValJSON( I32(I) )      => { "type" : "i32"   , "value" : I }
    rule #eventScValJSON( U64(I) )      => { "type" : "u64"   , "value" : I }
    rule #eventScValJSON( I64(I) )      => { "type" : "i64"   , "value" : I }
    rule #eventScValJSON( U128(I) )     => { "type" : "u128"  , "value" : I }
    rule #eventScValJSON( I128(I) )     => { "type" : "i128"  , "value" : I }
    rule #eventScValJSON( Symbol(S) )   => { "type" : "symbol", "value" : S }
    rule #eventScValJSON( ScString(S) ) => { "type" : "string", "value" : S }
    rule #eventScValJSON( ScBytes(B) )  => { "type" : "bytes" , "value" : #bytesHex(B) }
    rule #eventScValJSON( ScAddress(Account(B)) )  => { "type" : "address", "addrType" : "account" , "value" : #bytesHex(B) }
    rule #eventScValJSON( ScAddress(Contract(B)) ) => { "type" : "address", "addrType" : "contract", "value" : #bytesHex(B) }
    rule #eventScValJSON( ScVec(L) )    => { "type" : "vec"   , "value" : [ #eventScValsJSONs(L) ] }
    rule #eventScValJSON( _ )           => null [owise]

    syntax String ::= #bytesHex( Bytes ) [function, total]
 // ------------------------------------------------------
    rule #bytesHex( B ) => #padLeft( Base2String( Bytes2Int(B, BE, Unsigned), 16 ), 2 *Int lengthBytes(B) )

    syntax String ::= #padLeft( String, Int ) [function, total]
 // -----------------------------------------------------------
    rule #padLeft( S, W ) => #padLeft( "0" +String S, W ) requires lengthString(S) <Int W
    rule #padLeft( S, W ) => S requires lengthString(S) >=Int W

endmodule
```

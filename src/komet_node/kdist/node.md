
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
                   | #respondTx( JSON, Int )
                   | #enableTrace( String )
                   | #getTxResult( String, String, JSON, Int )
                   | #respondTrace( JSON, String )
                   | #getEvents( JSON, Int )
                   | #respondEvents( JSON, Int, JSONs )
                   | #respond( JSON, JSON )
                   | #respondError( JSON, Int, String )

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
`request.json`, and marks the run successful (exit code 0).

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
```

`#respondError(ID, CODE, MESSAGE)` is the error-shaped counterpart of `#respond`, for
requests that are recognised but cannot be answered (e.g. a ledger range outside the chain).

```k
    rule <k> #respondError( ID, CODE, MESSAGE )
          => #writeFile( "response.json", JSON2String({
                 "jsonrpc" : "2.0",
                 "id"      : ID,
                 "error"   : { "code" : CODE, "message" : MESSAGE }
             }))
          ~> #remove( "request.json" )
             ...
         </k>
         <exitCode> _ => 0 </exitCode>
```

###############################################################################
## Read-only methods

```k
    rule <k> #dispatchMethod( "getHealth", REQ )
          => #respond( #getJSON( "id", REQ ), { "status" : "healthy" } )
             ...
         </k>

    rule <k> #dispatchMethod( "getNetwork", REQ )
          => #respond( #getJSON( "id", REQ ), {
                 "friendbotUrl"    : null,
                 "passphrase"      : #getString( "passphrase", REQ ),
                 "protocolVersion" : #getString( "protocolVersion", REQ )
             })
             ...
         </k>

    rule <k> #dispatchMethod( "getLatestLedger", REQ )
          => #respond( #getJSON( "id", REQ ), {
                 "id"              : "0000000000000000000000000000000000000000000000000000000000000000",
                 "protocolVersion" : #getString( "protocolVersion", REQ ),
                 "sequence"        : #getInt( "latest_ledger", String2JSON( {#readFile("metadata.json")}:>String ) )
             })
             ...
         </k>
```

## getTransaction

Look up the stored receipt by hash in its `receipts/receipt_<hash>.json` file. If the file
exists, return its contents merged with the current `latestLedger`/`latestLedgerCloseTime`;
otherwise return `NOT_FOUND`.

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
                 ( "latestLedger"          : Int2String( LL ) ,
                   "latestLedgerCloseTime" : NOW ,
                   .JSONs )
             )})
             ...
         </k>
      requires #fileExists( #receiptFile( HASH ) )

    rule <k> #getTxResult( HASH, NOW, ID, LL )
          => #respond( ID, {
                 "status"                : "NOT_FOUND",
                 "latestLedger"          : Int2String( LL ),
                 "latestLedgerCloseTime" : NOW
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

The steps come either from the `steps` array of the request envelope (the common path) or
from the `<program>` cell (the wasm-upload path, where they were pre-injected and have
already run by the time we get here, leaving `steps` empty).

```k
    rule <k> #dispatchMethod( "sendTransaction", REQ ) => #runTx( REQ ) ... </k>

    // Unknown method — respond with a null result.
    rule <k> #dispatchMethod( _, REQ ) => #respond( #getJSON( "id", REQ ), null ) ... </k> [owise]

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
          ~> #respondTx( REQ, L +Int 1 )
             ...
         </k>

    syntax JSON ::= #txReceipt( JSON, Int ) [function, symbol(txReceipt)]
 // ---------------------------------------------------------------------
    rule #txReceipt( REQ, NEWL ) => {
            "status"        : "SUCCESS",
            "ledger"        : Int2String( NEWL ),
            "createdAt"     : #getString( "now", REQ ),
            "envelopeXdr"   : #getString( "envelopeXdr", REQ ),
            "resultXdr"     : "",
            "resultMetaXdr" : ""
        }

    rule <k> #respondTx( REQ, NEWL )
          => #respond( #getJSON( "id", REQ ), {
                 "hash"                  : #getString( "txHash", REQ ),
                 "status"                : "PENDING",
                 "latestLedger"          : Int2String( NEWL ),
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
                 "cursor"       : #pageCursor( MATCHED, #getInt( "limit", REQ ), #scanEnd( REQ, LL ) )
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

    syntax Int ::= #lengthJSONs( JSONs ) [function, total]
 // ------------------------------------------------------
    rule #lengthJSONs( .JSONs )     => 0
    rule #lengthJSONs( ( _, ES ) )  => 1 +Int #lengthJSONs( ES )

    syntax String ::= #lastEventId( JSONs ) [function]
 // --------------------------------------------------
    rule #lastEventId( ( E, .JSONs ) ) => #getString( "id", E )
    rule #lastEventId( ( _, ES ) )     => #lastEventId( ES ) [owise]

    syntax String ::= #pageCursor( JSONs, Int, Int ) [function]
 // -----------------------------------------------------------
    rule #pageCursor( MATCHED, LIMIT, ENDEXCL ) => #ledgerCursor( ENDEXCL )
      requires #lengthJSONs( MATCHED ) <=Int LIMIT
    rule #pageCursor( MATCHED, LIMIT, _ ) => #lastEventId( #takeJSONs( LIMIT, MATCHED ) ) [owise]

    syntax String ::= #ledgerCursor( Int ) [function]
 // -------------------------------------------------
    rule #ledgerCursor( L ) => #padLeft( Int2String( L <<Int 32 ), 19 ) +String "-" +String #padLeft( "0", 10 )
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
                         "data"       : #scValJSON(HostVal2ScVal(DATA, OBJS, RELS))
                     }) +String "\n" )
                 ...
        </instrs>
        <callee> Contract(ADDR) </callee>
        <hostObjects> OBJS </hostObjects>
        <relativeObjects> RELS </relativeObjects>
```

`#scValJSON` serialises a resolved `ScVal` in the same `{"type": ..., "value": ...}` scheme
that `#decodeArg` consumes (see `scval.py`), extended with `vec`, `string`, and `void`.
Values with no JSON representation here (maps, errors, 256-bit ints) become `null`; the
Python side skips such events with a warning rather than fabricating XDR for them.

```k
    syntax JSONs ::= #topicsJSONs( ScVal ) [function, total]
 // --------------------------------------------------------
    rule #topicsJSONs( ScVec(L) ) => #scValsJSONs(L)
    rule #topicsJSONs( _ )        => .JSONs [owise]

    syntax JSONs ::= #scValsJSONs( List ) [function, total]
 // -------------------------------------------------------
    rule #scValsJSONs( .List )                 => .JSONs
    rule #scValsJSONs( ListItem(V:ScVal) L )   => ( #scValJSON(V), #scValsJSONs(L) )
    rule #scValsJSONs( ListItem(_) L )         => ( null, #scValsJSONs(L) ) [owise]

    syntax JSON ::= #scValJSON( ScVal ) [function, total]
 // -----------------------------------------------------
    rule #scValJSON( SCBool(B) )   => { "type" : "bool"  , "value" : B }
    rule #scValJSON( Void )        => { "type" : "void" }
    rule #scValJSON( U32(I) )      => { "type" : "u32"   , "value" : I }
    rule #scValJSON( I32(I) )      => { "type" : "i32"   , "value" : I }
    rule #scValJSON( U64(I) )      => { "type" : "u64"   , "value" : I }
    rule #scValJSON( I64(I) )      => { "type" : "i64"   , "value" : I }
    rule #scValJSON( U128(I) )     => { "type" : "u128"  , "value" : I }
    rule #scValJSON( I128(I) )     => { "type" : "i128"  , "value" : I }
    rule #scValJSON( Symbol(S) )   => { "type" : "symbol", "value" : S }
    rule #scValJSON( ScString(S) ) => { "type" : "string", "value" : S }
    rule #scValJSON( ScBytes(B) )  => { "type" : "bytes" , "value" : #bytesHex(B) }
    rule #scValJSON( ScAddress(Account(B)) )  => { "type" : "address", "addrType" : "account" , "value" : #bytesHex(B) }
    rule #scValJSON( ScAddress(Contract(B)) ) => { "type" : "address", "addrType" : "contract", "value" : #bytesHex(B) }
    rule #scValJSON( ScVec(L) )    => { "type" : "vec"   , "value" : [ #scValsJSONs(L) ] }
    rule #scValJSON( _ )           => null [owise]

    syntax String ::= #bytesHex( Bytes ) [function, total]
 // ------------------------------------------------------
    rule #bytesHex( B ) => #padLeft( Base2String( Bytes2Int(B, BE, Unsigned), 16 ), 2 *Int lengthBytes(B) )

    syntax String ::= #padLeft( String, Int ) [function, total]
 // -----------------------------------------------------------
    rule #padLeft( S, W ) => #padLeft( "0" +String S, W ) requires lengthString(S) <Int W
    rule #padLeft( S, W ) => S requires lengthString(S) >=Int W

endmodule
```

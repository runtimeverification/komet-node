
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
                   | #respond( JSON, JSON )

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
        => callTx(Account(HexBytes(FROM)), Contract(HexBytes(TO)), string2WasmToken("\"" +String FUNC +String "\""), #decodeArgList(ARGS), Void)

    rule #decodeStep({ "op" : "callTx" , "from" : FROM:String , "fromIsContract" : true , "func" : FUNC:String , "to" : TO:String , "args" : [ARGS:JSONs] })
        => callTx(Contract(HexBytes(FROM)), Contract(HexBytes(TO)), string2WasmToken("\"" +String FUNC +String "\""), #decodeArgList(ARGS), Void)

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

endmodule
```

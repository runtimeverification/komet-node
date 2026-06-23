
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
  - The RPC bookkeeping (the transaction store and the latest-ledger counter) is persisted
    as small JSON files (`transactions.json`, `metadata.json`) in the working directory,
    read and written by these rules via the file-system hooks.

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
                   | #runTx( JSON, String )
                   | #finalizeTx( JSON, String )
                   | #recordAndRespond( JSON, String, Int, JSON, JSON )
                   | #respondTx( JSON, String, Int, JSON )
                   | #maybeEnableTrace( JSON, String )
                   | #getTxResult( String, String, JSON, JSON, Int )
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

These rules provide order-independent accessors over JSON objects, plus an upsert helper for
the transaction store, ported from kontrol-node's `json-utils.md`.

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

    // Upsert KEY |-> VAL into a JSON object, dropping any existing entry for KEY.
    syntax JSON  ::= #putJSON( JSONKey, JSON, JSON )   [function, symbol(putJSON)]
    syntax JSONs ::= #removeKeyJSONs( JSONKey, JSONs ) [function, symbol(removeKeyJSONs)]
 // ------------------------------------------------------------------------------------
    rule #putJSON( KEY, VAL, { OBJ } ) => { KEY : VAL , #removeKeyJSONs( KEY, OBJ ) }

    rule #removeKeyJSONs( _, .JSONs ) => .JSONs
    rule #removeKeyJSONs( KEY, ( KEY  : _, REST ) ) => #removeKeyJSONs( KEY, REST )
    rule #removeKeyJSONs( KEY, ( KEY2 : V, REST ) ) => ( KEY2 : V , #removeKeyJSONs( KEY, REST ) )
      requires KEY =/=K KEY2

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

Look up the stored receipt by hash in `transactions.json`. If present, return it merged
with the current `latestLedger`/`latestLedgerCloseTime`; otherwise return `NOT_FOUND`.

```k
    rule <k> #dispatchMethod( "getTransaction", REQ )
          => #getTxResult(
                 #getString( "hash", REQ ),
                 #getString( "now", REQ ),
                 #getJSON( "id", REQ ),
                 String2JSON( {#readFile("transactions.json")}:>String ),
                 #getInt( "latest_ledger", String2JSON( {#readFile("metadata.json")}:>String ) )
             )
             ...
         </k>

    rule <k> #getTxResult( HASH, NOW, ID, { TXS }, LL )
          => #respond( ID, { #concatJSONs(
                 #recordOf( #getJSON( HASH, { TXS } ) ),
                 ( "latestLedger"          : Int2String( LL ) ,
                   "latestLedgerCloseTime" : NOW ,
                   .JSONs )
             )})
             ...
         </k>
      requires #getJSON( HASH, { TXS } ) =/=K null

    rule <k> #getTxResult( HASH, NOW, ID, { TXS }, LL )
          => #respond( ID, {
                 "status"                : "NOT_FOUND",
                 "latestLedger"          : Int2String( LL ),
                 "latestLedgerCloseTime" : NOW
             })
             ...
         </k>
      requires #getJSON( HASH, { TXS } ) ==K null

    // Extract the entries of a stored receipt object so they can be concatenated.
    syntax JSONs ::= #recordOf( JSON ) [function, symbol(recordOf)]
 // --------------------------------------------------------------
    rule #recordOf( { OBJ } ) => OBJ
```

###############################################################################
## sendTransaction / traceTransaction

Both run the decoded steps, then record a receipt, bump the ledger, and respond. The only
differences are whether instruction tracing is enabled and the shape of the immediate
response (`PENDING` for sendTransaction, the result + trace for traceTransaction).

The steps come either from the `steps` array of the request envelope (the common path) or
from the `<program>` cell (the wasm-upload path, where they were pre-injected and have
already run by the time we get here, leaving `steps` empty).

```k
    rule <k> #dispatchMethod( "sendTransaction",  REQ ) => #runTx( REQ, "sendTransaction" )  ... </k>
    rule <k> #dispatchMethod( "traceTransaction", REQ ) => #runTx( REQ, "traceTransaction" ) ... </k>

    // Unknown method — respond with a null result.
    rule <k> #dispatchMethod( _, REQ ) => #respond( #getJSON( "id", REQ ), null ) ... </k> [owise]

    rule <k> #runTx( REQ, METHOD )
          => #maybeEnableTrace( REQ, METHOD )
          ~> setLedgerSequence( #getInt( "latest_ledger", String2JSON( {#readFile("metadata.json")}:>String ) ) )
          ~> #decodeSteps( #stepsJSONs( #getJSON( "steps", REQ, [ .JSONs ] ) ) )
          ~> #finalizeTx( REQ, METHOD )
             ...
         </k>

    syntax JSONs ::= #stepsJSONs( JSON ) [function, symbol(stepsJSONs)]
 // ------------------------------------------------------------------
    rule #stepsJSONs( [ SS ] ) => SS
    rule #stepsJSONs( _ )      => .JSONs [owise]
```

Tracing is enabled for `traceTransaction` and for any request that carries `"trace": true`
(set by the `--trace` server flag). When enabled we clear the trace file and point the
trace `<ioDir>` at it; otherwise tracing stays disabled.

```k
    syntax Bool ::= #tracingOn( JSON, String ) [function, symbol(tracingOn)]
 // -----------------------------------------------------------------------
    rule #tracingOn( _, METHOD ) => true
      requires METHOD ==String "traceTransaction"
    rule #tracingOn( REQ, METHOD ) => #getJSON( "trace", REQ, false ) ==K true
      requires METHOD =/=String "traceTransaction"

    rule <k> #maybeEnableTrace( REQ, METHOD ) => #writeFile( "trace.jsonl", "" ) ... </k>
         <ioDir> _ => "trace.jsonl" </ioDir>
      requires #tracingOn( REQ, METHOD )

    rule <k> #maybeEnableTrace( REQ, METHOD ) => .K ... </k>
         <ioDir> _ => "" </ioDir>
      requires notBool #tracingOn( REQ, METHOD )
```

After the steps run, read the current bookkeeping, capture the trace (if tracing was on),
record the receipt, write the new ledger counter, and respond. Reaching this point means
the steps completed without getting stuck, so the status is `SUCCESS`.

```k
    rule <k> #finalizeTx( REQ, METHOD )
          => #recordAndRespond(
                 REQ, METHOD,
                 #getInt( "latest_ledger", String2JSON( {#readFile("metadata.json")}:>String ) ),
                 String2JSON( {#readFile("transactions.json")}:>String ),
                 {#readFile("trace.jsonl")}:>String
             )
             ...
         </k>
         <ioDir> _ => "" </ioDir>
      requires #tracingOn( REQ, METHOD )

    rule <k> #finalizeTx( REQ, METHOD )
          => #recordAndRespond(
                 REQ, METHOD,
                 #getInt( "latest_ledger", String2JSON( {#readFile("metadata.json")}:>String ) ),
                 String2JSON( {#readFile("transactions.json")}:>String ),
                 null
             )
             ...
         </k>
      requires notBool #tracingOn( REQ, METHOD )

    rule <k> #recordAndRespond( REQ, METHOD, L, TXS, TRACE )
          => #writeFile( "metadata.json", JSON2String({ "latest_ledger" : L +Int 1 }) )
          ~> #writeFile( "transactions.json",
                 JSON2String( #putJSON( #getString( "txHash", REQ ), #txReceipt( REQ, L +Int 1, TRACE ), TXS ) ) )
          ~> #respondTx( REQ, METHOD, L +Int 1, TRACE )
             ...
         </k>

    syntax JSON ::= #txReceipt( JSON, Int, JSON ) [function, symbol(txReceipt)]
 // ---------------------------------------------------------------------------
    rule #txReceipt( REQ, NEWL, TRACE ) => {
            "status"        : "SUCCESS",
            "ledger"        : Int2String( NEWL ),
            "createdAt"     : #getString( "now", REQ ),
            "envelopeXdr"   : #getString( "envelopeXdr", REQ ),
            "resultXdr"     : "",
            "resultMetaXdr" : "",
            "trace"         : TRACE
        }

    rule <k> #respondTx( REQ, "sendTransaction", NEWL, _TRACE )
          => #respond( #getJSON( "id", REQ ), {
                 "hash"                  : #getString( "txHash", REQ ),
                 "status"                : "PENDING",
                 "latestLedger"          : Int2String( NEWL ),
                 "latestLedgerCloseTime" : #getString( "now", REQ )
             })
             ...
         </k>

    rule <k> #respondTx( REQ, "traceTransaction", NEWL, TRACE )
          => #respond( #getJSON( "id", REQ ), {
                 "hash"                  : #getString( "txHash", REQ ),
                 "status"                : "SUCCESS",
                 "ledger"                : Int2String( NEWL ),
                 "trace"                 : TRACE,
                 "latestLedger"          : Int2String( NEWL ),
                 "latestLedgerCloseTime" : #getString( "now", REQ )
             })
             ...
         </k>
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

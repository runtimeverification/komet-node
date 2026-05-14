
This module implements the komet-node request lifecycle:
  1. On startup, check for "request.json"
  2. If found: read it, remove it, handle the request, write response to a file.
  3. If not found: halt — the empty <k>, <instrs>, and <program> cells
     represent the idle/ready state, awaiting the next invocation with a new request.json

This design avoids generating or processing Kore files at runtime. Instead:
  - A one-time initial Kore configuration is produced at startup
  - Each request cycle runs that configuration against request.json
  - The final configuration (idle state) is saved and reused for the next request


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

    // Internal control-flow items for the node request lifecycle.
    syntax KItem ::= "#handleRequestFile"
                   | "#removeRequestFile"
                   | #handleRequest(String)
```

HexBytes: decode a lowercase hex string to Bytes (big-endian, length = hex length / 2).
Relies on K's String2Base hook (base-16) and Int2Bytes with an explicit byte count so that
leading zero bytes are preserved.

```k
    syntax Bytes ::= HexBytes(String) [function]
    rule HexBytes("") => .Bytes
    rule HexBytes(S)  => Int2Bytes(lengthString(S) /Int 2, String2Base(S, 16), BE)
      requires lengthString(S) >Int 0
```

string2WasmToken: wrap a plain K String (e.g. "foo") in double-quote delimiters and
produce a WasmStringToken using K's generic string-to-token hook.

```k
    syntax WasmStringToken ::= string2WasmToken(String) [function, hook(STRING.string2token)]
```

insert-handleRequestFile: This rule fires when all three cells empty and the `request.json` file is present, which is the initial state.

If `request.json` does NOT exist, this rule does not fire and the execution terminates.
This is the idle state after a completed request, and the Node can save this configuration for reuse.

```k
    rule [insert-handleRequestFile]:
        <k> .K => #handleRequestFile </k>
        <instrs> .K </instrs>
        <program> .Steps </program>
      requires #fileExists("request.json")
```

handleRequestFile: wraps the request lifecycle in an exit-code guard.

```k
    rule [handleRequestFile]:
        <k> #handleRequestFile
         => setExitCode(1)
         ~> #handleRequest({#readFile("request.json")}:>String)
         ~> #removeRequestFile
         ~> setExitCode(0) 
            ...
        </k>

    rule [removeRequestFile]:
        <k> #removeRequestFile => #remove("request.json") ... </k>

    // KASMER's steps-empty requires <k> .Steps </k> exactly (no frame).
    // When steps are injected into <k> with a continuation, we need this rule
    // to consume .Steps and let the continuation proceed.
    rule [steps-done]:
        <k> .Steps => .K ... </k>
        <instrs> .K </instrs>
```

handleRequest: parse the JSON request body and inject the decoded Steps directly into
`<k>`. `steps-seq` executes each step; `steps-done` (above) consumes the final `.Steps`
so the continuation (`#removeRequestFile ~> setExitCode(0)`) can proceed.

```k
    rule [handleRequest]:
        <k> #handleRequest(S:String)
         => #decodeRequest(String2JSON(S))
            ...
        </k>
```

JSON request format (key order is significant — must match Python's json.dumps output):

  { "steps": [ <step>, ... ] }

where each <step> is one of:

  { "op": "setAccount",     "account": "<hex32>", "balance": <int> }
  { "op": "deployContract", "from": "<hex32>", "address": "<hex32>", "wasmHash": "<hex32>" }
  { "op": "callTx",         "from": "<hex32>", "fromIsContract": <bool>,
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
    syntax Steps ::= #decodeRequest(JSON)  [function]
                   | #decodeSteps(JSONs)   [function]
    syntax Step  ::= #decodeStep(JSON)     [function]

    rule #decodeRequest({ "steps" : [SS:JSONs] }) => #decodeSteps(SS)
    rule #decodeSteps(.JSONs)                     => .Steps
    rule #decodeSteps(S:JSON, SS:JSONs)           => #decodeStep(S) #decodeSteps(SS)

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

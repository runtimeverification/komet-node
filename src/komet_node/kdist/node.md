
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

module NODE-SYNTAX
    imports KASMER-SYNTAX
endmodule

module NODE
    imports KASMER
    imports FILE-OPERATIONS

    // Internal control-flow items for the node request lifecycle.
    syntax KItem ::= "#handleRequestFile"
                   | "#removeRequestFile"
                   | #handleRequest(String)
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

endmodule 
```

```k
requires "soroban-semantics/kasmer.md"
requires "fs.md"

module NODE-SYNTAX
    imports KASMER-SYNTAX
endmodule

module NODE
    imports KASMER
    imports FILE-OPERATIONS

    syntax KItem ::= "#startNode"
                   | "#loadRequest"
                   | "#removeRequestFile"
                   | #handleRequest(String)

    rule [insert-startNode]:
        <k> .K => #startNode </k>
        <instrs> .K </instrs>
        <program> .Steps </program>
      requires #fileExists("request.json")

    rule [startNode]:
        <k> #startNode
         => setExitCode(1)
         ~> #loadRequest
         ~> setExitCode(0) 
            ...
        </k>

    rule [loadRequest]:
        <k> #loadRequest
         => #removeRequestFile
         ~> #handleRequest({#readFile("request.json")}:>String)
            ... 
        </k>

    rule [removeRequestFile]:
        <k> #removeRequestFile => #remove("request.json") ... </k>

endmodule 
```
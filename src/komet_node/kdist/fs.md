
```k
requires "soroban-semantics/fs.md"

module FILE-OPERATIONS
    imports BOOL
    imports K-IO
    imports FILE-SYSTEM

    syntax Bool ::= #fileExists( String )          [function, impure]
                  | #fileExistsResult( IOInt )     [function, impure]
 // -----------------------------------------------------------------
    rule #fileExists( FILE ) => #fileExistsResult(#open(FILE, "r"))

    rule #fileExistsResult(HANDLE:Int)    => #let _ = #close(HANDLE) #in true
    rule #fileExistsResult(_:IOError)     => false

endmodule
```


```k
module FILE-OPERATIONS
    imports BOOL
    imports INT
    imports K-EQUAL
    imports K-IO
    imports STRING

    syntax Int ::= "MAX_READ" [alias]
    // ------------------------------

    rule MAX_READ => 104857600 // 100mb

    syntax IOString ::= #readFile( String ) [function, impure, symbol(readFile)]
    // -----------------------------------------------------------------------------------

    syntax K ::= #writeFile( String, String ) [function, impure, symbol(writeFile)]
               | #appendFile( String, String) [function, impure, symbol(appendFile)]
               | #appendFileToFile( String, String ) [function, impure, symbol(appendFileToFile)]
    // --------------------------------------------------------------------------------------------------

    rule #readFile( FILE )
          => #let HANDLE:IOInt = #open( FILE, "r" ) #in
             #let RESULT = #read({HANDLE}:>Int, MAX_READ) #in
             #let _ = #close({HANDLE}:>Int) #in
             RESULT

    rule #writeFile( FILE, CONTENTS )
          => #let HANDLE:IOInt = #open( FILE, "w") #in
             #let RESULT = #write({HANDLE}:>Int, CONTENTS) #in
             #let _ = #close({HANDLE}:>Int) #in
             RESULT

    rule #appendFile( FILE, CONTENTS )
          => #let HANDLE:IOInt = #open( FILE, "a" ) #in
             #let RESULT = #write({HANDLE}:>Int, CONTENTS) #in
             #let _ = #close({HANDLE}:>Int) #in
             RESULT

    rule #appendFileToFile( DEST, SOURCE )
          => #system( "dd if=" +String SOURCE +String " of=" +String DEST +String " bs=1M oflag=append conv=notrunc" )

    rule #systemResult( _, _, _ ) => .K [owise]
    
    syntax Bool ::= #fileExists( String )   [function, impure]
 // ----------------------------------------------------------
    rule #fileExists( FILE ) => #let HANDLE:IOInt = #open( FILE, "r" ) #in
                                #if isError(HANDLE)
                                #then
                                  false
                                #else
                                  #let _ = #close({HANDLE}:>Int) #in
                                  true
                                #fi

    syntax Bool ::= isError(KItem)     [function, total]
 // ----------------------------------------------------
    rule isError(_:IOError) => true
    rule isError(_)         => false     [owise]

endmodule
```
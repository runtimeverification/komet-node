(module
  (type (;0;) (func (result i64)))
  (type (;1;) (func))
  ;; pub fn foo(_:Env) -> () { }
  (func (;0;) (type 0) (result i64)
    i64.const 2) ;; Void
  (func (;1;) (type 1))
  (memory (;0;) 16)
  (global (;0;) (mut i32) (i32.const 1048576))
  (global (;1;) i32 (i32.const 1048576))
  (global (;2;) i32 (i32.const 1048576))
  (export "memory" (memory 0))
  (export "foo" (func 0))
  (export "_" (func 1))
  (export "__data_end" (global 1))
  (export "__heap_base" (global 2)))

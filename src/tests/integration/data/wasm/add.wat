(module
  (type (;0;) (func (param i64 i64) (result i64)))
  (type (;1;) (func))
  ;; pub fn add(_:Env, a:u32, b:u32) -> u32 { a + b }
  ;; Soroban small U32 host val: (value << 32) | 4. Decode each arg with >>32,
  ;; add, then re-encode the sum as a U32 host val.
  (func (;0;) (type 0) (param i64 i64) (result i64)
    local.get 0
    i64.const 32
    i64.shr_u
    local.get 1
    i64.const 32
    i64.shr_u
    i64.add
    i64.const 32
    i64.shl
    i64.const 4
    i64.or)
  (func (;1;) (type 1))
  (memory (;0;) 16)
  (global (;0;) (mut i32) (i32.const 1048576))
  (global (;1;) i32 (i32.const 1048576))
  (global (;2;) i32 (i32.const 1048576))
  (export "memory" (memory 0))
  (export "add" (func 0))
  (export "_" (func 1))
  (export "__data_end" (global 1))
  (export "__heap_base" (global 2)))

(module
  (type (;0;) (func))
  (type (;1;) (func (param i64 i64) (result i64)))

  ;; add: accept two u32 args, return their sum as u32 (a non-Void value)
  ;; u32 HostVals carry the payload in the high 32 bits and tag 4 in the low bits
  (func (;0;) (type 1) (param i64 i64) (result i64)
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

  ;; _ (Soroban ABI stub)
  (func (;1;) (type 0))

  (memory (;0;) 16)
  (global (;0;) (mut i32) (i32.const 1048576))
  (global (;1;) i32 (i32.const 1048576))
  (global (;2;) i32 (i32.const 1048576))

  (export "memory" (memory 0))
  (export "add" (func 0))
  (export "_" (func 1))
  (export "__data_end" (global 1))
  (export "__heap_base" (global 2))
)

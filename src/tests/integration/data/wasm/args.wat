(module
  (type (;0;) (func))
  (type (;1;) (func (param i64) (result i64)))
  (type (;2;) (func (param i64 i64 i64 i64) (result i64)))
  (type (;3;) (func (param i64 i64) (result i64)))

  ;; test_bool: accept 1 bool arg, return Void
  (func (;0;) (type 1) (param i64) (result i64)
    i64.const 2)

  ;; test_integers: accept u32, i32, u64, i64, return Void
  (func (;1;) (type 2) (param i64 i64 i64 i64) (result i64)
    i64.const 2)

  ;; test_wide_integers: accept u128, i128, return Void
  (func (;2;) (type 3) (param i64 i64) (result i64)
    i64.const 2)

  ;; test_symbol: accept 1 symbol arg, return Void
  (func (;3;) (type 1) (param i64) (result i64)
    i64.const 2)

  ;; _ (Soroban ABI stub)
  (func (;4;) (type 0))

  (memory (;0;) 16)
  (global (;0;) (mut i32) (i32.const 1048576))
  (global (;1;) i32 (i32.const 1048576))
  (global (;2;) i32 (i32.const 1048576))

  (export "memory" (memory 0))
  (export "test_bool" (func 0))
  (export "test_integers" (func 1))
  (export "test_wide_integers" (func 2))
  (export "test_symbol" (func 3))
  (export "_" (func 4))
  (export "__data_end" (global 1))
  (export "__heap_base" (global 2))
)

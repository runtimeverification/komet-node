(module
  (type (;0;) (func))
  (type (;1;) (func (result i64)))

  ;; bytes_new: Soroban host function "b"."4" — allocates an empty Bytes object
  (import "b" "4" (func (;0;) (type 1)))

  ;; empty_bytes: take no args, return a freshly allocated empty Bytes object
  (func (;1;) (type 1) (result i64)
    call 0)

  ;; _ (Soroban ABI stub)
  (func (;2;) (type 0))

  (memory (;0;) 16)
  (global (;0;) (mut i32) (i32.const 1048576))
  (global (;1;) i32 (i32.const 1048576))
  (global (;2;) i32 (i32.const 1048576))

  (export "memory" (memory 0))
  (export "empty_bytes" (func 1))
  (export "_" (func 2))
  (export "__data_end" (global 1))
  (export "__heap_base" (global 2))
)

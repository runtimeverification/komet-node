(module
  (type (;0;) (func))
  (type (;1;) (func (result i64)))
  (type (;2;) (func (param i64 i64 i64) (result i64)))

  ;; put_contract_data(key, val, storage_type) -> Void
  (import "l" "_" (func $put_contract_data (type 2)))

  ;; store: write the persistent storage entry U32(7) -> U32(42), return Void.
  ;; u32 HostVals carry the payload in the high 32 bits and tag 4 in the low bits;
  ;; the storage type is a raw integer (0: temporary, 1: persistent, 2: instance).
  (func $store (type 1) (result i64)
    i64.const 30064771076   ;; key U32(7):  (7 << 32) | 4
    i64.const 180388626436  ;; val U32(42): (42 << 32) | 4
    i64.const 1             ;; storage type: persistent
    call $put_contract_data)

  ;; _ (Soroban ABI stub)
  (func $stub (type 0))

  (memory (;0;) 16)
  (global (;0;) (mut i32) (i32.const 1048576))
  (global (;1;) i32 (i32.const 1048576))
  (global (;2;) i32 (i32.const 1048576))

  (export "memory" (memory 0))
  (export "store" (func $store))
  (export "_" (func $stub))
  (export "__data_end" (global 1))
  (export "__heap_base" (global 2))
)

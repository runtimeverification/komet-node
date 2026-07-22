(module
  (type (;0;) (func))
  (type (;1;) (func (result i64)))
  (type (;2;) (func (param i64 i64) (result i64)))

  ;; Soroban host imports (the semantics resolve these to hostCall):
  ;;   v._  vec_new()                 -> VecObject
  ;;   v.6  vec_push_back(vec, val)   -> VecObject
  ;;   x.1  contract_event(topics, data) -> Void
  (import "v" "_" (func (;0;) (type 1)))
  (import "v" "6" (func (;1;) (type 2)))
  (import "x" "1" (func (;2;) (type 2)))

  ;; emit: publish one contract event with topics [Symbol("transfer")] and
  ;; data U32(42), then return Void.
  ;;
  ;; Small-value encodings per CAP-46-01 (tag in the low 8 bits):
  ;;   Symbol("transfer") = (encode6bit("transfer") << 8) | 14 = 65154533130155790
  ;;   U32(42)            = (42 << 32) | 4                     = 180388626436
  ;;   Void               = 2
  (func (;3;) (type 1) (result i64)
    call 0                          ;; vec_new() -> empty topics vec
    i64.const 65154533130155790     ;; Symbol("transfer")
    call 1                          ;; vec_push_back(vec, symbol)
    i64.const 180388626436          ;; U32(42)
    call 2                          ;; contract_event(topics, data)
    drop
    i64.const 2)                    ;; Void

  ;; _ (Soroban ABI stub)
  (func (;4;) (type 0))

  (memory (;0;) 16)
  (global (;0;) (mut i32) (i32.const 1048576))
  (global (;1;) i32 (i32.const 1048576))
  (global (;2;) i32 (i32.const 1048576))

  (export "memory" (memory 0))
  (export "emit" (func 3))
  (export "_" (func 4))
)

final: prev:
let
  inherit (final) resolveBuildSystem;
  inherit (builtins) mapAttrs;

  # Build system dependencies specified in the shape expected by resolveBuildSystem
  # The empty lists below are lists of optional dependencies.
  #
  # A package `foo` with specification written as:
  # `setuptools-scm[toml]` in pyproject.toml would be written as
  # `foo.setuptools-scm = [ "toml" ]` in Nix
  buildSystemOverrides = {
    # add dependencies here, e.g.:
    # pyperclip.setuptools = [ ];

    # First-party RV packages are consumed as git dependencies (see uv.lock),
    # so uv2nix builds them from source with build isolation disabled. Their
    # build backends are therefore not on PYTHONPATH automatically and must be
    # declared here, otherwise the wheel build fails with e.g.
    # `ModuleNotFoundError: No module named 'hatchling'`.
    komet-node.hatchling = [ ];   # this repo's root package
    komet.hatchling = [ ];        # github.com/runtimeverification/komet
    pykwasm.hatchling = [ ];      # wasm-semantics//pykwasm
    py-wasm.setuptools = [ ];     # github.com/runtimeverification/py-wasm (legacy setup.py)
    py-wasm.wheel = [ ];
  };
in
mapAttrs (
  name: spec:
  prev.${name}.overrideAttrs (old: {
    nativeBuildInputs = old.nativeBuildInputs ++ resolveBuildSystem spec;
  })
) buildSystemOverrides

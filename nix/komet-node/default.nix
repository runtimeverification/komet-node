{
  lib,
  stdenv,
  makeWrapper,

  clang,
  cmake,
  git,
  k,
  boost,
  mpfr,
  openssl,
  gmp,
  secp256k1,
  which,

  komet-node-pyk,
  rev ? null
}:
stdenv.mkDerivation {
  pname = "komet-node";
  version = if (rev != null) then rev else "dirty";

  # Single `out` output holding both the wrapper (`$out/bin/komet-node`) and the
  # compiled semantics (`$out/kdist`). `kup publish`/`kup install` operate on the
  # output literally named `out`, so the runnable artifact must live there. Keeping
  # everything in one output also sidesteps the cross-output reference cycle that
  # forces a separate `bin`/`dev` split (a wrapper in `$bin` referencing `$out`).

  # The K sources for every kdist target (`soroban-semantics.*` from the `komet`
  # dependency and `komet-node.*` from this project) ship inside `komet-node-pyk`,
  # so there are no sources to unpack here; the build only needs a working
  # directory to place the kdist output into.
  dontUnpack = true;

  buildInputs = [
    clang
    cmake
    git
    boost
    mpfr
    openssl
    gmp
    secp256k1
    komet-node-pyk
    k
  ];

  nativeBuildInputs = [ makeWrapper ];

  dontUseCmakeConfigure = true;

  enableParallelBuilding = true;

  # `kdist` writes the compiled semantics under `$XDG_CACHE_HOME/kdist-<hash>/`.
  # Build the same targets the Makefile's `kdist-build` does. The `komet-node.*`
  # targets pull in `soroban-semantics.source` as a dependency automatically.
  #
  # Cap concurrency at 2 (matching the Makefile's `kdist-build`): each LLVM/Haskell
  # `kompile` job links a large generated interpreter and peaks at several GB of
  # RAM, so an unbounded `-j$NIX_BUILD_CORES` can OOM the builder on machines with
  # many cores but limited memory.
  buildPhase = ''
    runHook preBuild
    XDG_CACHE_HOME=$(pwd) ${
      lib.optionalString
      (stdenv.isAarch64 && stdenv.isDarwin)
      "APPLE_SILICON=true"
    } kdist -v build -j2 'soroban-semantics.*' 'komet-node.*'
    runHook postBuild
  '';

  installPhase = ''
    runHook preInstall
    mkdir -p $out/bin
    mkdir -p $out/kdist

    cp -r ./kdist-*/* $out/kdist/

    # Wrap the `komet-node` entrypoint so that, at runtime, it finds the compiled
    # semantics via `KDIST_DIR` and the K tools (krun/kore) via `PATH`.
    makeWrapper ${komet-node-pyk}/bin/komet-node $out/bin/komet-node \
      --prefix PATH : ${lib.makeBinPath [ which k ]} \
      --set KDIST_DIR $out/kdist
    runHook postInstall
  '';

  meta = {
    description = "Local development testnet for Stellar based on K semantics";
    mainProgram = "komet-node";
  };
}

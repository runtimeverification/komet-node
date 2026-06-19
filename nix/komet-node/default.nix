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

  outputs = [
    "bin"
    # contains kdist artifacts (the compiled K semantics)
    "out"
    # this empty `dev` output is required as we otherwise get cyclic dependencies between `bin` and `out`
    # this is due to a setup-hook creating references in a new directory `nix-support` in either `out` or `dev`
    "dev"
  ];

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
    mkdir -p $bin/bin
    mkdir -p $out/kdist

    cp -r ./kdist-*/* $out/kdist/

    # Wrap the `komet-node` entrypoint so that, at runtime, it finds the compiled
    # semantics via `KDIST_DIR` and the K tools (krun/kore) via `PATH`.
    makeWrapper ${komet-node-pyk}/bin/komet-node $bin/bin/komet-node \
      --prefix PATH : ${lib.makeBinPath [ which k ]} \
      --set KDIST_DIR $out/kdist
    runHook postInstall
  '';

  meta = {
    description = "Local development testnet for Stellar based on K semantics";
    mainProgram = "komet-node";
  };
}

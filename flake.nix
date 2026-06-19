{
  description = "komet-node - Local development testnet for Stellar based on K semantics";
  inputs = {
    rv-nix-tools.url = "github:runtimeverification/rv-nix-tools/854d4f05ea78547d46e807b414faad64cea10ae4";
    nixpkgs.follows = "rv-nix-tools/nixpkgs";
    flake-utils.url = "github:numtide/flake-utils";
    # K Framework, pinned to match the `kframework` (pyk) version in uv.lock —
    # pyk and the K binaries must be the same version. We consume the prebuilt
    # `k` package directly (replacing the imperative `kup install k`); its
    # nixpkgs is intentionally NOT followed, so the k-framework binary caches
    # are hit instead of rebuilding K against our nixpkgs.
    k-framework.url = "github:runtimeverification/k/v7.1.319";
    # Use the same uv2nix as k-framework so we inherit the pyproject-nix version
    # that fixes the missing 'riscv64' attribute in pep600.nix (pep599.manyLinuxTargetMachines
    # lookup now uses `or tagArch` as a safe default for unknown architectures).
    uv2nix.follows = "k-framework/uv2nix";
    nixpkgs-unstable.url = "github:NixOS/nixpkgs/nixos-unstable";
    pyproject-build-systems.url = "github:pyproject-nix/build-system-pkgs/7dba6dbc73120e15b558754c26024f6c93015dd7";
    pyproject-build-systems = {
      inputs.nixpkgs.follows = "uv2nix/nixpkgs";
      inputs.uv2nix.follows = "uv2nix";
      inputs.pyproject-nix.follows = "uv2nix/pyproject-nix";
    };
    pyproject-nix.follows = "uv2nix/pyproject-nix";
  };
  outputs = { self, nixpkgs, nixpkgs-unstable, flake-utils, pyproject-nix, pyproject-build-systems, uv2nix, k-framework, rv-nix-tools }:
  let
    pythonVer = "310";
  in flake-utils.lib.eachDefaultSystem (system:
    let
      # uv is heavily outdated in older nixpkgs revisions; use the binary
      # release of uv provided by uv2nix instead.
      uvOverlay = final: prev: {
        uv = uv2nix.packages.${final.system}.uv-bin;
      };
      # The k-framework overlay also pulls in a lot of unrelated packages, so we
      # only expose the `k` binary that we actually consume.
      kOverlay = final: prev: {
        k = k-framework.packages.${final.system}.k;
      };
      komet-nodeOverlay = final: prev:
      let
        # The Python virtual environment, built against a modern stdenv from
        # nixpkgs-unstable (see below). It carries the `komet-node` entrypoint
        # together with the `kdist` tool and the K sources for every kdist target.
        komet-node-pyk = pkgs.callPackage ./nix/komet-node-pyk {
          inherit pyproject-nix pyproject-build-systems uv2nix;
          python = pkgs."python${pythonVer}";
        };
      in {
        # Build the wrapper against `nixpkgs` (the RV-pinned package set that the K
        # toolchain is built and tested against) rather than nixpkgs-unstable, so
        # the C/C++ toolchain that kompiles the LLVM semantics matches `k`. The
        # compiled semantics run as a subprocess (krun), so there is no ABI
        # coupling with the nixpkgs-unstable Python environment.
        komet-node = final.callPackage ./nix/komet-node {
          inherit komet-node-pyk;
          rev = self.rev or null;
        };
      };
      # Use nixpkgs-unstable for the Python package set so the Python environment
      # is built against a modern stdenv on all platforms.
      pkgs = import nixpkgs-unstable {
        inherit system;
        overlays = [
          uvOverlay
        ];
      };
      # The native toolchain for kompiling the K semantics comes from the RV-pinned
      # nixpkgs so it matches the `k` binary.
      pkgsK = import nixpkgs {
        inherit system;
        overlays = [
          kOverlay
          komet-nodeOverlay
        ];
      };
      python = pkgs."python${pythonVer}";
    in {
      devShells.default = pkgs.mkShell {
        name = "uv develop shell";
        buildInputs = [
          python
          pkgs.uv
          pkgs.gnumake                          # the project's Makefile drives every dev task
          pkgs.wabt                             # wat2wasm, used by integration tests
          k-framework.packages.${system}.k      # K Framework (kompile/krun/...), replaces `kup install k`
        ];
        env = {
          # prevent uv from managing Python downloads and force use of specific
          UV_PYTHON_DOWNLOADS = "never";
          UV_PYTHON = python.interpreter;
          UV_LINK_MODE = "copy";
        };
        shellHook = ''
          unset PYTHONPATH
        '';
      };
      packages = rec {
        inherit (pkgsK) komet-node;
        default = komet-node;
      };
    }) // {
      overlays.default = final: prev: {
        inherit (self.packages.${final.system}) komet-node;
      };
    };
}

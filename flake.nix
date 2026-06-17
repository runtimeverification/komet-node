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
    uv2nix.url = "github:pyproject-nix/uv2nix/680e2f8e637bc79b84268949d2f2b2f5e5f1d81c";
    # uv2nix requires a newer version of nixpkgs, so pin it separately and
    # overlay only the missing compatibility helpers into our main package set.
    nixpkgs-unstable.url = "github:NixOS/nixpkgs/nixos-unstable";
    uv2nix.inputs.nixpkgs.follows = "nixpkgs-unstable";
    pyproject-build-systems.url = "github:pyproject-nix/build-system-pkgs/7dba6dbc73120e15b558754c26024f6c93015dd7";
    pyproject-build-systems = {
      inputs.nixpkgs.follows = "uv2nix/nixpkgs";
      inputs.uv2nix.follows = "uv2nix";
      inputs.pyproject-nix.follows = "uv2nix/pyproject-nix";
    };
    pyproject-nix.follows = "uv2nix/pyproject-nix";
  };
  outputs = { self, nixpkgs, nixpkgs-unstable, flake-utils, pyproject-nix, pyproject-build-systems, uv2nix, k-framework }:
  let
    pythonVer = "310";
  in flake-utils.lib.eachDefaultSystem (system:
    let
      pkgs-unstable = import nixpkgs-unstable {
        inherit system;
      };
      staleNixpkgsOverlay = final: prev: {
        inherit (pkgs-unstable) replaceVars;
        lib = prev.lib // {
          match = builtins.match;
        };
      };
      # due to the nixpkgs that we use in this flake being outdated, uv is also heavily outdated
      # we can instead use the binary release of uv provided by uv2nix for now
      uvOverlay = final: prev: {
        uv = uv2nix.packages.${final.system}.uv-bin;
      };
      komet-nodeOverlay = final: prev: {
        komet-node = final.callPackage ./nix/komet-node {
          inherit pyproject-nix pyproject-build-systems uv2nix;
          python = final."python${pythonVer}";
        };
      };
      pkgs = import nixpkgs {
        inherit system;
        overlays = [
          staleNixpkgsOverlay
          uvOverlay
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
        inherit (pkgs) komet-node;
        default = komet-node;
      };
    }) // {
      overlays.default = final: prev: {
        inherit (self.packages.${final.system}) komet-node;
      };
    };
}

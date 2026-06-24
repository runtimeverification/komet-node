
<div align="center">

# 🌠 Komet Node

**A local Stellar testnet node based on [K formal semantics](https://github.com/runtimeverification/komet) of Soroban.**

[![Install](https://img.shields.io/badge/install-kup-blue)](https://kframework.org/install)
[![Discord](https://img.shields.io/badge/discord-join-7289da)](https://discord.gg/CurfmXNtbN)

[Installation](#installation) • [Usage](#usage) • [Contribute](#for-developers)

</div>

---

## 🌟 Overview

`komet-node` is a Stellar testnet node you run on your own machine. It serves the standard [Stellar RPC](https://developers.stellar.org/docs/data/apis/rpc) API, so the SDKs, wallets, and tooling you already use with Stellar work against it unchanged — you point them at `localhost` instead of a public network.

It is built for developing, testing, and debugging Soroban contracts locally, and adds two capabilities a public Stellar network does not offer:

- **Instruction-level traces.** Every transaction is traced as it runs, and the `traceTransaction` method returns that step-by-step record of every WebAssembly instruction the contract executed, so you can see exactly what happened — and where it went wrong.
- **Reproducible, replayable state.** The ledger state is persisted to disk, so you can stop and restart the node, save a state, and replay transactions against it to reproduce a result.

## 🚀 Quick Start

### Installation

#### Install with kup

`komet-node` is distributed through [`kup`](https://github.com/runtimeverification/kup), Runtime Verification's Nix-based package manager. It pulls prebuilt binaries (including the matching K Framework and compiled semantics) from RV's binary cache, so there is nothing to compile.

```bash
# 1. Install the kup package manager (one time)
bash <(curl https://kframework.org/install)

# 2. Install komet-node
kup install komet-node

# 3. Verify the installation
komet-node --help
```

To upgrade later, run `kup update komet-node`.

#### Run with Docker

Alternatively, a prebuilt image is published to Docker Hub for each release. It bundles K, the compiled semantics, and `komet-node` ready to serve.

```bash
# Pull the image (replace the tag with the release you want)
docker pull runtimeverificationinc/komet-node:ubuntu-jammy-0.1.0

# Start the server, exposing the RPC port on the host
docker run --rm -p 8000:8000 \
  runtimeverificationinc/komet-node:ubuntu-jammy-0.1.0 \
  komet-node --host 0.0.0.0 --port 8000
```

> The server binds to `localhost` by default; pass `--host 0.0.0.0` inside the container so the port is reachable from the host.

---

### Usage

#### Start the server

Run `komet-node` to start the server; `komet-node --help` prints the full usage:

```
usage: komet-node [-h] [--host HOST] [--port PORT] [--io-dir IO_DIR]

Komet Node — a local Stellar testnet backed by the K semantics of Soroban.

options:
  -h, --help       show this help message and exit
  --host HOST      bind address (default: localhost)
  --port PORT      port to listen on (default: 8000)
  --io-dir IO_DIR  directory for all input/output artifacts (default: a fresh
                   temporary directory)

examples:
  komet-node                     serve on localhost:8000 in a fresh temp dir
  komet-node --port 9000         use a custom port
  komet-node --io-dir ./chain    keep all artifacts under ./chain (persistent)
  komet-node --host 0.0.0.0      accept connections from outside localhost
```

#### Trace a transaction

Every submitted transaction is traced as it executes, and the instruction-level trace is stored on its receipt. `traceTransaction` retrieves that stored trace, looked up by transaction hash — the same hash `getTransaction` takes. So tracing a contract invocation is two calls: `sendTransaction` to run it, then `traceTransaction` with the returned hash. Tracing is always on; there is no flag to enable.

Submitting transactions uses the standard two-step Stellar pattern — `sendTransaction` with a base64 XDR envelope, then poll `getTransaction` by hash. Because there is no mempool, `komet-node` executes the transaction synchronously inside `sendTransaction`, so the result is already available by the time you poll. See [docs/server.md](docs/server.md) for the full RPC reference.

A trace requires a deployed contract. The four envelopes below are pre-built and signed (a tiny contract whose `foo()` returns void, deployed from a fixed key) so you can paste them straight in — the local node does not check signatures, sequence numbers, or timebounds, so they work as-is on a fresh chain. After a quick health check, run them in order against the server started above.

```bash
# Is the server alive?
curl -s http://localhost:8000 -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"getHealth","params":{}}'
# => {"jsonrpc":"2.0","id":1,"result":{"status":"healthy"}}

# 1. Create the deployer account
curl -s http://localhost:8000 -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"sendTransaction","params":{"transaction":"AAAAAgAAAAADoQe/884Qvh1w3RjnS8CZZ+TWMJulDV8d3IZkElUxuAAAAGQAAAAAAAAAAQAAAAEAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAAAADoQe/884Qvh1w3RjnS8CZZ+TWMJulDV8d3IZkElUxuAAAAAJUC+QAAAAAAAAAAAESVTG4AAAAQMOMXdUuK9E9tF0pgpqX+z+nXFlE6Mn5e7rqOFL8jIolInsXc7XHPgvYs4VWDqlCGI/fom9SpYiHOQYUqKTvDAc="}}'

# 2. Upload the contract wasm
curl -s http://localhost:8000 -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"sendTransaction","params":{"transaction":"AAAAAgAAAAADoQe/884Qvh1w3RjnS8CZZ+TWMJulDV8d3IZkElUxuAAAAGQAAAAAAAAAAgAAAAEAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAEAAAAAAAAAGAAAAAIAAABzAGFzbQEAAAABCAJgAAF+YAAAAwMCAAEFAwEAEAYZA38BQYCAwAALfwBBgIDAAAt/AEGAgMAACwcvBQZtZW1vcnkCAANmb28AAAFfAAEKX19kYXRhX2VuZAMBC19faGVhcF9iYXNlAwIKCQIEAEICCwIACwAAAAAAAAAAAAAAAAESVTG4AAAAQOk89R0Qlko4dCBI3XziT3XTjdm4kyKtpy9ky3uVksIYsSFWXKHTHOiCDaxNKdecQKbhQnD/9ELWxxr98D5ecQ4="}}'

# 3. Deploy a contract instance
curl -s http://localhost:8000 -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"sendTransaction","params":{"transaction":"AAAAAgAAAAADoQe/884Qvh1w3RjnS8CZZ+TWMJulDV8d3IZkElUxuAAAAGQAAAAAAAAAAwAAAAEAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAEAAAAAAAAAGAAAAAMAAAAAAAAAAAAAAAADoQe/884Qvh1w3RjnS8CZZ+TWMJulDV8d3IZkElUxuAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFdPOLtg6vmmrgodRyN6P3wk1UfHrQxVekpbnsYYOcpvAAAAAAAAAAAAAAAAAAAAARJVMbgAAABAnLtNirBI7XdD2xwH3ws3rTDEhCxJ8mCRNU66d7b4MR2Ih9WtZzqb6akBqK6yA1GIavzVa7ahq2FNBflk+JpOBg=="}}'

# 4. Invoke foo() — sendTransaction runs it and returns its hash
curl -s http://localhost:8000 -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"sendTransaction","params":{"transaction":"AAAAAgAAAAADoQe/884Qvh1w3RjnS8CZZ+TWMJulDV8d3IZkElUxuAAAAGQAAAAAAAAABAAAAAEAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAEAAAAAAAAAGAAAAAAAAAABaiD+wakIF3Ol8jzjcPkl8jY0blEEON3W1A9rJxHBNOAAAAADZm9vAAAAAAAAAAAAAAAAAAAAAAESVTG4AAAAQKB9w/QmdK59UzXVbxXJp+5qfNpFSa495yajOyPM5KmYblE3/AbWqnnZMxTiBea0ShGZehgvo12AIyw48Lb1Xw0="}}'
# => {"jsonrpc":"2.0","id":1,"result":{"hash":"c7099cbe10a9bfa1cdf9c9d368e1e1c932f535a70e4403b7aa409ce19fc36805","status":"PENDING", ...}}

# 5. Retrieve the trace for that hash
curl -s http://localhost:8000 -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"traceTransaction","params":{"hash":"c7099cbe10a9bfa1cdf9c9d368e1e1c932f535a70e4403b7aa409ce19fc36805"}}'
```

`traceTransaction` returns the stored trace as its result. The trace is a JSONL string (one JSON record per executed WebAssembly instruction); it is shown decoded here for readability:

```jsonc
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": [
    {"pos": 3,    "instr": ["const", "i32", 1048576], "stack": [], "locals": {}},
    {"pos": 11,   "instr": ["const", "i32", 1048576], "stack": [], "locals": {}},
    {"pos": 19,   "instr": ["const", "i32", 1048576], "stack": [], "locals": {}},
    {"pos": null, "instr": ["block"],                 "stack": [], "locals": {}},
    {"pos": 3,    "instr": ["const", "i64", 2],       "stack": [], "locals": {}}
  ]
}
```

Each trace record captures the VM state at instruction entry: `pos` is the instruction's byte offset in the binary (`null` for synthetic instructions), `instr` is the instruction and its operands, and `stack`/`locals` are the value stack and locals as `[type, value]` pairs. See [docs/interpreter.md](docs/interpreter.md) for the full trace format.

---

## For Developers

Prerequisites: `python >= 3.10`, [`uv`](https://docs.astral.sh/uv/), [`wabt`](https://github.com/WebAssembly/wabt) (for `wat2wasm`), and the K Framework. The Dev Container provisions all of these for you.

1. Install [VS Code](https://code.visualstudio.com/) and the [Dev Containers extension](https://marketplace.visualstudio.com/items?itemName=ms-vscode-remote.remote-containers).
2. Open this repository in VS Code and choose **Reopen in Container** when prompted.
3. Once the container finishes building, build the semantics and run the test suite:

   ```bash
   make kdist-build   # compile the K semantics (first run only; takes a while)
   make test-unit     # quick sanity check
   ```

Common tasks are driven by `make` (see the [Makefile](Makefile) for the complete list):

| Target | Description |
|---|---|
| `make kdist-build` | Compile the K semantics (required before running integration tests) |
| `make build` | Build the wheel |
| `make test-unit` | Run unit tests |
| `make test-integration` | Run integration tests |
| `make test` | Run the full test suite |
| `make cov` | Run tests with a coverage report |
| `make check` | Run all style/type checks (flake8, mypy, autoflake, isort, black) |
| `make format` | Auto-format the codebase |

To build the node from source use:

```bash
make build-kdist
make build
pip install dist/*.whl
```

### Documentation

- [Architecture overview](docs/architecture.md) — how the pieces fit together
- [Server](docs/server.md) — the long-running HTTP server that wraps the K interpreter, plus the state lifecycle and the full method reference
- [Transaction encoding](docs/transaction.md) — Stellar XDR → K request envelope
- [Interpreter](docs/interpreter.md) — running request envelopes through the K semantics
- [K semantics](docs/node-semantics.md) — the on-chain RPC dispatch and execution model

---

## About

`komet-node` is developed by [Runtime Verification](https://runtimeverification.com/). It builds on [Komet](https://github.com/runtimeverification/komet), the K semantics of Soroban smart contracts, and the [K Framework](https://github.com/runtimeverification/k).

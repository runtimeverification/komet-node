
<div align="center">

# 🌠 Komet Node

**A local Stellar testnet node based on [K formal semantics](https://github.com/runtimeverification/komet) of Soroban.**

[![Install](https://img.shields.io/badge/install-kup-blue)](https://kframework.org/install)
[![Discord](https://img.shields.io/badge/discord-join-7289da)](https://discord.gg/CurfmXNtbN)

[Installation](#installation) • [Usage](#usage) • [Contribute](#for-developers) • [Community](https://discord.gg/CurfmXNtbN)

</div>

---

## 🌟 Overview

`komet-node` extends the standard Stellar RPC with a `traceTransaction` method that provides instruction-level execution traces. The node's ledger state can be saved to and restored from a single file, allowing the same ledger state to be reproduced and transactions to be replayed.

## 🚀 Quick Start

### Installation

#### Install with kup

`komet-node` is distributed through [`kup`](https://github.com/runtimeverification/kup), Runtime Verification's Nix-based package manager. It pulls prebuilt binaries (including the matching K Framework and kompiled semantics) from RV's binary cache, so there is nothing to compile.

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

Alternatively, a prebuilt image is published to Docker Hub for each release. It bundles K, the kompiled semantics, and `komet-node` ready to serve.

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

```bash
komet-node                       # serve on localhost:8000, state in ./state.kore
komet-node --help                # print general usage information
komet-node --port 9000           # custom port
komet-node --trace               # enable instruction-level execution tracing
```

| Flag | Default | Description |
|---|---|---|
| `--host` | `localhost` | Bind address |
| `--port` | `8000` | Port to listen on |
| `--state-file` | `state.kore` | Path to the persistent state file |
| `--trace` | off | Enable instruction-level execution tracing |

On first start the server creates an empty `state.kore`. Delete that file to reset the chain, or point `--state-file` at a pre-built configuration to resume from a snapshot.

#### Verify the server with `curl`

The server is operated via the Stellar RPC protocol. The read-only methods below take no transaction payload and can be used as a quick health check:

```bash
# Is the server alive?
curl -s http://localhost:8000 \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"getHealth","params":{}}'
# => {"jsonrpc":"2.0","id":1,"result":{"status":"healthy"}}
```

```bash
# Which network am I connected to?
curl -s http://localhost:8000 \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"getNetwork","params":{}}'
# => {"jsonrpc":"2.0","id":1,"result":{"passphrase":"Test SDF Network ; September 2015","protocolVersion":"22","friendbotUrl":null}}
```

```bash
# What is the current ledger sequence? (increments per committed transaction)
curl -s http://localhost:8000 \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"getLatestLedger","params":{}}'
# => {"jsonrpc":"2.0","id":1,"result":{"id":"00...00","protocolVersion":"22","sequence":0}}
```

Submitting transactions uses the standard two-step Stellar pattern — `sendTransaction` with a base64 XDR envelope, then poll `getTransaction` by hash. Because there is no mempool, `komet-node` executes the transaction synchronously inside `sendTransaction`, so the result is already available by the time you poll. The trace example below shows this flow end-to-end with ready-to-run envelopes; see [docs/server.md](docs/server.md) for the full RPC reference.

#### Trace a transaction

`traceTransaction` executes a transaction and returns an instruction-level execution trace inline, in a single call. Tracing only applies to contract invocations, so the server must be started with `--trace`:

```bash
komet-node --trace
```

A trace requires a deployed contract. The four envelopes below are pre-built and signed (a tiny contract whose `foo()` returns void, deployed from a fixed key) so you can paste them straight in — the local node does not check signatures, sequence numbers, or timebounds, so they work as-is on a fresh chain. Run them in order against the server above.

```bash
# 1. Create the deployer account
curl -s http://localhost:8000 -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"sendTransaction","params":{"transaction":"AAAAAgAAAAADoQe/884Qvh1w3RjnS8CZZ+TWMJulDV8d3IZkElUxuAAAAGQAAAAAAAAAAQAAAAEAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAAAADoQe/884Qvh1w3RjnS8CZZ+TWMJulDV8d3IZkElUxuAAAAAJUC+QAAAAAAAAAAAESVTG4AAAAQMOMXdUuK9E9tF0pgpqX+z+nXFlE6Mn5e7rqOFL8jIolInsXc7XHPgvYs4VWDqlCGI/fom9SpYiHOQYUqKTvDAc="}}'

# 2. Upload the contract wasm
curl -s http://localhost:8000 -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"sendTransaction","params":{"transaction":"AAAAAgAAAAADoQe/884Qvh1w3RjnS8CZZ+TWMJulDV8d3IZkElUxuAAAAGQAAAAAAAAAAgAAAAEAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAEAAAAAAAAAGAAAAAIAAABzAGFzbQEAAAABCAJgAAF+YAAAAwMCAAEFAwEAEAYZA38BQYCAwAALfwBBgIDAAAt/AEGAgMAACwcvBQZtZW1vcnkCAANmb28AAAFfAAEKX19kYXRhX2VuZAMBC19faGVhcF9iYXNlAwIKCQIEAEICCwIACwAAAAAAAAAAAAAAAAESVTG4AAAAQOk89R0Qlko4dCBI3XziT3XTjdm4kyKtpy9ky3uVksIYsSFWXKHTHOiCDaxNKdecQKbhQnD/9ELWxxr98D5ecQ4="}}'

# 3. Deploy a contract instance
curl -s http://localhost:8000 -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"sendTransaction","params":{"transaction":"AAAAAgAAAAADoQe/884Qvh1w3RjnS8CZZ+TWMJulDV8d3IZkElUxuAAAAGQAAAAAAAAAAwAAAAEAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAEAAAAAAAAAGAAAAAMAAAAAAAAAAAAAAAADoQe/884Qvh1w3RjnS8CZZ+TWMJulDV8d3IZkElUxuAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFdPOLtg6vmmrgodRyN6P3wk1UfHrQxVekpbnsYYOcpvAAAAAAAAAAAAAAAAAAAAARJVMbgAAABAnLtNirBI7XdD2xwH3ws3rTDEhCxJ8mCRNU66d7b4MR2Ih9WtZzqb6akBqK6yA1GIavzVa7ahq2FNBflk+JpOBg=="}}'

# 4. Invoke foo() via traceTransaction — the trace comes back inline
curl -s http://localhost:8000 -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"traceTransaction","params":{"transaction":"AAAAAgAAAAADoQe/884Qvh1w3RjnS8CZZ+TWMJulDV8d3IZkElUxuAAAAGQAAAAAAAAABAAAAAEAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAEAAAAAAAAAGAAAAAAAAAABaiD+wakIF3Ol8jzjcPkl8jY0blEEON3W1A9rJxHBNOAAAAADZm9vAAAAAAAAAAAAAAAAAAAAAAESVTG4AAAAQKB9w/QmdK59UzXVbxXJp+5qfNpFSa495yajOyPM5KmYblE3/AbWqnnZMxTiBea0ShGZehgvo12AIyw48Lb1Xw0="}}'
```

The final call returns the result inline. The `trace` field is itself a JSONL string (one JSON record per executed WebAssembly instruction); it is shown decoded here for readability:

```jsonc
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "hash": "c7099cbe10a9bfa1cdf9c9d368e1e1c932f535a70e4403b7aa409ce19fc36805",
    "status": "SUCCESS",
    "ledger": "4",
    "latestLedger": "4",
    "latestLedgerCloseTime": "1716000000",
    "trace": [
      {"pos": 3,    "instr": ["const", "i32", 1048576], "stack": [], "locals": {}},
      {"pos": 11,   "instr": ["const", "i32", 1048576], "stack": [], "locals": {}},
      {"pos": 19,   "instr": ["const", "i32", 1048576], "stack": [], "locals": {}},
      {"pos": null, "instr": ["block"],                 "stack": [], "locals": {}},
      {"pos": 3,    "instr": ["const", "i64", 2],       "stack": [], "locals": {}}
    ]
  }
}
```

Each trace record captures the VM state at instruction entry: `pos` is the instruction's byte offset in the binary (`null` for synthetic instructions), `instr` is the instruction and its operands, and `stack`/`locals` are the value stack and locals as `[type, value]` pairs. See [docs/interpreter.md](docs/interpreter.md) for the full trace format.

#### Walk through a contract lifecycle

The bundled demo deploys and invokes a Soroban contract end-to-end (create account → upload wasm → deploy → invoke):

```bash
uv run python -m komet_node.demo src/tests/integration/data/wasm/empty.wat
```

This produces `state.kore` plus `state_<n>_<step>.pretty` files under `./out`, letting you inspect exactly how the formal state evolves. (Requires `wat2wasm` from [`wabt`](https://github.com/WebAssembly/wabt) on your `PATH`.)

---

## For Developers

Prerequisites: `python >= 3.10`, [`uv`](https://docs.astral.sh/uv/), [`wabt`](https://github.com/WebAssembly/wabt) (for `wat2wasm`), and the K Framework. The [Dev Container](#for-developers) provisions all of these for you.

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
make build
pip install dist/*.whl
```

### Documentation

- [Architecture overview](docs/architecture.md) — how the pieces fit together
- [Server](docs/server.md) — the RPC layer, state lifecycle, and full method reference
- [Interpreter](docs/interpreter.md) — transaction → K step translation
- [K semantics](docs/node-semantics.md) — the on-chain execution model

---

## About

`komet-node` is developed by [Runtime Verification](https://runtimeverification.com/). It builds on [Komet](https://github.com/runtimeverification/komet), the K semantics of Soroban smart contracts, and the [K Framework](https://github.com/runtimeverification/k).

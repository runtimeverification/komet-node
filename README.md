
<div align="center">

# 🌠 Komet Node

**A local Stellar testnet node based on [K formal semantics](https://github.com/runtimeverification/komet) of Soroban.**

[![Install](https://img.shields.io/badge/install-kup-blue)](https://kframework.org/install)
[![Discord](https://img.shields.io/badge/discord-join-7289da)](https://discord.gg/CurfmXNtbN)

[Installation](#installation) • [Usage](#usage) • [Contribute](#for-developers)•  [Community](#community)

</div>

---

## 🌟 Overview

`komet-node` is designed for Soroban developers who need advanced debugging capabilities. It extends the standard Stellar RPC with a `traceTransaction` method that provides instruction-level execution traces. The node's ledger state can be saved to and restored from a single file, enabling developers to reproduce exact network conditions and deterministically replay transactions.

## 🚀 Quick Start

### Installation

Pick the method that matches how you intend to use `komet-node`:

| You are a… | Use |
|---|---|
| User who wants the `komet-node` binary | [**kup**](#install-with-kup-recommended) (recommended) |
| User who prefers containers | [**Docker**](#run-with-docker) |
| Developer hacking on `komet-node` | [**Dev Container**](#develop-with-the-dev-container) |

#### Install with kup (recommended)

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

A prebuilt image is published to Docker Hub for each release. It bundles K, the kompiled semantics, and `komet-node` ready to serve.

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

The server is operated via the Stellar RPC protocol. The read-only methods below take no transaction payload, which makes them perfect for a quick health check:

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

Submitting transactions uses the standard two-step Stellar pattern — `sendTransaction` with a base64 XDR envelope, then poll `getTransaction` by hash:

```bash
# Submit a signed transaction (XDR envelope produced by a Stellar SDK)
curl -s http://localhost:8000 \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"sendTransaction","params":{"transaction":"<base64-XDR-envelope>"}}'
# => {... "result":{"hash":"<64-char hex>","status":"PENDING", ...}}

# Poll for the result using the returned hash
curl -s http://localhost:8000 \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"getTransaction","params":{"hash":"<64-char hex>"}}'
# => {... "result":{"status":"SUCCESS","ledger":"1", ...}}
```

Because there is no mempool, `komet-node` executes the transaction synchronously inside `sendTransaction`; the result is already available by the time you poll. See [docs/server.md](docs/server.md) for the full RPC reference, including the `traceTransaction` method.

#### Walk through a contract lifecycle

The bundled demo deploys and invokes a Soroban contract end-to-end (create account → upload wasm → deploy → invoke):

```bash
uv run python -m komet_node.demo src/tests/integration/data/wasm/empty.wat
```

This produces `state.kore` plus `state_<n>_<step>.pretty` files under `./out`, letting you inspect exactly how the formal state evolves. (Requires `wat2wasm` from [`wabt`](https://github.com/WebAssembly/wabt) on your `PATH`.)

---

## For Developers

Prerequisites: `python >= 3.10`, [`uv`](https://docs.astral.sh/uv/), [`wabt`](https://github.com/WebAssembly/wabt) (for `wat2wasm`), and the K Framework. The [Dev Container](#develop-with-the-dev-container) provisions all of these for you.

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

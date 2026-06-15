# komet-node runtime image.
#
# Built on the K Framework base image (which already bundles K and a matching
# Z3), then layered with the komet-node Python package and its kompiled K
# semantics. komet-node only needs `wat2wasm` (wabt) at runtime to turn the
# `.wat` test contracts into wasm -- it does not invoke stellar-cli/soroban or
# cargo, so the Rust/Soroban toolchain is intentionally left out to keep the
# image small.
#
# K_VERSION is supplied by the release workflow from deps/k_release.
ARG K_VERSION
FROM runtimeverificationinc/kframework-k:ubuntu-jammy-${K_VERSION}

ARG PYTHON_VERSION=3.10

RUN    apt-get -y update             \
    && apt-get -y install            \
         curl                        \
         git                         \
         graphviz                    \
         python${PYTHON_VERSION}     \
         python${PYTHON_VERSION}-dev \
         python3-pip                 \
         wabt                        \
    && apt-get -y clean

ARG USER_ID=1010
ARG GROUP_ID=1010
RUN    groupadd -g ${GROUP_ID} user \
    && useradd -m -u ${USER_ID} -s /bin/bash -g user user

USER user
WORKDIR /home/user

ADD --chown=user:user . komet-node

ENV PATH=/home/user/.local/bin:${PATH}
# Installs komet-node together with its dependencies (the `komet` package, which
# carries the Soroban K semantics source, and the matching `kframework` pyk).
RUN    pip install ./komet-node \
    && rm -rf komet-node

# Pre-kompile the K semantics so the container is ready to serve immediately.
# The sources come from the installed packages, so the checkout is no longer needed.
RUN kdist --verbose build -j2 'komet-node.*' 'soroban-semantics.*'

# syntax=docker/dockerfile:1.7
FROM ubuntu:24.04 AS runner

ARG RUNNER_VERSION=2.336.0
ARG RUNNER_SHA256_X64=04cf0be1aff4c3ec3554466c39124ca250e3effd8873bb7e8d68535aa9505d5d
ARG RUNNER_SHA256_ARM64=58b758e420b87093fbd4bfddd368074960053e2f1388f01848c82624b90f27d1
ARG RUST_TOOLCHAIN=stable
ARG TARGETARCH

ENV DEBIAN_FRONTEND=noninteractive
RUN --mount=type=cache,id=easyrunners-runner-apt-cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,id=easyrunners-runner-apt-lists,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
      bash build-essential ca-certificates curl docker.io docker-compose-v2 git jq \
      clang cmake libicu74 libssl-dev libssl3 lld pkg-config protobuf-compiler \
      python3 python3-pip python3-venv sudo tar unzip zip \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 1001 runner \
    && useradd --uid 1001 --gid 1001 --create-home --shell /bin/bash runner \
    && echo 'runner ALL=(ALL) NOPASSWD:ALL' >/etc/sudoers.d/runner \
    && chmod 0440 /etc/sudoers.d/runner

WORKDIR /home/runner
RUN case "${TARGETARCH}" in \
      amd64) runner_arch=x64; runner_sha="${RUNNER_SHA256_X64}" ;; \
      arm64) runner_arch=arm64; runner_sha="${RUNNER_SHA256_ARM64}" ;; \
      *) echo "Unsupported architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac \
    && curl --fail --location --retry 5 \
      --output actions-runner.tar.gz \
      "https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/actions-runner-linux-${runner_arch}-${RUNNER_VERSION}.tar.gz" \
    && echo "${runner_sha}  actions-runner.tar.gz" | sha256sum --check --strict \
    && tar xzf actions-runner.tar.gz \
    && rm actions-runner.tar.gz \
    && ./bin/installdependencies.sh \
    && mkdir -p _work _diag \
    && chown -R runner:runner /home/runner

COPY runner/entrypoint.sh /entrypoint.sh
RUN chmod 0755 /entrypoint.sh

ENV CARGO_HOME=/home/runner/.cargo \
    RUSTUP_HOME=/home/runner/.rustup \
    PATH=/home/runner/.cargo/bin:${PATH}
USER 1001:1001
RUN curl --proto '=https' --tlsv1.2 --fail --silent --show-error \
      https://sh.rustup.rs --output /tmp/rustup-init.sh \
    && sh /tmp/rustup-init.sh -y --no-modify-path --profile minimal --default-toolchain none \
    && rm /tmp/rustup-init.sh \
    && rustup toolchain install "${RUST_TOOLCHAIN}" --profile minimal \
      --component clippy --component rustfmt \
    && rustup default "${RUST_TOOLCHAIN}" \
    && rustc --version \
    && cargo --version

ENTRYPOINT ["/entrypoint.sh"]

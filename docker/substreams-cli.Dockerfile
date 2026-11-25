# Substreams CLI, built for Linux.
#
# WHY THIS EXISTS
# StreamingFast publishes no Windows build of the `substreams` CLI. Checked
# 2026-10-16 against the v1.25.0 release (published 2026-10-08): the assets are
# darwin_arm64, darwin_x86_64, linux_arm64, linux_x86_64 only. Zapproof runs on
# Windows 10, so the CLI runs here in a Linux container instead. Prompt Section 4
# permits Docker for packaging and clean-room work.
#
# WHAT IT IS FOR — AND WHAT IT IS NOT FOR
# This container runs a THIRD-PARTY decoder (`x402-base-pulse`), not ours. That
# is the entire point: Phase 0 gate check G0.3 requires two INDEPENDENT sources
# to agree, and two RPC endpoints read through our own decoder is a
# self-comparison, which prompt Section 6 forbids. Nothing in this image is part
# of the product.
#
# The module requires Go >= 1.26.0 — read from the repository's go.mod, not guessed.
#
# Build:
#   docker build -f docker/substreams-cli.Dockerfile -t zapproof/substreams-cli:1.25.0 .
# Check:
#   docker run --rm zapproof/substreams-cli:1.25.0 --version

FROM golang:1.27-bookworm AS build

ARG SUBSTREAMS_VERSION=v1.25.0

# Built from a checkout of the tag, NOT `go install pkg@version`.
#
# Measured 2026-10-16: `go install github.com/streamingfast/substreams/cmd/substreams@v1.25.0`
# fails with
#     "The go.mod file for the module providing named packages contains one or
#      more replace directives. It must not contain directives that would cause
#      it to be interpreted differently than if it were the main module."
# Go refuses to honour replace directives for a module that is not the main
# module. Checking out the tag and building it as the main module is the
# supported path.
RUN git clone --depth 1 --branch ${SUBSTREAMS_VERSION} \
        https://github.com/streamingfast/substreams.git /src \
    && cd /src \
    && go build -o /out/substreams ./cmd/substreams

FROM debian:bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=build /out/substreams /usr/local/bin/substreams

# Run unprivileged. The CLI writes only to its own home directory.
RUN useradd --create-home --uid 10001 substreams
USER substreams
WORKDIR /home/substreams

ENTRYPOINT ["substreams"]

# The image a --box runs in: the harnesses, the tools they shell out to, and
# nothing else. No credentials are baked in — the token arrives from the broker
# in the environment on every run.
#
# Build it with: broker box build
# node 24: what current projects ask for in "engines", and the harnesses run on
# anything recent. Change it here when a project needs otherwise.
FROM node:24-bookworm-slim

# What a coding harness actually reaches for. ripgrep is not optional: claude
# uses it for search and falls back to something much slower without it.
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl git jq less openssh-client procps ripgrep \
      python3 python3-venv unzip \
 && rm -rf /var/lib/apt/lists/*

# Pinned at build time rather than floating: an image that quietly changes
# harness version between runs is an image you cannot reproduce a bug in.
# `broker box build --claude <v> --codex <v>` overrides these.
ARG CLAUDE_VERSION=latest
ARG CODEX_VERSION=latest
# bun is a project dependency, not a harness one: repositories pin it in
# "packageManager", and without it `bun install` and every script that shells out
# to it fails inside the box while working fine outside.
ARG BUN_VERSION=latest
RUN npm install -g --no-fund --no-audit \
      "@anthropic-ai/claude-code@${CLAUDE_VERSION}" \
      "@openai/codex@${CODEX_VERSION}" \
      "bun@${BUN_VERSION}" \
 && npm cache clean --force

# A box that runs a project's test suite needs the runtime that suite expects:
# finik's orchestrator shells out to `docker compose`, binds ports and then
# connects to them on localhost. A socket shared from the host would put those
# containers — and those ports — on the host's localhost, not the box's, so the
# daemon has to live in here. Only installed; started per box (see box.py).
# compose is not packaged for bookworm, so the plugin comes from its release
# directly. Pinned: a test suite that pulls a different compose between runs is
# a test suite whose failures you cannot reproduce.
ARG COMPOSE_VERSION=v5.5.1
RUN apt-get update && apt-get install -y --no-install-recommends \
      docker.io iptables \
 && rm -rf /var/lib/apt/lists/* \
 && mkdir -p /usr/libexec/docker/cli-plugins \
 && curl -fsSL -o /usr/libexec/docker/cli-plugins/docker-compose \
      "https://github.com/docker/compose/releases/download/${COMPOSE_VERSION}/docker-compose-linux-$(dpkg --print-architecture | sed s/amd64/x86_64/ | sed s/arm64/aarch64/)" \
 && chmod 0755 /usr/libexec/docker/cli-plugins/docker-compose

# agy ships platform archives rather than an npm package, so it comes from its
# own release. gh and glab are here because work that ends in a pull or merge
# request should not have to leave the box to finish.
ARG AGY_VERSION=1.2.7
ARG GH_VERSION=2.101.0
ARG GLAB_VERSION=1.118.0
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in arm64) agy_arch=arm64; gh_arch=arm64; glab_arch=arm64 ;; \
                    amd64) agy_arch=x64;   gh_arch=amd64; glab_arch=amd64 ;; \
                    *) echo "unsupported architecture: $arch" >&2; exit 1 ;; esac; \
    curl -fsSL -o /tmp/agy.tar.gz \
      "https://github.com/google-antigravity/antigravity-cli/releases/download/${AGY_VERSION}/agy_cli_linux_${agy_arch}.tar.gz"; \
    tar -xzf /tmp/agy.tar.gz -C /tmp; \
    install -m 0755 /tmp/antigravity /usr/local/bin/agy; \
    rm -f /tmp/antigravity; \
    curl -fsSL -o /tmp/gh.tar.gz \
      "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_${gh_arch}.tar.gz"; \
    tar -xzf /tmp/gh.tar.gz -C /tmp; \
    install -m 0755 "/tmp/gh_${GH_VERSION}_linux_${gh_arch}/bin/gh" /usr/local/bin/gh; \
    curl -fsSL -o /tmp/glab.tar.gz \
      "https://gitlab.com/gitlab-org/cli/-/releases/v${GLAB_VERSION}/downloads/glab_${GLAB_VERSION}_linux_${glab_arch}.tar.gz"; \
    tar -xzf /tmp/glab.tar.gz -C /tmp; \
    install -m 0755 /tmp/bin/glab /usr/local/bin/glab; \
    rm -rf /tmp/agy.tar.gz /tmp/gh.tar.gz /tmp/glab.tar.gz /tmp/gh_* /tmp/bin; \
    agy --version; gh --version | head -1; glab --version | head -1

COPY box-entry.sh /usr/local/bin/broker-box-entry
RUN chmod 0755 /usr/local/bin/broker-box-entry

# The harnesses' own updaters must not run here: the image is the version, and a
# self-update inside a container would be thrown away with it anyway.
ENV DISABLE_AUTOUPDATER=1 \
    AGY_CLI_DISABLE_AUTO_UPDATE=1 \
    npm_config_update_notifier=false

# The box runs as the host's own uid, passed at run time, so no user is created
# here — and nothing in the image is owned by it. $HOME comes in as a tmpfs with
# the right owner (see box.py), with the harness's real directories mounted on
# top of it.
WORKDIR /

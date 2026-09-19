# The image a --box runs in: the harnesses, the tools they shell out to, and
# nothing else. No credentials are baked in — the token arrives from the broker
# in the environment on every run.
#
# Build it with: broker box build
FROM node:22-bookworm-slim

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
RUN npm install -g --no-fund --no-audit \
      "@anthropic-ai/claude-code@${CLAUDE_VERSION}" \
      "@openai/codex@${CODEX_VERSION}" \
 && npm cache clean --force

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

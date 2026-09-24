# The image a --box runs in: the harnesses, the tools they shell out to, and
# nothing else. No credentials are baked in — the token arrives from the broker
# in the environment on every run.
#
# Build it with: broker box build
# node 24 deliberately, not the newest: projects pin it in "engines" (one here
# asks for >=24 <25), and the harnesses run on anything recent. trixie for the rest —
# it carries python 3.13 rather than 3.11, and the MCP shims run on the image's
# python.
FROM node:24-trixie-slim

# What a coding harness actually reaches for. ripgrep is not optional: claude
# uses it for search and falls back to something much slower without it.
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl git jq less openssh-client procps ripgrep \
      python3 python3-venv unzip \
 && rm -rf /var/lib/apt/lists/*

# Pinned at build time rather than floating: an image that quietly changes
# harness version between runs is an image you cannot reproduce a bug in.
# `broker box build --claude <v> --codex <v>` overrides these.
# Pinned rather than floating: an image whose harness version changes between
# builds is an image you cannot reproduce a bug in. Raise them deliberately.
ARG CLAUDE_VERSION=2.1.267
ARG CODEX_VERSION=0.155.1
# bun is a project dependency, not a harness one: repositories pin it in
# "packageManager", and without it `bun install` and every script that shells out
# to it fails inside the box while working fine outside.
ARG BUN_VERSION=1.4.2
# A fourth harness, kept at the same version as the machine outside. The broker
# holds no credentials for this one and chooses no account for it — it is here
# so that `opencode --box <name>` has something to run.
ARG OPENCODE_VERSION=1.18.31
RUN npm install -g --no-fund --no-audit \
      "@anthropic-ai/claude-code@${CLAUDE_VERSION}" \
      "@openai/codex@${CODEX_VERSION}" \
      "opencode-ai@${OPENCODE_VERSION}" \
      "bun@${BUN_VERSION}" \
 && npm cache clean --force

# A box that runs a project's test suite needs the runtime that suite expects:
# a test orchestrator shells out to `docker compose`, binds ports and then
# connects to them on localhost. A socket shared from the host would put those
# containers — and those ports — on the host's localhost, not the box's, so the
# daemon has to live in here. Only installed; started per box (see box.py).
# compose is not packaged for bookworm, so the plugin comes from its release
# directly. Pinned: a test suite that pulls a different compose between runs is
# a test suite whose failures you cannot reproduce.
ARG COMPOSE_VERSION=v5.5.1
# A project's own build may ask for buildx — and a box that has docker but not
# buildx fails at the first `docker build` with a plugin that is simply absent.
ARG BUILDX_VERSION=v0.37.1
# docker.io is the daemon only on Debian 13; the client is its own package now,
# and without it the box has a daemon nothing can talk to.
RUN apt-get update && apt-get install -y --no-install-recommends \
      docker.io docker-cli iptables \
 && rm -rf /var/lib/apt/lists/* \
 && mkdir -p /usr/libexec/docker/cli-plugins \
 && curl -fsSL -o /usr/libexec/docker/cli-plugins/docker-compose \
      "https://github.com/docker/compose/releases/download/${COMPOSE_VERSION}/docker-compose-linux-$(dpkg --print-architecture | sed s/amd64/x86_64/ | sed s/arm64/aarch64/)" \
 && chmod 0755 /usr/libexec/docker/cli-plugins/docker-compose \
 && curl -fsSL -o /usr/libexec/docker/cli-plugins/docker-buildx \
      "https://github.com/docker/buildx/releases/download/${BUILDX_VERSION}/buildx-${BUILDX_VERSION}.linux-$(dpkg --print-architecture)" \
 && chmod 0755 /usr/libexec/docker/cli-plugins/docker-buildx

# agy ships platform archives rather than an npm package, so it comes from its
# own release. gh and glab are here because work that ends in a pull or merge
# request should not have to leave the box to finish.
# rtk trims command output before it reaches the model. It hooks every tool use,
# so bridging it to the host would cost a round trip per command — it belongs in
# the image.
ARG RTK_VERSION=0.49.0
ARG AGY_VERSION=1.2.10
ARG GH_VERSION=2.101.0
ARG GLAB_VERSION=1.118.0
ARG GITLEAKS_VERSION=8.30.1
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in arm64) agy_arch=arm64; gh_arch=arm64; glab_arch=arm64; leaks_arch=arm64 ;; \
                    amd64) agy_arch=x64;   gh_arch=amd64; glab_arch=amd64; leaks_arch=x64 ;; \
                    *) echo "unsupported architecture: $arch" >&2; exit 1 ;; esac; \
    case "$arch" in arm64) rtk_target=aarch64-unknown-linux-gnu ;; \
                    amd64) rtk_target=x86_64-unknown-linux-musl ;; esac; \
    curl -fsSL -o /tmp/rtk.tar.gz \
      "https://github.com/rtk-ai/rtk/releases/download/v${RTK_VERSION}/rtk-${rtk_target}.tar.gz"; \
    tar -xzf /tmp/rtk.tar.gz -C /tmp; \
    install -m 0755 "$(find /tmp -maxdepth 2 -name rtk -type f | head -1)" /usr/local/bin/rtk; \
    rm -rf /tmp/rtk.tar.gz; \
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
    curl -fsSL -o /tmp/gitleaks.tar.gz \
      "https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/gitleaks_${GITLEAKS_VERSION}_linux_${leaks_arch}.tar.gz"; \
    tar -xzf /tmp/gitleaks.tar.gz -C /tmp gitleaks; \
    install -m 0755 /tmp/gitleaks /usr/local/bin/gitleaks; \
    rm -rf /tmp/agy.tar.gz /tmp/gh.tar.gz /tmp/glab.tar.gz /tmp/gitleaks.tar.gz /tmp/gitleaks \
           /tmp/gh_* /tmp/bin; \
    agy --version; gh --version | head -1; glab --version | head -1; rtk --version; \
    gitleaks version

# A personal machine keeps commands in ~/bin and ~/.local/bin, and things
# mounted from there must be reachable by name. Set in the shell profile too,
# not only by the entry point: a login shell re-reads /etc/profile and would
# otherwise drop them again.
RUN printf '%s\n' 'case ":$PATH:" in' \
      '  *":$HOME/bin:"*) ;;' \
      '  *) PATH="$HOME/bin:$HOME/.local/bin:$PATH"; export PATH ;;' \
      'esac' > /etc/profile.d/broker-box-path.sh \
 && chmod 0644 /etc/profile.d/broker-box-path.sh

COPY box-entry.sh /usr/local/bin/broker-box-entry
RUN chmod 0755 /usr/local/bin/broker-box-entry

# The harnesses' own updaters must not run here: the image is the version, and a
# self-update inside a container would be thrown away with it anyway.
ENV DISABLE_AUTOUPDATER=1 \
    AGY_CLI_DISABLE_AUTO_UPDATE=1 \
    npm_config_update_notifier=false

# git refuses to touch a repository it believes belongs to someone else, and in
# here it always believes so: Docker Desktop's file sharing presents a mounted
# directory as root while the box runs as your own uid. run.py already passes
# safe.directory through GIT_CONFIG_* environment variables, but a program that
# rebuilds its environment for a child — dropping every GIT_* on the way, which is
# a reasonable thing to do — strips that protection off git without meaning to,
# and the failure surfaces far from its cause as "detected dubious ownership".
# The system config is read whatever the environment says, so this is the copy
# that survives. It grants nothing: every path in a box was named by the box and
# mounted from this machine.
RUN git config --system --add safe.directory '*' \
 && git config --system --get-all safe.directory

# What git may believe about a file without opening it. The index remembers
# size, mtime, inode, device and ctime, and takes a mismatch as "this file
# changed". The file sharing under a box renumbers inodes whenever a directory
# is remounted, so after a restart git reports local changes to files nobody
# touched — `cherry-pick` and `rebase` refuse to run, while `status` in the same
# worktree says the tree is clean. It reads as a corrupt checkout and is not
# one. "minimal" leaves git comparing size and mtime, which cross the boundary
# intact, and the content itself settles anything they cannot.
RUN git config --system core.checkStat minimal \
 && git config --system --get core.checkStat

# The box runs as the host's own uid, passed at run time, so no user is created
# here — and nothing in the image is owned by it. $HOME comes in as a tmpfs with
# the right owner (see box.py), with the harness's real directories mounted on
# top of it.
WORKDIR /

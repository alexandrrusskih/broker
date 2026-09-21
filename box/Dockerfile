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
RUN npm install -g --no-fund --no-audit \
      "@anthropic-ai/claude-code@${CLAUDE_VERSION}" \
      "@openai/codex@${CODEX_VERSION}" \
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
# docker.io is the daemon only on Debian 13; the client is its own package now,
# and without it the box has a daemon nothing can talk to.
# cloud-sql-proxy is psql's other half here: the gate reaches the cloud database
# THROUGH it, with IAM impersonation, so without the proxy the gate dies one step
# earlier than it would without psql.
# postgresql-client: a deploy gate checks migrations against the cloud database
# with psql before it lets a release through. Without it the gate cannot run at
# all, and the alternative — a wrapper that starts a postgres container for
# every psql call — pays a container start to borrow one binary.
RUN apt-get update && apt-get install -y --no-install-recommends \
      docker.io docker-cli iptables postgresql-client \
 && rm -rf /var/lib/apt/lists/* \
 && mkdir -p /usr/libexec/docker/cli-plugins \
 && curl -fsSL -o /usr/libexec/docker/cli-plugins/docker-compose \
      "https://github.com/docker/compose/releases/download/${COMPOSE_VERSION}/docker-compose-linux-$(dpkg --print-architecture | sed s/amd64/x86_64/ | sed s/arm64/aarch64/)" \
 && chmod 0755 /usr/libexec/docker/cli-plugins/docker-compose

# agy ships platform archives rather than an npm package, so it comes from its
# own release. gh and glab are here because work that ends in a pull or merge
# request should not have to leave the box to finish.
# rtk trims command output before it reaches the model. It hooks every tool use,
# so bridging it to the host would cost a round trip per command — it belongs in
# the image.
ARG RTK_VERSION=0.49.0
ARG AGY_VERSION=1.2.7
ARG GH_VERSION=2.101.0
ARG GLAB_VERSION=1.118.0
ARG GITLEAKS_VERSION=8.30.1
ARG CLOUD_SQL_PROXY_VERSION=2.25.4
ARG TOFU_VERSION=1.12.6
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
    # Pinned level with the host's own tofu, not at the newest: OpenTofu
    # records the version that last wrote a state file, and once a newer one
    # has written it the older refuses to read it. A box reaching for the
    # latest would put the machine's own infrastructure beyond the machine.
    curl -fsSL -o /tmp/tofu.tar.gz \
      "https://github.com/opentofu/opentofu/releases/download/v${TOFU_VERSION}/tofu_${TOFU_VERSION}_linux_${arch}.tar.gz"; \
    tar -xzf /tmp/tofu.tar.gz -C /tmp tofu; \
    install -m 0755 /tmp/tofu /usr/local/bin/tofu; \
    curl -fsSL -o /tmp/cloud-sql-proxy \
      "https://storage.googleapis.com/cloud-sql-connectors/cloud-sql-proxy/v${CLOUD_SQL_PROXY_VERSION}/cloud-sql-proxy.linux.${arch}"; \
    install -m 0755 /tmp/cloud-sql-proxy /usr/local/bin/cloud-sql-proxy; \
    rm -rf /tmp/agy.tar.gz /tmp/gh.tar.gz /tmp/glab.tar.gz /tmp/gitleaks.tar.gz /tmp/gitleaks \
           /tmp/cloud-sql-proxy /tmp/tofu.tar.gz /tmp/tofu /tmp/gh_* /tmp/bin; \
    agy --version; gh --version | head -1; glab --version | head -1; rtk --version; \
    gitleaks version; cloud-sql-proxy --version; tofu --version | head -1

# A personal machine keeps commands in ~/bin and ~/.local/bin, and things
# mounted from there must be reachable by name. Set in the shell profile too,
# not only by the entry point: a login shell re-reads /etc/profile and would
# otherwise drop them again.
RUN printf '%s\n' 'case ":$PATH:" in' \
      '  *":$HOME/bin:"*) ;;' \
      '  *) PATH="$HOME/bin:$HOME/.local/bin:$PATH"; export PATH ;;' \
      'esac' > /etc/profile.d/broker-box-path.sh \
 && chmod 0644 /etc/profile.d/broker-box-path.sh

# Cloud SDK: a project's deploy script shells out to `gcloud`, and a box without
# it cannot ship anything however ready the fix is. Installed from Google's apt
# repository rather than the tarball so it updates with the image.
RUN apt-get update && apt-get install -y --no-install-recommends gnupg \
 && curl -fsSL https://packages.cloud.google.com/apt/doc/apt-key.gpg \
      | gpg --dearmor -o /usr/share/keyrings/cloud.google.gpg \
 && echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main" \
      > /etc/apt/sources.list.d/google-cloud-sdk.list \
 && apt-get update && apt-get install -y --no-install-recommends google-cloud-cli \
 && rm -rf /var/lib/apt/lists/* \
 && gcloud --version | head -1

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

# The box runs as the host's own uid, passed at run time, so no user is created
# here — and nothing in the image is owned by it. $HOME comes in as a tmpfs with
# the right owner (see box.py), with the harness's real directories mounted on
# top of it.
WORKDIR /

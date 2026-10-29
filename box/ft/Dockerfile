# The ft box: a bot and a render pipeline, with the game on the host.
#
# The game, the server and the GPU stay outside. Docker on macOS has no GPU and
# cannot drive the host screen, so a box that tried to play would render
# Minecraft through llvmpipe at a framerate nobody wants to watch. Instead the
# box writes actions into a file queue on the shared mount, and a controller the
# owner starts in a Terminal window carries them out.
#
# So this image adds exactly two things to the base: ffmpeg, which the base has
# no reason to carry, and node 26 in place of the pinned 24.
FROM broker-box

# ffmpeg with libx264, for the offline Replay render. Debian's build carries it.
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/* \
 && ffmpeg -hide_banner -version | head -1 \
 && ffmpeg -hide_banner -encoders 2>/dev/null | grep -q libx264

# node 26 over the base's 24. The base pins 24 on purpose, because projects name
# it in "engines" — this box overrides that on the owner's word.
#
# The tarball is extracted over /usr/local, which is what the official node
# image does itself. It replaces bin/node, bin/npm, bin/npx and
# lib/node_modules/npm, and leaves every other global package alone: claude,
# codex, agy and opencode keep their own directories and run on the new node.
#
# The checksum is not written down here. It is read from the signed SHASUMS256
# of that release and checked against the file, so the pin is the VERSION and
# the integrity check still happens.
#
# The .tar.gz, not the .tar.xz the node docs reach for first: this base has no
# xz. Measured — `tar -xJf` died with exit 2 on a build that had already
# downloaded the file and verified its checksum. gzip is built into GNU tar, so
# this costs no package and one extra megabyte of download.
ARG NODE_VERSION=v26.13.0
RUN set -eu; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in arm64) node_arch=arm64 ;; amd64) node_arch=x64 ;; \
      *) echo "no node tarball for $arch" >&2; exit 1 ;; esac; \
    file="node-${NODE_VERSION}-linux-${node_arch}.tar.gz"; \
    cd /tmp; \
    curl -fsSLO "https://nodejs.org/dist/${NODE_VERSION}/${file}"; \
    curl -fsSLO "https://nodejs.org/dist/${NODE_VERSION}/SHASUMS256.txt"; \
    grep " ${file}\$" SHASUMS256.txt | sha256sum -c -; \
    tar -xzf "$file" -C /usr/local --strip-components=1 \
        --exclude CHANGELOG.md --exclude LICENSE --exclude README.md; \
    rm -f "$file" SHASUMS256.txt; \
    node --version; npm --version

# Prove the box can still do its job after the node swap. A silent break here
# costs the owner a start, a failure inside the box and a guess about why.
RUN node --version | grep -q '^v26\.' \
 && bun --version \
 && python3 --version \
 && git --version \
 && claude --version

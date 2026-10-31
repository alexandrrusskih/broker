# The aic box: Accounting Intake, on the shared base plus medulla.
#
# FROM broker-box, not broker-box-finik. The finik image is finik's own: it is
# built from a Dockerfile in the finik repository, which is not on this
# machine, so a box standing on it cannot be rebuilt here at all — which is
# how this box came to be missing medulla in the first place. Borrowing
# another project's image also borrows its schedule and its contents.
FROM broker-box

# medulla, which the base installs too — and this is belt and braces on
# purpose. The base IMAGE on this machine was built on 12 October and medulla
# entered box/Dockerfile on 16 October (24e6c22), so a base that has not been
# rebuilt since does not carry it. Measured: the built image's history has no
# medulla layer. Installing it here means this box is right whether the base
# has been rebuilt or not, and the step is a no-op in cost once it has: the
# same version, found the same way.
#
# The same recipe as the base, down to the venv path, because
# `medulla --version` is what lib/box-pins.js reads to keep the pin honest, and
# a box that got medulla some other way would answer differently from every
# other box. Debian is externally-managed, so there is no system pip — the venv
# is not tidiness, it is the only way in. The commit is found by the version in
# pyproject.toml rather than a tag, because the repository does not tag every
# release.
ARG MEDULLA_VERSION=4.98.1
RUN set -eu; \
    if medulla --version 2>/dev/null | grep -qF "medulla ${MEDULLA_VERSION}"; then \
        echo "medulla ${MEDULLA_VERSION} is already here"; exit 0; \
    fi; \
    git clone -q https://github.com/skopanev/medulla.git /tmp/medulla; \
    commit="$(git -C /tmp/medulla log --format=%H -G "^version = \"${MEDULLA_VERSION}\"\$" -- pyproject.toml | tail -1)"; \
    test -n "$commit"; \
    git -C /tmp/medulla checkout -q "$commit"; \
    python3 -m venv /opt/medulla; \
    /opt/medulla/bin/pip install -q --no-cache-dir /tmp/medulla; \
    ln -sf /opt/medulla/bin/medulla /usr/local/bin/medulla; \
    rm -rf /tmp/medulla

# gcloud, which is the one thing this box kept from the finik image it used to
# stand on. psql and tofu came from there too and are deliberately not here:
# the owner named gcloud and nothing else.
#
# Google's own apt repository rather than the standalone tarball the rest of
# this image prefers. The tarball carries its own bundled python and lands near
# a quarter of a gigabyte, and there is no checksum published beside it to pin
# against — so the pinning argument that justifies a tarball elsewhere does not
# apply, and apt at least tracks the architecture by itself. The keyring goes
# to /usr/share/keyrings and the source names it with signed-by, because an
# apt source without one trusts the repository for everything.
RUN set -eu; \
    apt-get update; \
    apt-get install -y --no-install-recommends apt-transport-https gnupg; \
    curl -fsSL https://packages.cloud.google.com/apt/doc/apt-key.gpg \
      | gpg --dearmor -o /usr/share/keyrings/cloud.google.gpg; \
    echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main" \
      > /etc/apt/sources.list.d/google-cloud-sdk.list; \
    apt-get update; \
    apt-get install -y --no-install-recommends google-cloud-cli; \
    apt-get purge -y gnupg; \
    apt-get autoremove -y; \
    rm -rf /var/lib/apt/lists/*

# Prove the box can do its job before the owner finds out the hard way.
RUN medulla --version | grep -F "medulla ${MEDULLA_VERSION}" \
 && node --version \
 && python3 --version \
 && git --version \
 && gcloud --version | head -1

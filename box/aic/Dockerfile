# The aic box: Accounting Intake, on the shared base plus medulla.
#
# FROM broker-box, not broker-box-finik. The finik image is finik's own: it is
# built from a Dockerfile in the finik repository, which is not on this
# machine, so a box standing on it cannot be rebuilt here at all — which is
# how this box came to be missing medulla in the first place. Borrowing
# another project's image also borrows its schedule and its contents.
FROM broker-box

# medulla, from the tip of its default branch. NOT pinned, on the owner's word:
# this box wants whatever the current release is, and a version written down
# here is a version that goes stale between rebuilds.
#
# What that costs, said out loud rather than discovered later: the image stops
# being reproducible — two builds a week apart give two different medullas — and
# lib/box-pins.js, which reads `medulla --version` to keep the base's pin honest,
# has nothing to compare this box against. The base keeps its pin; this box does
# not.
#
# Installed here even though the base installs it too. The base IMAGE on this
# machine was built on 12 October and medulla entered box/Dockerfile on 16
# October (24e6c22), so a base that has not been rebuilt does not carry it at
# all. Installing unconditionally also means the tip wins over whatever version
# the base happens to hold, which is the point of not pinning.
#
# Debian is externally-managed, so there is no system pip — the venv is not
# tidiness, it is the only way in. --depth 1 is enough now that no commit has
# to be found by its version string.
RUN set -eu; \
    git clone -q --depth 1 https://github.com/skopanev/medulla.git /tmp/medulla; \
    python3 -m venv /opt/medulla; \
    /opt/medulla/bin/pip install -q --no-cache-dir /tmp/medulla; \
    ln -sf /opt/medulla/bin/medulla /usr/local/bin/medulla; \
    rm -rf /tmp/medulla; \
    medulla --version

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
RUN medulla --version \
 && node --version \
 && python3 --version \
 && git --version \
 && gcloud --version | head -1

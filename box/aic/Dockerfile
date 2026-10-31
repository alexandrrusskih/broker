# The aic box: Accounting Intake, on the finik image plus medulla.
#
# Why this file exists at all. medulla entered box/Dockerfile on 16 October
# (24e6c22) and this machine's broker-box image was built on 12 October, so the
# base here predates the step that installs it. broker-box-finik is built on
# top of that base — 15 of 15 layers match — so it inherited the gap, and
# `command -v medulla` inside the box is empty. Measured, not guessed: the
# image's own history carries no medulla layer.
#
# Rebuilding the shared base would fix it for every box, and should happen. But
# broker-box-finik would then have to be rebuilt on top, and ITS Dockerfile
# lives in the finik repository, which is not on this machine. So this box takes
# the one thing it needs and leaves the others alone.
FROM broker-box-finik

# The same recipe as the base, deliberately, down to the venv path: a box that
# gets medulla some other way would answer differently from every other box,
# and `medulla --version` is what box-pins.js reads to keep the pin honest.
#
# Debian is externally-managed, so there is no system pip — the venv is not
# tidiness, it is the only way in. The commit is found by the version in
# pyproject.toml rather than a tag, because the repository does not tag every
# release.
ARG MEDULLA_VERSION=4.98.1
RUN set -eu; \
    git clone -q https://github.com/skopanev/medulla.git /tmp/medulla; \
    commit="$(git -C /tmp/medulla log --format=%H -G "^version = \"${MEDULLA_VERSION}\"\$" -- pyproject.toml | tail -1)"; \
    test -n "$commit"; \
    git -C /tmp/medulla checkout -q "$commit"; \
    python3 -m venv /opt/medulla; \
    /opt/medulla/bin/pip install -q --no-cache-dir /tmp/medulla; \
    ln -s /opt/medulla/bin/medulla /usr/local/bin/medulla; \
    rm -rf /tmp/medulla; \
    medulla --version | grep -F "medulla ${MEDULLA_VERSION}"

# Prove the box can still do its job. A silent break here costs the owner a
# start, a failure inside the box and a guess about why.
RUN medulla --version \
 && node --version \
 && python3 --version \
 && git --version \
 && gcloud --version | head -1

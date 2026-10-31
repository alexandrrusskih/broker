# The m box: the owner's general box over all projects, on the shared base.
#
# FROM broker-box, not broker-box-finik. That image is finik's own and is built
# from a Dockerfile in the finik repository, which is not on this machine — so a
# box standing on it cannot be rebuilt here at all. That is not a theory: it is
# how the aic box came to be missing medulla for two weeks, because the base
# moved on and the borrowed image could not follow.
FROM broker-box

# gcloud, which is what this box actually used from that image: its profile
# mounts ~/.config/gcloud. psql and tofu came from there too and are NOT here —
# the owner named gcloud. Say so if something in /Volumes/hdd/Projects needs
# them and they get added properly rather than borrowed.
#
# Google's own apt repository rather than the standalone tarball the rest of
# this image prefers. That tarball bundles its own python, runs near a quarter
# of a gigabyte, and publishes no checksum beside it — so the pinning argument
# that justifies a tarball elsewhere does not apply. The keyring is named with
# signed-by, because an apt source without one is trusted for every package.
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
RUN gcloud --version | head -1 \
 && medulla --version \
 && node --version \
 && python3 --version \
 && git --version

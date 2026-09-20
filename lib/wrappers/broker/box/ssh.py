"""The ssh material a box is given, which is only what it names.

Mounting ~/.ssh hands a box every key on the machine when what it usually needs
is one host. Naming keys keeps the rest out of reach entirely: they are not
mounted, so nothing inside can use them however it is asked to.
"""

import os
import subprocess

from .. import config
from ..out import die
from .paths import expand

def _write_in_place(path, text):
    """Rewrite a file without replacing it.

    The usual write-beside-and-rename would give the file a new inode, and a box
    already running has the old one bind-mounted: start a second box and the
    first one finds its own ssh config gone — "No such file or directory" on a
    path that is right there. Truncating in place keeps the inode, so every
    container mounting it keeps reading the same file.
    """
    with open(path, "w") as fh:
        fh.write(text)
    os.chmod(path, 0o600)


def _ssh_config(name, profile):
    """A private ~/.ssh for this box: only the keys it was told about.

    Mounting the real ~/.ssh hands a box every key on the machine — GitHub, the
    cloud VMs, whatever else is in there — when what it usually needs is one
    host. Naming keys in the box keeps the rest out of reach entirely: they are
    not mounted, so nothing inside can use them however it is asked to.

    The generated config pins each host to its key with IdentitiesOnly, because
    the tools that need this call plain `ssh <host>` with no -i of their own.
    """
    spec = profile.get("ssh")
    if not isinstance(spec, dict):
        return None, [], None
    keys = [expand(k) for k in (spec.get("keys") or [])]

    # A host is either just its key, or a small table when the name you use is
    # not the address: your own ~/.ssh/config does not come along, so an alias
    # that resolves on the host resolves to nothing in here.
    hosts = {}
    for host, entry in (spec.get("hosts") or {}).items():
        if isinstance(entry, dict):
            settings = {k: v for k, v in entry.items() if k != "key"}
            hosts[host] = (expand(entry.get("key") or ""), settings)
        else:
            hosts[host] = (expand(entry), {})
    keys += [k for k, _ in hosts.values() if k and k not in keys]
    missing = [k for k in keys if k and not os.path.exists(k)]
    if missing:
        die("the '%s' box names ssh keys that do not exist: %s" % (name, ", ".join(missing)))
    if not keys:
        return None, [], None

    # ssh reads these in the order it finds them, and the spelling is its own:
    # HostName, User, Port, ProxyJump. Anything else the box names is passed
    # through as written rather than guessed at.
    ORDER = ("hostname", "user", "port", "proxyjump")
    SPELLING = {"hostname": "HostName", "user": "User", "port": "Port", "proxyjump": "ProxyJump"}
    lines = ["# Written by the broker for the '%s' box." % name]
    for host, (key, settings) in sorted(hosts.items()):
        lines.append("Host %s" % host)
        for field in ORDER:
            if settings.get(field) is not None:
                lines.append("  %s %s" % (SPELLING[field], settings[field]))
        for field, value in sorted(settings.items()):
            if field not in ORDER and value is not None:
                lines.append("  %s %s" % (field, value))
        if key:
            lines += ["  IdentityFile %s" % key, "  IdentitiesOnly yes"]
        lines.append("")
    # The hosts it will talk to, and only those. Without a known_hosts the box
    # cannot verify anything and cannot write what it learns either — the
    # directory it would write into belongs to the container. Copying the whole
    # host file instead would tell the box about every machine you have ever
    # reached, which is not access but is not its business either.
    known = []
    for host, (_, settings) in sorted(hosts.items()):
        # Look up what ssh will actually connect to, not the name you call it by.
        lookup = settings.get("hostname") or host
        try:
            found = subprocess.run(["ssh-keygen", "-F", str(lookup)], capture_output=True, text=True, timeout=15)
            known += [l for l in found.stdout.splitlines() if l and not l.startswith("#")]
        except (OSError, subprocess.SubprocessError):
            pass

    directory = os.path.join(config.CONFIG_DIR, "box")
    os.makedirs(directory, mode=0o700, exist_ok=True)
    path = os.path.join(directory, "ssh-config-%s" % name.replace("/", "_"))
    _write_in_place(path, "\n".join(lines) + "\n")

    hosts_file = None
    if known:
        hosts_file = os.path.join(directory, "ssh-known-hosts-%s" % name.replace("/", "_"))
        _write_in_place(hosts_file, "\n".join(known) + "\n")
    return path, keys, hosts_file



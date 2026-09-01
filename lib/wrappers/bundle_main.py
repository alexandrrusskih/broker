"""Entry point for the zipapp bundle — the whole engine travels inside one file.

Used where a launcher plus a package directory is impractical: medulla mounts
private tooling into its container file by file, so a package would need a mount
per module, and every new module would silently go missing until someone noticed.
"""

import sys

from hltm.cli import main

if __name__ == "__main__":
    try:
        sys.exit(main("__PROVIDER__"))
    except KeyboardInterrupt:
        sys.exit(130)

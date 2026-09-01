"""Rendering the account table."""

from . import accounts, config


def human(seconds):
    if seconds is None:
        return "—"
    seconds = int(seconds)
    if seconds >= 86400:
        return "%dd %dh" % (seconds // 86400, (seconds % 86400) // 3600)
    if seconds >= 3600:
        return "%dh %02dm" % (seconds // 3600, (seconds % 3600) // 60)
    return "%dm" % max(1, seconds // 60)


def window_label(seconds):
    # A provider may hand back something that is not a duration; a status table
    # must never be the thing that crashes.
    if not isinstance(seconds, (int, float)):
        return "—" if not seconds else str(seconds)
    if not seconds:
        return "—"
    if seconds % 86400 == 0:
        return "%dd" % (seconds // 86400)
    return "%dh" % (seconds // 3600)


def status_of(row):
    if row["error"]:
        return row["error"]
    return "limit reached" if row["blocked"] else "ok"


def render(cfg, provider, rows):
    picked = accounts.would_pick(cfg, provider, rows)
    home = config.home_account(cfg, provider.NAME)

    head = ("", "ACCOUNT", "EMAIL", "PLAN", "USED", "WINDOW", "RESETS IN", "STATUS")
    table = [head]
    for row in rows:
        table.append(
            (
                "*" if row["account"] == picked else "",
                row["account"] + (" (yours)" if row["account"] == home else ""),
                row["email"],
                row["plan"],
                "—" if row["used"] is None else "%d%%" % row["used"],
                window_label(row["window"]),
                human(row["resets_in"]),
                status_of(row),
            )
        )

    widths = [max(len(r[c]) for r in table) for c in range(len(head))]
    for row in table:
        print("  ".join(cell.ljust(widths[c]) for c, cell in enumerate(row)).rstrip())

    if home:
        print(
            "\n* = what a bare `%s` would use: yours while it has %d%%+ left, "
            "else whoever has room." % (provider.CMD, config.min_headroom(cfg))
        )
    else:
        print(
            "\n* = what a bare `%s` would use.  Set yours with `broker set-default <name>`."
            % provider.CMD
        )
    print("Pin one run with `%s account <name>`." % provider.CMD)

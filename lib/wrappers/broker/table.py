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


def used_label(row):
    """What to print in the USED column.

    A provider that only reports limits past a warning threshold says nothing at
    all while an account is healthy. Printing a dash there reads as "no data";
    it actually means "under the line".
    """
    if row.get("used") is not None:
        return "%d%%" % row["used"]
    return "<75%" if row.get("below_threshold") else "—"


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


def credits_label(row):
    """What to print in the CREDITS column.

    A balance the plan will not spend is marked rather than hidden: it is real
    money on the account and worth seeing, but reading it as a way past a spent
    window is exactly the mistake. `*` is taken already — it marks the account a
    bare run would pick — so the flag is `!`.
    """
    balance = row.get("credits_balance")
    if not balance:
        return "—"
    return "%d" % balance if row.get("credits_spendable") else "%d!" % balance


def resets_label(row):
    """What to print in the RESETS column.

    Two different numbers: how many banked resets the account holds, and how many
    it could redeem right now — which is none until it is actually out of room.
    A parenthesised count is "banked, not yet usable", so a full column of them
    does not read as a way out of a machine-wide limit.
    """
    held = row.get("resets_held")
    if not held:
        return "—"
    applicable = row.get("resets_applicable") or 0
    return "%d" % applicable if applicable else "(%d)" % held


def status_of(row):
    if row["error"]:
        return row["error"]
    return "limit reached" if row["blocked"] else "ok"


def render(cfg, provider, rows):
    picked = accounts.would_pick(cfg, provider, rows)
    home = config.home_account(cfg, provider.NAME)

    # Only providers that report them get the columns: a table of dashes says
    # "this account has none" where the truth is "this harness never says".
    extra = any(
        row.get("credits_balance") is not None or row.get("resets_held") is not None
        for row in rows
    )
    head = ["", "ACCOUNT", "EMAIL", "PLAN", "USED", "WINDOW", "RESETS IN"]
    if extra:
        head += ["CREDITS", "RESETS"]
    head += ["STATUS"]
    table = [tuple(head)]
    for row in rows:
        cells = [
            "*" if row["account"] == picked else "",
            row["account"] + (" (yours)" if row["account"] == home else ""),
            row["email"],
            row["plan"],
            used_label(row),
            window_label(row["window"]),
            human(row["resets_in"]),
        ]
        if extra:
            cells += [credits_label(row), resets_label(row)]
        cells.append(status_of(row))
        table.append(tuple(cells))

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

    # Said only when the table actually shows one, so the ordinary case stays
    # three lines rather than five.
    if extra:
        if any(credits_label(row).endswith("!") for row in rows):
            print(
                "! = a balance this plan will not spend on the model in use — money on "
                "the account, not room to run."
            )
        if any(resets_label(row).startswith("(") for row in rows):
            print(
                "(n) = banked rate-limit resets, not redeemable yet: one applies only "
                "once that account is out of room."
            )
        print("Neither is used to pick an account — `%s` still goes by the window alone."
              % provider.CMD)

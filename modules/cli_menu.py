"""The terminal menus, from the bot's (modules/registry.py): the main one (main.py) and a project's
screens on its workers (functions/projects.py), in the bot's order and words."""
from rich.markup import escape

from modules.console import console
from modules.registry import NOT_MENU_ITEMS, RISK_NOTE, RISKY, cli_menu


def menu_entries(functions, project: bool = False) -> list:
    """[(screen, instance, title, hint, risky, group)] in the bot's order and words: the main menu's,
    or (`project`) a project's screens'; in the main menu, a function the registry doesn't know goes to
    «Прочее» under its docstring (one run from another item only, NOT_MENU_ITEMS, nowhere)."""
    by_class = {type(instance).__name__: (instance, doc) for instance, doc in functions}
    entries = []
    for section, items in cli_menu(project):
        for item in items:
            if item.classname in by_class:
                instance, _ = by_class.pop(item.classname)
                entries.append((section, instance, item.title, item.hint, item.risk == RISKY, item.group))
    if project:
        return entries
    known = {item.classname for _, items in cli_menu(True) for item in items}  # a project's
    for classname, (instance, doc) in by_class.items():
        if classname not in NOT_MENU_ITEMS and classname not in known:
            entries.append(("Прочее", instance, doc or type(instance).__name__, "", False, ""))
    return entries


def print_menu(entries) -> None:
    # a section whose every item is risky: ⚠️ once, on its title, as in the bot (registry.risk_marker)
    all_risky = {}
    for entry_section, _, _, _, risky, _ in entries:
        all_risky[entry_section] = all_risky.get(entry_section, True) and risky
    section = group = None
    for number, (entry_section, _, title, hint, risky, entry_group) in enumerate(entries, 1):
        if entry_section != section:
            section, group = entry_section, ""
            console.print(f"\n[bold magenta]{escape(section)}[/]" + (" ⚠️" if all_risky[section] else ""))
        if entry_group != group:  # a subheading, as in the bot's section message
            group = entry_group
            console.print(f"  [italic]{escape(group)}[/]")
        marker = "⚠️ " if risky and not all_risky[entry_section] else ""
        line = f"  \\[{number}] {marker}[bold white]{escape(title)}[/]"
        console.print(line + (f" — {escape(hint)}" if hint else ""))
    if any(risky for _, _, _, _, risky, _ in entries):
        console.print(f"\n[italic]{escape(RISK_NOTE)}[/]")

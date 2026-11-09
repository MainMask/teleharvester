import functools
import importlib.util
import inspect
import os



@functools.cache
def discover(directory: str) -> dict[str, type]:
    """{class name: class} of every *Func in the directory's modules, loaded once: each project's
    functions are these classes on its own workers (the bot's workspaces).

    Discovery convention: a class is found iff its name ends in "Func". The bot keys these classes by
    name in modules/registry.py (validated at bot startup). A helper base like functions/broadcast.py's
    `Broadcast` is deliberately named without the suffix so it is skipped here."""
    found = {}
    for file in os.listdir(directory):
        if file.endswith(".py"):
            spec = importlib.util.spec_from_file_location(file[:-3], os.path.join(directory, file))
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            found.update((name, cls) for name, cls in inspect.getmembers(module, inspect.isclass)
                         if name.endswith("Func"))
    return found

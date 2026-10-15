import asyncio
import functools
import importlib.util
import inspect
import os

from .sessions_storage import SessionsStorage
from ..settings import Settings


@functools.cache
def discover(directory: str) -> dict[str, type]:
    """{class name: class} of every *Func in the directory's modules, loaded once: each project's
    functions are these classes on its own workers (the bot's workspaces, the CLI's project card).

    Discovery convention: a class is a menu entry iff its name ends in "Func", and its docstring is
    the menu label. The bot keys the same classes by name in modules/registry.py (validated at bot
    startup). A helper base like functions/broadcast.py's `Broadcast` is deliberately named without
    the suffix so it is skipped here."""
    found = {}
    for file in os.listdir(directory):
        if file.endswith(".py"):
            spec = importlib.util.spec_from_file_location(file[:-3], os.path.join(directory, file))
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            found.update((name, cls) for name, cls in inspect.getmembers(module, inspect.isclass)
                         if name.endswith("Func"))
    return found


class FunctionsStorage:
    def __init__(
        self,
        directory: str,
        sessions_storage: SessionsStorage,
        settings: Settings
    ):
        self.storage = sessions_storage
        self.settings = settings

        # keep one loop for the whole run: sessions are connected on it at startup,
        # and a scraper function's asyncio.run() must not leave it detached for the
        # next telethon function (which would then rebuild clients on a foreign loop)
        try:
            self.loop = asyncio.get_running_loop()
        except RuntimeError:  # no running loop (CLI path)
            # reuse the sessions' loop: Telethon refuses calls from another one
            self.loop = sessions_storage.loop or asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)

        # (the *Func instance, its docstring); item[1] is the menu label: a *Func without one would
        # make None.lower() crash startup, so treat a missing docstring as empty
        self.functions: list[tuple[object, str | None]] = sorted(
            ((cls(self.storage, self.settings), cls.__doc__) for cls in discover(directory).values()),
            key=lambda item: (item[1] or "").lower())

    def run_instance(self, function_instance):
        """Run a menu item: a new instance of its class, so no run inherits another's state."""
        self.run(type(function_instance)(self.storage, self.settings).execute())

    def run(self, function):
        """A function's result: awaited on the sessions' loop if it is a coroutine."""
        if inspect.isawaitable(function):
            # restore our loop in case a scraper function's asyncio.run() cleared it
            asyncio.set_event_loop(self.loop)
            task = self.loop.create_task(function)
            try:
                self.loop.run_until_complete(task)
            except KeyboardInterrupt:
                # Ctrl-C leaves the task pending on our shared loop, where it would resume
                # inside the next menu function (e.g. trigger listeners keep broadcasting)
                task.cancel()
                self.loop.run_until_complete(asyncio.gather(task, return_exceptions=True))
                if self.storage.initialize:  # cancelled run_until_disconnected() disconnects
                    self.loop.run_until_complete(asyncio.gather(*[
                        s.connect() for s in self.storage.sessions if not s.is_connected()
                    ], return_exceptions=True))
                raise

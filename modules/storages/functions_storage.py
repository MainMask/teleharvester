import asyncio
import importlib.util
import inspect
import os

from typing import List, Callable, Awaitable, Union

from .sessions_storage import SessionsStorage
from ..settings import Settings
from ..console import console

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

        self.functions: List[Union[Callable, Awaitable]] = []

        for file in os.listdir(directory):
            if file.endswith(".py"):
                self.load_function(
                    file[:-3], os.path.join(directory, file)
                )

        # item[1] is the class docstring (the menu label); a *Func without one would
        # make None.lower() crash startup, so treat a missing docstring as empty.
        self.functions.sort(key=lambda item: (item[1] or "").lower())

    def load_function(self, name: str, path: str):
        spec = importlib.util.spec_from_file_location(name, path)
        function = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(function)

        self.register_function(function)

    def register_function(self, module):
        # Discovery convention: a class is a menu entry iff its name ends in "Func",
        # and its docstring is the menu label. The bot keys the same classes by name
        # in bot/services/registry.py (validated at bot startup). A helper base like
        # functions/broadcast.py's `Broadcast` is deliberately named without the suffix
        # so it is skipped here.
        for classname, classobj in inspect.getmembers(module, inspect.isclass):
            if classname.endswith("Func"):
                self.functions.append((
                    classobj(self.storage, self.settings),
                    classobj.__doc__
                ))

    def execute(self, index: int):
        try:
            function_instance = self.functions[index][0]
        except IndexError:
            console.print(f"[bold red]no function at index {index}[/]")
            return

        function = function_instance.execute()

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

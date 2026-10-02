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
            self.loop = asyncio.get_event_loop()
        except RuntimeError:
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)

        self.functions: List[Union[Callable, Awaitable]] = []

        for file in os.listdir(directory):
            if file.endswith(".py"):
                self.load_function(
                    file[:-3], os.path.join(directory, file)
                )

        self.functions.sort(key=lambda item: item[1].lower())

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
            self.loop.run_until_complete(function)

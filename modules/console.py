"""The one shared Rich console, imported everywhere instead of a per-module Console()."""

from rich.console import Console

console = Console()

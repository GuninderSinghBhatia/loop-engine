"""Entry point for the `loop` command. Phase 1: starts and exits cleanly."""
from rich.console import Console


def main() -> None:
    console = Console()
    console.print("[bold green]Loop Engine v2[/bold green]")

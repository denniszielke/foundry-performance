"""Small Textual console for an Agent Framework harness."""

from __future__ import annotations

from typing import Any

from agent_framework import get_agent_mode, set_agent_mode
from rich.markdown import Markdown
from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.widgets import Footer, Header, Input, RichLog, Static


class HarnessConsole(App[None]):
    """Interactive terminal UI with plan/execute mode commands."""

    CSS = """
    Screen { layout: vertical; }
    #mode { height: 1; padding: 0 1; color: $text-muted; }
    #log { height: 1fr; border: round $accent; padding: 0 1; }
    #prompt { dock: bottom; }
    """
    BINDINGS = [("ctrl+c", "quit", "Quit")]

    def __init__(self, agent: Any, session: Any, *, initial_mode: str) -> None:
        super().__init__()
        self.agent = agent
        self.session = session
        self.initial_mode = initial_mode

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical():
            yield Static(id="mode")
            yield RichLog(id="log", markup=True, wrap=True)
            yield Input(placeholder="Ask about the weather, or use /mode plan|execute", id="prompt")
        yield Footer()

    def on_mount(self) -> None:
        set_agent_mode(self.session, self.initial_mode, available_modes=("plan", "execute"))
        self._show_mode()
        self.query_one("#log", RichLog).write(
            "[dim]Commands: /mode plan, /mode execute, /mode, /exit[/dim]"
        )
        self.query_one("#prompt", Input).focus()

    def _show_mode(self) -> None:
        mode = get_agent_mode(
            self.session,
            default_mode="plan",
            available_modes=("plan", "execute"),
        )
        self.query_one("#mode", Static).update(f"Mode: {mode}")

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        prompt = event.value.strip()
        event.input.value = ""
        if not prompt:
            return
        if prompt == "/exit":
            self.exit()
            return
        if prompt.startswith("/mode"):
            self._handle_mode(prompt)
            return

        log = self.query_one("#log", RichLog)
        event.input.disabled = True
        log.write(f"\n[bold cyan]You:[/bold cyan] {prompt}")
        try:
            result = await self.agent.run(prompt, session=self.session)
            log.write("[bold green]Agent:[/bold green]")
            log.write(Markdown(result.text or ""))
        except Exception as exc:  # noqa: BLE001 - display interactive failures
            log.write(f"[bold red]Error:[/bold red] {exc}")
        finally:
            event.input.disabled = False
            event.input.focus()
            self._show_mode()

    def _handle_mode(self, command: str) -> None:
        parts = command.split(maxsplit=1)
        log = self.query_one("#log", RichLog)
        if len(parts) == 1:
            self._show_mode()
            return
        try:
            mode = set_agent_mode(
                self.session,
                parts[1],
                available_modes=("plan", "execute"),
            )
        except ValueError as exc:
            log.write(f"[bold red]Error:[/bold red] {exc}")
            return
        log.write(f"[dim]Switched to {mode} mode.[/dim]")
        self._show_mode()


async def run_agent_async(agent: Any, *, session: Any, initial_mode: str = "plan") -> None:
    """Run one harness session in the terminal UI."""
    async with agent:
        await HarnessConsole(agent, session, initial_mode=initial_mode).run_async()
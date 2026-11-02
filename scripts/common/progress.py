"""Small terminal progress bars with elapsed time and request-based estimates."""

import os
import shutil
import sys
import time
from types import TracebackType


def duration(seconds: float) -> str:
    """Format a non-negative duration as hours, minutes, and seconds."""
    minutes, seconds = divmod(int(max(0, seconds)), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:d}h{minutes:02d}m" if hours else f"{minutes:d}m{seconds:02d}s"


class Progress:
    """Display one stage, keeping partial items in its percentage and ETA.

    For boats, each daily request is one step and a boat is one complete item.
    Interactive terminals redraw a single line; redirected logs receive updates
    at most every 30 seconds. Context exit preserves incomplete progress on errors.
    """

    def __init__(self, label: str, total: int, *, steps_per_item: int = 1) -> None:
        self.label = label
        self.total = total
        self.steps_per_item = steps_per_item
        self.completed = 0
        self.detail = ""
        self.started = time.monotonic()
        self.last_render = float("-inf")
        self.interactive = sys.stderr.isatty() and os.environ.get("TERM") != "dumb"

    def __enter__(self) -> "Progress":
        self.render(force=True)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if exc_type:
            self.detail = "Stopped; download incomplete"
        self.render(force=True)
        if self.interactive:
            print(file=sys.stderr, flush=True)

    def advance(self) -> None:
        """Count one successfully processed request, including empty responses."""
        self.completed += 1
        self.render()

    def describe(self, detail: str) -> None:
        """Show the active tile or boat before its potentially slow request."""
        self.detail = detail
        self.render()

    def message(self, message: str) -> None:
        """Print a warning without mixing it into an interactive progress line."""
        if self.interactive:
            sys.stderr.write("\r\033[K")
        print(message, file=sys.stderr, flush=True)
        self.render(force=True)

    def render(self, *, force: bool = False) -> None:
        """Render counts and an ETA based on elapsed time per completed request."""
        now = time.monotonic()
        interval = 0.2 if self.interactive else 30
        if not force and now - self.last_render < interval:
            return
        self.last_render = now
        elapsed = now - self.started
        steps = self.total * self.steps_per_item
        fraction = self.completed / steps if steps else 1
        eta = (
            duration(elapsed / self.completed * (steps - self.completed))
            if self.completed
            else "--"
        )
        width = 16
        filled = int(width * fraction)
        bar = "#" * filled + "-" * (width - filled)
        line = (
            f"{self.label} [{bar}] {fraction:5.1%} "
            f"{self.completed // self.steps_per_item}/{self.total} "
            f"elapsed {duration(elapsed)} | ETA {eta} | {self.detail}"
        )
        if self.interactive:
            columns = shutil.get_terminal_size(fallback=(120, 24)).columns
            sys.stderr.write("\r\033[K" + line[: max(1, columns - 1)])
            sys.stderr.flush()
        else:
            print(line, file=sys.stderr, flush=True)

"""Floating 'Ask SAGE' pill button for selection context."""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from .styles import SURFACE


class FloatingAskButton(tk.Canvas):
    """Floating pill button that appears above selections, similar to ChatGPT's prompt button."""

    WIDTH = 96
    HEIGHT = 30
    RADIUS = 7

    BG_NORMAL = "#212425"
    BG_HOVER = "#36393B"
    BG_PRESSED = "#161819"
    BORDER_NORMAL = "#3A3D40"
    BORDER_HOVER = "#4E5256"
    BORDER_PRESSED = "#2E3032"
    TEXT_COLOR = "#FFFFFF"
    FONT = ("Segoe UI Semibold", 9)

    def __init__(
        self,
        parent: tk.Misc,
        command: Callable[[], None] | None = None,
        text: str = "Ask SAGE",
    ) -> None:
        super().__init__(
            parent,
            width=self.WIDTH,
            height=self.HEIGHT,
            bg=SURFACE,
            highlightthickness=0,
            borderwidth=0,
            cursor="hand2",
        )
        self._command = command
        self._text = text
        self._state = "normal"
        self._is_visible = False
        self._draw(self.BG_NORMAL, self.BORDER_NORMAL)

        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)

    def lift(self, aboveThis=None) -> None:  # type: ignore[override]
        """Ensure tk.Misc.lift is called rather than Canvas.tag_raise."""
        tk.Misc.lift(self, aboveThis)

    def set_command(self, command: Callable[[], None]) -> None:
        self._command = command

    def _draw(self, fill: str, border: str) -> None:
        self.delete("all")
        r = self.RADIUS
        w = self.WIDTH - 2
        h = self.HEIGHT - 2
        x1, y1 = 1, 1
        x2, y2 = x1 + w, y1 + h

        # 4 corner arcs
        self.create_arc(x1, y1, x1 + 2 * r, y1 + 2 * r, start=90, extent=90, fill=fill, outline=border)
        self.create_arc(x2 - 2 * r, y1, x2, y1 + 2 * r, start=0, extent=90, fill=fill, outline=border)
        self.create_arc(x1, y2 - 2 * r, x1 + 2 * r, y2, start=180, extent=90, fill=fill, outline=border)
        self.create_arc(x2 - 2 * r, y2 - 2 * r, x2, y2, start=270, extent=90, fill=fill, outline=border)

        # Fill rectangles
        self.create_rectangle(x1 + r, y1, x2 - r, y2, fill=fill, outline="")
        self.create_rectangle(x1, y1 + r, x2, y2 - r, fill=fill, outline="")

        # Border lines connecting arcs
        self.create_line(x1 + r, y1, x2 - r, y1, fill=border)
        self.create_line(x1 + r, y2, x2 - r, y2, fill=border)
        self.create_line(x1, y1 + r, x1, y2 - r, fill=border)
        self.create_line(x2, y1 + r, x2, y2 - r, fill=border)

        # Centered label
        self.create_text(
            self.WIDTH // 2,
            self.HEIGHT // 2,
            text=self._text,
            fill=self.TEXT_COLOR,
            font=self.FONT,
        )

    def _on_enter(self, _event: tk.Event) -> None:
        self._state = "hover"
        self._draw(self.BG_HOVER, self.BORDER_HOVER)

    def _on_leave(self, _event: tk.Event) -> None:
        self._state = "normal"
        self._draw(self.BG_NORMAL, self.BORDER_NORMAL)

    def _on_press(self, _event: tk.Event) -> None:
        self._draw(self.BG_PRESSED, self.BORDER_PRESSED)

    def _on_release(self, event: tk.Event) -> None:
        if 0 <= event.x <= self.WIDTH and 0 <= event.y <= self.HEIGHT:
            self._draw(self.BG_HOVER, self.BORDER_HOVER)
            self.hide()
            if self._command:
                self._command()
        else:
            self._draw(self.BG_NORMAL, self.BORDER_NORMAL)

    def show(self, x: int, y: int) -> None:
        """Place and show the hovering button at the specified coordinates."""
        self._is_visible = True
        self.place(x=x, y=y)
        self.lift()

    def hide(self) -> None:
        """Hide the button."""
        if self._is_visible:
            self._is_visible = False
            self.place_forget()

    def is_visible(self) -> bool:
        return self._is_visible

    def invoke(self) -> None:
        """Trigger the button command programmatically."""
        self.hide()
        if self._command:
            self._command()

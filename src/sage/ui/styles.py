"""Small, dependency-free ttk theme configuration."""

from __future__ import annotations

from tkinter import ttk


def configure_styles(style: ttk.Style) -> None:
    try:
        style.theme_use("vista")
    except Exception:
        pass
    style.configure("Title.TLabel", font=("Segoe UI", 16, "bold"))
    style.configure("Subtitle.TLabel", foreground="#5f6b7a")
    style.configure("Treeview", rowheight=25, font=("Segoe UI", 9))
    style.configure("Treeview.Heading", font=("Segoe UI", 9, "bold"))
    style.configure("Status.TLabel", padding=(8, 5), foreground="#44546a")


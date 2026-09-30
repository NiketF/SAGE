"""Evidence assistant pane."""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from tkinter import ttk


class AssistantPanel(ttk.Frame):
    def __init__(self, parent: tk.Misc, on_question: Callable[[str], str]) -> None:
        super().__init__(parent, padding=10)
        self.on_question = on_question
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        ttk.Label(self, text="Evidence Assistant", style="Title.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, 8)
        )
        self.transcript = tk.Text(
            self,
            wrap="word",
            state="disabled",
            borderwidth=1,
            relief="solid",
            padx=12,
            pady=12,
            font=("Segoe UI", 10),
        )
        self.transcript.grid(row=1, column=0, sticky="nsew")

        composer = ttk.Frame(self)
        composer.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        composer.columnconfigure(0, weight=1)
        self.question = ttk.Entry(composer)
        self.question.grid(row=0, column=0, sticky="ew", padx=(0, 7))
        self.question.bind("<Return>", self._submit)
        ttk.Button(composer, text="Ask", command=self._submit).grid(row=0, column=1)

        self.write(
            "SAGE",
            "Scan an NTFS drive, then ask about counts, used space, or largest folders. "
            "Answers come only from the current scan; no API key is used.",
        )

    def write(self, speaker: str, message: str) -> None:
        self.transcript.configure(state="normal")
        if self.transcript.index("end-1c") != "1.0":
            self.transcript.insert("end", "\n\n")
        self.transcript.insert("end", f"{speaker}\n", ("speaker",))
        self.transcript.insert("end", message)
        self.transcript.tag_configure("speaker", font=("Segoe UI", 10, "bold"))
        self.transcript.see("end")
        self.transcript.configure(state="disabled")

    def _submit(self, _event: object = None) -> None:
        question = self.question.get().strip()
        if not question:
            return
        self.question.delete(0, "end")
        self.write("You", question)
        try:
            answer = self.on_question(question)
        except RuntimeError as exc:
            answer = str(exc)
        self.write("SAGE", answer)


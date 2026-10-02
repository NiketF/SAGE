"""Evidence assistant pane."""

from __future__ import annotations

import tkinter as tk
import time
from collections.abc import Callable
from tkinter import ttk
from .markdown import markdown_spans


class AssistantPanel(ttk.Frame):
    def __init__(self, parent: tk.Misc, on_question: Callable[[str], str | None]) -> None:
        super().__init__(parent, padding=10)
        self.on_question = on_question
        self._busy = False
        self._reply_mark: str | None = None
        self._reply_buffer = ""
        self._render_job: str | None = None
        self._spinner_job: str | None = None
        self._started = 0.0
        self._phase = "Ready"
        self._spin_index = 0
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
        scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.transcript.yview)
        scrollbar.grid(row=1, column=1, sticky="ns")
        self.transcript.configure(yscrollcommand=scrollbar.set)
        self.transcript.tag_configure("speaker", font=("Segoe UI", 10, "bold"))
        self.transcript.tag_configure("bold", font=("Segoe UI", 10, "bold"))
        self.transcript.tag_configure("italic", font=("Segoe UI", 10, "italic"))
        self.transcript.tag_configure("code", font=("Consolas", 10))

        self.activity = ttk.Frame(self)
        self.activity.grid(row=2, column=0, sticky="ew", pady=(6, 0))
        self.spinner = ttk.Label(self.activity, text="", width=2)
        self.spinner.pack(side="left")
        self.activity_text = tk.StringVar(value="Ready")
        ttk.Label(self.activity, textvariable=self.activity_text).pack(side="left")

        composer = ttk.Frame(self)
        composer.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        composer.columnconfigure(0, weight=1)
        self.question = ttk.Entry(composer)
        self.question.grid(row=0, column=0, sticky="ew", padx=(0, 7))
        self.question.bind("<Return>", self._submit)
        self.ask_button = ttk.Button(composer, text="Ask", command=self._submit)
        self.ask_button.grid(row=0, column=1)

        self.write(
            "SAGE",
            "Hey! I'm SAGE. Scan a drive and ask me about a folder by name, "
            "what's using space, or what to consider before removing something.",
        )

    def write(self, speaker: str, message: str) -> None:
        self.transcript.configure(state="normal")
        if self.transcript.index("end-1c") != "1.0":
            self.transcript.insert("end", "\n\n")
        self.transcript.insert("end", f"{speaker}\n", ("speaker",))
        for text, tags in markdown_spans(message) if speaker == "SAGE" else [(message, ())]:
            self.transcript.insert("end", text, tags)
        self.transcript.see("end")
        self.transcript.configure(state="disabled")

    def begin_reply(self) -> None:
        self.write("SAGE", "")
        self._reply_mark = "assistant_reply"
        self.transcript.mark_set(self._reply_mark, "end-1c")
        self.transcript.mark_gravity(self._reply_mark, "left")
        self.transcript.mark_set("assistant_reply_end", "end-1c")
        self.transcript.mark_gravity("assistant_reply_end", "left")
        self._reply_buffer = ""
        self._busy = True
        self.ask_button.configure(state="disabled")
        self._started = time.monotonic()
        self.set_status("Preparing your answer…")
        self._animate()

    def set_status(self, phase: str) -> None:
        self._phase = phase
        self.activity_text.set(phase)

    def _animate(self) -> None:
        if not self._busy:
            return
        self.spinner.configure(text=("◐", "◓", "◑", "◒")[self._spin_index % 4])
        self._spin_index += 1
        elapsed = int(time.monotonic() - self._started)
        self.activity_text.set(f"{self._phase}  {elapsed}s")
        self._spinner_job = self.after(120, self._animate)

    def append_reply(self, text: str) -> None:
        if self._reply_mark is None:
            return
        self._reply_buffer += text
        if self._render_job is None:
            self._render_job = self.after(80, self._render_reply)

    def _render_reply(self, final: bool = False) -> None:
        self._render_job = None
        if self._reply_mark is None:
            return
        self.transcript.configure(state="normal")
        end = self.transcript.index("assistant_reply_end")
        self.transcript.delete(self._reply_mark, end)
        self.transcript.mark_set("assistant_reply_insert", self._reply_mark)
        self.transcript.mark_gravity("assistant_reply_insert", "right")
        for text, tags in markdown_spans(self._reply_buffer, partial=not final):
            self.transcript.insert("assistant_reply_insert", text, tags)
        self.transcript.mark_set("assistant_reply_end", "assistant_reply_insert")
        self.transcript.mark_unset("assistant_reply_insert")
        self.transcript.see("end")
        self.transcript.configure(state="disabled")

    def finish_reply(self, message: str) -> None:
        if self._render_job:
            self.after_cancel(self._render_job)
            self._render_job = None
        if self._reply_mark:
            self._reply_buffer = message
            self._render_reply(final=True)
            self.transcript.mark_unset(self._reply_mark, "assistant_reply_end")
            self._reply_mark = None
        if self._spinner_job:
            self.after_cancel(self._spinner_job)
            self._spinner_job = None
        self._busy = False
        self.spinner.configure(text="")
        elapsed = time.monotonic() - self._started
        self.activity_text.set(f"Answered in {elapsed:.1f}s")
        self.ask_button.configure(state="normal")

    def _submit(self, _event: object = None) -> None:
        question = self.question.get().strip()
        if not question or self._busy:
            return
        self.question.delete(0, "end")
        self.write("You", question)
        try:
            answer = self.on_question(question)
        except RuntimeError as exc:
            answer = str(exc)
        if answer is not None:
            self.write("SAGE", answer)

    def destroy(self) -> None:
        for job in (self._spinner_job, self._render_job):
            if job:
                self.after_cancel(job)
        super().destroy()

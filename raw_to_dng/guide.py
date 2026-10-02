"""Scrollable, non-modal quality guide. Does not require Markdown or network access."""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk
import webbrowser

from .help_content import HELP_PAGES, REFERENCES, Paragraph, Table


class QualityGuide:
    def __init__(self, parent: tk.Tk, family: str, page: str = "recommendations"):
        self.window = tk.Toplevel(parent)
        self.window.title("DngConverterEx — 画質ガイド")
        self.window.geometry("1060x780")
        self.window.minsize(850, 620)
        self.window.transient(parent)
        self.window.bind("<Escape>", lambda _event: self.window.destroy())
        self.window.bind("<F1>", lambda _event: self.select("usage"))
        outer = ttk.Frame(self.window, padding=18)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="画質ガイド", font=(family, 20, "bold")).pack(anchor="w")
        ttk.Label(outer, text="明暗の編集耐性、圧縮効率、解像度を選ぶための目安。", style="Muted.TLabel").pack(anchor="w", pady=(2, 12))
        self.notebook = ttk.Notebook(outer)
        self.notebook.pack(fill="both", expand=True)
        self.pages: dict[str, ttk.Frame] = {}
        self.canvases: dict[str, tk.Canvas] = {}
        self.page_headings: dict[str, ttk.Label] = {}
        self.scroll_targets: dict[tk.Widget, tk.Canvas] = {}
        for help_page in HELP_PAGES:
            tab = ttk.Frame(self.notebook)
            self.notebook.add(tab, text=help_page.title)
            self.pages[help_page.key] = tab
            canvas = tk.Canvas(tab, background="#f4f6fa", highlightthickness=0, takefocus=False)
            self.canvases[help_page.key] = canvas
            scrollbar = ttk.Scrollbar(tab, orient="vertical", command=canvas.yview)
            canvas.configure(yscrollcommand=scrollbar.set)
            scrollbar.pack(side="right", fill="y")
            canvas.pack(side="left", fill="both", expand=True)
            content = ttk.Frame(canvas, padding=(16, 12, 16, 16))
            content.columnconfigure(0, weight=1)
            item = canvas.create_window((0, 0), window=content, anchor="nw")
            wrapping: list[tuple[ttk.Label, int]] = []
            content.bind("<Configure>", lambda _event, c=canvas: c.configure(scrollregion=c.bbox("all")))

            def resize(event, c=canvas, window_item=item, labels=wrapping):
                c.itemconfigure(window_item, width=event.width)
                for label, weight in labels:
                    label.configure(wraplength=max(70, (event.width - 44) * weight // 100 - 16))

            canvas.bind("<Configure>", resize)
            for row_index, block in enumerate(help_page.blocks):
                if isinstance(block, Paragraph):
                    label = ttk.Label(content, text=block.text, justify="left", anchor="w",
                                      font=(family, 12, "bold") if block.heading else (family, 10),
                                      wraplength=900)
                    label.grid(row=row_index, column=0, sticky="ew", pady=(12, 5) if block.heading else (0, 9))
                    wrapping.append((label, 100))
                    if block.heading and help_page.key not in self.page_headings:
                        self.page_headings[help_page.key] = label
                else:
                    self._table(content, row_index, block, family, wrapping)
            if help_page.key == "usage":
                links = ttk.Frame(content)
                links.grid(row=len(help_page.blocks), column=0, sticky="ew", pady=6)
                for label, url in REFERENCES:
                    button = ttk.Button(links, text=label + " ↗", command=lambda target=url: webbrowser.open(target))
                    button.pack(anchor="w", pady=2)
            self._bind_scroll(canvas, canvas)
        footer = ttk.Frame(outer)
        footer.pack(fill="x", pady=(12, 0))
        ttk.Label(footer, text="初期設定と推奨設定は「おすすめ設定」で確認できます。", style="Muted.TLabel").pack(side="left")
        ttk.Button(footer, text="閉じる（Esc）", command=self.window.destroy).pack(side="right")
        self.select(page)

    @staticmethod
    def _table(parent, row_index: int, block: Table, family: str, wrapping) -> None:
        frame = ttk.Frame(parent)
        frame.grid(row=row_index, column=0, sticky="ew", pady=(0, 10))
        weights = block.weights or tuple(100 // len(block.headers) for _ in block.headers)
        for column, weight in enumerate(weights):
            frame.columnconfigure(column, weight=weight, uniform="table")
        for row, values in enumerate((block.headers, *block.rows)):
            for column, value in enumerate(values):
                label = ttk.Label(frame, text=value, justify="left", anchor="nw", padding=(8, 7),
                                  background="#e4ebf5" if row == 0 else ("#ffffff" if row % 2 else "#edf2f8"),
                                  font=(family, 10, "bold") if row == 0 else (family, 10), wraplength=300)
                label.grid(row=row, column=column, sticky="nsew", padx=(0, 1), pady=(0, 1))
                wrapping.append((label, weights[column]))

    def _bind_scroll(self, widget: tk.Widget, canvas: tk.Canvas) -> None:
        self.scroll_targets[widget] = canvas
        widget.bind("<MouseWheel>", self._scroll, add="+")
        widget.bind("<Button-4>", self._scroll, add="+")
        widget.bind("<Button-5>", self._scroll, add="+")
        for child in widget.winfo_children():
            self._bind_scroll(child, canvas)

    def _scroll(self, event) -> str:
        canvas = self.scroll_targets[event.widget]
        if event.num in (4, 5):
            units = -1 if event.num == 4 else 1
        else:
            delta = event.delta
            if not delta:
                return "break"
            units = -int(delta / 120) if abs(delta) >= 120 else (-1 if delta > 0 else 1)
        if canvas.bbox("all") and canvas.bbox("all")[3] > canvas.winfo_height():
            canvas.yview_scroll(units, "units")
        return "break"

    def select(self, key: str) -> None:
        self.notebook.select(self.pages.get(key, self.pages["recommendations"]))
        self.window.deiconify()
        self.window.lift()

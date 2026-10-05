"""Standard-library desktop GUI. Workers communicate with Tk only via a queue."""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import traceback
import webbrowser
from dataclasses import replace
from pathlib import Path
import tkinter as tk
from tkinter import font as tkfont
from tkinter import filedialog, messagebox, simpledialog, ttk

from . import __version__
from .core import (
    ADOBE_DOWNLOAD_URL, COMPATIBILITIES, MODE_LABELS, RAW_EXTENSIONS,
    Converter, Mode, Settings, plan_jobs, resolve_converter, run_batch, scan_inputs,
)
from .guide import QualityGuide

QUALITY_PRESETS = {
    "最高画質": 0.1, "高画質": 0.5, "標準": 1.0,
    "軽量": 2.0, "小容量": 4.0, "最小容量": 6.0,
}


def config_path() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", str(Path.home())))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    return base / "DngConverterEx" / "settings.json"


def open_folder(path: Path) -> None:
    if os.name == "nt":
        os.startfile(str(path))
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


class Application:
    def __init__(self, root: tk.Tk, initial_inputs: list[str] | None = None):
        self.root = root
        root.title(f"DngConverterEx — 圧縮画質・出力画素数 {__version__}")
        root.geometry("1140x900")
        root.minsize(920, 700)
        self.inputs: list[Path] = []
        self.events: queue.Queue = queue.Queue()
        self.cancel = threading.Event()
        self.worker: threading.Thread | None = None
        self.busy = False
        self.last_report: Path | None = None
        self.quality_guide: QualityGuide | None = None
        self.widgets_to_disable: list[tk.Widget] = []
        self.converter = tk.StringVar()
        self.output = tk.StringVar()
        self.mode = tk.StringVar(value=MODE_LABELS[Mode.LOSSY_JXL])
        self.quality = tk.StringVar(value="最高画質")
        self.distance = tk.StringVar(value="0.1")
        self.effort = tk.StringVar(value="7")
        self.resize = tk.StringVar(value="原寸を維持")
        self.megapixels = tk.StringVar(value="24")
        self.long_edge = tk.StringVar(value="6000")
        self.preview = tk.StringVar(value="中サイズ")
        self.compatibility = tk.StringVar(value="13.2")
        self.collision = tk.StringVar(value="連番を付ける")
        self.name_template = tk.StringVar(value="{stem}")
        self.start_index = tk.StringVar(value="1")
        self.timeout = tk.StringVar(value="1800")
        self.recursive = tk.BooleanVar(value=True)
        self.preserve_folders = tk.BooleanVar(value=True)
        self.include_dng = tk.BooleanVar(value=False)
        self.fast_load = tk.BooleanVar(value=True)
        self.embed_original = tk.BooleanVar(value=False)
        self.linear = tk.BooleanVar(value=False)
        self.strict_wb = tk.BooleanVar(value=True)
        self.preserve_mtime = tk.BooleanVar(value=True)
        self.jxl_retry = tk.BooleanVar(value=True)
        self.jxl_fallback = tk.BooleanVar(value=False)
        self.hint = tk.StringVar()
        self.status = tk.StringVar(value="RAWを追加し、出力先を選択してください。")
        self._style()
        self._load_settings()
        self._build()
        self._settings_changed()
        root.report_callback_exception = self._callback_error
        root.protocol("WM_DELETE_WINDOW", self._close)
        root.bind("<F1>", lambda _event: self._show_guide())
        root.after(100, self._poll)
        if initial_inputs:
            self._add_paths(initial_inputs)

    def _style(self) -> None:
        style = ttk.Style()
        if "clam" in style.theme_names():
            style.theme_use("clam")
        preferred = ("Yu Gothic UI", "Meiryo") if os.name == "nt" else ("Hiragino Sans", "Noto Sans CJK JP", "Helvetica")
        families = set(tkfont.families(self.root))
        font = next((candidate for candidate in preferred if candidate in families), tkfont.nametofont("TkDefaultFont").actual("family"))
        self.font_family = font
        self.root.option_add("*Font", (font, 10))
        style.configure(".", font=(font, 10))
        style.configure("TFrame", background="#f4f6fa")
        style.configure("TLabel", background="#f4f6fa", foreground="#203047")
        style.configure("TLabelframe", background="#f4f6fa", borderwidth=1)
        style.configure("TLabelframe.Label", background="#f4f6fa", foreground="#203047")
        style.configure("TCheckbutton", background="#f4f6fa")
        style.configure("Title.TLabel", font=(font, 21, "bold"))
        style.configure("Muted.TLabel", foreground="#596b82")
        style.configure("Accent.TButton", foreground="white", background="#2266aa", padding=(18, 9))
        style.map("Accent.TButton", background=[("active", "#18578e"), ("disabled", "#90a9c2")])
        style.configure("Treeview", rowheight=27, font=(font, 10))
        style.configure("Treeview.Heading", font=(font, 10, "bold"))

    def _button(self, parent, text, command, **kwargs):
        widget = ttk.Button(parent, text=text, command=command, **kwargs)
        self.widgets_to_disable.append(widget)
        return widget

    def _combo(self, parent, variable, values, width=28):
        widget = ttk.Combobox(parent, textvariable=variable, values=list(values), state="readonly", width=width)
        self.widgets_to_disable.append(widget)
        return widget

    def _entry(self, parent, variable, **kwargs):
        widget = ttk.Entry(parent, textvariable=variable, **kwargs)
        self.widgets_to_disable.append(widget)
        return widget

    def _check(self, parent, text, variable):
        widget = ttk.Checkbutton(parent, text=text, variable=variable)
        self.widgets_to_disable.append(widget)
        return widget

    def _build(self) -> None:
        outer = ttk.Frame(self.root, padding=18)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="DngConverterEx", style="Title.TLabel", font=(self.font_family, 21, "bold")).pack(anchor="w")
        ttk.Label(outer, text="AdobeのRAW変換を、画質と画素数を指定して一括処理。", style="Muted.TLabel").pack(anchor="w", pady=(2, 10))

        engine = ttk.Frame(outer)
        engine.pack(fill="x", pady=(0, 10))
        ttk.Label(engine, text="Adobe DNG Converter").pack(side="left")
        self._entry(engine, self.converter).pack(side="left", fill="x", expand=True, padx=8)
        self._button(engine, "参照", self._choose_converter).pack(side="left")
        self._button(engine, "自動検出", self._detect_converter).pack(side="left", padx=5)
        self._button(engine, "Adobe公式サイト", lambda: webbrowser.open(ADOBE_DOWNLOAD_URL)).pack(side="left")

        self.notebook = ttk.Notebook(outer)
        self.notebook.pack(fill="x")
        main = ttk.Frame(self.notebook, padding=12)
        advanced = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(main, text="変換設定")
        self.notebook.add(advanced, text="詳細設定")
        main.columnconfigure(0, weight=1)
        main.columnconfigure(1, weight=0)

        inputs = ttk.LabelFrame(main, text="入力 RAW / DNG", padding=10)
        inputs.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        bar = ttk.Frame(inputs)
        bar.pack(fill="x", pady=(0, 8))
        for text, command in (("ファイルを追加", self._choose_files), ("フォルダーを追加", self._choose_folder),
                              ("選択を削除", self._remove_selected), ("クリア", self._clear_inputs)):
            self._button(bar, text, command).pack(side="left", padx=(0, 4))
        self.input_tree = ttk.Treeview(inputs, columns=("path",), show="headings", height=7, selectmode="extended")
        self.input_tree.heading("path", text="入力パス（フォルダーは変換開始時に走査）")
        self.input_tree.column("path", width=470)
        self.input_tree.pack(fill="both", expand=True)
        toggles = ttk.Frame(inputs)
        toggles.pack(fill="x", pady=(8, 0))
        self._check(toggles, "サブフォルダーも対象", self.recursive).pack(anchor="w")
        self._check(toggles, "フォルダー走査でDNGも対象にする", self.include_dng).pack(anchor="w")

        settings = ttk.LabelFrame(main, text="圧縮・解像度", padding=10)
        settings.grid(row=0, column=1, sticky="nsew")
        settings.columnconfigure(1, weight=1)
        ttk.Label(settings, text="圧縮方式").grid(row=0, column=0, sticky="w", pady=5)
        self.mode_widget = self._combo(settings, self.mode, MODE_LABELS.values(), 39)
        self.mode_widget.grid(row=0, column=1, columnspan=3, sticky="ew", pady=5)
        self.mode_widget.bind("<<ComboboxSelected>>", self._settings_changed)
        ttk.Label(settings, text="圧縮画質").grid(row=1, column=0, sticky="w", pady=5)
        self.quality_widget = self._combo(settings, self.quality, [*QUALITY_PRESETS, "カスタム"], 12)
        self.quality_widget.grid(row=1, column=1, sticky="w", pady=5)
        self.quality_widget.bind("<<ComboboxSelected>>", self._quality_changed)
        ttk.Label(settings, text="distance").grid(row=1, column=2, padx=(6, 4))
        self.distance_widget = ttk.Spinbox(settings, from_=0, to=6, increment=0.01, textvariable=self.distance, width=7)
        self.widgets_to_disable.append(self.distance_widget)
        self.distance_widget.grid(row=1, column=3, pady=5)
        self.distance.trace_add("write", self._distance_changed)
        ttk.Label(settings, text="小さいほど高画質。詳しくは「画質ガイド / F1」。", style="Muted.TLabel").grid(row=2, column=0, columnspan=4, sticky="w")
        ttk.Label(settings, text="出力解像度").grid(row=3, column=0, sticky="w", pady=(12, 5))
        self.resize_widget = self._combo(settings, self.resize, ("原寸を維持", "画素数で指定（MP）", "長辺で指定（px）"), 23)
        self.resize_widget.grid(row=3, column=1, columnspan=3, sticky="ew", pady=(12, 5))
        self.resize_widget.bind("<<ComboboxSelected>>", self._resize_changed)
        ttk.Label(settings, text="目標画素数").grid(row=4, column=0, sticky="w", pady=5)
        self.mp_widget = self._entry(settings, self.megapixels, width=12)
        self.mp_widget.grid(row=4, column=1, sticky="w", pady=5)
        ttk.Label(settings, text="MP（24＝2400万画素）").grid(row=4, column=2, columnspan=2, sticky="w")
        ttk.Label(settings, text="目標長辺").grid(row=5, column=0, sticky="w", pady=5)
        self.side_widget = self._entry(settings, self.long_edge, width=12)
        self.side_widget.grid(row=5, column=1, sticky="w", pady=5)
        ttk.Label(settings, text="px").grid(row=5, column=2, sticky="w")
        ttk.Label(settings, text="縦横比を維持。指定値は上限、拡大はしません。", style="Muted.TLabel").grid(row=6, column=0, columnspan=4, sticky="w", pady=4)

        output = ttk.Frame(main)
        output.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        ttk.Label(output, text="出力先").pack(side="left")
        self._entry(output, self.output).pack(side="left", fill="x", expand=True, padx=8)
        self._button(output, "フォルダー選択", self._choose_output).pack(side="left")
        ttk.Label(main, textvariable=self.hint, wraplength=1020, style="Muted.TLabel").grid(row=2, column=0, columnspan=2, sticky="w", pady=(8, 0))

        advanced.columnconfigure(1, weight=1)
        rows = (("JPEGプレビュー", self.preview, ("なし", "中サイズ", "全画素")),
                ("Camera Raw互換（JPEG XL以外）", self.compatibility, COMPATIBILITIES),
                ("同名ファイル", self.collision, ("連番を付ける", "スキップ", "上書き")))
        for row, (text, variable, values) in enumerate(rows):
            ttk.Label(advanced, text=text).grid(row=row, column=0, sticky="w", padx=(0, 16), pady=4)
            self._combo(advanced, variable, values, 26).grid(row=row, column=1, sticky="w", pady=4)
        for row, (text, variable) in enumerate((("JPEG XL effort（1＝高速、9＝低速）", self.effort),
                                               ("出力名（.dngは自動付加）", self.name_template),
                                               ("開始番号", self.start_index), ("1ファイルの制限時間（秒）", self.timeout)), 3):
            ttk.Label(advanced, text=text).grid(row=row, column=0, sticky="w", padx=(0, 16), pady=4)
            widget = self._entry(advanced, variable, width=28)
            widget.grid(row=row, column=1, sticky="w", pady=4)
            if variable is self.effort:
                self.effort_widget = widget
        ttk.Label(advanced, text="名前の例: {stem} / {index:04d}_{stem} / {date}_{stem} / {stem}_{ext}", style="Muted.TLabel").grid(row=7, column=0, columnspan=2, sticky="w", pady=4)
        checks = ttk.Frame(advanced)
        checks.grid(row=0, column=2, rowspan=7, sticky="nw", padx=(40, 0))
        for text, variable in (("出力でフォルダー構造を維持", self.preserve_folders),
                               ("Fast Load Dataを埋め込む", self.fast_load),
                               ("元RAWをDNG内に埋め込む（容量増）", self.embed_original),
                               ("ロスレス／無圧縮でもLinear DNGにする", self.linear),
                               ("カラーRAWのWB情報を必須にする", self.strict_wb),
                               ("元ファイルの更新日時を引き継ぐ", self.preserve_mtime)):
            self._check(checks, text, variable).pack(anchor="w", pady=5)

        recovery = ttk.LabelFrame(advanced, text="JPEG XLのエラー対策", padding=8)
        recovery.grid(row=8, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        self.jxl_retry_widget = self._check(recovery, "該当するassert時、effort 8/9から7へ一度再試行（画質設定・解像度は維持）", self.jxl_retry)
        self.jxl_retry_widget.grid(row=0, column=0, columnspan=2, sticky="w", pady=3)
        self.jxl_fallback_widget = self._check(recovery, "該当assertで失敗したら、ロスレスJPEG圧縮DNGへ変更（原寸指定時のみ）", self.jxl_fallback)
        self.jxl_fallback_widget.grid(row=1, column=0, columnspan=2, sticky="w", pady=3)
        self.effort_7_button = self._button(recovery, "互換性優先：effort 7に設定", self._prefer_effort7)
        self.effort_7_button.grid(row=2, column=0, sticky="w", pady=(5, 0))
        ttk.Label(recovery, text="画質設定・解像度は変更しません。詳しくは画質ガイド。", style="Muted.TLabel").grid(row=2, column=1, sticky="w", padx=12)

        action = ttk.Frame(outer)
        action.pack(fill="x", pady=12)
        self.start_button = self._button(action, "変換を開始", lambda: self._start(False), style="Accent.TButton")
        self.start_button.pack(side="left")
        self._button(action, "選択RAWの画質を比較", lambda: self._start(True)).pack(side="left", padx=8)
        self.cancel_button = ttk.Button(action, text="中止", command=self._cancel, state="disabled")
        self.cancel_button.pack(side="left")
        ttk.Button(action, text="出力先を開く", command=self._open_output).pack(side="right")
        ttk.Button(action, text="使い方", command=self._help).pack(side="right", padx=8)
        ttk.Button(action, text="画質ガイド / F1", command=self._show_guide).pack(side="right")

        footer = ttk.Frame(outer)
        footer.pack(side="bottom", fill="x", pady=(8, 0))
        self.progress = ttk.Progressbar(footer, mode="determinate")
        self.progress.pack(fill="x", pady=(0, 4))
        ttk.Label(footer, textvariable=self.status, style="Muted.TLabel", wraplength=1040).pack(anchor="w")

        results_frame = ttk.Frame(outer)
        results_frame.pack(fill="both", expand=True)
        columns = ("file", "state", "resolution", "size", "ratio", "message")
        self.result_tree = ttk.Treeview(results_frame, columns=columns, show="headings", height=7)
        for key, label, width in zip(columns, ("ファイル", "状態", "出力解像度", "容量", "出力/入力", "結果"), (230, 80, 170, 100, 85, 370)):
            self.result_tree.heading(key, text=label)
            self.result_tree.column(key, width=width, minwidth=55, stretch=key in ("file", "message"))
        scrollbar = ttk.Scrollbar(results_frame, orient="vertical", command=self.result_tree.yview)
        horizontal = ttk.Scrollbar(results_frame, orient="horizontal", command=self.result_tree.xview)
        self.result_tree.configure(yscrollcommand=scrollbar.set, xscrollcommand=horizontal.set)
        self.result_tree.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        results_frame.columnconfigure(0, weight=1)
        results_frame.rowconfigure(0, weight=1)
        self.result_tree.bind("<Double-1>", self._show_result)
        self.result_details: dict[str, dict] = {}

    def _add_paths(self, paths) -> None:
        for raw in paths:
            path = Path(raw).expanduser().resolve()
            if path not in self.inputs:
                self.inputs.append(path)
                self.input_tree.insert("", "end", iid=str(path), values=(str(path),))
        self.status.set(f"入力 {len(self.inputs)} 件。フォルダー内のRAWは開始時に展開します。")

    def _choose_files(self) -> None:
        # Case-insensitive wildcard behavior differs between Tk/platforms; include all.
        paths = filedialog.askopenfilenames(title="RAW / DNGを追加", filetypes=(("すべてのファイル", "*"),))
        invalid = [Path(p).name for p in paths if Path(p).suffix.lower() not in RAW_EXTENSIONS | {".dng"}]
        if invalid:
            messagebox.showerror("RAW/DNGを選択", "対象外: " + ", ".join(invalid), parent=self.root)
        self._add_paths([p for p in paths if Path(p).suffix.lower() in RAW_EXTENSIONS | {".dng"}])

    def _choose_folder(self) -> None:
        path = filedialog.askdirectory(title="RAWが入ったフォルダー")
        if path:
            self._add_paths([path])

    def _remove_selected(self) -> None:
        for value in self.input_tree.selection():
            self.inputs.remove(Path(value))
            self.input_tree.delete(value)

    def _clear_inputs(self) -> None:
        self.inputs.clear()
        self.input_tree.delete(*self.input_tree.get_children())

    def _choose_output(self) -> None:
        path = filedialog.askdirectory(title="DNGの出力先")
        if path:
            self.output.set(path)

    def _choose_converter(self) -> None:
        path = filedialog.askopenfilename(title="Adobe DNG Converter実行ファイル") if os.name == "nt" else filedialog.askdirectory(title="Adobe DNG Converter.app（または実行ファイルは直接入力）")
        if path:
            self.converter.set(path)

    def _detect_converter(self) -> None:
        try:
            self.converter.set(str(resolve_converter()))
        except Exception as exc:
            messagebox.showerror("自動検出", str(exc), parent=self.root)

    def _selected_mode(self) -> Mode:
        return next(mode for mode, label in MODE_LABELS.items() if label == self.mode.get())

    def _quality_changed(self, _event=None) -> None:
        if self.quality.get() in QUALITY_PRESETS:
            self.distance.set(str(QUALITY_PRESETS[self.quality.get()]))

    def _distance_changed(self, *_args) -> None:
        try:
            value = float(self.distance.get())
            matching = next((key for key, distance in QUALITY_PRESETS.items() if abs(value - distance) < 1e-9), "カスタム")
            self.quality.set(matching)
        except ValueError:
            self.quality.set("カスタム")

    def _resize_changed(self, _event=None) -> None:
        if self.resize.get() != "原寸を維持" and self._selected_mode() not in (Mode.LOSSY_JXL, Mode.LOSSY_JPEG):
            self.mode.set(MODE_LABELS[Mode.LOSSY_JXL])
        self._settings_changed()

    def _settings_changed(self, _event=None) -> None:
        mode = self._selected_mode()
        jxl = mode == Mode.LOSSY_JXL
        if hasattr(self, "quality_widget"):
            self.quality_widget.configure(state="readonly" if jxl and not self.busy else "disabled")
            self.distance_widget.configure(state="normal" if jxl and not self.busy else "disabled")
            self.effort_widget.configure(state="normal" if jxl and not self.busy else "disabled")
            self.jxl_retry_widget.configure(state="normal" if jxl and not self.busy else "disabled")
            self.effort_7_button.configure(state="normal" if jxl and not self.busy else "disabled")
            fallback_allowed = mode in (Mode.LOSSY_JXL, Mode.LOSSLESS_JXL) and self.resize.get() == "原寸を維持"
            if not fallback_allowed:
                self.jxl_fallback.set(False)
            self.jxl_fallback_widget.configure(state="normal" if fallback_allowed and not self.busy else "disabled")
            self.mp_widget.configure(state="normal" if self.resize.get() == "画素数で指定（MP）" and not self.busy else "disabled")
            self.side_widget.configure(state="normal" if self.resize.get() == "長辺で指定（px）" and not self.busy else "disabled")
        if mode in (Mode.LOSSY_JXL, Mode.LOSSY_JPEG):
            self.hint.set("Linear DNGを出力します。縮小と非可逆圧縮は元に戻せません。Kelvin編集用のRAW/WBタグを検査します。JPEG XLはLightroom Classic 13 / Camera Raw 16以降を対象とします。")
        else:
            self.hint.set("原寸のRAW情報を優先するモードです。縮小する場合は画質指定JPEG XLを選択してください。入力RAWは削除しません。")
            if mode == Mode.LOSSLESS_JXL:
                self.hint.set(self.hint.get() + " このモードではGUIのeffort指定は適用されません。")

    def _prefer_effort7(self) -> None:
        self.effort.set("7")
        self.jxl_retry.set(True)

    def _collect_settings(self) -> Settings:
        settings = Settings(
            mode=self._selected_mode(), distance=float(self.distance.get()), effort=int(self.effort.get()),
            megapixels=float(self.megapixels.get()) if self.resize.get() == "画素数で指定（MP）" else None,
            long_edge=int(self.long_edge.get()) if self.resize.get() == "長辺で指定（px）" else None,
            preview={"なし": 0, "中サイズ": 1, "全画素": 2}[self.preview.get()],
            fast_load=self.fast_load.get(), embed_original=self.embed_original.get(), linear=self.linear.get(),
            compatibility=self.compatibility.get(), collision={"連番を付ける": "rename", "スキップ": "skip", "上書き": "overwrite"}[self.collision.get()],
            name_template=self.name_template.get(), timeout_seconds=float(self.timeout.get()),
            strict_wb=self.strict_wb.get(), preserve_mtime=self.preserve_mtime.get(),
            jxl_retry=self.jxl_retry.get(), jxl_fallback=self.jxl_fallback.get(),
        )
        settings.validate()
        return settings

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        for widget in self.widgets_to_disable:
            widget.configure(state="disabled" if busy else ("readonly" if isinstance(widget, ttk.Combobox) else "normal"))
        self.cancel_button.configure(state="normal" if busy else "disabled")
        self._settings_changed()

    def _start(self, compare: bool) -> None:
        if self.busy:
            return
        try:
            settings = self._collect_settings()
            converter = Converter(resolve_converter(self.converter.get().strip() or None))
            if not self.inputs:
                raise ValueError("入力RAWまたはフォルダーを追加してください。")
            if not self.output.get().strip():
                raise ValueError("出力先フォルダーを指定してください。")
            output = Path(self.output.get()).expanduser().resolve()
            inputs = list(self.inputs)
            distances = None
            if compare:
                selected = self.input_tree.selection()
                if len(selected) != 1 or not Path(selected[0]).is_file():
                    raise ValueError("入力一覧で比較用のRAWファイルを1つ選択してください。")
                inputs = [Path(selected[0])]
                value = simpledialog.askstring("圧縮画質を比較", "distanceをカンマで区切って指定\n小さいほど高画質。MP／長辺指定も各出力に適用します。", initialvalue="0.1,0.3,0.5", parent=self.root)
                if value is None:
                    return
                distances = list(dict.fromkeys(float(x.strip()) for x in value.split(",")))
                if not 1 <= len(distances) <= 12:
                    raise ValueError("比較する画質は1〜12種類です。")
                settings = replace(settings, mode=Mode.LOSSY_JXL)
                for distance in distances:
                    replace(settings, distance=distance).validate()
            if settings.collision == "overwrite" and not messagebox.askyesno("同名DNGを上書き", "出力先の同名DNGを置き換えます。入力RAWは対象外です。続行しますか？", parent=self.root):
                return
            recursive, include_dng = self.recursive.get(), self.include_dng.get()
            preserve_folders = self.preserve_folders.get()
            start_index = int(self.start_index.get())
            if start_index < 1:
                raise ValueError("開始番号は1以上です。")
            self._save_settings()
            self.cancel.clear()
            self.result_tree.delete(*self.result_tree.get_children())
            self.result_details.clear()
            self._set_busy(True)
            self.progress.configure(mode="indeterminate")
            self.progress.start(15)
            self.status.set("入力RAWを走査しています…")

            def worker() -> None:
                try:
                    # If output equals an input directory, do not exclude that entire input.
                    exclude = output if not any(p.is_dir() and p.resolve() == output for p in inputs) else None
                    sources = scan_inputs(inputs, recursive, include_dng, exclude, self.cancel)
                    if self.cancel.is_set():
                        self.events.put(("aborted", {"message": "走査を中止しました。"}))
                        return
                    if not sources:
                        raise ValueError("変換対象のRAWがありません。DNGは直接追加するか、DNGも対象の設定を有効にしてください。")
                    if distances is None:
                        jobs = plan_jobs(sources, output, settings, preserve_folders, start_index)
                    else:
                        jobs = []
                        for distance in distances:
                            tag = format(distance, ".8g").replace(".", "p")
                            folder = output / ("quality_d" + tag)
                            jobs.extend(plan_jobs(sources, folder, replace(settings, distance=distance), False, start_index))
                    self.events.put(("planned", {"jobs": jobs}))
                    run_batch(converter, jobs, output, self.cancel,
                              lambda kind, data: self.events.put((kind, data)))
                except Exception as exc:
                    self.events.put(("fatal", {"message": str(exc), "traceback": traceback.format_exc()}))
            self.worker = threading.Thread(target=worker, name="raw-to-dng-worker", daemon=True)
            self.worker.start()
        except Exception as exc:
            messagebox.showerror("変換設定", str(exc), parent=self.root)

    def _poll(self) -> None:
        try:
            while True:
                kind, data = self.events.get_nowait()
                if kind == "planned":
                    self.progress.stop()
                    self.progress.configure(mode="determinate", maximum=len(data["jobs"]), value=0)
                    for index, job in enumerate(data["jobs"]):
                        self.result_tree.insert("", "end", iid=str(index), values=(job.source.path.name, "待機", "", "", "", str(job.destination)))
                elif kind == "start":
                    self.result_tree.set(str(data["index"]), "state", "変換中")
                    self.result_tree.see(str(data["index"]))
                    self.status.set("変換中: " + Path(data["source"]).name)
                elif kind == "retry":
                    iid = str(data["index"])
                    self.result_tree.set(iid, "state", "再試行中")
                    self.result_tree.set(iid, "message", data["message"])
                    self.status.set("再試行中: " + Path(data["source"]).name + " / " + data["message"])
                elif kind == "result":
                    iid = str(data["index"])
                    info = data.get("dng") or {}
                    dims = f"{info['width']}×{info['height']} ({info['megapixels']:.2f} MP)" if info else ""
                    size = f"{data['output_bytes'] / 1_000_000:.2f} MB" if data.get("output_bytes") else ""
                    ratio = f"{data['size_ratio']:.1%}" if data.get("size_ratio") is not None and info else ""
                    state = {"ok": "完了", "error": "エラー", "skipped": "スキップ", "cancelled": "中止"}[data["status"]]
                    if data["status"] == "ok" and len(data.get("attempts", [])) > 1:
                        state = "完了（再試行）"
                    self.result_tree.item(iid, values=(Path(data["source"]).name, state, dims, size, ratio, data["message"].replace("\n", " ")))
                    self.result_details[iid] = data
                    self.progress.configure(value=data["index"] + 1)
                elif kind == "done":
                    self.last_report = Path(data["report"])
                    self._set_busy(False)
                    for iid in self.result_tree.get_children():
                        if self.result_tree.set(iid, "state") == "待機":
                            self.result_tree.set(iid, "state", "未処理")
                    self.status.set(f"完了 {data['ok']} / エラー {data['errors']} / スキップ {data['skipped']} / 未処理 {data['pending']}。詳細レポート: {self.last_report.name}")
                elif kind in ("fatal", "aborted"):
                    self.progress.stop()
                    self._set_busy(False)
                    self.status.set(data["message"])
                    if kind == "fatal":
                        messagebox.showerror("変換処理", data["message"], parent=self.root)
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def _cancel(self) -> None:
        self.cancel.set()
        self.cancel_button.configure(state="disabled")
        self.status.set("中止しています。未完了の一時DNGを破棄します…")

    def _show_result(self, _event=None) -> None:
        selection = self.result_tree.selection()
        if not selection or selection[0] not in self.result_details:
            return
        popup = tk.Toplevel(self.root)
        popup.title("変換結果・検査情報")
        popup.geometry("850x550")
        text = tk.Text(popup, wrap="word", padx=12, pady=12)
        text.pack(fill="both", expand=True)
        text.insert("1.0", json.dumps(self.result_details[selection[0]], ensure_ascii=False, indent=2))
        text.configure(state="disabled")

    def _open_output(self) -> None:
        try:
            path = Path(self.output.get()).expanduser()
            if not self.output.get().strip() or not path.is_dir():
                raise ValueError("出力先フォルダーがまだありません。")
            open_folder(path)
        except Exception as exc:
            messagebox.showerror("出力先", str(exc), parent=self.root)

    def _help(self) -> None:
        self._show_guide("usage")

    def _show_guide(self, page: str = "recommendations") -> None:
        if self.quality_guide is not None and self.quality_guide.window.winfo_exists():
            self.quality_guide.select(page)
        else:
            self.quality_guide = QualityGuide(self.root, self.font_family, page)

    def _load_settings(self) -> None:
        try:
            data = json.loads(config_path().read_text(encoding="utf-8"))
            for name in self._saved_names():
                if name in data:
                    getattr(self, name).set(data[name])
            if self.mode.get() not in MODE_LABELS.values():
                self.mode.set(MODE_LABELS[Mode.LOSSY_JXL])
        except (OSError, ValueError, TypeError, tk.TclError):
            pass
        if not self.converter.get():
            try:
                self.converter.set(str(resolve_converter()))
            except Exception:
                pass

    @staticmethod
    def _saved_names():
        return ("converter", "output", "mode", "quality", "distance", "effort", "resize", "megapixels", "long_edge", "preview",
                "compatibility", "collision", "name_template", "start_index", "timeout", "recursive", "preserve_folders",
                "include_dng", "fast_load", "embed_original", "linear", "strict_wb", "preserve_mtime",
                "jxl_retry", "jxl_fallback")

    def _save_settings(self) -> None:
        try:
            path = config_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps({name: getattr(self, name).get() for name in self._saved_names()}, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(path)
        except OSError:
            # Preference persistence is not allowed to stop a conversion.
            pass

    def _callback_error(self, exc_type, exc, tb) -> None:
        self.status.set("画面操作でエラーが発生しました。操作を継続できます。")
        messagebox.showerror("操作エラー", str(exc), parent=self.root)

    def _close(self) -> None:
        if self.busy:
            if not messagebox.askyesno("変換を中止して終了", "実行中の変換を中止して終了しますか？", parent=self.root):
                return
            self._cancel()
            self.status.set("終了準備中…")

            def finish() -> None:
                if self.worker and self.worker.is_alive():
                    self.root.after(100, finish)
                else:
                    self._save_settings()
                    self.root.destroy()
            self.root.after(100, finish)
        else:
            self._save_settings()
            self.root.destroy()


def launch(initial_inputs: list[str] | None = None) -> None:
    root = tk.Tk()
    Application(root, initial_inputs)
    root.mainloop()

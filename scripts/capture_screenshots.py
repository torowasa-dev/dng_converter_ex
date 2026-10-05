#!/usr/bin/env python3
"""Capture actual Tkinter windows and check quality-guide navigation.

Requires a display and Pillow. No Adobe process or RAW conversion is used.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import tkinter as tk
from tkinter import ttk
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from raw_to_dng import __version__
from raw_to_dng.core import MODE_LABELS, Mode
from raw_to_dng.gui import Application
from raw_to_dng.help_content import HELP_PAGES


def refresh(root: tk.Tk) -> None:
    root.update_idletasks()
    root.update()
    root.update_idletasks()
    root.update()


def capture(window: tk.Toplevel | tk.Tk, destination: Path) -> None:
    from PIL import ImageGrab
    window.lift()
    refresh(window)
    x, y = window.winfo_rootx(), window.winfo_rooty()
    bbox = (x, y, x + window.winfo_width(), y + window.winfo_height())
    kwargs = {"xdisplay": os.environ.get("DISPLAY")} if sys.platform.startswith("linux") else {}
    ImageGrab.grab(bbox=bbox, **kwargs).save(destination)


def descendants(widget):
    for child in widget.winfo_children():
        yield child
        yield from descendants(child)


def main() -> int:
    screenshots = ROOT / "docs" / "screenshots"
    screenshots.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="raw-to-dng-gui-") as temporary:
        settings_file = Path(temporary) / "settings.json"
        with patch("raw_to_dng.gui.config_path", return_value=settings_file), \
             patch("raw_to_dng.gui.resolve_converter", side_effect=FileNotFoundError("Screenshot: no converter")):
            root = tk.Tk()
            errors: list[str] = []
            try:
                app = Application(root)
                root.report_callback_exception = lambda *args: errors.append(str(args[1]))
                root.geometry("1280x960+0+0")
                app.output.set("/写真/DNG")
                app._add_paths(["/写真/RAW"])
                app.resize.set("画素数で指定（MP）")
                app._resize_changed()
                app.effort.set("7")
                app.preview.set("なし")
                app.fast_load.set(False)
                app.status.set("画面例：サンプルの入力パスを表示。変換は実行していません。")
                refresh(root)
                before = app._collect_settings()
                capture(root, screenshots / "main.png")
                app.notebook.select(1)
                capture(root, screenshots / "advanced.png")
                assert 'disabled' in app.jxl_fallback_widget.state(), 'Resized outputs must disable fallback'
                app.resize.set('原寸を維持')
                app._settings_changed()
                app.jxl_fallback.set(True)
                app.effort.set('9')
                app.effort_7_button.invoke()
                assert app.effort.get() == '7' and app.jxl_retry.get(), 'Compatibility preset failed'
                assert app.distance.get() == '0.1' and app.resize.get() == '原寸を維持'
                app._save_settings()
                saved = json.loads(settings_file.read_text(encoding='utf-8'))
                assert saved['jxl_retry'] and saved['jxl_fallback'], 'Recovery preferences were not saved'
                app.jxl_retry.set(False)
                app.jxl_fallback.set(False)
                app._load_settings()
                assert app.jxl_retry.get() and app.jxl_fallback.get(), 'Recovery preferences were not restored'
                settings_file.unlink()
                app.mode.set(MODE_LABELS[Mode.LOSSLESS_JXL])
                app._settings_changed()
                assert 'disabled' in app.effort_widget.state(), 'Ignored effort should be disabled'
                assert 'disabled' not in app.jxl_fallback_widget.state(), 'Lossless JXL fallback should be selectable'
                app.mode.set(MODE_LABELS[Mode.LOSSY_JXL])
                app.resize.set('画素数で指定（MP）')
                app._settings_changed()
                assert not app.jxl_fallback.get(), 'Resize must clear fallback'
                app._set_busy(True)
                assert 'disabled' in app.jxl_retry_widget.state(), 'Recovery controls changed while busy'
                app._set_busy(False)
                assert app._collect_settings() == before, 'GUI recovery checks changed conversion settings'
                app.notebook.select(0)

                app.result_tree.insert('', 'end', iid='0', values=('sample.NEF', '変換中', '', '', '', ''))
                app.events.put(('retry', {'index': 0, 'source': 'sample.NEF', 'message': 'effort 9→7で再試行'}))
                app._poll()
                assert app.result_tree.set('0', 'state') == '再試行中', 'Retry progress was not shown'
                app.events.put(('result', {'index': 0, 'source': 'sample.NEF', 'status': 'ok',
                                          'message': 'effort 9→7で再試行成功', 'attempts': [{}, {}]}))
                app._poll()
                assert app.result_tree.set('0', 'state') == '完了（再試行）', 'Retry completion was not shown'
                app.result_tree.delete('0')

                root.focus_force()
                refresh(root)
                root.event_generate("<F1>")
                refresh(root)
                guide = app.quality_guide
                assert guide is not None, "F1 did not open the quality guide"
                guide.window.geometry("1120x980+0+0")
                refresh(root)
                capture(guide.window, screenshots / "quality-guide.png")
                for page in HELP_PAGES:
                    guide.select(page.key)
                    refresh(root)
                    canvas = guide.canvases[page.key]
                    assert canvas.winfo_ismapped(), f"Page is not visible: {page.key}"
                    assert canvas.bbox("all"), f"Page is empty: {page.key}"
                    # A short window makes overflow certain and verifies real wheel scrolling.
                    guide.window.geometry("900x620+0+0")
                    refresh(root)
                    bounds = canvas.bbox("all")
                    if bounds[3] > canvas.winfo_height():
                        canvas.yview_moveto(0)
                        heading = guide.page_headings[page.key]
                        heading.event_generate("<MouseWheel>", delta=-120)
                        refresh(root)
                        assert canvas.yview()[0] > 0, f"Mouse wheel did not scroll: {page.key}"
                    canvas.yview_moveto(0)
                guide.window.geometry("1120x980+0+0")
                guide.select("parameters")
                capture(guide.window, screenshots / "parameters.png")
                guide.select("recovery")
                capture(guide.window, screenshots / "recovery-guide.png")
                assert app._collect_settings() == before, "Guide changed conversion settings"

                app._show_guide("adobe")
                assert app.quality_guide is guide, "Guide should reuse its existing window"
                app._help()
                assert guide.notebook.select() == str(guide.pages["usage"])
                guide.window.destroy()
                app._set_busy(True)
                guide_button = next(widget for widget in descendants(root)
                                    if isinstance(widget, ttk.Button) and widget.cget("text") == "画質ガイド / F1")
                assert "disabled" not in guide_button.state(), "Guide should be readable during conversion"
                guide_button.invoke()
                refresh(root)
                assert app.quality_guide.window.winfo_exists(), "Guide did not reopen"
                app.quality_guide.window.focus_force()
                refresh(root)
                app.quality_guide.window.event_generate("<Escape>")
                refresh(root)
                assert not app.quality_guide.window.winfo_exists(), "Escape did not close the guide"
                app._set_busy(False)
                assert not errors, errors
                assert app.progress.winfo_ismapped(), "Progress bar is not visible"
                assert not settings_file.exists(), "Capture left application preferences"
                print(json.dumps({
                    "app_version": __version__,
                    "font": app.font_family,
                    "guide_pages_checked": len(HELP_PAGES),
                    "screenshots": [str(path.relative_to(ROOT)) for path in sorted(screenshots.glob("*.png"))],
                    "checks": ["F1", "all pages", "wheel scrolling", "window reuse", "reopen", "Escape",
                               "help entry", "available while busy", "conversion settings unchanged", "no user preferences changed"],
                    "recovery_checks": ["effort 7 preset", "fallback disabled and cleared for resize", "lossless JXL effort disabled",
                                        "preferences save and restore", "controls disabled while busy", "retry progress", "retry completion"],
                    "adobe_conversion_run": False,
                }, ensure_ascii=False, indent=2))
            finally:
                root.destroy()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

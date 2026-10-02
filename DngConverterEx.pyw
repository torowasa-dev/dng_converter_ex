"""Windowed Windows entry point (double-click when Python is installed)."""
from raw_to_dng.cli import main

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        from tkinter import Tk, messagebox
        root = Tk()
        root.withdraw()
        messagebox.showerror("DngConverterEx — 起動エラー", str(exc))
        root.destroy()

"""GUI entry point plus optional automation and DNG inspection commands."""
from __future__ import annotations

import argparse
import json
import platform
import signal
import sys
import threading
from dataclasses import replace
from pathlib import Path

from . import __version__
from .core import (
    COMPATIBILITIES, Converter, Mode, Settings, converter_version,
    plan_jobs, resolve_converter, run_batch, scan_inputs,
)
from .dng import inspect_dng


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("inputs", nargs="+", type=Path, help="RAW/DNGファイル、またはRAWフォルダー")
    parser.add_argument("--output", "-o", required=True, type=Path)
    parser.add_argument("--converter", type=Path, help="Adobe DNG Converterのexeまたはapp")
    parser.add_argument("--mode", choices=[mode.value for mode in Mode], default=Mode.LOSSY_JXL.value)
    parser.add_argument("--distance", type=float, default=0.1, help="JPEG XL画質: 0〜6、小さいほど高画質")
    parser.add_argument("--effort", type=int, default=7)
    parser.add_argument("--priority", choices=("normal", "low", "idle"), default="normal", help="変換プロセスの優先度")
    parser.add_argument("--cpu-limit", type=int, default=100, help="全論理CPUに対する使用率上限の目安（1〜100%%）")
    parser.add_argument("--no-jxl-retry", action="store_true", help="該当assert時のeffort段階再試行（9→8→7）を無効化")
    parser.add_argument("--jxl-fallback", action="store_true", help="該当assert時にロスレスJPEGへ変更（原寸指定時のみ）")
    resolution = parser.add_mutually_exclusive_group()
    resolution.add_argument("--megapixels", "--mp", type=float, help="上限MP。24は2400万画素")
    resolution.add_argument("--long-edge", type=int, help="上限長辺px")
    parser.add_argument("--preview", choices=(0, 1, 2), type=int, default=1)
    parser.add_argument("--embed-original", action="store_true")
    parser.add_argument("--no-fast-load", action="store_true")
    parser.add_argument("--linear", action="store_true")
    parser.add_argument("--compatibility", choices=COMPATIBILITIES, default="13.2")
    parser.add_argument("--collision", choices=("rename", "skip", "overwrite"), default="rename")
    parser.add_argument("--name-template", default="{stem}")
    parser.add_argument("--start-index", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=1800)
    parser.add_argument("--no-recursive", action="store_true")
    parser.add_argument("--flatten", action="store_true")
    parser.add_argument("--include-dng", action="store_true")
    parser.add_argument("--allow-missing-wb", action="store_true", help="モノクロRAW等。Kelvin編集は保証しない")
    parser.add_argument("--no-preserve-mtime", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="計画とAdobe引数のみ出力。変換なし")


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="DngConverterEx — Adobe公式コンバーターのGUI／CLIフロントエンド")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    gui = commands.add_parser("gui", help="GUIを起動（引数なしでもGUI起動）")
    gui.add_argument("inputs", nargs="*")
    convert = commands.add_parser("convert", help="一括変換")
    _common(convert)
    compare = commands.add_parser("compare", help="同じRAWを複数のJPEG XL画質で変換")
    _common(compare)
    compare.add_argument("--distances", default="0.1,0.5,1,2")
    inspect = commands.add_parser("inspect", help="Adobe不要のDNG構造・WB情報検査")
    inspect.add_argument("file", type=Path)
    doctor = commands.add_parser("doctor", help="Adobe実行ファイルとバージョンを確認")
    doctor.add_argument("--converter", type=Path)
    return parser


def _settings(args) -> Settings:
    result = Settings(mode=Mode(args.mode), distance=args.distance, effort=args.effort,
                      megapixels=args.megapixels, long_edge=args.long_edge,
                      preview=args.preview, fast_load=not args.no_fast_load,
                      embed_original=args.embed_original, linear=args.linear,
                      compatibility=args.compatibility, collision=args.collision,
                      name_template=args.name_template, timeout_seconds=args.timeout,
                      strict_wb=not args.allow_missing_wb, preserve_mtime=not args.no_preserve_mtime,
                      jxl_retry=not args.no_jxl_retry, jxl_fallback=args.jxl_fallback,
                      priority=args.priority, cpu_limit=args.cpu_limit)
    result.validate()
    return result


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    # Dragging RAW files onto a launcher also opens GUI with those inputs.
    if not argv or argv[0] not in {"gui", "convert", "compare", "inspect", "doctor"} and not argv[0].startswith("-"):
        from .gui import launch
        launch(argv)
        return 0
    parser = make_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "gui":
            from .gui import launch
            launch(args.inputs)
            return 0
        if args.command == "inspect":
            print(json.dumps(inspect_dng(args.file).to_dict(), ensure_ascii=False, indent=2))
            return 0
        if args.command == "doctor":
            converter = resolve_converter(args.converter)
            print(json.dumps({"app_version": __version__, "python": platform.python_version(),
                              "platform": platform.platform(), "converter": str(converter),
                              "converter_version": converter_version(converter),
                              "adobe_supported_platforms": "Windows / macOS",
                              "target_for_jxl": "Adobe DNG Converter 16+, Lightroom Classic 13+, Camera Raw 16+",
                              "lightroom_verified_here": False}, ensure_ascii=False, indent=2))
            return 0
        settings = _settings(args)
        output = args.output.expanduser().resolve()
        exclude = output if not any(p.is_dir() and p.resolve() == output for p in args.inputs) else None
        sources = scan_inputs(args.inputs, not args.no_recursive, args.include_dng, exclude)
        if not sources:
            raise ValueError("変換対象のRAWがありません。")
        if args.command == "compare":
            distances = list(dict.fromkeys(float(value.strip()) for value in args.distances.split(",")))
            if not 1 <= len(distances) <= 12:
                raise ValueError("比較するdistanceは1〜12種類です。")
            jobs = []
            for distance in distances:
                tag = format(distance, ".8g").replace(".", "p")
                settings_for_quality = replace(settings, mode=Mode.LOSSY_JXL, distance=distance)
                jobs.extend(plan_jobs(sources, output / ("quality_d" + tag), settings_for_quality, not args.flatten, args.start_index))
        else:
            jobs = plan_jobs(sources, output, settings, not args.flatten, args.start_index)
        if args.dry_run:
            print(json.dumps([{"input": str(job.source.path), "output": str(job.destination),
                               "settings": job.settings.to_dict(), "adobe_flags": job.settings.adobe_flags()}
                              for job in jobs], ensure_ascii=False, indent=2))
            return 0
        converter = Converter(resolve_converter(args.converter))
        cancel = threading.Event()
        previous_handler = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, lambda *_args: cancel.set())
        def event(kind: str, data: dict) -> None:
            if kind == "result":
                print(f"[{data['status']}] {Path(data['source']).name}: {data['message']}", flush=True)
            elif kind == "retry":
                print(f"[再試行 {data['attempt']}] {Path(data['source']).name}: {data['message']}", flush=True)
            elif kind == "done":
                print(f"report: {data['report']}", flush=True)
        try:
            results, _report = run_batch(converter, jobs, output, cancel, event)
        finally:
            signal.signal(signal.SIGINT, previous_handler)
        return 130 if cancel.is_set() else (1 if any(r.status == "error" for r in results) else 0)
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 2

"""Adobe conversion jobs, collision-safe output publication and batch reports."""
from __future__ import annotations

import errno
import json
import math
import os
import plistlib
import re
import shutil
import string
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable

from . import __version__
from .dng import DngError, DngInfo, inspect_dng


RAW_EXTENSIONS = frozenset(".3fr .arw .bay .cap .cr2 .cr3 .crw .dcs .dcr .drf .eip .erf .fff .iiq .k25 .kdc .mdc .mef .mos .mrw .nef .nrw .obm .orf .pef .ptx .pxn .r3d .raf .raw .rw2 .rwl .sr2 .srf .srw .sti .x3f".split())
ADOBE_DOWNLOAD_URL = "https://helpx.adobe.com/camera-raw/desktop/dng-and-file-formats/adobe-dng-converter.html"
COMPATIBILITIES = ("2.4", "4.1", "4.6", "5.4", "6.6", "7.1", "11.2", "12.4", "13.2", "14.0", "15.3")


class ConversionError(RuntimeError):
    pass


class Cancelled(ConversionError):
    pass


class AdobeProcessError(ConversionError):
    """A nonzero Adobe exit, distinct from validation/cancellation failures."""

    def __init__(self, exit_code: int, output: str):
        self.exit_code = exit_code
        self.output = output
        super().__init__(f"Adobe変換エラー: exit={exit_code}\n{output}")


def is_jxl_histogram_assert(output: str) -> bool:
    """Match the reported assertion, not unrelated Adobe/GPU failures."""
    return re.search(r"enc_ans\.cc[^\r\n]*\bJXL_DASSERT\b[^\r\n]*\bn\s*<=\s*255\b", output) is not None


class Mode(str, Enum):
    LOSSY_JXL = "lossy-jxl"
    LOSSLESS_JPEG = "lossless-jpeg"
    LOSSLESS_JXL = "lossless-jxl"
    LOSSY_JPEG = "lossy-jpeg"
    UNCOMPRESSED = "uncompressed"


MODE_LABELS = {
    Mode.LOSSY_JXL: "画質指定：JPEG XL（Linear DNG）",
    Mode.LOSSLESS_JPEG: "RAW保持優先：ロスレスJPEG圧縮DNG",
    Mode.LOSSLESS_JXL: "RAW保持優先：ロスレスJPEG XL圧縮DNG",
    Mode.LOSSY_JPEG: "旧版互換：非可逆JPEG圧縮DNG（画質固定）",
    Mode.UNCOMPRESSED: "無圧縮DNG",
}


@dataclass(frozen=True)
class Settings:
    mode: Mode = Mode.LOSSY_JXL
    distance: float = 0.1
    effort: int = 7
    megapixels: float | None = None
    long_edge: int | None = None
    preview: int = 1
    fast_load: bool = True
    embed_original: bool = False
    linear: bool = False
    compatibility: str = "13.2"
    collision: str = "rename"
    name_template: str = "{stem}"
    timeout_seconds: float = 1800.0
    strict_wb: bool = True
    preserve_mtime: bool = True
    jxl_retry: bool = True
    jxl_fallback: bool = False

    @property
    def pixel_limit(self) -> int | None:
        return round(self.megapixels * 1_000_000) if self.megapixels is not None else None

    def validate(self) -> None:
        if not isinstance(self.mode, Mode):
            raise ValueError("圧縮方式が不正です。")
        if not math.isfinite(self.distance) or not 0 <= self.distance <= 6:
            raise ValueError("JPEG XL distanceは0〜6です。小さいほど高画質です。")
        if not isinstance(self.effort, int) or not 1 <= self.effort <= 9:
            raise ValueError("JPEG XL effortは1〜9です。大きいほど処理に時間をかけます。")
        if self.megapixels is not None and (not math.isfinite(self.megapixels) or not 0.001 <= self.megapixels <= 500):
            raise ValueError("出力画素数は0.001〜500 MPです。")
        if self.long_edge is not None and (not isinstance(self.long_edge, int) or not 32 <= self.long_edge <= 65000):
            raise ValueError("長辺は32〜65000 pxです。")
        if self.megapixels is not None and self.long_edge is not None:
            raise ValueError("MP指定と長辺指定はどちらか一方を選んでください。")
        if (self.megapixels is not None or self.long_edge is not None) and self.mode not in (Mode.LOSSY_JXL, Mode.LOSSY_JPEG):
            raise ValueError("縮小には画質指定JPEG XL、または非可逆JPEGのモードを選んでください。")
        if not isinstance(self.jxl_retry, bool) or not isinstance(self.jxl_fallback, bool):
            raise ValueError("JPEG XLの再試行・代替出力はON/OFFで指定してください。")
        if self.jxl_fallback and (self.megapixels is not None or self.long_edge is not None):
            raise ValueError("ロスレスJPEGへの代替出力は原寸指定時だけ使用できます。縮小指定を維持する場合はOFFにしてください。")
        if self.preview not in (0, 1, 2):
            raise ValueError("プレビューは0（なし）・1（中）・2（全画素）です。")
        if self.compatibility not in COMPATIBILITIES:
            raise ValueError("Camera Raw互換設定が不正です。")
        if self.mode == Mode.LOSSY_JPEG and tuple(map(int, self.compatibility.split("."))) < (7, 1):
            raise ValueError("非可逆JPEG DNGはCamera Raw 7.1以降を指定してください。")
        if self.collision not in ("rename", "skip", "overwrite"):
            raise ValueError("同名ファイルの処理が不正です。")
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("タイムアウトは正の秒数です。")
        if not self.name_template or len(self.name_template) > 200:
            raise ValueError("ファイル名テンプレートが空、または長すぎます。")
        try:
            for _, field_name, spec, conversion in string.Formatter().parse(self.name_template):
                if field_name is not None and field_name not in ("stem", "ext", "index", "date"):
                    raise ValueError("名前に使える項目は{stem}・{ext}・{index}・{date}です。")
                if conversion:
                    raise ValueError("ファイル名の変換指定は使えません。")
                if spec and (len(spec) > 8 or any(ch not in "0123456789d" for ch in spec)):
                    raise ValueError("名前の書式指定は短い数値書式のみです（例: {index:04d}）。")
                if spec and (not re.fullmatch(r"\d{1,3}d?", spec) or int(spec.rstrip("d")) > 220):
                    raise ValueError("ファイル名の数値書式は220文字以内です。")
            validate_basename(self.name_template.format(stem="photo", ext="NEF", index=1, date="20261001"))
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError(f"ファイル名テンプレートが不正です: {exc}") from exc

    def adobe_flags(self) -> list[str]:
        self.validate()
        flags: list[str] = []
        if self.mode == Mode.LOSSY_JXL:
            flags += ["-dng1.7", "-lossy", "-jxl", "-jxl_distance", format(self.distance, ".8g"), "-jxl_effort", str(self.effort)]
        elif self.mode == Mode.LOSSLESS_JXL:
            flags += ["-losslessJXL"]
        elif self.mode == Mode.LOSSY_JPEG:
            flags += ["-cr" + self.compatibility, "-dng1.6", "-lossy"]
        else:
            flags += ["-u" if self.mode == Mode.UNCOMPRESSED else "-c", "-cr" + self.compatibility]
        if self.linear and self.mode not in (Mode.LOSSY_JXL, Mode.LOSSY_JPEG):
            flags.append("-l")
        flags.append("-p" + str(self.preview))
        if self.fast_load:
            flags.append("-fl")
        if self.embed_original:
            flags.append("-e")
        if self.pixel_limit is not None:
            # Adobe's -count uses PIXELS, not MP. 24 MP must be 24000000.
            flags += ["-count", str(self.pixel_limit)]
        if self.long_edge is not None:
            flags += ["-side", str(self.long_edge)]
        return flags

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["mode"] = self.mode.value
        result["adobe_pixel_limit"] = self.pixel_limit
        result["adobe_jxl_effort"] = self.effort if self.mode == Mode.LOSSY_JXL else None
        result["adobe_jxl_distance"] = self.distance if self.mode == Mode.LOSSY_JXL else None
        return result


def validate_basename(value: str) -> str:
    if not value or value in (".", "..") or value[-1] in (" ", ".") or len(value) > 220:
        raise ValueError("出力ファイル名が不正です。")
    if any(ch in value for ch in '/\\<>:"|?*') or any(ord(ch) < 32 for ch in value):
        raise ValueError("出力ファイル名に使えない文字が含まれています。")
    device = value.split(".", 1)[0].upper()
    if device in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
        raise ValueError("Windowsの予約名は使用できません。")
    return value


def converter_version(executable: Path) -> str | None:
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes
            api = ctypes.WinDLL("version", use_last_error=True)
            api.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
            api.GetFileVersionInfoSizeW.restype = wintypes.DWORD
            api.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
            api.VerQueryValueW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.UINT)]
            size = api.GetFileVersionInfoSizeW(str(executable), None)
            if not size:
                return None
            block = ctypes.create_string_buffer(size)
            if not api.GetFileVersionInfoW(str(executable), 0, size, block):
                return None
            pointer, length = ctypes.c_void_p(), wintypes.UINT()
            if not api.VerQueryValueW(block, "\\", ctypes.byref(pointer), ctypes.byref(length)) or length.value < 52:
                return None
            fields = ctypes.cast(pointer, ctypes.POINTER(wintypes.DWORD))
            ms, ls = fields[2], fields[3]
            return f"{ms >> 16}.{ms & 65535}.{ls >> 16}.{ls & 65535}"
        except (OSError, ValueError, AttributeError):
            return None
    if ".app" in str(executable):
        try:
            info = plistlib.loads((executable.parent.parent / "Info.plist").read_bytes())
            return info.get("CFBundleShortVersionString") or info.get("CFBundleVersion")
        except (OSError, ValueError, plistlib.InvalidFileException):
            return None
    return None


def resolve_converter(explicit: str | Path | None = None) -> Path:
    if explicit:
        candidates = [Path(explicit).expanduser()]
    else:
        candidates = []
        if os.environ.get("ADOBE_DNG_CONVERTER"):
            candidates.append(Path(os.environ["ADOBE_DNG_CONVERTER"]))
        for variable in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
            if os.environ.get(variable):
                base = Path(os.environ[variable])
                candidates += [base / "Adobe" / "Adobe DNG Converter" / "Adobe DNG Converter.exe",
                               base / "Adobe" / "Adobe DNG Converter.exe", base / "Adobe DNG Converter.exe"]
        candidates += [Path("C:/Program Files/Adobe/Adobe DNG Converter/Adobe DNG Converter.exe"),
                       Path("/Applications/Adobe DNG Converter.app"),
                       Path.home() / "Applications/Adobe DNG Converter.app"]
    for candidate in candidates:
        if candidate.suffix.lower() == ".app":
            candidate = candidate / "Contents" / "MacOS" / "Adobe DNG Converter"
        if candidate.is_file():
            return candidate.resolve()
    raise ConversionError("Adobe DNG Converterが見つかりません。インストール後に実行ファイルを指定してください。")


@dataclass(frozen=True)
class Source:
    path: Path
    relative_parent: Path = Path(".")


def scan_inputs(inputs: Iterable[str | Path], recursive: bool = True, include_dng: bool = False,
                exclude_directory: Path | None = None, cancel: threading.Event | None = None) -> list[Source]:
    sources: list[Source] = []
    seen: set[Path] = set()
    accepted = RAW_EXTENSIONS | ({".dng"} if include_dng else set())
    excluded = exclude_directory.resolve() if exclude_directory else None
    for value in inputs:
        if cancel and cancel.is_set():
            break
        root = Path(value).expanduser().resolve()
        if root.is_file():
            if root.suffix.lower() not in RAW_EXTENSIONS | {".dng"}:
                raise ValueError(f"RAW/DNG以外のファイルです: {root.name}")
            if root not in seen:
                sources.append(Source(root))
                seen.add(root)
        elif root.is_dir():
            paths = root.rglob("*") if recursive else root.glob("*")
            for path in paths:
                if cancel and cancel.is_set():
                    break
                if path.is_file() and path.suffix.lower() in accepted:
                    absolute = path.resolve()
                    if excluded and absolute.is_relative_to(excluded):
                        continue
                    if absolute not in seen:
                        sources.append(Source(absolute, path.parent.relative_to(root)))
                        seen.add(absolute)
        else:
            raise ValueError(f"入力が見つかりません: {root}")
    sources.sort(key=lambda item: str(item.path).casefold())
    return sources


@dataclass(frozen=True)
class Job:
    source: Source
    destination: Path
    settings: Settings


def plan_jobs(sources: Iterable[Source], output: str | Path, settings: Settings,
              preserve_folders: bool = True, start_index: int = 1) -> list[Job]:
    settings.validate()
    if start_index < 1:
        raise ValueError("開始番号は1以上です。")
    source_list = list(sources)
    output = Path(output).expanduser().resolve()
    input_paths = {source.path.resolve() for source in source_list}
    destinations: set[str] = set()
    jobs: list[Job] = []
    for index, source in enumerate(source_list, start_index):
        name = settings.name_template.format(
            stem=source.path.stem, ext=source.path.suffix[1:].upper(), index=index,
            date=datetime.fromtimestamp(source.path.stat().st_mtime).strftime("%Y%m%d"))
        name = validate_basename(name)
        destination = output / (source.relative_parent if preserve_folders else Path(".")) / (name + ".dng")
        if not destination.resolve().is_relative_to(output):
            raise ValueError("出力先が指定フォルダーの外になります。")
        if destination.resolve() in input_paths:
            raise ValueError("入力DNGと同じ場所には出力できません。別の出力フォルダーを指定してください。")
        counter = 1
        original = destination
        # Even overwrite never overwrites another job in the same batch.
        while str(destination).casefold() in destinations:
            destination = original.with_name(f"{original.stem}_{counter:03d}.dng")
            counter += 1
        destinations.add(str(destination).casefold())
        jobs.append(Job(source, destination, settings))
    return jobs


def verify_output(info: DngInfo, settings: Settings) -> None:
    expected = {
        Mode.LOSSY_JXL: {52546}, Mode.LOSSLESS_JXL: {52546},
        Mode.LOSSY_JPEG: {34892}, Mode.LOSSLESS_JPEG: {7, 8, 32946},
        Mode.UNCOMPRESSED: {1},
    }[settings.mode]
    if info.compression not in expected:
        raise ConversionError(f"指定した圧縮方式と出力が一致しません: {info.compression_name}。旧版Converterが指定を無視した可能性があります。")
    if settings.mode == Mode.LOSSY_JXL and (info.raw_kind != "Linear RAW" or min(info.bits_per_sample) < 16):
        raise ConversionError("画質指定JPEG XLが16bit以上のLinear RAWとして出力されませんでした。")
    if settings.mode in (Mode.LOSSY_JXL, Mode.LOSSLESS_JXL):
        if tuple(map(int, info.dng_version.split("."))) < (1, 7):
            raise ConversionError("JPEG XL出力に必要なDNG 1.7以降になっていません。")
    if settings.strict_wb and not info.wb_metadata_ready:
        raise ConversionError("Kelvin編集に必要な色行列／撮影時WB情報を確認できません。モノクロRAWは詳細設定でWB必須を解除してください。")
    if settings.embed_original and not info.has_original_raw:
        raise ConversionError("指定したオリジナルRAW埋め込みを確認できません。")
    if settings.pixel_limit is not None:
        # Small allowance for Adobe's integer-size rounding, not for an ignored resize flag.
        allowance = max(4, 2 * (info.width + info.height))
        if info.width * info.height > settings.pixel_limit + allowance:
            raise ConversionError(f"出力が指定画素数を超えています: {info.megapixels:.3f} MP。縮小指定が反映されていない可能性があります。")
    if settings.long_edge is not None and max(info.width, info.height) > settings.long_edge + 2:
        raise ConversionError("出力が指定長辺を超えています。縮小指定が反映されていない可能性があります。")


def _publish_without_overwrite(stage: Path, target: Path) -> None:
    if os.name == "nt":
        # Windows rename fails if target exists, unlike POSIX rename.
        os.rename(stage, target)
        return
    try:
        os.link(stage, target)
        stage.unlink()
    except FileExistsError:
        raise
    except OSError as exc:
        if exc.errno not in (errno.EPERM, errno.ENOTSUP, errno.EXDEV, errno.ENOSYS):
            raise
        # Filesystems without hard links: exclusive creation still prevents overwrite.
        with target.open("xb") as out:
            try:
                with stage.open("rb") as incoming:
                    shutil.copyfileobj(incoming, out, 1024 * 1024)
                out.flush()
                os.fsync(out.fileno())
            except BaseException:
                out.close()
                target.unlink(missing_ok=True)
                raise
        stage.unlink()


def publish(stage: Path, target: Path, policy: str) -> Path | None:
    if policy == "overwrite":
        os.replace(stage, target)
        return target
    candidate, index = target, 1
    while True:
        try:
            _publish_without_overwrite(stage, candidate)
            return candidate
        except FileExistsError:
            if policy == "skip":
                return None
            candidate = target.with_name(f"{target.stem}_{index:03d}{target.suffix}")
            index += 1


@dataclass
class Attempt:
    number: int
    reason: str
    settings: dict[str, Any]
    command: list[str] = field(default_factory=list)
    status: str = "error"
    exit_code: int | None = None
    log: str = ""
    message: str = ""
    elapsed_seconds: float = 0.0


@dataclass
class Result:
    source: str
    destination: str
    status: str
    message: str = ""
    source_bytes: int = 0
    output_bytes: int = 0
    elapsed_seconds: float = 0.0
    command: list[str] = field(default_factory=list)
    dng: dict[str, Any] | None = None
    requested_settings: dict[str, Any] | None = None
    effective_settings: dict[str, Any] | None = None
    attempts: list[Attempt] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["size_ratio"] = self.output_bytes / self.source_bytes if self.source_bytes else None
        return result


class Converter:
    def __init__(self, executable: str | Path):
        self.executable = resolve_converter(executable)
        self.version = converter_version(self.executable)

    @staticmethod
    def _stop(process: subprocess.Popen) -> None:
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    @staticmethod
    def _check_running(cancel: threading.Event, started: float, timeout: float) -> None:
        if cancel.is_set():
            raise Cancelled("変換を中止しました。")
        if time.monotonic() - started >= timeout:
            raise ConversionError(f"タイムアウト（{timeout:g}秒。再試行を含む）。")

    @staticmethod
    def _check_source(job: Job, source_stat: os.stat_result) -> None:
        current = job.source.path.stat()
        if (source_stat.st_size, source_stat.st_mtime_ns) != (current.st_size, current.st_mtime_ns):
            raise ConversionError("変換中に入力ファイルが変更されたため、出力を採用しません。")

    def _attempt(self, job: Job, settings: Settings, cancel: threading.Event,
                 started: float, source_stat: os.stat_result, result: Result,
                 reason: str) -> None:
        attempt_started = time.monotonic()
        attempt = Attempt(len(result.attempts) + 1, reason, settings.to_dict())
        result.attempts.append(attempt)
        try:
            self._check_running(cancel, started, job.settings.timeout_seconds)
            self._check_source(job, source_stat)
            # Each attempt has a fresh process, log and output directory. A failed
            # Adobe run may leave a partial DNG that must never be reused.
            with tempfile.TemporaryDirectory(prefix=".raw-to-dng-", dir=job.destination.parent) as temporary:
                stage = Path(temporary) / "converted.dng"
                attempt.command = [str(self.executable), *settings.adobe_flags(), "-d", temporary,
                                   "-o", stage.name, str(job.source.path)]
                result.command = attempt.command.copy()
                with tempfile.TemporaryFile() as log:
                    kwargs: dict[str, Any] = {}
                    if os.name == "nt":
                        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
                    process = None
                    try:
                        process = subprocess.Popen(attempt.command, stdout=log, stderr=subprocess.STDOUT,
                                                   stdin=subprocess.DEVNULL, shell=False, **kwargs)
                        while process.poll() is None:
                            cancel.wait(0.15)
                            self._check_running(cancel, started, job.settings.timeout_seconds)
                    except BaseException:
                        if process is not None:
                            self._stop(process)
                        raise
                    finally:
                        if process is not None:
                            attempt.exit_code = process.returncode
                        log.seek(0, os.SEEK_END)
                        log.seek(max(0, log.tell() - 16000))
                        attempt.log = log.read().decode("utf-8", "replace").strip()
                self._check_running(cancel, started, job.settings.timeout_seconds)
                if attempt.exit_code:
                    raise AdobeProcessError(attempt.exit_code, attempt.log)
                if not stage.is_file():
                    raise ConversionError("AdobeがDNGを生成しませんでした。未対応機種・RAW破損・変換指定を確認してください。\n" + attempt.log)
                self._check_source(job, source_stat)
                info = inspect_dng(stage)
                verify_output(info, settings)
                if settings.preserve_mtime:
                    os.utime(stage, ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns))
                self._check_running(cancel, started, job.settings.timeout_seconds)
                self._check_source(job, source_stat)
                final = publish(stage, job.destination, settings.collision)
                if final is None:
                    result.status, result.message = "skipped", "変換中に同名出力が作成されたためスキップ。"
                else:
                    result.status, result.destination = "ok", str(final)
                    result.output_bytes = info.file_size
                    result.dng = info.to_dict()
                    result.dng["path"] = str(final)
                    result.effective_settings = settings.to_dict()
                    result.message = f"{info.width}×{info.height} / {info.megapixels:.2f} MP / {info.compression_name}"
                    if settings.mode != job.settings.mode:
                        result.message += " / JPEG XL失敗 → ロスレスJPEGで成功（原寸）"
                    elif settings.effort != job.settings.effort:
                        result.message += f" / effort {job.settings.effort}→{settings.effort}で再試行成功"
                attempt.status, attempt.message = result.status, result.message
        except Cancelled as exc:
            attempt.status, attempt.message = "cancelled", str(exc)
            raise
        except Exception as exc:
            attempt.status, attempt.message = "error", str(exc)
            raise
        finally:
            attempt.elapsed_seconds = round(time.monotonic() - attempt_started, 3)

    def convert(self, job: Job, cancel: threading.Event | None = None,
                on_retry: Callable[[dict[str, Any]], None] | None = None) -> Result:
        cancel = cancel or threading.Event()
        started = time.monotonic()
        result = Result(str(job.source.path), str(job.destination), "error")
        result.requested_settings = job.settings.to_dict()
        try:
            settings = job.settings
            settings.validate()
            if cancel.is_set():
                raise Cancelled("中止しました。")
            source_stat = job.source.path.stat()
            result.source_bytes = source_stat.st_size
            if self.version and settings.mode in (Mode.LOSSY_JXL, Mode.LOSSLESS_JXL):
                if int(self.version.split(".")[0]) < 16:
                    raise ConversionError("JPEG XL画質指定にはAdobe DNG Converter 16以降を使用してください。")
            if job.source.path.resolve() == job.destination.resolve():
                raise ConversionError("入力ファイルへの上書きは禁止しています。")
            if settings.collision == "skip" and job.destination.exists():
                result.status, result.message = "skipped", "同名出力があるためスキップ。"
                return result
            job.destination.parent.mkdir(parents=True, exist_ok=True)
            reason = "初回"
            # At most: requested effort -> effort 7 -> opt-in lossless JPEG.
            # Effort 5 remains a manual experiment, not a verified workaround.
            for _ in range(3):
                try:
                    self._attempt(job, settings, cancel, started, source_stat, result, reason)
                    break
                except AdobeProcessError as exc:
                    if settings.mode not in (Mode.LOSSY_JXL, Mode.LOSSLESS_JXL) or not is_jxl_histogram_assert(exc.output):
                        raise
                    if settings.mode == Mode.LOSSY_JXL and settings.jxl_retry and settings.effort > 7:
                        reason = f"effort {settings.effort}→7で再試行"
                        settings = replace(settings, effort=7)
                    elif settings.jxl_fallback:
                        reason = "ロスレスJPEGへ変更（原寸）"
                        settings = replace(settings, mode=Mode.LOSSLESS_JPEG, jxl_retry=False, jxl_fallback=False)
                    else:
                        raise
                    if on_retry:
                        on_retry({"attempt": len(result.attempts) + 1, "message": reason,
                                  "settings": settings.to_dict()})
        except Cancelled as exc:
            result.status, result.message = "cancelled", str(exc)
        except Exception as exc:
            # One unusual file must not terminate the remaining batch or the GUI.
            result.status, result.message = "error", str(exc)
            if len(result.attempts) > 1:
                result.message = f"再試行を含む{len(result.attempts)}回の変換に失敗しました。\n" + result.message
        finally:
            result.elapsed_seconds = round(time.monotonic() - started, 3)
        return result


def run_batch(converter: Converter, jobs: list[Job], report_directory: str | Path,
              cancel: threading.Event | None = None,
              on_event: Callable[[str, dict[str, Any]], None] | None = None) -> tuple[list[Result], Path]:
    cancel = cancel or threading.Event()
    event = on_event or (lambda *args: None)
    directory = Path(report_directory)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = directory / f"conversion_report_{stamp}_{uuid.uuid4().hex[:6]}.jsonl"
    results: list[Result] = []
    with report_path.open("x", encoding="utf-8") as report:
        def record(value: dict[str, Any]) -> None:
            report.write(json.dumps(value, ensure_ascii=False) + "\n")
            report.flush()
        record({"type": "batch", "app": f"DngConverterEx {__version__}", "created": datetime.now().astimezone().isoformat(),
                "converter": str(converter.executable), "converter_version": converter.version,
                "jobs": len(jobs), "validation": "metadata/structure only; Lightroom and quality distance not independently verified"})
        for index, job in enumerate(jobs):
            if cancel.is_set():
                break
            event("start", {"index": index, "source": str(job.source.path)})
            result = converter.convert(job, cancel, on_retry=lambda data: event(
                "retry", {"index": index, "source": str(job.source.path), **data}))
            results.append(result)
            data = {"type": "result", "index": index, "settings": job.settings.to_dict(), **result.to_dict()}
            record(data)
            event("result", data)
            if result.status == "cancelled":
                break
        summary = {"type": "summary", "completed": len(results), "pending": len(jobs) - len(results),
                   "ok": sum(r.status == "ok" for r in results),
                   "errors": sum(r.status == "error" for r in results),
                   "skipped": sum(r.status == "skipped" for r in results), "cancelled": cancel.is_set(),
                   "report": str(report_path)}
        record(summary)
        event("done", summary)
    return results, report_path

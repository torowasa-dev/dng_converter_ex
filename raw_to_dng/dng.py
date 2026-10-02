"""Bounded TIFF/DNG metadata inspection. Does not decode or alter image data.

This product includes DNG technology under license by Adobe.
"""
from __future__ import annotations

import math
import struct
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, BinaryIO


class DngError(ValueError):
    pass


# Only decode metadata needed for verification, never RAW/XMP/private payloads.
INTERESTING = {
    254, 256, 257, 258, 259, 262, 271, 272, 273, 277, 279, 322, 323,
    324, 325, 330, 33421, 33422, 34665, 50706, 50707, 50708, 50710,
    50714, 50717, 50718, 50719, 50720, 50721, 50722, 50728, 50729,
    50827, 50829, 50933, 50964, 50965, 50974, 50975,
}
TYPE_INFO = {
    1: (1, "B"), 2: (1, None), 3: (2, "H"), 4: (4, "I"),
    5: (8, "II"), 6: (1, "b"), 7: (1, None), 8: (2, "h"),
    9: (4, "i"), 10: (8, "ii"), 11: (4, "f"), 12: (8, "d"),
    13: (4, "I"), 16: (8, "Q"), 17: (8, "q"), 18: (8, "Q"),
}
MAX_IFDS = 128
MAX_ENTRIES = 65535
MAX_VALUE_BYTES = 2 * 1024 * 1024
COMPRESSION_NAMES = {
    1: "Uncompressed", 7: "Lossless JPEG", 8: "Deflate",
    32946: "Deflate", 34892: "Lossy JPEG", 52546: "JPEG XL",
}


@dataclass(frozen=True)
class DngInfo:
    path: str
    file_size: int
    dng_version: str
    backward_version: str
    camera: str
    raw_kind: str
    stored_width: int
    stored_height: int
    width: int
    height: int
    bits_per_sample: tuple[int, ...]
    samples_per_pixel: int
    compression: int
    compression_name: str
    color_planes: int
    has_color_matrix: bool
    has_as_shot_white_balance: bool
    wb_metadata_ready: bool
    has_original_raw: bool
    original_raw_name: str
    warnings: tuple[str, ...]

    @property
    def megapixels(self) -> float:
        return self.width * self.height / 1_000_000

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["megapixels"] = round(self.megapixels, 6)
        result["verification_scope"] = "TIFF structure and RAW/WB tags; not pixel decoding or Lightroom verification"
        return result


class _Tiff:
    def __init__(self, stream: BinaryIO, size: int):
        self.stream, self.size = stream, size
        header = self.read(0, 8)
        if header[:2] not in (b"II", b"MM"):
            raise DngError("TIFF/DNGのヘッダーがありません。")
        self.endian = "<" if header[:2] == b"II" else ">"
        magic = self.unpack("H", header[2:4])[0]
        if magic == 42:
            self.big = False
            self.inline, self.entry_size = 4, 12
            self.count_format, self.offset_format = "H", "I"
            self.first = self.unpack("I", header[4:8])[0]
        elif magic == 43:
            if self.unpack("HH", header[4:8]) != (8, 0):
                raise DngError("未対応のBigTIFFヘッダーです。")
            self.big = True
            self.inline, self.entry_size = 8, 20
            self.count_format, self.offset_format = "Q", "Q"
            self.first = self.unpack("Q", self.read(8, 8))[0]
        else:
            raise DngError("未対応のTIFF形式です。")
        self.ifds: list[dict[int, Any]] = []
        self.presence: list[dict[int, int]] = []

    def unpack(self, fmt: str, data: bytes) -> tuple:
        return struct.unpack(self.endian + fmt, data)

    def read(self, offset: int, length: int) -> bytes:
        if offset < 0 or length < 0 or offset > self.size or length > self.size - offset:
            raise DngError("TIFF内の参照がファイル範囲外です（破損・未完了の可能性）。")
        self.stream.seek(offset)
        value = self.stream.read(length)
        if len(value) != length:
            raise DngError("DNGを最後まで読めませんでした。")
        return value

    def decode(self, kind: int, count: int, raw: bytes) -> Any:
        if kind == 2:
            return raw.rstrip(b"\x00").decode("utf-8", "replace")
        if kind == 7:
            return tuple(raw)
        fmt = TYPE_INFO[kind][1]
        if kind in (5, 10):
            parts = self.unpack(fmt * count, raw)
            return tuple(a / b if b else float("nan") for a, b in zip(parts[::2], parts[1::2]))
        return self.unpack(fmt * count, raw)

    def parse(self) -> None:
        todo = [self.first]
        seen: set[int] = set()
        while todo:
            offset = todo.pop(0)
            if not offset:
                continue
            if offset in seen:
                raise DngError("TIFFのIFD参照が重複・循環しています。")
            seen.add(offset)
            if len(seen) > MAX_IFDS:
                raise DngError("IFDが多すぎます。")
            count_size = struct.calcsize(self.count_format)
            count = self.unpack(self.count_format, self.read(offset, count_size))[0]
            if count > MAX_ENTRIES:
                raise DngError("IFDエントリーが多すぎます。")
            entry_start = offset + count_size
            data = self.read(entry_start, count * self.entry_size + self.inline)
            values: dict[int, Any] = {}
            present: dict[int, int] = {}
            for index in range(count):
                entry = data[index * self.entry_size:(index + 1) * self.entry_size]
                tag, kind = self.unpack("HH", entry[:4])
                if tag in present:
                    raise DngError(f"TIFFタグ {tag} が同じIFD内で重複しています。")
                value_count = self.unpack("Q" if self.big else "I", entry[4:12] if self.big else entry[4:8])[0]
                present[tag] = value_count
                if kind not in TYPE_INFO:
                    continue
                nbytes = TYPE_INFO[kind][0] * value_count
                inline_data = entry[12:20] if self.big else entry[8:12]
                if nbytes > self.inline:
                    value_offset = self.unpack(self.offset_format, inline_data)[0]
                    # Bounds-check all recognized types, including large RAW/private blocks.
                    if value_offset > self.size or nbytes > self.size - value_offset:
                        raise DngError(f"TIFFタグ {tag} のデータがファイル範囲外です。")
                else:
                    value_offset = 0
                if tag not in INTERESTING:
                    continue
                if nbytes > MAX_VALUE_BYTES:
                    raise DngError(f"検査用メタデータ {tag} が大きすぎます。")
                raw = self.read(value_offset, nbytes) if nbytes > self.inline else inline_data[:nbytes]
                values[tag] = self.decode(kind, value_count, raw)
            self.ifds.append(values)
            self.presence.append(present)
            for pointer in (330, 34665):
                todo.extend(int(x) for x in values.get(pointer, ()))
            todo.append(self.unpack(self.offset_format, data[-self.inline:])[0])


def _first(tags: dict[int, Any], tag: int, default: Any = None) -> Any:
    values = tags.get(tag)
    if values is None:
        return default
    return values if isinstance(values, str) else (values[0] if values else default)


def _version(values: Any) -> str:
    return ".".join(str(int(x)) for x in values) if values else ""


def _valid_matrix(value: Any) -> bool:
    return bool(value and len(value) in (9, 12, 15, 18, 21, 24) and
                all(math.isfinite(x) for x in value) and any(abs(x) > 0 for x in value))


def inspect_dng(path: str | Path) -> DngInfo:
    path = Path(path)
    size = path.stat().st_size
    with path.open("rb") as stream:
        tiff = _Tiff(stream, size)
        tiff.parse()
    if not tiff.ifds or not tiff.ifds[0].get(50706):
        raise DngError("DNGVersionタグがありません。")
    main = tiff.ifds[0]
    candidates = [(index, tags) for index, tags in enumerate(tiff.ifds)
                  if _first(tags, 262) in (32803, 34892)
                  and int(_first(tags, 254, 0)) & 1 == 0]
    if not candidates:
        raise DngError("RAW IFDがありません（JPEG/RGB画像を格納しただけのDNGは受け付けません）。")
    raw_index, raw = max(candidates, key=lambda item: int(_first(item[1], 256, 0)) * int(_first(item[1], 257, 0)))
    tags = {**main, **raw}
    stored_width = int(_first(raw, 256, 0))
    stored_height = int(_first(raw, 257, 0))
    if stored_width < 1 or stored_height < 1:
        raise DngError("RAW画像の寸法が不正です。")
    # Verify all RAW strips/tiles are present without decompressing them.
    offsets = raw.get(324, raw.get(273, ()))
    lengths = raw.get(325, raw.get(279, ()))
    if not offsets or len(offsets) != len(lengths):
        raise DngError("RAWのタイル／ストリップ参照が不正です。")
    for offset, length in zip(offsets, lengths):
        if offset <= 0 or length <= 0 or offset > size or length > size - offset:
            raise DngError("RAWタイル／ストリップがファイル範囲外です。")
    bits = tuple(int(x) for x in raw.get(258, ()))
    if not bits or any(x <= 0 or x > 64 for x in bits):
        raise DngError("RAWのビット深度が不正です。")
    crop = tags.get(50720)
    width, height = stored_width, stored_height
    if crop and len(crop) == 2 and all(math.isfinite(x) and x > 0 for x in crop):
        width, height = round(crop[0]), round(crop[1])
    scale = tags.get(50718, (1, 1))
    if len(scale) == 2 and all(math.isfinite(x) and x > 0 for x in scale):
        width, height = round(width * scale[0]), round(height * scale[1])
    if width < 1 or height < 1:
        raise DngError("有効画像の寸法が不正です。")
    matrix = next((value for candidate in (tags, *tiff.ifds)
                   for code in (50721, 50722) if _valid_matrix(value := candidate.get(code))), None)
    samples = int(_first(raw, 277, 1))
    planes = len(matrix) // 3 if matrix else len(tags.get(50710, ())) or samples
    neutral = tags.get(50728, ())
    xy = tags.get(50729, ())
    has_neutral = bool(len(neutral) == planes and all(math.isfinite(x) and x > 0 for x in neutral))
    has_xy = bool(len(xy) == 2 and all(math.isfinite(x) and 0 < x < 1 for x in xy) and sum(xy) < 1)
    warnings: list[str] = []
    wb_ready = bool(matrix and (has_neutral or has_xy))
    if not wb_ready:
        warnings.append("色行列または撮影時WBがありません。カラー画像のKelvin編集は確認できません。")
    compression = int(_first(raw, 259, 1))
    if compression not in COMPRESSION_NAMES:
        warnings.append(f"未確認の圧縮方式: {compression}")
    original_present = bool(tiff.presence[0].get(50828) or tiff.presence[raw_index].get(50828))
    return DngInfo(
        str(path.resolve()), size, _version(main.get(50706)), _version(main.get(50707)),
        str(tags.get(50708) or " ".join(str(_first(main, code, "")) for code in (271, 272))).strip(),
        "CFA" if _first(raw, 262) == 32803 else "Linear RAW",
        stored_width, stored_height, width, height, bits, samples, compression,
        COMPRESSION_NAMES.get(compression, str(compression)), planes,
        bool(matrix), bool(has_neutral or has_xy), wb_ready, original_present,
        str(tags.get(50827, "")), tuple(warnings),
    )

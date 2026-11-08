"""Write decoded JPEG samples as a 16-bit linear RGB DNG."""
from __future__ import annotations

import io
import struct
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from .dng import _Tiff
from .jpeg_exif import EXIF_IFD, GPS_IFD, jpeg_exif_tags

if TYPE_CHECKING:
    from PIL.Image import Image


def _linear_sample(value: int) -> int:
    sample = value / 255
    linear = sample / 12.92 if sample <= 0.04045 else ((sample + 0.055) / 1.055) ** 2.4
    return round(linear * 65535)


_LUT = tuple(_linear_sample(value) for value in range(256))
_LOW = bytes(value & 255 for value in _LUT)
_HIGH = bytes(value >> 8 for value in _LUT)
# XYZ D65 to linear sRGB, represented as DNG ColorMatrix1.
_COLOR_MATRIX = (3240454, -1537139, -498531, -969266, 1876011, 41556, 55643, -204026, 1057225)
# Linear sRGB to the D50 profile connection space (Bradford-adapted).
_FORWARD_MATRIX = (4360747, 3850649, 1430804, 2225045, 7168786, 606169, 139322, 971045, 7141733)
# Inverse of the D50 forward matrix, used for output-referred JPEG pixels.
_PCS_COLOR_MATRIX = (31338564, -16168668, -4906148, -9787686, 19161416, 334541, 719452, -2289913, 14052427)
MIN_WB_KELVIN = 2000
MAX_WB_KELVIN = 25000


def _white_balance_tag(kelvin: int) -> tuple[int, int, bytes]:
    # Piecewise approximation of the CIE 1931 Planckian locus.
    t = 1000 / kelvin
    if kelvin <= 4000:
        x = ((-0.2661239 * t - 0.2343580) * t + 0.8776956) * t + 0.179910
        if kelvin <= 2222:
            y = ((-1.1063814 * x - 1.34811020) * x + 2.18555832) * x - 0.20219683
        else:
            y = ((-0.9549476 * x - 1.37418593) * x + 2.09137015) * x - 0.16748867
    else:
        x = ((-3.0258469 * t + 2.1070379) * t + 0.2226347) * t + 0.240390
        y = ((3.0817580 * x - 5.87338670) * x + 3.75112997) * x - 0.37001483
    xyz = (x, y, 1 - x - y)
    neutral = [sum(coefficient * component for coefficient, component in zip(_COLOR_MATRIX[i:i + 3], xyz))
               for i in (0, 3, 6)]
    scale = max(neutral)
    return _numbers(5, *(n for value in neutral for n in (round(value / scale * 1000000), 1000000)))


def _numbers(kind: int, *values: int) -> tuple[int, int, bytes]:
    fmt = {1: "B", 3: "H", 4: "I", 5: "I", 10: "i"}[kind]
    count = len(values) // 2 if kind in (5, 10) else len(values)
    return kind, count, struct.pack("<" + fmt * len(values), *values)


def _ascii(value: str) -> tuple[int, int, bytes]:
    data = value.encode("ascii", "replace").split(b"\0", 1)[0][:1024] + b"\0"
    return 2, len(data), data


def _ifd(tags: dict, offset: int) -> bytes:
    entries = bytearray(struct.pack("<H", len(tags)))
    extra = bytearray()
    extra_start = offset + 2 + 12 * len(tags) + 4
    for tag, (kind, count, data) in sorted(tags.items()):
        entries.extend(struct.pack("<HHI", tag, kind, count))
        if len(data) <= 4:
            entries.extend(data.ljust(4, b"\0"))
        else:
            entries.extend(struct.pack("<I", extra_start + len(extra)))
            extra.extend(data)
            if len(extra) % 2:
                extra.append(0)
    return bytes(entries) + bytes(4) + bytes(extra)


def _image_tags(width: int, height: int, bits: int, photometric: int, reduced: bool) -> dict:
    return {
        254: _numbers(4, int(reduced)), 256: _numbers(4, width), 257: _numbers(4, height),
        258: _numbers(3, bits, bits, bits), 259: _numbers(3, 1), 262: _numbers(3, photometric),
        273: _numbers(4, 0), 277: _numbers(3, 3), 278: _numbers(4, height),
        279: _numbers(4, width * height * 3 * (bits // 8)), 284: _numbers(3, 1),
    }


def write_jpeg_dng(source: Path, stage: Path, check_running: Callable[[], None], *,
                   wb_kelvin: int | None = None) -> Image:
    try:
        from PIL import Image, ImageCms, ImageOps
    except ImportError as exc:
        raise ValueError("JPEGモードにはPillowが必要です。python -m pip install . を実行してください。") from exc

    with Image.open(source, formats=("JPEG",)) as original:
        exif = original.getexif()
        capture = exif.get_ifd(34665)
        source_tags, capture_tags, gps_tags = jpeg_exif_tags(original.info.get("exif", b""))
        image = ImageOps.exif_transpose(original)
        if icc := original.info.get("icc_profile"):
            try:
                image = ImageCms.profileToProfile(image, io.BytesIO(icc), ImageCms.createProfile("sRGB"), outputMode="RGB")
            except (ImageCms.PyCMSError, OSError, ValueError) as exc:
                raise ValueError("JPEGのICCプロファイルをsRGBへ変換できません。") from exc
        else:
            if image.mode not in ("RGB", "L") or capture.get(40961, exif.get(40961, 1)) == 2:
                raise ValueError("色を特定するため、このJPEGには有効なICCプロファイルが必要です。")
            image = image.convert("RGB")

    check_running()
    width, height = image.size
    if width * height * 6 >= 2**32 - 1_000_000:
        raise ValueError("画像が大きすぎてClassic TIFF形式のDNGに収まりません。")
    thumbnail = image.copy()
    thumbnail.thumbnail((256, 256), Image.Resampling.LANCZOS)
    preview = thumbnail.tobytes()
    main = source_tags | _image_tags(*thumbnail.size, 8, 2, True)
    main.update({
        274: _numbers(3, 1), 305: _ascii("DngConverterEx JPEG to DNG"), 330: _numbers(4, 0),
        50706: _numbers(1, 1, 4, 0, 0),
        50708: _ascii("DngConverterEx JPEG sRGB"),
        50730: _numbers(10, 0, 1),
        # JPEG pixels already include rendering. Give the embedded profile an
        # identity tone curve and request no additional automatic black render.
        50940: (11, 4, struct.pack("<4f", 0, 0, 1, 1)),
        50964: _numbers(10, *(n for coefficient in _FORWARD_MATRIX for n in (coefficient, 10_000_000))),
        51109: _numbers(10, 0, 1), 51110: _numbers(4, 1),
    })
    if wb_kelvin is None:
        # The JPEG already contains the camera's tone curve and white balance.
        # DNG 1.2 output-referred data uses the D50 PCS white point and AsShotWhiteXY.
        main.update({
            50707: _numbers(1, 1, 2, 0, 0),
            50721: _numbers(10, *(n for coefficient in _PCS_COLOR_MATRIX for n in (coefficient, 10_000_000))),
            50729: _numbers(5, 34567, 100000, 35850, 100000),
            50778: _numbers(3, 23), 50879: _numbers(3, 1),
        })
    else:
        main.update({
            50707: _numbers(1, 1, 1, 0, 0),
            50721: _numbers(10, *(n for coefficient in _COLOR_MATRIX for n in (coefficient, 1_000_000))),
            50728: _white_balance_tag(wb_kelvin), 50778: _numbers(3, 21),
        })
    if capture_tags:
        capture_tags.update({40962: _numbers(4, width), 40963: _numbers(4, height)})
        main[EXIF_IFD] = _numbers(4, 0)
    if gps_tags:
        main[GPS_IFD] = _numbers(4, 0)
    raw = _image_tags(width, height, 16, 34892, False)
    raw.update({50714: _numbers(3, 0), 50717: _numbers(4, 65535),
                50719: _numbers(4, 0, 0), 50720: _numbers(4, width, height),
                50829: _numbers(4, 0, 0, height, width)})
    raw_offset = 8 + len(_ifd(main, 8))
    preview_offset = raw_offset + len(_ifd(raw, raw_offset))
    metadata = bytearray()
    for pointer, tags in ((EXIF_IFD, capture_tags), (GPS_IFD, gps_tags)):
        if tags:
            main[pointer] = _numbers(4, preview_offset + len(metadata))
            metadata.extend(_ifd(tags, preview_offset + len(metadata)))
    preview_offset += len(metadata)
    raw_pixels_offset = preview_offset + len(preview) + len(preview) % 2
    main[330] = _numbers(4, raw_offset)
    main[273] = _numbers(4, preview_offset)
    raw[273] = _numbers(4, raw_pixels_offset)

    with stage.open("wb") as output:
        output.write(b"II*\0\x08\0\0\0")
        output.write(_ifd(main, 8))
        output.write(_ifd(raw, raw_offset))
        output.write(metadata)
        output.write(preview)
        if len(preview) % 2:
            output.write(b"\0")
        for top in range(0, height, 64):
            check_running()
            samples = image.crop((0, top, width, min(top + 64, height))).tobytes()
            encoded = bytearray(2 * len(samples))
            encoded[::2] = samples.translate(_LOW)
            encoded[1::2] = samples.translate(_HIGH)
            output.write(encoded)
    return thumbnail


def preserve_jpeg_thumbnail(stage: Path, thumbnail: Image) -> None:
    """Replace Adobe's rendered IFD0 thumbnail for supported single-strip layouts."""
    from PIL import Image

    with stage.open("r+b") as stream:
        tiff = _Tiff(stream, stage.stat().st_size)
        tiff.parse()
        if not tiff.ifds:
            return
        tags = tiff.ifds[0]
        offsets, lengths = tags.get(273, ()), tags.get(279, ())
        width = next(iter(tags.get(256, ())), 0)
        height = next(iter(tags.get(257, ())), 0)
        if (tags.get(254) != (1,) or tags.get(258) != (8, 8, 8) or tags.get(277) != (3,)
                or tags.get(274, (1,)) != (1,) or tags.get(284, (1,)) != (1,)
                or tags.get(50970, (2,)) != (2,) or len(offsets) != 1 or len(lengths) != 1
                or not (0 < width <= 256 and 0 < height <= 256)
                or next(iter(tags.get(278, (height,))), 0) < height):
            return
        thumbnail = thumbnail.resize((width, height), Image.Resampling.LANCZOS)
        compression, photometric = tags.get(259), tags.get(262)
        if compression == (1,) and photometric == (2,) and lengths[0] == width * height * 3:
            pixels = thumbnail.tobytes()
        elif compression == (7,) and photometric == (6,) and 347 not in tiff.presence[0]:
            subsampling = {(1, 1): 0, (2, 1): 1, (2, 2): 2}.get(tags.get(530, (2, 2)))
            if subsampling is None:
                return
            buffer = io.BytesIO()
            thumbnail.save(buffer, format="JPEG", quality=95, subsampling=subsampling, optimize=True)
            pixels = buffer.getvalue()
        else:
            return

        offset, capacity = offsets[0], lengths[0]
        if not (0 < offset < tiff.size and 0 < capacity <= tiff.size - offset):
            return
        if len(pixels) > capacity:
            alignment = 8 if tiff.big else 2
            offset = tiff.size + (-tiff.size % alignment)
        count_size = struct.calcsize(tiff.count_format)
        entry_start = tiff.first + count_size
        count = tiff.unpack(tiff.count_format, tiff.read(tiff.first, count_size))[0]
        entries = tiff.read(entry_start, count * tiff.entry_size)
        replacements = {273: offset, 279: len(pixels)}
        updates = []
        for index in range(count):
            position = index * tiff.entry_size
            tag, kind = tiff.unpack("HH", entries[position:position + 4])
            if tag in replacements:
                fmt = {3: "H", 4: "I", 16: "Q"}.get(kind)
                if fmt is None or replacements[tag] >= 1 << (8 * struct.calcsize(fmt)):
                    return
                updates.append((entry_start + position + tiff.entry_size - tiff.inline,
                                struct.pack(tiff.endian + fmt, replacements[tag])))
        stream.seek(offset)
        stream.write(pixels)
        for position, value in updates:
            stream.seek(position)
            stream.write(value)

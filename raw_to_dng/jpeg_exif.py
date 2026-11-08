"""Relocate standard JPEG Exif fields and retain the source Exif block."""
from __future__ import annotations

import struct

from .dng import TYPE_INFO

Tag = tuple[int, int, bytes]

EXIF_IFD = 34665
GPS_IFD = 34853
PRIVATE_DATA = 50740
# Image layout is generated separately for the DNG thumbnail and RAW IFD.
_ROOT_FIELDS = {269, 270, 271, 272, 285, 286, 287, 306, 315, 316, 33432, 40091, 40092, 40093, 40094, 40095}
_EXIF_LAYOUT_FIELDS = {EXIF_IFD, GPS_IFD, 37121, 37122, 37500, 40961, 40962, 40963, 40965, 41486, 41487, 41488}


def jpeg_exif_tags(exif: bytes) -> tuple[dict[int, Tag], dict[int, Tag], dict[int, Tag]]:
    from PIL.ExifTags import TAGS

    main: dict[int, Tag] = {}
    capture: dict[int, Tag] = {}
    gps: dict[int, Tag] = {}
    if not exif:
        return main, capture, gps
    # The original byte order and all MakerNote-relative offsets stay intact.
    private = b"DngConverterEx\0" + exif
    main[PRIVATE_DATA] = (1, len(private), private)
    tiff = exif[6:] if exif.startswith(b"Exif\0\0") else exif
    byte_order = {b"II*\0": "<", b"MM\0*": ">"}.get(tiff[:4])
    if byte_order is None or len(tiff) < 8:
        return main, capture, gps

    def read_ifd(offset: int) -> dict[int, Tag]:
        if not 8 <= offset <= len(tiff) - 2:
            return {}
        entries = struct.unpack_from(byte_order + "H", tiff, offset)[0]
        start = offset + 2
        if start + 12 * entries + 4 > len(tiff):
            return {}
        tags = {}
        for i in range(entries):
            position = start + i * 12
            tag, kind, count = struct.unpack_from(byte_order + "HHI", tiff, position)
            if not 1 <= kind <= 12 or count == 0:
                continue
            size = count * TYPE_INFO[kind][0]
            pointer = position + 8 if size <= 4 else struct.unpack_from(byte_order + "I", tiff, position + 8)[0]
            if pointer + size > len(tiff) or pointer < 8:
                continue
            data = tiff[pointer:pointer + size]
            unit = 4 if kind in (5, 10) else TYPE_INFO[kind][0]
            if byte_order == ">" and unit > 1:
                data = b"".join(data[j:j + unit][::-1] for j in range(0, size, unit))
            tags[tag] = (kind, count, data)
        return tags

    def child(parent: dict[int, Tag], tag: int) -> dict[int, Tag]:
        entry = parent.get(tag)
        if entry is None or entry[:2] != (4, 1):
            return {}
        return read_ifd(struct.unpack("<I", entry[2])[0])

    source_main = read_ifd(struct.unpack_from(byte_order + "I", tiff, 4)[0])
    main.update({tag: entry for tag, entry in source_main.items() if tag in _ROOT_FIELDS})
    # A few encoders put capture tags in IFD0. Prefer the dedicated Exif IFD.
    source_capture = source_main | child(source_main, EXIF_IFD)
    capture.update({tag: entry for tag, entry in source_capture.items()
                    if 33434 <= tag < 43000 and tag in TAGS and tag not in _EXIF_LAYOUT_FIELDS})
    gps.update({tag: entry for tag, entry in child(source_main, GPS_IFD).items() if 0 <= tag <= 31})
    return main, capture, gps

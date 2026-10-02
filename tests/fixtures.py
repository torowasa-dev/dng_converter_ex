"""Metadata-only test fixture builder; compressed payload is deliberately synthetic.

Do not use these generated files for Lightroom or image-quality testing.
"""
from __future__ import annotations

import struct
from pathlib import Path


def make_dng(path: Path, *, width=6000, height=4000, compression=52546, linear=True,
             wb=True, matrix=True, embedded=False, endian="<", big=False,
             raw_ifd_child=False, tile_outside=False, photo=None):
    def pack(fmt, *args):
        return struct.pack(endian + fmt, *args)
    sizes = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 7: 1, 10: 8, 16: 8}
    def encoded(kind, value):
        if kind in (2, 7):
            return len(value), value
        if kind in (5, 10):
            return len(value), b"".join(pack("II" if kind == 5 else "ii", *pair) for pair in value)
        return len(value), pack({1: "B", 3: "H", 4: "I", 16: "Q"}[kind] * len(value), *value)
    offset_kind = 16 if big else 4
    raw = {
        254: (4, [0]), 256: (4, [width]), 257: (4, [height]),
        258: (3, [8 if compression == 34892 else 16]), 259: (3, [compression]),
        262: (3, [photo if photo is not None else (34892 if linear else 32803)]),
        277: (3, [3 if linear else 1]), 273: (offset_kind, [0]), 279: (offset_kind, [256]),
        50719: (4, [0, 0]), 50720: (4, [width, height]),
    }
    if not linear:
        raw.update({33421: (3, [2, 2]), 33422: (1, [0, 1, 1, 2]), 50710: (1, [0, 1, 2])})
    metadata = {50706: (1, [1, 7, 1, 0]), 50707: (1, [1, 7, 0, 0]),
                50708: (2, b"Synthetic test camera\0")}
    if matrix:
        metadata[50721] = (10, [(1 if i % 4 == 0 else 0, 1) for i in range(9)])
    if wb:
        metadata[50728] = (5, [(1, 2), (1, 1), (2, 3)])
    if embedded:
        metadata[50827] = (2, b"original.NEF\0")
        metadata[50828] = (7, b"not-a-real-embedded-RAW")
    if raw_ifd_child:
        metadata.update({254: (4, [1]), 256: (4, [100]), 257: (4, [100]),
                         262: (3, [2]), 330: (offset_kind, [0])})
        ifds = [metadata, raw]
    else:
        ifds = [{**metadata, **raw}]
    header_size, inline, entry_size, count_size = (16, 8, 20, 8) if big else (8, 4, 12, 2)
    table_offsets = []
    cursor = header_size
    for tags in ifds:
        table_offsets.append(cursor)
        cursor += count_size + entry_size * len(tags) + inline
    if raw_ifd_child:
        ifds[0][330] = (offset_kind, [table_offsets[1]])
    entries_encoded = []
    extra = bytearray()
    tile_entry = None
    for tags in ifds:
        table = []
        for tag, (kind, value) in sorted(tags.items()):
            count, data = encoded(kind, value)
            if len(data) > inline:
                pointer = cursor + len(extra)
                extra.extend(data)
                field = pack("Q" if big else "I", pointer)
            else:
                field = data.ljust(inline, b"\0")
            entry = pack("HH", tag, kind) + pack("Q" if big else "I", count) + field
            if tag == 273:
                tile_entry = (len(entries_encoded), len(table))
            table.append(entry)
        entries_encoded.append(table)
    tile_start = cursor + len(extra)
    if tile_outside:
        tile_start += 99999
    i, j = tile_entry
    old = entries_encoded[i][j]
    entries_encoded[i][j] = old[:-inline] + pack("Q" if big else "I", tile_start)
    header = (b"II" if endian == "<" else b"MM") + pack("H", 43 if big else 42)
    header += pack("HHQ", 8, 0, header_size) if big else pack("I", header_size)
    tables = b"".join(pack("Q" if big else "H", len(table)) + b"".join(table) + bytes(inline) for table in entries_encoded)
    path.write_bytes(header + tables + extra + bytes([23]) * 256)
    return path

# Copyright © 2026 Rutgers, the State University of New Jersey. All rights reserved except as defined by the Rutgers Non-Commercial License, included with this software.
"""Sanity-check an uploaded dataset file against its extension.

Instructors occasionally upload a "zip" that is really an HTML error page, a CSV renamed by
hand, or an empty file — and nothing complains until a student's code fails to open it. For
formats with an unambiguous signature we check the first bytes match; `.zip` (and zip-based
containers) also go through ``zipfile.is_zipfile`` so a truncated archive is caught too.
Plain-text formats are deliberately not inspected: UTF-16 CSVs exported from Excel contain
NUL bytes, so a "looks binary" heuristic would reject legitimate files.
"""
from __future__ import annotations

import os
import zipfile
from typing import BinaryIO, Optional

# extension → (human label, tuple of accepted leading byte signatures)
_SIGNATURES: dict[str, tuple[str, tuple[bytes, ...]]] = {
    '.gz': ('gzip archive', (b'\x1f\x8b',)),
    '.tgz': ('gzip archive', (b'\x1f\x8b',)),
    '.bz2': ('bzip2 archive', (b'BZh',)),
    '.xz': ('xz archive', (b'\xfd7zXZ\x00',)),
    '.7z': ('7-Zip archive', (b'7z\xbc\xaf\x27\x1c',)),
    '.rar': ('RAR archive', (b'Rar!\x1a\x07',)),
    '.parquet': ('Parquet file', (b'PAR1',)),
    '.pdf': ('PDF', (b'%PDF',)),
    '.png': ('PNG image', (b'\x89PNG\r\n\x1a\n',)),
    '.jpg': ('JPEG image', (b'\xff\xd8\xff',)),
    '.jpeg': ('JPEG image', (b'\xff\xd8\xff',)),
    '.gif': ('GIF image', (b'GIF87a', b'GIF89a')),
    '.npy': ('NumPy array', (b'\x93NUMPY',)),
    '.sqlite': ('SQLite database', (b'SQLite format 3\x00',)),
    '.sqlite3': ('SQLite database', (b'SQLite format 3\x00',)),
    '.db': ('SQLite database', (b'SQLite format 3\x00',)),
    '.h5': ('HDF5 file', (b'\x89HDF\r\n\x1a\n',)),
    '.hdf5': ('HDF5 file', (b'\x89HDF\r\n\x1a\n',)),
}

# Zip containers: checked structurally, not just by the leading "PK".
_ZIP_LIKE: dict[str, str] = {
    '.zip': 'ZIP archive',
    '.npz': 'NumPy .npz archive',
    '.xlsx': 'Excel workbook',
    '.docx': 'Word document',
    '.pptx': 'PowerPoint deck',
    '.jar': 'Java archive',
    '.whl': 'Python wheel',
}

_MAX_SIGNATURE_LEN = max(len(sig) for _label, sigs in _SIGNATURES.values() for sig in sigs)


def dataset_file_problem(name: str, fileobj: BinaryIO, size: int) -> Optional[str]:
    """Return a user-facing reason this file should be rejected, or None if it looks fine.

    Only the extension's own signature is checked; an unknown extension passes. The file
    position is restored so the caller can go on to save it.
    """
    if size == 0:
        return f"'{name}' is empty."

    ext = os.path.splitext(name)[1].lower()
    label = _ZIP_LIKE.get(ext)
    if label is not None:
        pos = fileobj.tell()
        try:
            ok = zipfile.is_zipfile(fileobj)
        finally:
            fileobj.seek(pos)
        if not ok:
            return (f"'{name}' is not a valid {label} — the content doesn't match the "
                    f"{ext} extension. Re-export it, or rename the file to its real type.")
        return None

    entry = _SIGNATURES.get(ext)
    if entry is None:
        return None
    label, signatures = entry
    pos = fileobj.tell()
    try:
        fileobj.seek(0)
        head = fileobj.read(_MAX_SIGNATURE_LEN)
    finally:
        fileobj.seek(pos)
    if not any(head.startswith(sig) for sig in signatures):
        return (f"'{name}' is not a valid {label} — the content doesn't match the "
                f"{ext} extension. Re-export it, or rename the file to its real type.")
    return None

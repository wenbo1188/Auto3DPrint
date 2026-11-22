"""Checks on .3mf files before they are sent to the printer."""

from __future__ import annotations

import re
import zipfile


def sliced_plates(path: str) -> list[int]:
    """Return plate numbers that have sliced G-code inside the .3mf.

    An unsliced project (plain "Save project") has no ``Metadata/plate_N.gcode``
    and the printer cannot run it; it has to be exported from Bambu Studio
    with "Export plate sliced file" (or "Export all sliced file").
    """
    try:
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
    except (OSError, zipfile.BadZipFile):
        return []
    plates = []
    for name in names:
        m = re.fullmatch(r"Metadata/plate_(\d+)\.gcode", name)
        if m:
            plates.append(int(m.group(1)))
    return sorted(plates)

#!/usr/bin/env python3
"""Gunzip XMLTV files and confirm each one parses as XML.

Missing files are reported and left for the publisher to keep the previous
copy. A file that exists but does not parse, or that has no channels, is
removed so it cannot replace a good guide, and the process exits 1.
"""

from __future__ import annotations

import gzip
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def inspect(path: Path) -> tuple[int, int]:
    with gzip.open(path, "rb") as handle:
        payload = handle.read()
    root = ET.fromstring(payload)
    if root.tag != "tv":
        raise ValueError(f"root element is {root.tag!r}, expected 'tv'")
    channels = [element for element in root if element.tag == "channel"]
    programmes = [element for element in root if element.tag == "programme"]
    channel_ids = {element.get("id") for element in channels if element.get("id")}
    programme_channels = {element.get("channel") for element in programmes if element.get("channel")}
    if not channels or not programmes:
        raise ValueError(f"expected channels and programmes, found {len(channels)} and {len(programmes)}")
    missing = programme_channels - channel_ids
    if missing:
        raise ValueError(f"programmes reference unknown channels: {sorted(missing)[:5]}")
    return len(channels), len(programmes)


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: validate_guides.py us.xml.gz mx.xml.gz", file=sys.stderr)
        return 1
    failed = False
    for raw in argv:
        path = Path(raw)
        if not path.is_file():
            print(f"{path}: missing, keeping any previously published file")
            continue
        try:
            channels, programmes = inspect(path)
        except Exception as error:  # noqa: BLE001 - report gzip/XML failures uniformly
            print(f"{path}: invalid ({error}); removing so it is not published", file=sys.stderr)
            path.unlink(missing_ok=True)
            failed = True
            continue
        print(f"{path}: {channels} channels, {programmes} programmes")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

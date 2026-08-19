#!/usr/bin/env python3
"""Inject the QCRT exporter into an installed NVIDIA CloudXR Web Client."""

from __future__ import annotations

import argparse
import re
import shutil
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = Path.home() / ".cloudxr" / "static-client"
DEFAULT_OUTPUT = ROOT / ".cloudxr-client"
EXPORTER = ROOT / "cloudxr" / "qcrt-exporter.js"
INJECTION_MARKER = "QCRT_CLOUDXR_INJECTED"
BUNDLE_TAG = re.compile(
    r"<script\b(?=[^>]*\bsrc=[\"']bundle\.js[\"'])[^>]*></script>", re.IGNORECASE
)


def prepare_client(source: Path, output: Path) -> Path:
    source = source.resolve()
    output = output.resolve()
    if source == output:
        raise ValueError("source and output directories must differ")

    required = ("index.html", "bundle.js", "bundle.emulator.js")
    missing = [name for name in required if not (source / name).is_file()]
    if missing:
        raise FileNotFoundError(f"CloudXR client is missing: {', '.join(missing)}")

    index = (source / "index.html").read_text(encoding="utf-8")
    exporter = EXPORTER.read_text(encoding="utf-8")
    if INJECTION_MARKER in index:
        raise ValueError("source index already contains the QCRT injection")
    if "</script" in exporter.lower():
        raise ValueError("exporter cannot be embedded safely")

    match = BUNDLE_TAG.search(index)
    if match is None:
        raise ValueError("bundle.js script tag was not found in CloudXR index.html")
    injected = (
        index[: match.start()]
        + f"<script>/* {INJECTION_MARKER} */\n{exporter}\n</script>"
        + match.group(0)
        + index[match.end() :]
    )

    output.mkdir(parents=True, exist_ok=True)
    (output / "index.html").write_text(injected, encoding="utf-8")
    for name in required[1:]:
        shutil.copy2(source / name, output / name)
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    output = prepare_client(args.source, args.output)
    print(output)


if __name__ == "__main__":
    main()

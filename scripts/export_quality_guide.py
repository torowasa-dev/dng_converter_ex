#!/usr/bin/env python3
"""Generate the repository's offline guide from the same content as the GUI."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from raw_to_dng.help_content import markdown_guide


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Check consistency without writing")
    args = parser.parse_args()
    destination = ROOT / "docs" / "QUALITY_GUIDE.md"
    content = markdown_guide()
    if args.check:
        if not destination.exists() or destination.read_text(encoding="utf-8") != content:
            print("Guide is out of date. Run: python scripts/export_quality_guide.py", file=sys.stderr)
            return 1
        print("Offline GUI guide and docs/QUALITY_GUIDE.md match.")
        return 0
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(content, encoding="utf-8")
    print(destination.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

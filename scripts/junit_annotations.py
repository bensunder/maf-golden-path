"""Turn pytest JUnit XML failures into GitHub Actions error annotations.

Annotations appear on the run's summary page (visible without opening the logs):
    python scripts/junit_annotations.py results/*.xml
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def _escape(text: str) -> str:
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def main(paths: list[str]) -> int:
    failures = 0
    for path in paths:
        if not Path(path).exists():
            continue
        for case in ET.parse(path).getroot().iter("testcase"):
            for kind in ("failure", "error"):
                node = case.find(kind)
                if node is None:
                    continue
                failures += 1
                name = f"{case.get('classname')}::{case.get('name')}"
                detail = (node.get("message") or "") + "\n" + (node.text or "")[-1500:]
                print(f"::error title={_escape(name)}::{_escape(detail.strip())}")
    print(f"{failures} failing test(s) annotated")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

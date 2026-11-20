"""Fail when a core module falls below the agreed coverage floor."""

import json
import sys
from pathlib import Path


CORE_MODULES = (
    "services/state_machine.py",
    "alert.py",
    "auth.py",
    "services/retention.py",
)
MIN_COVERAGE = 80.0


def main(report_path: Path) -> int:
    report = json.loads(report_path.read_text(encoding="utf-8"))
    files = report.get("files", {})
    failed = False
    for module in CORE_MODULES:
        summary = files.get(module, {}).get("summary")
        percent = summary.get("percent_covered", 0.0) if summary else 0.0
        print(f"{module}: {percent:.1f}%")
        if percent < MIN_COVERAGE:
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(Path(sys.argv[1] if len(sys.argv) > 1 else "coverage.json")))

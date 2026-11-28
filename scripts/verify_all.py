"""Verify EVERY database of a multi-alliance install: the home database, the platform registry and each alliance's own file.

    python scripts/verify_all.py [data_dir]          (default: DATA_DIR / the Railway volume / ./data)

Read-only. Never prints a key. Exit code 0 = every file checked out.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    if len(sys.argv) > 1:
        data = Path(sys.argv[1])
    else:
        from tunbank.config import resolve_data_dir
        data = resolve_data_dir()
    files = [data / "tunbank.db", data / "platform.db"] + sorted((data / "tenants").glob("*/tunbank.db"))
    files = [f for f in files if f.exists()]
    if not files:
        print(f"No databases found in {data}")
        return 1
    bad = 0
    for f in files:
        print(f"\n##### {f.relative_to(data)}")
        if subprocess.call([sys.executable, str(ROOT / "scripts" / "verify_database.py"), str(f)]) != 0:
            bad += 1
    print(f"\n{len(files) - bad} of {len(files)} database(s) verified" + ("" if not bad else f"; {bad} FAILED"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

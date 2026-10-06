"""Export the dashboard as a static, read-only site (no backend) for hosting anywhere.

Run:  .venv/bin/python scripts/export_static.py [dest]      (default dest: dist/)

Uses the saved plan in output/ (run ./run.sh or `python -m optimizer.run` first).
Re-optimizing is disabled in the static build since it needs the Python solver.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB, OUT = ROOT / "web", ROOT / "output"


def main(dest: Path) -> None:
    plan, det = OUT / "plan.json", OUT / "details.json"
    if not (plan.exists() and det.exists()):
        sys.exit("No saved plan in output/ - run `python -m optimizer.run` first.")
    if dest.exists():
        shutil.rmtree(dest)
    (dest / "data").mkdir(parents=True)

    html = (WEB / "index.html").read_text()
    html = html.replace('<html lang="en">', '<html lang="en" data-static="1">', 1)
    html = html.replace('href="/static/', 'href="').replace('src="/static/', 'src="')
    (dest / "index.html").write_text(html)
    for name in ("app.js", "styles.css"):
        shutil.copy(WEB / name, dest / name)
    shutil.copy(plan, dest / "data" / "plan.json")
    shutil.copy(det, dest / "data" / "details.json")
    print(f"Static dashboard written to {dest}")


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else ROOT / "dist")

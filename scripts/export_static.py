"""Export the dashboard as a static, read-only site (no backend) for hosting anywhere.

Run:  .venv/bin/python scripts/export_static.py [dest] [--artifact]      (default dest: dist/)

Uses the saved plan in output/ (run ./run.sh or `python -m optimizer.run` first).
Re-optimizing and the data assistant are disabled in the static build since they need
the Python backend. The map draws US state outlines instead of tiles, so the build
works on hosts that block third-party images.

--artifact writes index.html as page content only (no <html>/<head>/<body> wrapper)
for hosts that add their own document skeleton, such as a claude.ai Artifact.
"""
from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB, OUT = ROOT / "web", ROOT / "output"
LEAFLET_CSS_CDN = "https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css"


def build_html(artifact: bool) -> str:
    html = (WEB / "index.html").read_text()
    html = html.replace(f'href="{LEAFLET_CSS_CDN}"', 'href="leaflet.css"')
    html = html.replace('href="/static/', 'href="').replace('src="/static/', 'src="')
    if not artifact:
        return html.replace('<html lang="en">', '<html lang="en" data-static="1">', 1)
    head = re.search(r"<head>(.*?)</head>", html, re.S).group(1)
    body = re.search(r"<body>(.*?)</body>", html, re.S).group(1)
    head = re.sub(r"\s*<meta[^>]*>", "", head)  # the host's skeleton supplies charset/viewport
    flag = '<script>document.documentElement.dataset.static = "1";</script>'
    return f"{head.strip()}\n  {flag}\n{body}"


def main(dest: Path, artifact: bool) -> None:
    plan, det = OUT / "plan.json", OUT / "details.json"
    if not (plan.exists() and det.exists()):
        sys.exit("No saved plan in output/ - run `python -m optimizer.run` first.")
    if dest.exists():
        shutil.rmtree(dest)
    (dest / "data").mkdir(parents=True)

    (dest / "index.html").write_text(build_html(artifact))
    for name in ("app.js", "chat.js", "styles.css"):
        shutil.copy(WEB / name, dest / name)
    shutil.copy(WEB / "vendor" / "leaflet-1.9.4.min.css", dest / "leaflet.css")
    shutil.copy(WEB / "us-states.geojson", dest / "data" / "us-states.geojson")
    shutil.copy(plan, dest / "data" / "plan.json")
    shutil.copy(det, dest / "data" / "details.json")
    print(f"Static dashboard written to {dest}")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    main(Path(args[0]).resolve() if args else ROOT / "dist", "--artifact" in sys.argv)

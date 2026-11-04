"""Render the architecture diagrams in this folder to SVG, with AWS's icons.

Each ``*.mmd`` here is Mermaid source; ``render.py`` writes the ``*.svg`` next
to it, which README.md and ARCHITECTURE.md show. GitHub's own Mermaid can't
load an icon pack, which is why the diagrams are committed as SVGs.

    python docs/diagrams/render.py            # every diagram
    python docs/diagrams/render.py system     # just system.mmd

The icons are AWS's official Architecture Icons. The first run downloads
AWS's icon package (about 14 MB) into ``.cache/`` and keeps only the icons
listed in ``ICONS``, as an Iconify pack Mermaid loads with the ``aws:`` prefix.
Only the rendered diagrams are committed, not the icons. AWS releases a new
package each quarter; to move to one, update ``PACKAGE_URL`` and
``PACKAGE_SHA256`` and check the paths below.

Rendering needs Node.js 22.13+ and pnpm, which fetches mermaid-cli and
puppeteer. puppeteer needs a Chrome: ``npx puppeteer browsers install
chrome-headless-shell``, or point ``PUPPETEER_EXECUTABLE_PATH`` at one you
have.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
CACHE = HERE / ".cache"

PACKAGE_URL = (
    "https://d1.awsstatic.com/onedam/marketing-channels/website/public/shared/"
    "architecture-icon-release/"
    "Icon-package_07312026.5846e92413caa21490223536cc97f1269e44fa92.zip"
)
PACKAGE_SHA256 = "d2d166c453526471749d520e0db022c459abef759d2946cf2dd1d1c992dc6526"

MERMAID_CLI = "@mermaid-js/mermaid-cli@12.2.0"
PUPPETEER = "puppeteer@25.16.0"

# Icon name in the diagrams (aws:<name>) → its SVG in AWS's package
_ARCH = "Architecture-Service-Icons_07312026"
ICONS = {
    "lambda": f"{_ARCH}/Arch_Compute/64/Arch_AWS-Lambda_64.svg",
    "dynamodb": f"{_ARCH}/Arch_Databases/64/Arch_Amazon-DynamoDB_64.svg",
    "s3": f"{_ARCH}/Arch_Storage/64/Arch_Amazon-Simple-Storage-Service_64.svg",
    "glacier": f"{_ARCH}/Arch_Storage/64/Arch_Amazon-Simple-Storage-Service-Glacier_64.svg",
    "systems-manager": f"{_ARCH}/Arch_Management-Tools/64/Arch_AWS-Systems-Manager_64.svg",
    "eventbridge": f"{_ARCH}/Arch_Application-Integration/64/Arch_Amazon-EventBridge_64.svg",
    "ses": f"{_ARCH}/Arch_Business-Applications/64/Arch_Amazon-Simple-Email-Service_64.svg",
    "sns": f"{_ARCH}/Arch_Application-Integration/64/Arch_Amazon-Simple-Notification-Service_64.svg",
    "ecr": f"{_ARCH}/Arch_Containers/64/Arch_Amazon-Elastic-Container-Registry_64.svg",
    "cloudformation": f"{_ARCH}/Arch_Management-Tools/64/Arch_AWS-CloudFormation_64.svg",
}


def icon_package() -> Path:
    """AWS's icon package, downloaded once and checked against its hash."""
    path = CACHE / PACKAGE_URL.rsplit("/", 1)[-1]
    if not path.exists():
        CACHE.mkdir(exist_ok=True)
        print(f"Downloading AWS's icon package to {path.relative_to(HERE)}")
        urllib.request.urlretrieve(PACKAGE_URL, path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != PACKAGE_SHA256:
        path.unlink()
        sys.exit(f"{path.name}: sha256 {digest}, expected {PACKAGE_SHA256}")
    return path


def icon_body(svg: str) -> tuple[str, int, int]:
    """An icon's inner SVG and its size. AWS's icons are flat: no defs, so
    ids aren't referenced and can go, keeping two copies on a page apart."""
    view_box = re.search(r'viewBox="0 0 (\d+) (\d+)"', svg)
    if not view_box:
        raise ValueError("icon has no viewBox")
    inner = re.search(r"<svg[^>]*>(.*)</svg>", svg, re.S).group(1)
    inner = re.sub(r"<title>.*?</title>", "", inner, flags=re.S)
    inner = re.sub(r'\s+id="[^"]*"', "", inner)
    return " ".join(inner.split()), int(view_box[1]), int(view_box[2])


def icon_pack() -> Path:
    """The icons in ICONS as an Iconify JSON pack, prefix ``aws``."""
    icons = {}
    with zipfile.ZipFile(icon_package()) as package:
        for name, member in ICONS.items():
            body, width, height = icon_body(package.read(member).decode())
            icons[name] = {"body": body, "width": width, "height": height}
    pack = CACHE / "aws-icons.json"
    pack.write_text(json.dumps({"prefix": "aws", "icons": icons}))
    return pack


def render(source: Path, pack: Path) -> None:
    output = source.with_suffix(".svg")
    subprocess.run(
        [
            "pnpm",
            "dlx",
            "--package",
            MERMAID_CLI,
            "--package",
            PUPPETEER,
            "mmdc",
            "--input",
            str(source),
            "--output",
            str(output),
            "--configFile",
            str(HERE / "mermaid-config.json"),
            "--backgroundColor",
            "white",
            "--iconPacksNamesAndUrls",
            f"aws#{pack.as_uri()}",
        ],
        check=True,
    )
    print(f"Wrote {output.relative_to(HERE.parent.parent)}")


def main(names: list[str]) -> None:
    sources = (
        [HERE / f"{name}.mmd" for name in names]
        if names
        else sorted(HERE.glob("*.mmd"))
    )
    missing = [s.name for s in sources if not s.exists()]
    if missing:
        sys.exit(f"No such diagram: {', '.join(missing)}")
    pack = icon_pack()
    for source in sources:
        render(source, pack)


if __name__ == "__main__":
    main(sys.argv[1:])

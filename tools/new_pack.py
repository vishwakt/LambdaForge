"""Write the packaging for a new strategy pack.

    python -m tools.new_pack my_strategies               # into packs/my_strategies/
    python -m tools.new_pack my_strategies --dir ../my-strategies

A pack keeps strategies out of this repo (in a private repo of your own, say)
and adds them to the engine's: the Dockerfile installs every pack in
``packs/``, and ``src.strategies`` merges each pack's ``STRATEGIES`` dict
into its own. This writes the pyproject that registers the entry point, an
empty ``STRATEGIES`` dict, a test that checks each strategy the way the
engine uses it, and a .gitignore for build output. It never overwrites a
file.
"""

from __future__ import annotations

import argparse
import keyword
import re
import sys
from pathlib import Path
from string import Template

PACKS = Path(__file__).resolve().parent.parent / "packs"
NAME = re.compile(r"[a-z][a-z0-9_]*")

PYPROJECT = Template("""\
[build-system]
requires = ["setuptools>=64"]
build-backend = "setuptools.build_meta"

[project]
name = "$project"
version = "0.1.0"
description = "Strategies for LambdaForge"
# Only libraries the engine doesn't install already. The image keeps the
# engine's versions, so a pack that needs different ones fails the build.
dependencies = []

# The engine finds the pack's strategies through this entry point.
[project.entry-points."lambdaforge.strategies"]
$module = "$module:STRATEGIES"

[tool.setuptools.packages.find]
include = ["$module*"]
""")

GITIGNORE = """\
__pycache__/
build/
*.egg-info/
"""

INIT = Template('''\
"""$module: a LambdaForge strategy pack.

Register each strategy the way the engine's src/strategies/__init__.py
registers the built-ins: its name, mapped to a Strategy subclass that builds
with no arguments and whose ``name`` is that same key. A name the engine or
another pack already uses fails the image build.

Import the engine from src.strategies.base, not src.strategies: this module
is loaded while src.strategies is still being built.
"""

STRATEGIES = {}
''')

TEST = Template('''\
"""Checks this pack's strategies the way the engine uses them.

Install the pack first, then run with the engine on the path:

    pip install .
    PYTHONPATH=path/to/LambdaForge python -m pytest
"""

from importlib.metadata import entry_points

import numpy as np
import pandas as pd
import pytest
from src.strategies import STRATEGIES as ENGINE_STRATEGIES
from src.strategies.base import Signal, Strategy

from $module import STRATEGIES


def sample_bars(n: int = 250) -> pd.DataFrame:
    """A seeded random walk of daily bars, in the engine's columns."""
    rng = np.random.default_rng(42)
    close = 100 * np.cumprod(1 + rng.normal(0, 0.02, n))
    return pd.DataFrame(
        {
            "open": close * (1 + rng.normal(0, 0.01, n)),
            "high": close * (1 + rng.uniform(0, 0.02, n)),
            "low": close * (1 - rng.uniform(0, 0.02, n)),
            "close": close,
            "volume": rng.integers(100_000, 10_000_000, n).astype(float),
            "vwap": close,
        },
        index=pd.date_range("2025-01-02", periods=n, freq="B"),
    )


def test_the_engine_runs_this_pack():
    registered = [ep.value for ep in entry_points(group="lambdaforge.strategies")]
    assert "$module:STRATEGIES" in registered, "install the pack: pip install ."
    assert all(ENGINE_STRATEGIES[name] is cls for name, cls in STRATEGIES.items())


@pytest.mark.parametrize("name", sorted(STRATEGIES))
def test_builds_with_no_arguments_under_its_own_name(name):
    strategy = STRATEGIES[name]()
    assert isinstance(strategy, Strategy)
    assert strategy.name == name


@pytest.mark.parametrize("name", sorted(STRATEGIES))
def test_signals_on_daily_bars(name):
    signal = STRATEGIES[name]().generate_signal("TEST", sample_bars())
    assert isinstance(signal, Signal)
    assert signal.symbol == "TEST"
    assert 0.0 <= signal.confidence <= 1.0
''')


def pack_files(name: str) -> dict[str, str]:
    """A new pack's files, keyed by their path inside the pack."""
    if not NAME.fullmatch(name) or keyword.iskeyword(name):
        raise ValueError(
            f"{name!r} isn't a usable package name: lowercase letters, digits "
            "and underscores, starting with a letter"
        )
    names = {"module": name, "project": name.replace("_", "-")}
    return {
        "pyproject.toml": PYPROJECT.substitute(names),
        ".gitignore": GITIGNORE,
        f"{name}/__init__.py": INIT.substitute(names),
        f"tests/test_{name}.py": TEST.substitute(names),
    }


def write_pack(name: str, root: Path) -> list[Path]:
    """Write a new pack under ``root``; refuse if any of its files exist."""
    files = {root / path: text for path, text in pack_files(name).items()}
    existing = [str(path) for path in files if path.exists()]
    if existing:
        raise FileExistsError(f"Not overwriting {', '.join(existing)}")
    for path, text in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return list(files)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Write the packaging for a new strategy pack."
    )
    p.add_argument("name", help="the pack's Python package name, e.g. my_strategies")
    p.add_argument("--dir", type=Path, help="where to write it (default: packs/<name>)")
    args = p.parse_args(argv)
    try:
        written = write_pack(args.name, args.dir or PACKS / args.name)
    except (ValueError, FileExistsError) as e:
        p.error(str(e))
    print("\n".join(f"wrote {path}" for path in written))
    return 0


if __name__ == "__main__":
    sys.exit(main())

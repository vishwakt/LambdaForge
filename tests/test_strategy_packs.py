"""Strategy packs: found through their entry point, merged, clashes refused.

A pack adds strategies from outside this repo. The built-ins must come
through unchanged, and a pack may never take a name already in use: lots
are exited by the strategy that opened them, by name.
"""

import os
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

from src.strategies import BUILTIN_STRATEGIES, STRATEGIES
from src.strategies.base import Action, Signal, Strategy
from src.strategies.macd import MACDStrategy
from src.strategies.packs import installed_packs, merge_strategies
from tools.new_pack import main, pack_files, write_pack

REPO = Path(__file__).resolve().parent.parent

HOLD_SOURCE = """
from src.strategies.base import Action, Signal, Strategy


class Hold(Strategy):
    name = "hold"

    def generate_signal(self, symbol, bars):
        return Signal(symbol, Action.HOLD, 0.0, "holds")
"""


class Hold(Strategy):
    name = "hold"

    def generate_signal(self, symbol: str, bars: pd.DataFrame) -> Signal:
        return Signal(symbol, Action.HOLD, 0.0, "holds")


def install(site: Path, project: str, entry_points: dict[str, str]) -> None:
    """What pip leaves on sys.path for a pack: a dist-info with its entry point."""
    info = site / f"{project.replace('-', '_')}-0.1.0.dist-info"
    info.mkdir(parents=True)
    (info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {project}\nVersion: 0.1.0\n"
    )
    lines = [f"{name} = {target}" for name, target in entry_points.items()]
    (info / "entry_points.txt").write_text(
        "[lambdaforge.strategies]\n" + "\n".join(lines) + "\n"
    )


def project_of(pyproject: str) -> dict:
    """The ``[project]`` table of a pyproject.toml."""
    # Imported here: ruff files tomllib as third-party for py39, stdlib for 3.11+.
    import tomllib

    return tomllib.loads(pyproject)["project"]


def test_without_packs_the_registry_is_the_built_ins():
    assert STRATEGIES == BUILTIN_STRATEGIES
    assert len(BUILTIN_STRATEGIES) == 8


def test_pack_strategies_join_the_built_ins_unchanged():
    merged = merge_strategies(BUILTIN_STRATEGIES, [("mine", {"hold": Hold})])
    assert merged == {**BUILTIN_STRATEGIES, "hold": Hold}
    assert list(merged)[: len(BUILTIN_STRATEGIES)] == list(BUILTIN_STRATEGIES)


def test_a_pack_reusing_a_built_in_name_is_refused():
    with pytest.raises(ValueError, match=r"'mine'.*macd \(a built-in strategy\)"):
        merge_strategies(BUILTIN_STRATEGIES, [("mine", {"macd": Hold})])


def test_two_packs_with_the_same_name_are_refused():
    packs = [("first", {"hold": Hold}), ("second", {"hold": Hold})]
    with pytest.raises(ValueError, match=r"'second'.*hold \(from pack 'first'\)"):
        merge_strategies(BUILTIN_STRATEGIES, packs)


def test_a_pack_that_is_not_a_dict_is_refused():
    with pytest.raises(TypeError, match="must point at a dict"):
        merge_strategies(BUILTIN_STRATEGIES, [("mine", [Hold])])


def test_a_pack_entry_that_is_not_a_strategy_class_is_refused():
    with pytest.raises(TypeError, match="hold must be Strategy subclasses"):
        merge_strategies(BUILTIN_STRATEGIES, [("mine", {"hold": Hold()})])


def test_an_installed_pack_is_found_through_its_entry_point(tmp_path, monkeypatch):
    (tmp_path / "found_pack.py").write_text(
        HOLD_SOURCE + '\nSTRATEGIES = {"hold": Hold}\n'
    )
    install(tmp_path, "found-pack", {"found_pack": "found_pack:STRATEGIES"})
    monkeypatch.syspath_prepend(tmp_path)

    packs = dict(installed_packs())

    assert list(packs["found_pack"]) == ["hold"]
    assert issubclass(packs["found_pack"]["hold"], Strategy)
    assert MACDStrategy is merge_strategies(BUILTIN_STRATEGIES, packs.items())["macd"]


def test_the_scaffold_registers_the_pack_under_its_own_name():
    files = pack_files("my_pack")

    project = project_of(files["pyproject.toml"])
    assert project["name"] == "my-pack"
    assert project["entry-points"]["lambdaforge.strategies"] == {
        "my_pack": "my_pack:STRATEGIES"
    }
    assert "STRATEGIES = {}" in files["my_pack/__init__.py"]
    assert {"tests/test_my_pack.py", ".gitignore"} <= set(files)


@pytest.mark.parametrize("name", ["My_Pack", "my-pack", "1pack", "_pack", "class"])
def test_the_scaffold_refuses_a_name_python_cannot_import(name):
    with pytest.raises(ValueError, match="usable package name"):
        pack_files(name)


def test_the_scaffold_never_overwrites(tmp_path):
    write_pack("my_pack", tmp_path)
    (tmp_path / "my_pack" / "__init__.py").write_text("STRATEGIES = {'kept': 1}\n")

    with pytest.raises(SystemExit):
        main(["my_pack", "--dir", str(tmp_path)])

    assert "kept" in (tmp_path / "my_pack" / "__init__.py").read_text()


def test_a_scaffolded_pack_passes_its_own_tests_once_installed(tmp_path):
    root = tmp_path / "my_pack"
    write_pack("my_pack", root)
    (root / "my_pack" / "hold.py").write_text(HOLD_SOURCE)
    (root / "my_pack" / "__init__.py").write_text(
        'from my_pack.hold import Hold\n\nSTRATEGIES = {"hold": Hold}\n'
    )
    pyproject = project_of((root / "pyproject.toml").read_text())
    install(
        tmp_path / "site",
        pyproject["name"],
        pyproject["entry-points"]["lambdaforge.strategies"],
    )

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"],
        cwd=root,
        env={
            **os.environ,
            "PYTHONPATH": os.pathsep.join(
                [str(root), str(tmp_path / "site"), str(REPO)]
            ),
        },
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "3 passed" in result.stdout

"""Strategy packs: strategies installed from outside this repo.

A pack is an installed package that registers a ``STRATEGIES`` dict under the
``lambdaforge.strategies`` entry-point group, shaped like the built-in one:
name -> ``Strategy`` subclass, built with no arguments. Its strategies join
the built-ins under their own names, and a name that's already taken is
refused, so a pack can never replace a strategy that holds open lots.

``python -m tools.new_pack <name>`` writes a pack's packaging. The Dockerfile
installs every pack in ``packs/``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from importlib.metadata import entry_points

from src.strategies.base import Strategy

ENTRY_POINT_GROUP = "lambdaforge.strategies"


def installed_packs() -> list[tuple[str, object]]:
    """Every installed pack as ``(entry-point name, its STRATEGIES dict)``.

    Needs Python 3.10+: before that ``entry_points()`` takes no ``group``.
    """
    return [(ep.name, ep.load()) for ep in entry_points(group=ENTRY_POINT_GROUP)]


def _check_pack(pack: str, strategies: object) -> Mapping:
    if not isinstance(strategies, Mapping):
        raise TypeError(
            f"Strategy pack {pack!r} must point at a dict of strategies, "
            f"not {type(strategies).__name__}"
        )
    not_strategies = sorted(
        name
        for name, cls in strategies.items()
        if not (isinstance(cls, type) and issubclass(cls, Strategy))
    )
    if not_strategies:
        raise TypeError(
            f"Strategy pack {pack!r}: {', '.join(not_strategies)} must be "
            "Strategy subclasses"
        )
    return strategies


def merge_strategies(
    builtins: Mapping[str, type[Strategy]],
    packs: Iterable[tuple[str, object]],
) -> dict[str, type[Strategy]]:
    """The built-ins plus every pack's strategies; a reused name is refused."""
    merged = dict(builtins)
    owners = dict.fromkeys(builtins, "a built-in strategy")
    for pack, strategies in packs:
        checked = _check_pack(pack, strategies)
        clashes = sorted(set(checked) & set(merged))
        if clashes:
            raise ValueError(
                f"Strategy pack {pack!r} reuses taken names: "
                + ", ".join(f"{name} ({owners[name]})" for name in clashes)
            )
        merged = {**merged, **checked}
        owners = {**owners, **dict.fromkeys(checked, f"from pack {pack!r}")}
    return merged

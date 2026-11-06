# Strategy packs

Each folder here is a strategy pack: a package that adds its own strategies
to the built-ins. The Dockerfile installs every pack in this folder, and
everything here but this file is gitignored, so a pack's code stays in its
own repo.

```bash
python -m tools.new_pack my_strategies   # writes packs/my_strategies/
```

See [Strategy packs](../README.md#strategy-packs) for how they work.

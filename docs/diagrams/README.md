# Diagrams

Diagrams with AWS service icons live here as Mermaid source (`*.mmd`), each
rendered to the SVG next to it. README.md and ARCHITECTURE.md show the SVGs:
GitHub's own Mermaid can't load an icon pack, so an `aws:` icon wouldn't show
there. Diagrams without AWS icons stay as Mermaid blocks in the Markdown,
where GitHub renders them.

| Diagram | Shown in |
|---|---|
| [`system.mmd`](system.mmd) | README (Architecture) and ARCHITECTURE.md (System Architecture) |

## Changing a diagram

Edit the `.mmd` file, then render it and commit both files:

```bash
python docs/diagrams/render.py system
```

An AWS service is a node with an icon, for example
`monitor@{ icon: "aws:lambda", label: "MonitorStops", pos: "b", h: 56 }`.
The icons available are the keys of `ICONS` in [`render.py`](render.py); to
add one, map a new name to its SVG in AWS's package.

Rendering needs Node.js 22.13 or later and pnpm. `render.py` fetches
mermaid-cli and puppeteer through `pnpm dlx`, and puppeteer needs a Chrome:
run `npx puppeteer browsers install chrome-headless-shell` once, or set
`PUPPETEER_EXECUTABLE_PATH` to a Chrome you already have.

## The icons

The icons are AWS's official [Architecture Icons](https://aws.amazon.com/architecture/icons/),
from the package AWS released on 2026-07-31. On its first run `render.py`
downloads that package (about 14 MB) into `.cache/`, checks its SHA-256, and
builds an Iconify pack of just the icons it uses. Only the rendered diagrams
are committed, not the icons themselves. AWS allows customers to use these
icons to create architecture diagrams. Third-party services, such as Alpaca
and Telegram, are plain shapes with no logos.

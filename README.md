# pr-brief — Claude Code skill

An interactive, browser-based PR review workflow for [Claude Code](https://docs.claude.com/en/docs/claude-code/overview). It pulls a pull request via `gh`, asks an Opus agent to group every changed file into features and author a **narrative layer** of "explain pills", and launches a local UI with GitHub-style stacked diffs, click-and-drag multi-line inline commenting, and realtime comment posting.

This repository is a **plugin marketplace**. It ships one plugin: `pr-brief`.

What reviewers get:

- A reviewer-ordered file list (migrations first, tests last) grouped into the features the PR actually contains
- Two parallel narrative layers next to each diff:
  - Purple ✨ **explain pills** — short AI-authored callouts that connect each block to the broader change
  - Red ⚠ **critique pills** with a severity score S1–S5 — actual review concerns, optional `suggested_change` blocks, and a "Use as comment →" button that turns a critique into an inline GitHub comment in one click
- Click-and-drag multi-line inline comments that POST to GitHub in realtime via `gh api`
- Runs locally; the only external call is to the Anthropic API your Claude Code session is already using

---

## Demo

[![Watch the demo](https://img.youtube.com/vi/9d9u7UEAgLU/maxresdefault.jpg)](https://youtu.be/9d9u7UEAgLU)

---

## Install

In Claude Code:

```
/plugin marketplace add lucastononro/pr-brief
/plugin install pr-brief@pr-brief-marketplace
```

To update later:

```
/plugin marketplace update pr-brief-marketplace
```

### Prerequisites

- `gh` CLI installed and authenticated (`gh auth status`)
- `python3` on `PATH`
- A modern browser

---

## Use

Once installed, ask Claude any of:

- "review this pr"
- "brief pr 1234"
- "narrate pr https://github.com/owner/repo/pull/1234"

…or invoke the skill explicitly:

```
/pr-brief:pr-brief 1234
```

Claude will fetch the PR, build a feature plan + narrative, and open a local UI at `http://localhost:7681`. Inline comments save in realtime — each save POSTs to GitHub via `gh api`.

All artifacts live under `~/.claude/pr-review/pr-<num>/`. The target repo is never modified.

---

## Repo layout

```
.
├── .claude-plugin/
│   └── marketplace.json          # the marketplace catalog
└── plugins/
    └── pr-brief/                 # the plugin
        ├── .claude-plugin/
        │   └── plugin.json       # plugin manifest
        └── skills/
            └── pr-brief/
                ├── SKILL.md      # skill definition + instructions
                └── templates/
                    ├── index.html  # browser UI
                    └── server.py   # local stdlib HTTP server
```

---

## Develop locally

Test the plugin without publishing:

```bash
claude --plugin-dir ./plugins/pr-brief
```

Or test the marketplace end-to-end from a sibling directory:

```
/plugin marketplace add /absolute/path/to/pr-brief
/plugin install pr-brief@pr-brief-marketplace
```

Validate the marketplace JSON:

```bash
claude plugin validate .
```

---

## License

MIT

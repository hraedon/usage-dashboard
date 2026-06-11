# AGENTS.md

Conventions and quick reference for agents (and humans) working on usage-dashboard.

## What this is

<!-- TODO: describe the project -->

## Build / test / lint

```bash
uv venv && uv pip install -e ".[dev]"
.venv/bin/pytest -q
.venv/bin/ruff check .
.venv/bin/mypy src
```

## Hard rules

- **Spec acceptance criteria are the boundary.** Don't add features beyond the spec without a tracked breadcrumb or plan entry.

## Active breadcrumbs

Check `breadcrumbs/active/` for active work items. Resolved items move to `breadcrumbs/resolved/`.

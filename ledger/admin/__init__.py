"""Operational CLIs that need the shipped package layout to run in
production (`fly ssh console -C "python -m ledger.admin.keys mint ..."`).

Unlike `scripts/` -- which ships in neither the wheel (see
`[tool.hatch.build.targets.wheel]` in `pyproject.toml`) nor the Docker image
-- everything under `ledger.admin` is part of the `ledger` package and is
therefore reachable on a deployed machine."""

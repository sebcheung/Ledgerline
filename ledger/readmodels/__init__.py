"""Framework-free read models backing the dashboard (SPEC.md §12 Phase 6).

Like `ledger.core` and `ledger.reconciliation`, this package never imports
`fastapi`, `starlette`, or `jinja2` -- `dashboard/` (thin routes, templates,
pure formatters) is the only layer that does. Every function here accepts an
`AsyncSession`, only SELECTs, and never commits, matching SPEC.md §13's rule
for every other service module. Rows are returned as frozen
`@dataclass(slots=True)` values (not Pydantic -- there is no serialisation
boundary; Jinja reads attributes directly).

Kept under `ledger/` rather than `dashboard/` for two reasons that happen to
agree: the layering rule above, and the coverage gate
(`[tool.coverage.run] source`), which covers `ledger/` but not `dashboard/`
-- see docs/DECISIONS.md Phase 6.
"""

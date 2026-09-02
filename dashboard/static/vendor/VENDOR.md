# Vendored third-party assets

Vendored rather than loaded from a CDN so `docker compose up` and the test
suite work with zero egress, and so an operator console displaying ledger
balances never executes script from a host this repo doesn't control. See
`docs/DECISIONS.md` Phase 6.

| File | Project | Version | License | SHA-256 |
|---|---|---|---|---|
| `htmx.min.js` | [htmx.org](https://htmx.org/) | 2.0.4 | BSD 2-Clause | `e209dda5c8235479f3166defc7750e1dbcd5a5c1808b7792fc2e6733768fb447` |
| `htmx-ext-sse.js` | [htmx-ext-sse](https://github.com/bigskysoftware/htmx-extensions) | 2.2.2 | BSD 2-Clause | `83eca6fa0611fe2b0bf1700b424b88b5eced38ef448ef9760a2ea08fbc875611` |

To bump a version: download the new file, recompute its SHA-256
(`sha256sum <file>`), and update both the file and this table in the same
commit.

# doclint public API contract (assumed by the test suite)

The tests in this directory were written first, against the public surface
below. The Implementer builds `agentos_doclint` to satisfy exactly this. Tests
assert through the CLI exit code and message text, never internal helpers, so
private module/function names are free to change.

## Package

`import agentos_doclint`

## Entrypoint

`agentos_doclint.main(argv: list[str]) -> int`
- Accepts `["--repo-root", "<path>"]`; `--repo-root` defaults to the git
  top-level when omitted.
- Returns `0` when the linted tree is clean, non-zero otherwise.
- Prints one line per finding (to stdout or stderr; the suite reads both).

## Findings API (documented, not asserted directly)

`agentos_doclint.lint(repo_root: Path) -> list[Finding]` runs the generate +
lint phases in memory and returns the findings `main` prints. A `Finding`
carries at least the repo-relative doc path, the offending citation/field, and
a human reason. Tests exercise this through `main`.

## Generator surface

`agentos_doclint.render_index_table(repo_root: Path) -> str`
- Renders the `docs/interfaces.md` seam-table block from front-matter, ordered
  by the `order` field (ties are a hard error). Byte-stable across runs and
  machines. Globs `docs/interfaces/*/INTERFACE.md` (no hardcoded seam list).

## Constants

`agentos_doclint.SOURCE_EXTENSIONS: tuple[str, ...]` — the single recognized
extension list, shared by the path rule and the raw line-ban rule. The test
list parametrizes over this constant so it cannot drift from the tool. Must
include at least: `py rs ts tsx yaml yml json sh toml md`.

## Linted root

`docs/` EXCLUDING `docs/adr/` (E2). ADR docs are never path/symbol/line
checked.

## Expected message fragments (the reason the suite asserts)

- Nonexistent path: names the doc, the citation, and contains `does not exist`.
- Unresolvable symbol: names the citation/symbol and contains `does not resolve`.
- Shorthand `::` with no path (rule 2): names the citation and contains
  `full repo-relative path`.
- Line-number / `#L` citation: names the doc and the offending coordinate.
- Missing/unpaired generated marker: contains `marker`.
- Front-matter missing required field: contains the field name and `required`.
- Invalid `kind`: contains `kind` and the offending value.
- Syntax error in a cited `.py` file: names the file and contains `syntax`.

## Front-matter schema (per `INTERFACE.md`)

Required: `seam`, `kind` (`CLEAN` / `SOFT` / `NONE`, optional `, qualifier`),
`impls`, `grade`, `epics` (list of `"#N"` / `"ADR-XXXX"`), `order` (int).
Optional: `epic_note`.

## Generated regions (marker-delimited)

- Index seam-table: `<!-- BEGIN GENERATED: seam-table (agentos dev docs-lint) -->`
  ... `<!-- END GENERATED: seam-table -->`, rows
  `| Seam | Kind | Impls | Grade | Epic(s) | INTERFACE.md |`.
- Per-doc header blockquote: `<!-- BEGIN GENERATED: header ... -->` ...
  `<!-- END GENERATED: header -->`.

# Benchmark: PR #2242 vs released coverage 7.15.2 — HTML report generation

**PR:** coveragepy/coveragepy#2242 — "perf: reuse SQLite connection during HTML reports"
(head `7a73c9b`, installed as 7.15.3a0.dev1, C tracer built)
**Baseline:** coverage 7.15.2 from PyPI (C tracer)
**Data:** pyca/cryptography @ 49.0.0 — full test suite (4264 passed, 208 skipped) run
under coverage 7.15.2 with cryptography's own config (branch coverage, sources =
`cryptography` + `tests/`). Result: 26,115 statements / 96% covered / 2.3 MB
`.coverage` file / 192 source files → 200-file HTML report.
**Machine:** Linux, 4 cores, Python 3.11.15. Runs interleaved A/B, `htmlcov`
deleted before every run (to defeat incremental reuse), n=10 per configuration.

## Results

### Scenario 1: cryptography's real config (has `[tool.coverage.paths]`)

| metric | 7.15.2 | PR #2242 | delta |
|---|---|---|---|
| CLI `coverage html` wall time | 10.004s ± 0.499 | 9.997s ± 0.573 | **1.001x (no change)** |
| in-process `html_report()` | 9.838s ± 0.285 | 9.967s ± 0.545 | 0.987x (noise) |

### Scenario 2: same data, config without `[paths]`

| metric | 7.15.2 | PR #2242 | delta |
|---|---|---|---|
| CLI `coverage html` wall time | 9.801s ± 0.253 | 9.597s ± 0.129 | **1.021x (~2% faster)** |
| in-process `html_report()` | 9.694s ± 0.242 | 9.512s ± 0.440 | 1.019x |

### Correctness

All 199 rendered HTML/CSS/JS output files are byte-identical between the two
versions after scrubbing the embedded version string; only `status.json`
differs (it records the generating version).

## Why the PR shows no benefit on this dataset

- When `[tool.coverage.paths]` is configured (as cryptography does),
  `Coverage._prepare_data_for_reporting()` in 7.15.2 already copies the data
  into an **in-memory** SQLite database before reporting. For an in-memory db,
  `SqliteDb.close()` is a no-op, so the connection is already reused: measured
  **5** `sqlite3.connect()` calls per report, totaling **0.5 ms**. The PR's
  `keep_db_open()` has nothing left to save.
- Without `[paths]`, 7.15.2 really does reconnect per query — measured **962**
  `sqlite3.connect()` calls vs **1** with the PR. That saves ~0.2s here (~2%),
  because per-connect cost is only ~0.2 ms.
- cProfile of a 10s report: ~7.2s tokenizing source for syntax highlighting
  (`phystokens.source_token_lines` / stdlib `tokenize`), ~5.8s parsing/AST
  analysis (`parser.parse_source`, `ast.parse`, `compile`), plus template
  rendering. SQLite work is not in the top 18 functions. Connection overhead is
  a fixed, tiny cost per query; on a large report the per-file rendering work
  dwarfs it.

The PR author's reported 27% (1.154s → 0.846s) is consistent with a small
dataset and no `[paths]` mapping, where the ~1000 reconnects are a large share
of a ~1s total. On a real-world-sized dataset like cryptography's, the change
is neutral-to-slightly-positive (0–2%) and produces identical output.

## Reproducing

```
python bench.py 10                 # scenario 1 (project's own config)
python bench.py 10 nopaths.toml    # scenario 2 (COVERAGE_RCFILE without [paths])
```

`bench.py` expects the venvs and a cryptography checkout with `.coverage` as
laid out in the paths at the top of the script; raw per-run timings are in
`bench-results*.json`.

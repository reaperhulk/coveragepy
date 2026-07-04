# Benchmark: this branch vs released coverage.py 7.15.0

Workload: pyca/cryptography 49.0.0 test suite (4,264 tests passed, 208
skipped), used to collect coverage data and generate reports.

- Date: 2026-07-04
- Machine: 4-core x86_64 Linux container, 15 GiB RAM
- Python: CPython 3.11.15
- Released: `coverage==7.15.0` wheel from PyPI (C tracer)
- Branch: this repo installed via `pip install .` (reports as 7.15.1a0.dev1,
  C tracer compiled locally)
- Coverage scope: `--source=cryptography,tests` — 200 measured files,
  26,115 statements, with branch data disabled (line coverage), 2.3 MB
  SQLite data file at 96% total coverage.

## Run phase (`coverage run -m pytest tests/ -q`)

Three interleaved runs each; times are wall clock for the whole command.

| configuration        | run 1  | run 2  | run 3  | median |
|----------------------|--------|--------|--------|--------|
| no coverage          | 37.4s  |        |        | 37.4s  |
| released 7.15.0      | 38.1s  | 37.9s  | 38.3s  | 38.1s  |
| this branch          | 39.1s  | 38.7s  | 39.4s  | 39.1s  |

Tracing overhead is small for this suite (most work happens inside
cryptography's Rust extension, not Python bytecode). The branch's ~1s /
~2.5% deficit is consistent across runs but the source trees are
identical, so it comes from how the C tracer was compiled (manylinux
wheel toolchain vs. local gcc), not from any code change.

## Report phase

All report commands read the *same* canonical data file, so both versions
parse identical data. Five iterations per command per version; medians:

| command  | 7.15.0 (released) | this branch | delta |
|----------|-------------------|-------------|-------|
| report   | 3.40s             | 3.41s       | +0.2% |
| html     | 11.23s            | 11.22s      | -0.1% |
| xml      | 3.90s             | 3.86s       | -1.2% |
| json     | 4.41s             | 4.39s       | -0.4% |
| lcov     | 6.32s             | 6.33s       | +0.1% |

All deltas are within run-to-run noise.

## Output equivalence

Generated reports were compared across versions: JSON and LCOV outputs are
byte-identical; XML and HTML differ only in the embedded coverage.py
version string.

## Conclusion

This branch is functionally identical to released 7.15.0 (`diff -r` of the
`coverage/` package shows only `version.py` differs), and the benchmarks
confirm parity: report generation is within noise of the release in every
format, and run-phase overhead differs only by C-extension build effects.

## Reproducing

```sh
python -m venv venv-rel && venv-rel/bin/pip install coverage==7.15.0
python -m venv venv-branch && venv-branch/bin/pip install <this repo>
# both venvs: pip install cryptography pytest pytest-benchmark pretend certifi iso8601
git clone --depth 1 --branch 49.0.0 https://github.com/pyca/cryptography.git
pip install ./cryptography/vectors   # in both venvs
cd cryptography
coverage run --source=cryptography,tests -m pytest tests/ -q
./bench_reports.sh                   # times report/html/xml/json/lcov x5
```

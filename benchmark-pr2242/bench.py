"""Benchmark `coverage html` for released coverage vs PR #2242 build.

Runs from the cryptography repo dir (where .coverage lives). Two modes:
  1. CLI wall time: `python -m coverage html` as a subprocess (includes startup).
  2. In-process: time c.load() + c.html_report() inside the interpreter.

Runs are interleaved (A, B, A, B, ...) to smooth machine drift.
"""

import json
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path

S = Path("/tmp/claude-0/-home-user-coveragepy/0ffea5c1-9605-5406-bd2a-cd02909ea87d/scratchpad")
CRYPTO = S / "cryptography"
VENVS = {
    "release-7.15.2": S / "venv-release" / "bin" / "python",
    "pr-2242": S / "venv-pr" / "bin" / "python",
}
N_RUNS = int(sys.argv[1]) if len(sys.argv) > 1 else 10
import os
ENV = dict(os.environ)
OUT_NAME = "bench-results.json"
if len(sys.argv) > 2:
    ENV["COVERAGE_RCFILE"] = sys.argv[2]
    OUT_NAME = "bench-results-nopaths.json"

INPROC_SNIPPET = """
import sys, time
import coverage
t0 = time.perf_counter()
c = coverage.Coverage()
c.load()
t1 = time.perf_counter()
c.html_report(directory="htmlcov")
t2 = time.perf_counter()
print(f"LOAD={t1-t0:.6f} HTML={t2-t1:.6f}")
"""


def clean():
    shutil.rmtree(CRYPTO / "htmlcov", ignore_errors=True)


def run_cli(py):
    clean()
    t0 = time.perf_counter()
    r = subprocess.run(
        [str(py), "-m", "coverage", "html", "-q"],
        cwd=CRYPTO, capture_output=True, text=True, env=ENV,
    )
    dt = time.perf_counter() - t0
    assert r.returncode == 0, r.stderr
    return dt


def run_inproc(py):
    clean()
    r = subprocess.run(
        [str(py), "-c", INPROC_SNIPPET],
        cwd=CRYPTO, capture_output=True, text=True, env=ENV,
    )
    assert r.returncode == 0, r.stderr
    parts = dict(p.split("=") for p in r.stdout.split())
    return float(parts["LOAD"]), float(parts["HTML"])


def stats(xs):
    return {
        "mean": statistics.mean(xs),
        "stdev": statistics.stdev(xs) if len(xs) > 1 else 0.0,
        "min": min(xs),
        "max": max(xs),
        "n": len(xs),
    }


def main():
    results = {name: {"cli": [], "load": [], "html": []} for name in VENVS}

    # Warmup: one run each (also validates both work)
    for name, py in VENVS.items():
        dt = run_cli(py)
        nfiles = len(list((CRYPTO / "htmlcov").glob("*.html")))
        print(f"warmup {name}: {dt:.3f}s, {nfiles} html files", flush=True)

    # Interleaved CLI runs
    for i in range(N_RUNS):
        for name, py in VENVS.items():
            results[name]["cli"].append(run_cli(py))
        print(f"cli round {i+1}/{N_RUNS} done", flush=True)

    # Interleaved in-process runs
    for i in range(N_RUNS):
        for name, py in VENVS.items():
            load, html = run_inproc(py)
            results[name]["load"].append(load)
            results[name]["html"].append(html)
        print(f"inproc round {i+1}/{N_RUNS} done", flush=True)

    out = {}
    for name in VENVS:
        out[name] = {k: stats(v) for k, v in results[name].items()}
        out[name]["raw"] = results[name]
    (S / OUT_NAME).write_text(json.dumps(out, indent=2))

    print()
    for metric, label in [("cli", "CLI `coverage html` wall time"),
                          ("load", "in-process data load()"),
                          ("html", "in-process html_report()")]:
        print(f"== {label} ==")
        for name in VENVS:
            s = out[name][metric]
            print(f"  {name:16s} mean {s['mean']:.3f}s ± {s['stdev']:.3f}  "
                  f"(min {s['min']:.3f}, max {s['max']:.3f}, n={s['n']})")
        a = out["release-7.15.2"][metric]["mean"]
        b = out["pr-2242"][metric]["mean"]
        if a and b:
            print(f"  speedup: {a/b:.3f}x  ({(1-b/a)*100:+.1f}% time)")
        print()


if __name__ == "__main__":
    main()

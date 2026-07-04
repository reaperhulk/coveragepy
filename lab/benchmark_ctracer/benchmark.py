# Licensed under the Apache License: http://www.apache.org/licenses/LICENSE-2.0
# For details: https://github.com/coveragepy/coveragepy/blob/main/NOTICE.txt

"""Benchmark and profiling harness for CTracer.

This harness answers "where does CTracer spend its time?" two independent
ways:

1. Ablation timing (the ``run`` command): the C extension is compiled several
   times, each build with one operation compiled out (see the ABLATE_* flags
   in coverage/ctracer/util.h).  The difference between a normal build and an
   ablated build is the cost of the removed operation.  A COLLECT_STATS build
   counts the exact number of trace events, so costs can be reported in
   nanoseconds per event.

2. Instruction profiling (the ``profile`` command): runs a workload under
   valgrind --tool=callgrind with collection toggled on only inside
   CTracer_trace, then prints the annotated cost breakdown, i.e. exactly which
   C functions the tracer spends its instructions in.

The tracer is driven standalone, wired up the same way coverage's Collector
wires it (see coverage/collector.py Collector._start_tracer), so the numbers
reflect real tracer behavior without the rest of coverage.py in the way.

Usage:
    python3 lab/benchmark_ctracer/benchmark.py run [--quick] [--json out.json]
    python3 lab/benchmark_ctracer/benchmark.py profile
    python3 lab/benchmark_ctracer/benchmark.py selftest
    python3 lab/benchmark_ctracer/benchmark.py build

See README.md in this directory for details and sample results.
"""

from __future__ import annotations

import argparse
import gc
import importlib.machinery
import importlib.util
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import sysconfig
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
CTRACER_DIR = REPO / "coverage" / "ctracer"
BUILD_DIR = HERE / "build"

SOURCES = ["datastack.c", "filedisp.c", "module.c", "tracer.c"]
HEADERS = ["datastack.h", "filedisp.h", "stats.h", "tracer.h", "util.h"]
BASE_CFLAGS = ["-O2", "-g", "-fno-omit-frame-pointer", "-fPIC", "-shared"]

# Buildable variants of the tracer.  "stats" is used to count events, the
# rest are timed.  Each ablation removes one operation; comparing it with
# "normal" measures that operation's cost.
VARIANTS = {
    "normal": [],
    "stats": ["-DCOLLECT_STATS"],
    "do_nothing": ["-DDO_NOTHING"],
    "no_record": ["-DABLATE_RECORD"],
    "no_set_add": ["-DABLATE_SET_ADD"],
    "no_lock": ["-DABLATE_LOCK"],
    "memo_cache": ["-DABLATE_TRACE_CACHE"],
}

# What each timed variant tells us, for the report.
VARIANT_NOTES = {
    "baseline": "no tracing at all",
    "pytracer": "pure-Python tracer, for scale",
    "do_nothing": "trace fn returns immediately: interpreter dispatch cost",
    "no_record": "no line-number ints created, no set adds",
    "no_set_add": "line-number ints created, but not added to the set",
    "no_lock": "no lock_data/unlock_data Python calls on call events",
    "memo_cache": "should_trace_cache dict lookup memoized on call events",
    "normal": "the real tracer",
}

DEFAULT_VARIANTS = [
    "baseline", "do_nothing", "no_record", "no_set_add",
    "no_lock", "memo_cache", "normal",
]
DEFAULT_WORKLOADS = ["lines", "lines_hi", "calls", "branchy", "generators", "recursion"]
DEFAULT_MODES = ["lines", "arcs"]
DEFAULT_N = 100_000
DEFAULT_REPEATS = 5
PROFILE_N = 20_000


# ---------------------------------------------------------------------------
# Building variant tracer modules
# ---------------------------------------------------------------------------

def build_tag() -> str:
    return f"cp{sys.version_info.major}{sys.version_info.minor}"


def variant_so(variant: str) -> Path:
    return BUILD_DIR / build_tag() / variant / "tracer.so"


def build_variant(variant: str, force: bool = False) -> Path:
    """Compile one variant of the tracer.  Returns the path to the .so."""
    out = variant_so(variant)
    flags = VARIANTS[variant]
    sources = [CTRACER_DIR / s for s in SOURCES]
    deps = sources + [CTRACER_DIR / h for h in HEADERS]

    stamp = out.with_suffix(".stamp")
    fingerprint = json.dumps(
        {
            "cflags": BASE_CFLAGS + flags,
            "deps": {d.name: d.stat().st_mtime for d in deps},
        },
        sort_keys=True,
    )
    if not force and out.exists() and stamp.exists() and stamp.read_text() == fingerprint:
        return out

    out.parent.mkdir(parents=True, exist_ok=True)
    cc = os.environ.get("CC", "gcc")
    cmd = [
        cc, *BASE_CFLAGS, *flags,
        "-I" + sysconfig.get_path("include"),
        *map(str, sources),
        "-o", str(out),
    ]
    print(f"building {variant}: {' '.join(cmd)}", file=sys.stderr)
    subprocess.run(cmd, check=True)
    stamp.write_text(fingerprint)
    return out


_loaded_variants: dict = {}


def load_variant(variant: str):
    """Import a built tracer variant as a module object.

    Loading the same .so twice in one process would return an empty module
    (module.c only populates the module on first init), so cache them.
    """
    if variant in _loaded_variants:
        return _loaded_variants[variant]
    so = build_variant(variant)
    loader = importlib.machinery.ExtensionFileLoader("tracer", str(so))
    spec = importlib.util.spec_from_file_location("tracer", str(so), loader=loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    _loaded_variants[variant] = mod
    return mod


# ---------------------------------------------------------------------------
# Wiring a tracer the way Collector does
# ---------------------------------------------------------------------------

def _no_op():
    pass


def make_tracer(tracer_class, disp_class, arcs: bool, traced_file: str):
    """Create and wire a tracer like Collector._start_tracer does.

    Only `traced_file` gets a trace=True disposition; every other file
    (including this harness) is looked up in should_trace_cache and skipped,
    just as in a real coverage run.
    """
    data: dict = {}

    def should_trace(filename, frame):
        disp = disp_class()
        disp.original_filename = filename
        disp.canonical_filename = filename
        disp.source_filename = filename
        disp.reason = ""
        disp.file_tracer = None
        disp.has_dynamic_filename = False
        disp.trace = filename == traced_file
        return disp

    def warn(msg, slug=None, once=False):
        print(f"tracer warning: {msg}", file=sys.stderr)

    tracer = tracer_class()
    tracer.data = data
    tracer.trace_arcs = arcs
    tracer.should_trace = should_trace
    tracer.should_trace_cache = {}
    tracer.warn = warn
    tracer.lock_data = _no_op
    tracer.unlock_data = _no_op
    if hasattr(tracer, "concur_id_func"):
        tracer.concur_id_func = None
    if hasattr(tracer, "file_tracers"):
        tracer.file_tracers = {}
    if hasattr(tracer, "threading"):
        tracer.threading = None
    if hasattr(tracer, "check_include"):
        tracer.check_include = lambda filename, frame: False
    if hasattr(tracer, "should_start_context"):
        tracer.should_start_context = None
    if hasattr(tracer, "switch_context"):
        tracer.switch_context = None
    if hasattr(tracer, "disable_plugin"):
        tracer.disable_plugin = lambda disp: warn("plugin disabled")
    return tracer, data


def get_workload(name: str):
    sys.path.insert(0, str(HERE))
    try:
        import workloads
    finally:
        sys.path.pop(0)
    fn = workloads.WORKLOADS[name]
    return fn, fn.__code__.co_filename


# ---------------------------------------------------------------------------
# Worker: run one (variant, workload, mode) measurement in this process
# ---------------------------------------------------------------------------

def cmd_worker(args) -> None:
    fn, traced_file = get_workload(args.workload)
    arcs = args.mode == "arcs"

    tracer = None
    data = None
    if args.variant == "pytracer":
        sys.path.insert(0, str(REPO))
        from coverage.pytracer import PyTracer

        # PyTracer reads dispositions by attribute, so reuse the C class.
        disp_class = load_variant("normal").CFileDisposition
        tracer, data = make_tracer(PyTracer, disp_class, arcs, traced_file)
    elif args.variant != "baseline":
        mod = load_variant(args.variant)
        tracer, data = make_tracer(mod.CTracer, mod.CFileDisposition, arcs, traced_file)

    # Warm up untraced: code objects, ranges, etc.
    fn(1000)

    if tracer is not None:
        tracer.start()
    times = []
    for _ in range(args.repeats):
        gc.collect()
        gc.disable()
        t0 = time.perf_counter()
        fn(args.n)
        t1 = time.perf_counter()
        gc.enable()
        times.append(t1 - t0)
    stats = None
    if tracer is not None:
        stats = tracer.get_stats()
        tracer.stop()

    result = {
        "variant": args.variant,
        "workload": args.workload,
        "mode": args.mode,
        "n": args.n,
        "times": times,
        "stats": stats,
        "data_sizes": {
            os.path.basename(f): len(d) for f, d in (data or {}).items()
        },
    }
    print(json.dumps(result))


def run_worker(variant, workload, mode, n, repeats, under=None) -> dict:
    """Run one measurement in a fresh subprocess.  Returns its JSON result."""
    cmd = (under or []) + [
        sys.executable, str(HERE / "benchmark.py"), "worker",
        "--variant", variant, "--workload", workload, "--mode", mode,
        "--n", str(n), "--repeats", str(repeats),
    ]
    proc = subprocess.run(cmd, check=True, capture_output=True, text=True)
    # Take the last line: valgrind etc. may write banners to stdout.
    return json.loads(proc.stdout.strip().splitlines()[-1])


# ---------------------------------------------------------------------------
# The timing suite
# ---------------------------------------------------------------------------

def fmt_ms(t: float) -> str:
    return f"{t * 1000:8.1f}ms"


def print_environment() -> None:
    cpu = ""
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("model name"):
                    cpu = line.split(":", 1)[1].strip()
                    break
    except OSError:
        pass
    print(f"Python {sys.version.split()[0]} on {platform.platform()}")
    if cpu:
        print(f"CPU: {cpu}")
    print()


def cmd_run(args) -> None:
    n = args.n or (DEFAULT_N // 10 if args.quick else DEFAULT_N)
    repeats = args.repeats or (3 if args.quick else DEFAULT_REPEATS)
    workloads = args.workloads.split(",") if args.workloads else DEFAULT_WORKLOADS
    modes = args.modes.split(",") if args.modes else DEFAULT_MODES
    variants = args.variants.split(",") if args.variants else list(DEFAULT_VARIANTS)
    if args.pytracer and "pytracer" not in variants:
        variants.append("pytracer")

    for v in variants:
        if v not in ("baseline", "pytracer"):
            build_variant(v)
    build_variant("stats")

    print_environment()
    print(f"n={n} repeats={repeats} (times are the minimum of {repeats} runs)")
    print()

    all_results = []
    for workload in workloads:
        for mode in modes:
            # Count events exactly, with a COLLECT_STATS build.
            counts = run_worker("stats", workload, mode, n, 1)["stats"]
            events = counts["calls"] + counts["lines"] + counts["returns"] + counts["others"]

            results = {}
            for variant in variants:
                results[variant] = run_worker(variant, workload, mode, n, repeats)
                all_results.append(results[variant])

            base = min(results["baseline"]["times"]) if "baseline" in results else None
            print(
                f"== workload {workload!r}, tracing {mode} == "
                f"(events: {counts['calls']:,} calls, {counts['lines']:,} lines, "
                f"{counts['returns']:,} returns)"
            )
            header = f"    {'variant':<12} {'time':>10} {'vs base':>8} {'ns/event':>9}   note"
            print(header)
            for variant in variants:
                t = min(results[variant]["times"])
                ratio = f"{t / base:6.1f}x" if base else ""
                per_event = ""
                if base is not None and variant != "baseline":
                    per_event = f"{(t - base) / events * 1e9:7.0f}"
                print(
                    f"    {variant:<12} {fmt_ms(t)} {ratio:>8} {per_event:>9}"
                    f"   {VARIANT_NOTES.get(variant, '')}"
                )

            # Ablation deltas: cost of each removed operation.
            def mintime(v):
                return min(results[v]["times"]) if v in results else None

            t_normal = mintime("normal")
            if t_normal is not None:
                # Only report a per-event cost if there are enough of that
                # event for the delta to be meaningful.
                MIN_EVENTS = 10_000
                line_evts = counts["lines"] + (counts["returns"] if mode == "arcs" else 0)
                call_evts = counts["calls"]
                if line_evts < MIN_EVENTS:
                    line_evts = 0
                if call_evts < MIN_EVENTS:
                    call_evts = 0
                deltas = []
                if mintime("no_record") is not None and line_evts:
                    deltas.append((
                        "int creation + set add (per line event)",
                        (t_normal - mintime("no_record")) / line_evts,
                    ))
                if mintime("no_set_add") is not None and line_evts:
                    deltas.append((
                        "set add alone (per line event)",
                        (t_normal - mintime("no_set_add")) / line_evts,
                    ))
                    if mintime("no_record") is not None:
                        deltas.append((
                            "int creation alone (per line event)",
                            (mintime("no_set_add") - mintime("no_record")) / line_evts,
                        ))
                if mintime("no_lock") is not None and call_evts:
                    deltas.append((
                        "lock/unlock Python calls (per call event)",
                        (t_normal - mintime("no_lock")) / call_evts,
                    ))
                if mintime("memo_cache") is not None and call_evts:
                    deltas.append((
                        "should_trace_cache dict lookup (per call event)",
                        (t_normal - mintime("memo_cache")) / call_evts,
                    ))
                if mintime("do_nothing") is not None and events:
                    deltas.append((
                        "whole tracer body (per event)",
                        (t_normal - mintime("do_nothing")) / events,
                    ))
                if mintime("do_nothing") is not None and base is not None and events:
                    deltas.append((
                        "interpreter trace dispatch (per event)",
                        (mintime("do_nothing") - base) / events,
                    ))
                print("    -- ablation deltas --")
                for label, cost in deltas:
                    print(f"    {cost * 1e9:8.0f} ns  {label}")
            print()

    if args.json:
        Path(args.json).write_text(json.dumps(all_results, indent=2))
        print(f"raw results written to {args.json}")


# ---------------------------------------------------------------------------
# Callgrind profiling
# ---------------------------------------------------------------------------

ANNOTATE_LINE = re.compile(r"^\s*([\d,]+)\s+\(\s*[\d.]+%\)?")


def total_ir(callgrind_out: Path) -> int:
    text = subprocess.run(
        ["callgrind_annotate", str(callgrind_out)],
        check=True, capture_output=True, text=True,
    ).stdout
    for line in text.splitlines():
        if "PROGRAM TOTALS" in line:
            return int(line.split()[0].replace(",", ""))
    return 0


def annotate(callgrind_out: Path, top: int, inclusive: bool = False) -> None:
    """Print the top function rows of callgrind_annotate output."""
    cmd = ["callgrind_annotate", "--threshold=99.9"]
    if inclusive:
        cmd.append("--inclusive=yes")
    cmd.append(str(callgrind_out))
    text = subprocess.run(cmd, check=True, capture_output=True, text=True).stdout
    shown = 0
    in_table = False
    for line in text.splitlines():
        if "file:function" in line:
            in_table = True
            print("    " + line.strip())
            continue
        if in_table:
            if ANNOTATE_LINE.match(line):
                print("    " + line.strip())
                shown += 1
                if shown >= top:
                    break
            elif shown and not line.strip("- \t"):
                continue


def cmd_profile(args) -> None:
    if not shutil.which("valgrind") or not shutil.which("callgrind_annotate"):
        sys.exit("profiling needs valgrind and callgrind_annotate on PATH")

    n = args.n or PROFILE_N
    workloads = args.workloads.split(",") if args.workloads else ["lines", "calls"]
    modes = args.modes.split(",") if args.modes else DEFAULT_MODES
    variant = args.variant

    build_variant(variant)
    outdir = BUILD_DIR / "callgrind"
    outdir.mkdir(parents=True, exist_ok=True)

    print_environment()
    for workload in workloads:
        for mode in modes:
            tag = f"{variant}-{workload}-{mode}"
            print(f"== profile: variant {variant!r}, workload {workload!r}, tracing {mode} ==")

            # 1. Full-run profile: how much of everything is CTracer_trace?
            full_out = outdir / f"full-{tag}.out"
            run_worker(variant, workload, mode, n, 1, under=[
                "valgrind", "--tool=callgrind", "--quiet",
                f"--callgrind-out-file={full_out}",
            ])
            print(f"  whole-process profile ({full_out}):")
            annotate(full_out, top=args.top)

            # 2. Toggle-collect profile: costs only while inside CTracer_trace.
            toggled_out = outdir / f"tracer-{tag}.out"
            run_worker(variant, workload, mode, n, 1, under=[
                "valgrind", "--tool=callgrind", "--quiet",
                f"--callgrind-out-file={toggled_out}",
                "--collect-atstart=no", "--toggle-collect=CTracer_trace",
            ])
            print(f"  instructions spent inside CTracer_trace ({toggled_out}):")
            if total_ir(toggled_out) == 0:
                print("    (toggle-collect matched nothing; is the build unstripped?)")
            else:
                annotate(toggled_out, top=args.top)
            share = total_ir(toggled_out) / max(total_ir(full_out), 1)
            print(f"  CTracer_trace is ~{share:.1%} of all instructions in this run")
            print()


# ---------------------------------------------------------------------------
# Self test
# ---------------------------------------------------------------------------

def expected_lines(fn, n, path) -> set:
    """Collect the line numbers fn(n) executes in `path`, with sys.settrace."""
    got = set()

    def tr(frame, event, arg):
        if event == "line" and frame.f_code.co_filename == path:
            got.add(frame.f_lineno)
        return tr

    sys.settrace(tr)
    try:
        fn(n)
    finally:
        sys.settrace(None)
    return got


def unpack_arcs(packed_ints):
    """Invert CTracer_record_pair's packing (see collector.py flush_data)."""
    arcs = set()
    for packed in packed_ints:
        l1 = packed & 0xFFFFFFF
        l2 = (packed & (0xFFFFFFF << 28)) >> 28
        if packed & (1 << 56):
            l1 *= -1
        if packed & (1 << 57):
            l2 *= -1
        arcs.add((l1, l2))
    return arcs


def cmd_selftest(args) -> None:
    fn, traced_file = get_workload("branchy")
    n = 300
    want = expected_lines(fn, n, traced_file)
    assert want, "sys.settrace found no lines?"

    def run(variant, arcs):
        mod = load_variant(variant)
        tracer, data = make_tracer(mod.CTracer, mod.CFileDisposition, arcs, traced_file)
        tracer.start()
        fn(n)
        tracer.stop()
        return data

    # Lines mode must agree exactly with sys.settrace.
    got = run("normal", arcs=False)[traced_file]
    assert got == want, f"line data mismatch: extra={got - want} missing={want - got}"

    # Arc mode: every line mentioned in an arc must be a traced line (or the
    # negative first-line-number entry/exit markers).
    arcs = unpack_arcs(run("normal", arcs=True)[traced_file])
    assert len(arcs) > 10, f"suspiciously few arcs: {arcs}"
    lines_in_arcs = {abs(l) for arc in arcs for l in arc}
    fn_first = fn.__code__.co_firstlineno
    assert lines_in_arcs <= (want | {fn_first}), (
        f"arc lines outside traced lines: {lines_in_arcs - want - {fn_first}}"
    )

    # Ablated builds behave as advertised.
    assert run("no_record", arcs=False)[traced_file] == set()
    assert run("no_set_add", arcs=False)[traced_file] == set()
    assert run("no_lock", arcs=False)[traced_file] == want
    assert run("memo_cache", arcs=False)[traced_file] == want
    assert traced_file not in run("do_nothing", arcs=False)

    # And the stats build counts events.
    mod = load_variant("stats")
    tracer, data = make_tracer(mod.CTracer, mod.CFileDisposition, False, traced_file)
    tracer.start()
    fn(n)
    stats = tracer.get_stats()
    tracer.stop()
    assert stats["lines"] >= n, stats

    print("selftest OK")


def cmd_build(args) -> None:
    for variant in VARIANTS:
        build_variant(variant, force=args.force)
    print(f"built {len(VARIANTS)} variants under {BUILD_DIR}/{build_tag()}/")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command")

    p_run = sub.add_parser("run", help="run the ablation timing suite")
    p_run.add_argument("--n", type=int, default=None, help=f"workload size (default {DEFAULT_N})")
    p_run.add_argument("--repeats", type=int, default=None)
    p_run.add_argument("--quick", action="store_true", help="smaller n, fewer repeats")
    p_run.add_argument("--workloads", help=f"comma list from: {','.join(DEFAULT_WORKLOADS)}")
    p_run.add_argument("--modes", help="comma list from: lines,arcs")
    p_run.add_argument("--variants", help=f"comma list from: baseline,pytracer,{','.join(VARIANTS)}")
    p_run.add_argument("--pytracer", action="store_true", help="also time PyTracer for scale")
    p_run.add_argument("--json", help="write raw results to this file")

    p_prof = sub.add_parser("profile", help="profile with valgrind/callgrind")
    p_prof.add_argument("--n", type=int, default=None, help=f"workload size (default {PROFILE_N})")
    p_prof.add_argument("--workloads", help="comma list (default: lines,calls)")
    p_prof.add_argument("--modes", help="comma list (default: lines,arcs)")
    p_prof.add_argument("--variant", default="normal", choices=list(VARIANTS))
    p_prof.add_argument("--top", type=int, default=20, help="rows of annotate output to show")

    p_build = sub.add_parser("build", help="build all tracer variants")
    p_build.add_argument("--force", action="store_true")

    sub.add_parser("selftest", help="check the harness records correct data")

    p_worker = sub.add_parser("worker")  # internal
    p_worker.add_argument("--variant", required=True)
    p_worker.add_argument("--workload", required=True)
    p_worker.add_argument("--mode", choices=["lines", "arcs"], required=True)
    p_worker.add_argument("--n", type=int, required=True)
    p_worker.add_argument("--repeats", type=int, required=True)

    commands = dict(
        run=cmd_run,
        profile=cmd_profile,
        build=cmd_build,
        selftest=cmd_selftest,
        worker=cmd_worker,
    )
    argv = sys.argv[1:] if argv is None else argv
    if not argv or (argv[0] not in commands and argv[0] not in ("-h", "--help")):
        argv = ["run", *argv]
    args = parser.parse_args(argv)
    commands[args.command](args)


if __name__ == "__main__":
    main()

# CTracer benchmark & profiling harness

A harness for answering "where does CTracer spend its time?" so optimization
work can target the real costs.  It measures the tracer two independent ways:

1. **Ablation timing** (`run`): the C extension is compiled several times,
   each build with one operation compiled out (the `ABLATE_*` flags documented
   in `coverage/ctracer/util.h`).  The timing difference between the normal
   build and an ablated build is the cost of the removed operation.  A
   `COLLECT_STATS` build counts the exact number of trace events, so costs are
   reported in nanoseconds per event.

2. **Instruction profiling** (`profile`): runs a workload under
   `valgrind --tool=callgrind` with collection toggled on only inside
   `CTracer_trace`, then prints the annotated breakdown of which functions the
   tracer's instructions actually go to.

The tracer is driven standalone but wired exactly the way
`Collector._start_tracer` wires it (including real `lock_data`/`unlock_data`
Python callables and a `should_trace` that returns `CFileDisposition`
objects), so the numbers reflect real tracer behavior without the rest of
coverage.py in the way.

## Usage

```sh
# Correctness check: recorded data must match a sys.settrace reference.
python3 lab/benchmark_ctracer/benchmark.py selftest

# The ablation timing suite (~5 minutes; --quick for a fast pass).
python3 lab/benchmark_ctracer/benchmark.py run
python3 lab/benchmark_ctracer/benchmark.py run --quick
python3 lab/benchmark_ctracer/benchmark.py run --pytracer --json results.json
python3 lab/benchmark_ctracer/benchmark.py run --workloads lines_hi,calls --modes arcs

# Callgrind profiling (needs valgrind; ~1 minute per combination).
python3 lab/benchmark_ctracer/benchmark.py profile
python3 lab/benchmark_ctracer/benchmark.py profile --workloads calls --modes arcs
```

Each measurement runs in a fresh subprocess.  Builds land in
`lab/benchmark_ctracer/build/` (gitignored) and are cached until the C sources
or flags change.  When comparing an optimization, save `--json` output from
before and after.

Timings are the minimum of N runs with the GC disabled during the timed
region, but this is still a wall-clock benchmark: treat deltas under a few
nanoseconds per event as noise, and prefer the callgrind instruction counts
when two numbers disagree.

## Workloads

| workload     | stresses                                                     |
| ------------ | ------------------------------------------------------------ |
| `lines`      | line events, line numbers < 256 (CPython caches those ints)  |
| `lines_hi`   | line events above line 256 — the realistic case              |
| `calls`      | call/return events (tiny functions)                          |
| `branchy`    | many distinct arcs                                           |
| `generators` | suspend/resume, the RESUME logic in handle_call/handle_return|
| `recursion`  | deep call stacks, the data stack                             |

## Variants

| variant      | meaning                                                        |
| ------------ | -------------------------------------------------------------- |
| `baseline`   | no tracing at all                                              |
| `do_nothing` | trace callback returns immediately: interpreter dispatch cost  |
| `no_record`  | no line-number ints created, no `PySet_Add`                    |
| `no_set_add` | ints created but not added to the set                          |
| `no_lock`    | no `lock_data`/`unlock_data` Python calls on call events       |
| `memo_cache` | `should_trace_cache` dict lookup memoized on call events       |
| `normal`     | the real tracer                                                |
| `pytracer`   | pure-Python `PyTracer`, for scale                              |
| `stats`      | `COLLECT_STATS` build, used to count events (not timed)        |

## Results (2026-07, Python 3.11.15, x86-64 Linux, gcc -O2)

Went in with the hypothesis that "the dict/set from Python will be the slow
point."  Verdict: **confirmed for line events, which dominate most programs —
but on the call path the dict lookup is cheap and the two Python lock
callbacks are the real cost.**

Representative per-event costs (min of 5 runs, n=100k iterations):

- **Fixed floor**: the interpreter's trace dispatch alone (`do_nothing`) costs
  **~19–31 ns/event** — tracing is 3–4x over baseline before CTracer does any
  work at all.  This is unreachable by optimizing CTracer (only a
  `sys.monitoring` core avoids it).
- **Line events** (`lines_hi`): recording — `PyLong_FromLong` +
  `PySet_Add` + the temp-object DECREF — costs **~20 ns of the ~24 ns**
  the tracer body spends per line event, i.e. ~80% of the optimizable cost.
  In arc mode it's ~25 ns of ~30 ns.  Callgrind agrees: inside
  `CTracer_trace`, `PySet_Add` (25%) + `PyLong_FromLong` (15%) +
  `_Py_Dealloc` (14%) + `PyObject_RichCompare` (11%, set probe compares) +
  `long_hash` (9%) ≈ **75% of in-tracer instructions**.
- Line numbers ≤ 256 hit CPython's small-int cache: the same delta drops from
  ~20 ns to ~8 ns in the `lines` workload.  Real files are longer than 256
  lines, so `lines_hi` is the honest number (and arc mode always pays full
  price: the packed arc ints are always large).
- **Call events** (`calls`): the two Python calls to
  `lock_data`/`unlock_data` cost **~55–68 ns per call event** — by far the
  largest single item on the call path.  The suspected
  `should_trace_cache` dict lookup is only **~4–10 ns**: its keys are
  pointer-identical interned strings with cached hashes, so `PyDict_GetItem`
  is nearly free.
- `PyTracer` is ~6–7x slower than CTracer throughout (~300–440 ns/event).

### What this says about optimizing

Ranked by measured payoff:

1. **Stop paying `PyLong` + `PySet_Add` per line event.**  Almost every line
   event re-records an already-seen line/arc (loops!).  Options: a C-side
   structure per `DataStackEntry` (bitset or open-addressed table of packed
   ints, converted to Python sets at flush time), or even just a small
   per-entry cache of recently recorded packed values to skip duplicates
   before touching Python objects.
2. **Get the Python `lock_data`/`unlock_data` callables off the per-call
   path** — e.g. take a C lock directly, or only lock when a second tracer
   thread actually exists.
3. Not worth it: memoizing `should_trace_cache` (≤10 ns, within noise on
   several workloads).

### Sample output

```
== workload 'lines_hi', tracing arcs == (events: 1 calls, 900,016 lines, 1 returns)
    variant            time  vs base  ns/event   note
    baseline         10.4ms     1.0x             no tracing at all
    do_nothing       30.7ms     2.9x        23   trace fn returns immediately: interpreter dispatch cost
    no_record        35.1ms     3.4x        27   no line-number ints created, no set adds
    no_set_add       45.9ms     4.4x        39   line-number ints created, but not added to the set
    no_lock          56.8ms     5.4x        52   no lock_data/unlock_data Python calls on call events
    memo_cache       58.8ms     5.6x        54   should_trace_cache dict lookup memoized on call events
    normal           57.4ms     5.5x        52   the real tracer
    pytracer        337.2ms    32.3x       363   pure-Python tracer, for scale
    -- ablation deltas --
          25 ns  int creation + set add (per line event)
          13 ns  set add alone (per line event)
          12 ns  int creation alone (per line event)
          30 ns  whole tracer body (per event)
          23 ns  interpreter trace dispatch (per event)

  instructions spent inside CTracer_trace:
    16,200,324 (20.55%)  PySet_Add
    11,520,256 (14.61%)  PyLong_FromUnsignedLongLong
    10,980,901 (13.93%)  tracer.c:CTracer_trace
     8,999,550 (11.41%)  _Py_Dealloc
     8,280,184 (10.50%)  [long free-list / dealloc helper]
     7,199,680 ( 9.13%)  PyObject_RichCompare
     6,839,658 ( 8.67%)  [long_hash]
     5,400,124 ( 6.85%)  tracer.c:CTracer_record_pair
```

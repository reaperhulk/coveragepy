# Reducing sys.monitoring branch-coverage overhead

Investigation of the sysmon core's branch-mode overhead on Python 3.14,
following up on issue #2172 and the measurements in the
`Tools/coverage_benchmark` harness (reaperhulk/cpython,
`claude/sys-monitoring-coverage-benchmark-amqxu5` branch).

## Environment

- CPython 3.14.2, built from source with default optimizations,
  4-core x86-64 Linux container.
- Real-suite benchmark: pyca/cryptography 49.0.0 test suite, 4,277 tests,
  192 traced files, `COVERAGE_CORE=sysmon python -m coverage run -m pytest
  tests/ -q` vs plain pytest.  Wall time, best of 3, noise floor ±2-3%.
- "PR stack" = main (7.15.1) + the five open performance PRs: #2213,
  #2214, #2215, #2216, #2218.

## Baseline: released 7.15.1 vs the PR stack

| configuration                | wall time | overhead vs base |
| ---------------------------- | --------- | ---------------- |
| no coverage                  | 35.44s    | —                |
| 7.15.1, line                 | 34.79s    | ≈0% (noise)      |
| 7.15.1, branch               | 47.90s    | **+35.2%**       |
| PR stack, line               | 34.46s    | ≈0% (noise)      |
| PR stack, branch             | 46.46s    | **+31.1%**       |

(The published +2.3% line / +43% branch numbers were measured on a
different machine; the shape reproduces here, slightly smaller.)

PR #2218 (only enable events with registered callbacks) verified on
microbenchmarks (line mode, best of 5):

| workload   | 7.15.1  | PR stack | note                              |
| ---------- | ------- | -------- | --------------------------------- |
| generators | +49.8%  | +10.0%   | PY_RESUME/PY_RETURN no longer on  |
| calls      | +33.0%  | +10.0%   | (residual ≈ fixed startup cost on |
|            |         |          | a ~1s workload; steady ≈ 0)       |

## Decomposition of the remaining +31% (on the PR stack)

Stub experiments on the cryptography suite, branch mode:

| variant                                          | wall time | overhead |
| ------------------------------------------------ | --------- | -------- |
| PR stack, unmodified                             | 46.46s    | +31.1%   |
| `get_multiline_map()` stubbed to `{}`            | 44.84s    | +26.5%   |
| `branch_trails()`/`always_jumps()`/parse stubbed | 35.64s    | **+0.6%** |

So on this suite:

- ~85% of the branch overhead is the `dis`-based analysis
  (`branch_trails()` + `always_jumps()`), triggered by the first BRANCH
  event of every code object (5,671 code objects here; the `breadth`
  microbenchmark measures ~2.2ms per code object).
- ~15% is the AST re-parsing behind `get_multiline_map()`'s
  `lru_cache(maxsize=20)` (192 traced files thrash it, but LRU locality
  keeps the damage lower than feared).
- The per-event callback bodies are ≈0.6% total: with one-shot DISABLE
  semantics the event count is bounded by the number of covered points,
  and a few dict/set operations per event just don't add up to much.
  A C callback core (avenue 3 in the harness RESULTS) would buy at most
  half a point on this suite: not worth it before the analysis cost is
  gone.

## The fix: resolve branch events lazily, one pair at a time

`branch_trails()` precomputes, for every branch site in the code object,
the full trail of instructions each direction can reach, and re-registers
every trail under every offset it contains.  That is O(code object) work
on first branch event — but BRANCH_LEFT/RIGHT events are one-shot
(DISABLEd after each fire), so at most 2×(branch sites) pairs ever need
resolving, and only for branches that actually execute.

The replacement (`BranchArcResolver` in `coverage/bytecode.py`):

- On each branch event, resolve just that `(source offset, destination
  offset)` pair: walk the raw `co_code` bytes from the destination,
  following unconditional jumps, skipping CACHE/EXTENDED_ARG, until
  reaching an instruction on a new line (arc), a RETURN (arc to exit), or
  another branch possibility (no arc — it will fire its own events).
  Lines come from the `byte_to_line` dict the tracer already builds, so
  no `dis.get_instructions()` decode of whole code objects remains
  anywhere in the measurement path.
- The multiline map is cached per `SysMonitor` instance, unbounded, so
  each traced file is parsed at most once per run (fixes the
  `lru_cache(maxsize=20)` thrash; also removes cross-run staleness since
  the cache dies with the tracer).

Results (cryptography suite, branch mode):

| configuration                          | wall time | overhead |
| -------------------------------------- | --------- | -------- |
| PR stack                               | 46.46s    | +31.1%   |
| + lazy resolver (dis-based, 1st cut)   | 42.23s    | +19.2%   |
| + raw-bytecode resolver (final)        | TBD       | TBD      |
| interpreter floor (all analysis stubbed)| 35.64s   | +0.6%    |

breadth microbenchmark (2,250 functions, cold-dominated, best of 5):

| configuration       | wall time |
| ------------------- | --------- |
| no coverage         | 0.03s     |
| 7.15.1              | 5.20s     |
| PR stack            | 4.98s     |
| lazy resolver (dis) | 3.49s     |
| raw-bytes resolver  | TBD       |

## Correctness

- `tests/` suite with `COVERAGE_CORE=sysmon` on 3.14: failure set
  identical to the unmodified PR stack (all failures are environmental:
  `coverage` script not on PATH for subprocess tests, no-network venv
  tests, etc.).
- Differential fuzz: 20 branchy snippet shapes (loops, else-clauses,
  match, with, short-circuit returns, exceptions across frames, multiline
  conditions, comprehensions, generators): identical arc data vs the PR
  stack.
- Offline corpus check: for 3,571 code objects across 35 stdlib +
  cryptography modules, resolving all 17,445 (source, dest) pairs from
  `co_branches()` plus every conditional jump found by dis: identical
  results between the raw-bytecode walk and a dis-based reference
  implementation.
- Full cryptography suite data files: 188/192 files byte-identical arcs;
  4 files differ by a total of 5 arcs present with the old code and
  absent with the new. All 5 are arcs between two lines *inside the same
  multi-line statement* (multi-line comprehensions/boolean conditions) —
  they come from the old code's raw `byte_to_line` fallback, which does
  not apply the multiline map. The new resolver resolves these events to
  the statement's first line instead (matching what the parser predicts),
  so the spurious intra-statement arcs disappear. TBD: determinism
  cross-check.

## Upstreamability

- The five PRs in the stack are independent of this change and remain
  worthwhile (#2218 especially, for line mode and generators).
- The lazy resolver replaces `branch_trails()`/`always_jumps()` and the
  `InstructionWalker` precompute path entirely; those become dead code.
- Open PR #2175 (fall back to ctrace for branch mode on 3.14) becomes
  unnecessary if this lands.
- Open PR #2207 (single-line class bodies) touches the same event
  handler; whichever lands second needs a trivial rebase.

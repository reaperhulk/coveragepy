# Licensed under the Apache License: http://www.apache.org/licenses/LICENSE-2.0
# For details: https://github.com/coveragepy/coveragepy/blob/main/NOTICE.txt

"""Workloads for the CTracer benchmark harness.

Each workload takes a single scaling parameter `n` and keeps all of its
significant work inside this file, so the harness can arrange to trace only
this file.  Workloads must be deterministic: the event-counting run and the
timing runs have to execute exactly the same trace events.

Each one stresses a different part of the tracer:

- ``lines``:      straight-line code in one frame; almost pure line events.
- ``calls``:      tiny function calls; dominated by call/return events.
- ``branchy``:    if/elif chains; many distinct arcs when tracing arcs.
- ``generators``: generator suspend/resume; stresses the RESUME/yield logic
                  in handle_call and handle_return.
- ``recursion``:  deep call stacks; stresses the data stack.
"""


def lines(n):
    """Line-event heavy: ~10 line events per iteration, no calls."""
    a = b = c = d = 0
    for i in range(n):
        a = i + 1
        b = a * 3
        c = b & 0xFF
        d = c | a
        a = d ^ b
        b = a - c
        c = b + d
        d = c & 0x7F
    return a + b + c + d


def calls(n):
    """Call-event heavy: four tiny calls per iteration."""

    def tiny(x):
        return x + 1

    x = 0
    for _ in range(n):
        x = tiny(x); x = tiny(x); x = tiny(x); x = tiny(x)  # noqa: E702
    return x


def branchy(n):
    """Branch heavy: exercises many distinct arcs in arc-tracing mode."""
    x = 0
    for i in range(n):
        if i & 1:
            x += 1
        elif i & 2:
            x += 3
        elif i & 4:
            x ^= 7
        else:
            x -= 2
        if x & 8:
            x += i & 3
        else:
            x -= 1
    return x


def generators(n):
    """Generator heavy: suspend/resume pairs stress RESUME handling."""

    def gen(k):
        x = 0
        for j in range(k):
            yield x
            x += j

    total = 0
    for _ in range(max(1, n // 50)):
        for v in gen(50):
            total += v
    return total


def recursion(n):
    """Recursion heavy: deep call stacks stress the data stack."""

    def fib(k):
        if k < 2:
            return k
        return fib(k - 1) + fib(k - 2)

    x = 0
    for _ in range(max(1, n // 10_000)):
        x += fib(18)
    return x


WORKLOADS = {
    "lines": lines,
    "calls": calls,
    "branchy": branchy,
    "generators": generators,
    "recursion": recursion,
}


# Padding to push the next workload above line 256, where CPython's
# small-int cache no longer covers line numbers.  Files in real projects
# are usually longer than 256 lines, so `lines_hi` is the realistic case
# and `lines` (small line numbers, cached ints) is the optimistic case.
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#


def lines_hi(n):
    """Same as ``lines``, but above line 256: line-number ints aren't cached."""
    a = b = c = d = 0
    for i in range(n):
        a = i + 1
        b = a * 3
        c = b & 0xFF
        d = c | a
        a = d ^ b
        b = a - c
        c = b + d
        d = c & 0x7F
    return a + b + c + d


WORKLOADS["lines_hi"] = lines_hi
assert lines_hi.__code__.co_firstlineno > 256, lines_hi.__code__.co_firstlineno

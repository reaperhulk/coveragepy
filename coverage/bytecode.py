# Licensed under the Apache License: http://www.apache.org/licenses/LICENSE-2.0
# For details: https://github.com/coveragepy/coveragepy/blob/main/NOTICE.txt

"""Bytecode analysis for coverage.py"""

from __future__ import annotations

import collections
import dis
import opcode as opcode_module
from collections.abc import Iterable, Mapping
from types import CodeType
from typing import Optional

from coverage.types import TArc, TLineNo, TOffset


class ByteParser:
    """Parse bytecode to understand the structure of code."""

    def __init__(
        self,
        *,
        code: CodeType | None = None,
        text: str | None = None,
        filename: str | None = None,
    ) -> None:
        if code is None:
            assert text is not None
            code = compile(text, filename or "<string>", "exec", dont_inherit=True)
        self.code = code

    def _child_parsers(self) -> Iterable[ByteParser]:
        """Iterate over all the code objects nested within this one.

        The iteration includes `self` as its first value.

        We skip code objects named `__annotate__` since they are deferred
        annotations that usually are never run.  If there are errors in the
        annotations, they will be caught by type checkers or other tools that
        use annotations.

        """
        return (ByteParser(code=c) for c in self.code_objects() if c.co_name != "__annotate__")

    def code_objects(self) -> Iterable[CodeType]:
        """Iterate over all the code objects in `code`."""
        stack = [self.code]
        while stack:
            # We're going to return the code object on the stack, but first
            # push its children for later returning.
            code = stack.pop()
            for c in code.co_consts:
                if isinstance(c, CodeType):
                    stack.append(c)
            yield code

    def _line_numbers(self) -> Iterable[TLineNo]:
        """Yield the line numbers possible in this code object.

        Uses co_lines() to produce a sequence: l0, l1, ...
        """
        for _, _, line in self.code.co_lines():
            if line:
                yield line

    def find_statements(self) -> Iterable[TLineNo]:
        """Find the statements in `self.code`.

        Produce a sequence of line numbers that start statements.  Recurses
        into all code objects reachable from `self.code`.

        """
        for bp in self._child_parsers():
            # Get all of the lineno information from this code.
            yield from bp._line_numbers()


def bytes_to_lines(code: CodeType) -> dict[TOffset, TLineNo]:
    """Make a dict mapping byte code offsets to line numbers."""
    b2l = {}
    for bstart, bend, lineno in code.co_lines():
        if lineno is not None:
            for boffset in range(bstart, bend, 2):
                b2l[boffset] = lineno
    return b2l


def op_set(*op_names: str) -> set[int]:
    """Make a set of opcodes from instruction names.

    The names might not exist in this version of Python, skip those if not.
    """
    ops = {op for name in op_names if (op := dis.opmap.get(name))}
    assert ops, f"At least one opcode must exist: {op_names}"
    return ops


# Opcodes that are unconditional jumps elsewhere.
ALWAYS_JUMPS = op_set(
    "JUMP_BACKWARD",
    "JUMP_BACKWARD_NO_INTERRUPT",
    "JUMP_FORWARD",
)

# Opcodes that exit from a function.
RETURNS = op_set(
    "RETURN_VALUE",
    "RETURN_GENERATOR",
)

# Opcodes that do nothing.
NOPS = op_set(
    "NOP",
    "NOT_TAKEN",
)


class InstructionWalker:
    """Utility to step through trails of instructions.

    We have two reasons to need sequences of instructions from a code object:
    First, in strict sequence to visit all the instructions in the object.
    This is `walk(follow_jumps=False)`.  Second, we want to follow jumps to
    understand how execution will flow: `walk(follow_jumps=True)`.
    """

    def __init__(self, code: CodeType) -> None:
        self.code = code
        self.insts: dict[TOffset, dis.Instruction] = {}

        inst = None
        for inst in dis.get_instructions(code):
            self.insts[inst.offset] = inst

        assert inst is not None
        self.max_offset = inst.offset

    def walk(
        self, *, start_at: TOffset = 0, follow_jumps: bool = True
    ) -> Iterable[dis.Instruction]:
        """
        Yield instructions starting from `start_at`.  Follow unconditional
        jumps if `follow_jumps` is true.
        """
        seen = set()
        offset = start_at
        while offset < self.max_offset + 1:
            if offset in seen:
                break
            seen.add(offset)
            if inst := self.insts.get(offset):
                yield inst
                if follow_jumps and inst.opcode in ALWAYS_JUMPS:
                    offset = inst.jump_target
                    continue
            offset += 2


TBranchTrailsOneSource = dict[Optional[TArc], set[TOffset]]
TBranchTrails = dict[TOffset, TBranchTrailsOneSource]


# CACHE doesn't exist in Python 3.10, but the branch resolver is only used
# on 3.14+, so a placeholder value is fine.
_CACHE = dis.opmap.get("CACHE", -1)
_EXTENDED_ARG = dis.opmap["EXTENDED_ARG"]

# All opcodes with a jump target.
JUMPS = set(dis.hasjrel) | set(dis.hasjabs)

# Opcodes that jump backwards.
BACKWARD_JUMPS = {op for op in JUMPS if "JUMP_BACKWARD" in dis.opname[op]}

def _cache_counts() -> list[int]:
    """Number of inline CACHE entries following each opcode."""
    counts = [0] * 256
    entries = getattr(opcode_module, "_inline_cache_entries", {})
    if isinstance(entries, dict):
        for name, num in entries.items():
            op = dis.opmap.get(name)
            if op is not None:
                counts[op] = num
    else:
        for op, num in enumerate(entries):
            if op < 256:
                counts[op] = num
    return counts


_CACHES_PER_OP = _cache_counts()


class BranchArcResolver:
    """Resolve branch events to line arcs, one (source, dest) pair at a time.

    Branch events are one-shot (they are DISABLEd after firing), so each
    (source offset, destination offset) pair is resolved at most a couple of
    times per code object.  Resolving pairs on demand is much cheaper than
    precomputing trails for every branch in the code object, most of which
    never fire.  We walk the raw bytecode bytes so that we never need to
    disassemble whole code objects with `dis`.

    The resolution of one pair follows the same rules as `branch_trails`:
    starting from the destination, follow the trail of instructions (through
    unconditional jumps) until we reach an instruction on a new source line
    (giving us the arc), a return (an arc to leaving the code object), or
    another branch possibility (no arc: that branch will produce its own
    events).

    """

    def __init__(
        self,
        code: CodeType,
        byte_to_line: Mapping[TOffset, TLineNo],
        multiline_map: Mapping[TLineNo, TLineNo],
    ) -> None:
        self.code = code
        # co_code re-copies the bytes on each access, so fetch it once.
        self.co_code = code.co_code
        self.byte_to_line = byte_to_line
        self.multiline_map = multiline_map

    def line_at(self, offset: TOffset) -> TLineNo | None:
        """The source line of the instruction at `offset`, de-multilined."""
        line = self.byte_to_line.get(offset)
        if line is not None:
            line = self.multiline_map.get(line, line)
        return line

    def resolve(self, source: TOffset, dest: TOffset) -> TArc | None:
        """Turn a branch event's (source, dest) offsets into an arc, or None."""
        from_line = self.line_at(source)
        if from_line is None:
            return None
        co_code = self.co_code
        max_offset = len(co_code)
        byte_to_line = self.byte_to_line
        multiline_map = self.multiline_map
        offset = dest
        ext_arg = 0
        seen: set[TOffset] = set()
        while 0 <= offset < max_offset and offset not in seen:
            seen.add(offset)
            op = co_code[offset]
            if op == _CACHE:
                offset += 2
                continue
            if op == _EXTENDED_ARG:
                ext_arg = (ext_arg | co_code[offset + 1]) << 8
                offset += 2
                continue
            line = byte_to_line.get(offset)
            if line is not None:
                line = multiline_map.get(line, line)
                if line and line != from_line:
                    return (from_line, line)
            if op in JUMPS:
                if op in ALWAYS_JUMPS:
                    arg = ext_arg | co_code[offset + 1]
                    next_offset = offset + 2 + 2 * _CACHES_PER_OP[op]
                    if op in BACKWARD_JUMPS:
                        offset = next_offset - 2 * arg
                    else:
                        offset = next_offset + 2 * arg
                    ext_arg = 0
                    continue
                # Another branch possibility: it will get its own events.
                return None
            if op in RETURNS:
                return (from_line, -self.code.co_firstlineno)
            ext_arg = 0
            offset += 2
        return None


def branch_trails(
    code: CodeType,
    multiline_map: Mapping[TLineNo, TLineNo],
) -> TBranchTrails:
    """
    Calculate branch trails for `code`.

    `multiline_map` maps line numbers to the first line number of a
    multi-line statement.

    Instructions can have a jump_target, where they might jump to next.  Some
    instructions with a jump_target are unconditional jumps (ALWAYS_JUMPS), so
    they aren't interesting to us, since they aren't the start of a branch
    possibility.

    Instructions that might or might not jump somewhere else are branch
    possibilities.  For each of those, we track a trail of instructions.  These
    are lists of instruction offsets, the next instructions that can execute.
    We follow the trail until we get to a new source line.  That gives us the
    arc from the original instruction's line to the new source line.

    """
    the_trails: TBranchTrails = collections.defaultdict(lambda: collections.defaultdict(set))
    iwalker = InstructionWalker(code)
    for inst in iwalker.walk(follow_jumps=False):
        if not inst.jump_target:
            # We only care about instructions with jump targets.
            continue
        if inst.opcode in ALWAYS_JUMPS:
            # We don't care about unconditional jumps.
            continue

        from_line = inst.line_number
        if from_line is None:
            continue
        from_line = multiline_map.get(from_line, from_line)

        def add_one_branch_trail(
            trails: TBranchTrailsOneSource,
            start_at: TOffset,
        ) -> None:
            # pylint: disable=cell-var-from-loop
            inst_offsets: set[TOffset] = set()
            to_line = None
            for inst2 in iwalker.walk(start_at=start_at, follow_jumps=True):
                inst_offsets.add(inst2.offset)
                l2 = inst2.line_number
                if l2 is not None:
                    l2 = multiline_map.get(l2, l2)
                if l2 and l2 != from_line:
                    to_line = l2
                    break
                elif inst2.jump_target and (inst2.opcode not in ALWAYS_JUMPS):
                    break
                elif inst2.opcode in RETURNS:
                    to_line = -code.co_firstlineno
                    break
            if to_line is not None:
                trails[(from_line, to_line)].update(inst_offsets)
            else:
                trails[None] = set()

        # Calculate two trails: one from the next instruction, and one from the
        # jump_target instruction.
        trails: TBranchTrailsOneSource = collections.defaultdict(set)
        add_one_branch_trail(trails, start_at=inst.offset + 2)
        add_one_branch_trail(trails, start_at=inst.jump_target)
        the_trails[inst.offset] = trails

        # Sometimes we get BRANCH_RIGHT or BRANCH_LEFT events from instructions
        # other than the original jump possibility instruction.  Register each
        # trail under all of their offsets so we can pick up in the middle of a
        # trail if need be.
        for arc, offsets in trails.items():
            for offset in offsets:
                the_trails[offset][arc].update(offsets)

    return the_trails


def always_jumps(code: CodeType) -> dict[TOffset, TOffset]:
    """Make a map of unconditional bytecodes jumping to others.

    Only include bytecodes that do no work and go to another bytecode.
    """
    jumps = {}
    iwalker = InstructionWalker(code)
    for inst in iwalker.walk(follow_jumps=False):
        if inst.opcode in ALWAYS_JUMPS:
            jumps[inst.offset] = inst.jump_target
        elif inst.opcode in NOPS:
            jumps[inst.offset] = inst.offset + 2
    return jumps

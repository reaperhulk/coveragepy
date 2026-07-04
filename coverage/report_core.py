# Licensed under the Apache License: http://www.apache.org/licenses/LICENSE-2.0
# For details: https://github.com/coveragepy/coveragepy/blob/main/NOTICE.txt

"""Reporter foundation for coverage.py."""

from __future__ import annotations

import os
import sys
import threading
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import IO, TYPE_CHECKING, Protocol

from coverage.data import CoverageData
from coverage.exceptions import ConfigError, NoDataError, NotPython
from coverage.files import GlobMatcher, prep_patterns
from coverage.misc import ensure_dir_for_file, file_be_gone
from coverage.plugin import FileReporter
from coverage.results import Analysis, analysis_from_file_reporter
from coverage.types import TMorf, TMorfs

if TYPE_CHECKING:
    from coverage import Coverage


class Reporter(Protocol):
    """What we expect of reporters."""

    report_type: str

    def report(self, morfs: TMorfs, outfile: IO[str]) -> float:
        """Generate a report of `morfs`, written to `outfile`."""


def render_report(
    output_path: str,
    reporter: Reporter,
    morfs: TMorfs,
    msgfn: Callable[[str], None],
) -> float:
    """Run a one-file report generator, managing the output file.

    This function ensures the output file is ready to be written to. Then writes
    the report to it. Then closes the file and cleans up.

    """
    file_to_close = None
    delete_file = False

    if output_path == "-":
        outfile = sys.stdout
    else:
        # Ensure that the output directory is created; done here because this
        # report pre-opens the output file.  HtmlReporter does this on its own
        # because its task is more complex, being multiple files.
        ensure_dir_for_file(output_path)
        outfile = open(output_path, "w", encoding="utf-8")
        file_to_close = outfile
        delete_file = True

    try:
        ret = reporter.report(morfs, outfile=outfile)
        if file_to_close is not None:
            msgfn(f"Wrote {reporter.report_type} to {output_path}")
        delete_file = False
        return ret
    finally:
        if file_to_close is not None:
            file_to_close.close()
            if delete_file:
                file_be_gone(output_path)  # pragma: part covered (doesn't return)


def get_analysis_to_report(
    coverage: Coverage,
    morfs: TMorfs,
) -> Iterable[tuple[FileReporter, Analysis]]:
    """Get the files to report on.

    For each morf in `morfs`, if it should be reported on (based on the omit
    and include configuration options), yield a pair, the `FileReporter` and
    `Analysis` for the morf.

    """
    fr_morfs = coverage._get_file_reporters(morfs)
    config = coverage.config

    if config.report_include:
        matcher = GlobMatcher(prep_patterns(config.report_include), "report_include")
        fr_morfs = [(fr, morf) for (fr, morf) in fr_morfs if matcher.match(fr.filename)]

    if config.report_omit:
        matcher = GlobMatcher(prep_patterns(config.report_omit), "report_omit")
        fr_morfs = [(fr, morf) for (fr, morf) in fr_morfs if not matcher.match(fr.filename)]

    if not fr_morfs:
        raise NoDataError("No data to report.")

    fr_morfs = sorted(fr_morfs)
    workers = _num_analysis_workers(coverage, len(fr_morfs))
    if workers > 1:
        results = _analyses_in_threads(coverage, [fr for fr, _ in fr_morfs], workers)
    else:
        results = _analyses_serial(coverage, [morf for _, morf in fr_morfs])

    for (fr, morf), result in zip(fr_morfs, results):
        if isinstance(result, NotPython):
            # Only report errors for .py files, and only if we didn't
            # explicitly suppress those errors.
            # NotPython is only raised by PythonFileReporter, which has a
            # should_be_python() method.
            if fr.should_be_python():  # type: ignore[attr-defined]
                if config.ignore_errors:
                    msg = f"Couldn't parse Python file '{fr.filename}'"
                    coverage._warn(msg, slug="couldnt-parse")
                else:
                    raise result
        elif isinstance(result, Exception):
            if config.ignore_errors:
                msg = f"Couldn't parse '{fr.filename}': {result}".rstrip()
                coverage._warn(msg, slug="couldnt-parse")
            else:
                raise result
        else:
            yield (fr, result)


def _analyses_serial(
    coverage: Coverage,
    morfs: Iterable[TMorf],
) -> Iterator[Analysis | Exception]:
    """Analyze morfs one at a time, yielding an Analysis or Exception for each."""
    for morf in morfs:
        try:
            yield coverage._analyze(morf)
        except Exception as exc:
            yield exc


def _analyses_in_threads(
    coverage: Coverage,
    file_reporters: list[FileReporter],
    workers: int,
) -> Iterator[Analysis | Exception]:
    """Analyze file reporters in worker threads.

    Yields an Analysis or Exception for each file reporter, in order.

    Each worker thread gets its own view of the coverage data.  The data
    could be in-memory (`_prepare_data_for_reporting` makes a no_disk
    CoverageData when `[paths]` is configured), and in-memory data is empty
    when read from a new thread, since CoverageData keeps a SQLite database
    per thread.  Serializing the data once here and deserializing it in each
    thread gives every worker the same snapshot.

    """
    config = coverage.config
    serialized_data = coverage.get_data().dumps()
    threadlocal = threading.local()

    def thread_data() -> CoverageData:
        """Get this thread's own copy of the coverage data."""
        data: CoverageData | None = getattr(threadlocal, "data", None)
        if data is None:
            data = CoverageData(no_disk=True, warn=coverage._warn, debug=coverage._debug)
            data.loads(serialized_data)
            data.set_query_contexts(config.report_contexts)
            threadlocal.data = data
        return data

    def analyze_one(fr: FileReporter) -> Analysis | Exception:
        """Analyze one file reporter, returning exceptions as values."""
        try:
            filename = coverage._file_mapper(fr.filename)
            return analysis_from_file_reporter(thread_data(), config.precision, fr, filename)
        except Exception as exc:
            return exc

    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="coverage_analysis")
    try:
        yield from pool.map(analyze_one, file_reporters)
    finally:
        pool.shutdown(wait=True, cancel_futures=True)


# With fewer files than this, analysis isn't parallelized unless a number of
# workers was explicitly configured.
WORKERS_FILE_CUTOFF = 20


def _num_analysis_workers(coverage: Coverage, num_files: int) -> int:
    """Decide how many worker threads to use for analyzing `num_files` files."""
    workers = coverage.config.report_workers
    if workers < 0:
        raise ConfigError(f"workers must be non-negative, not {workers}")
    if workers == 1:
        return 1
    if not _threads_worthwhile():
        coverage._warn(
            "Multi-threaded reporting needs a free-threaded Python, reporting serially.",
            slug="workers-need-free-threading",
            once=True,
        )
        return 1
    if workers == 0:
        # Automatic: use all the CPUs, but don't parallelize a small job.
        if num_files < WORKERS_FILE_CUTOFF:
            return 1
        workers = os.cpu_count() or 1
    return max(1, min(workers, os.cpu_count() or 1, num_files))


def _threads_worthwhile() -> bool:
    """Would threads run concurrently enough to speed up analysis?

    Analysis is pure Python, so threads only help when the GIL is disabled.
    This checks the runtime GIL state, not the build, since the GIL can be
    re-enabled at runtime on a free-threaded build.

    """
    return not getattr(sys, "_is_gil_enabled", lambda: True)()

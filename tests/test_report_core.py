# Licensed under the Apache License: http://www.apache.org/licenses/LICENSE-2.0
# For details: https://github.com/coveragepy/coveragepy/blob/main/NOTICE.txt

"""Tests for helpers in report.py"""

from __future__ import annotations

import json
import os
import re

from typing import IO, Any
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from unittest import mock

import pytest

import coverage
from coverage import report_core
from coverage.exceptions import ConfigError, CoverageException, CoverageWarning, NotPython
from coverage.files import abs_file
from coverage.report_core import render_report
from coverage.types import TMorf, TMorfs

from tests.coveragetest import CoverageTest


class FakeReporter:
    """A fake implementation of a one-file reporter."""

    report_type = "fake report file"

    def __init__(self, output: str = "", error: type[Exception] | None = None) -> None:
        self.output = output
        self.error = error
        self.morfs: Iterable[TMorf] | None = None

    def report(self, morfs: TMorfs, outfile: IO[str]) -> float:
        """Fake."""
        self.morfs = morfs
        outfile.write(self.output)
        if self.error:
            raise self.error("You asked for it!")
        return 17.25


class RenderReportTest(CoverageTest):
    """Tests of render_report."""

    def test_stdout(self) -> None:
        fake = FakeReporter(output="Hello!\n")
        msgs: list[str] = []
        res = render_report("-", fake, [pytest, "coverage"], msgs.append)
        assert res == 17.25
        assert fake.morfs == [pytest, "coverage"]
        assert self.stdout() == "Hello!\n"
        assert not msgs

    def test_file(self) -> None:
        fake = FakeReporter(output="Gréètings!\n")
        msgs: list[str] = []
        res = render_report("output.txt", fake, [], msgs.append)
        assert res == 17.25
        assert self.stdout() == ""
        with open("output.txt", "rb") as f:
            assert f.read().rstrip() == b"Gr\xc3\xa9\xc3\xa8tings!"
        assert msgs == ["Wrote fake report file to output.txt"]

    @pytest.mark.parametrize("error", [CoverageException, ZeroDivisionError])
    def test_exception(self, error: type[Exception]) -> None:
        fake = FakeReporter(error=error)
        msgs: list[str] = []
        with pytest.raises(error, match="You asked for it!"):
            render_report("output.txt", fake, [], msgs.append)
        assert self.stdout() == ""
        self.assert_doesnt_exist("output.txt")
        assert not msgs


@contextmanager
def force_threads() -> Iterator[None]:
    """Pretend threads will speed up analysis, even on a GIL build."""
    with mock.patch.object(report_core, "_threads_worthwhile", lambda: True):
        yield


class WorkersReportTest(CoverageTest):
    """Multi-threaded analysis must produce the same reports as serial."""

    NUM_FILES = 25

    def make_project(self) -> coverage.Coverage:
        """Make a project with many partially-covered files, run it, return the Coverage."""
        imports = []
        for i in range(self.NUM_FILES):
            self.make_file(
                f"mod{i:02d}.py",
                f"""\
                import sys
                if len(sys.argv) > 99{i}9:
                    x = 1
                else:
                    x = 2
                for i in range(2):
                    x += i
                """,
            )
            imports.append(f"import mod{i:02d}")
        # One file with everything covered, for skip_covered.
        self.make_file("allcovered.py", "y = 1\ny += 1\n")
        imports.append("import allcovered")
        self.make_file("main.py", "\n".join(imports))
        cov = coverage.Coverage(branch=True)
        self.start_import_stop(cov, "main")
        return cov

    @pytest.mark.parametrize(
        "kwargs",
        [{}, {"show_missing": True}, {"skip_covered": True}],
        ids=["plain", "show_missing", "skip_covered"],
    )
    def test_summary_identical(self, kwargs: dict[str, Any]) -> None:
        cov = self.make_project()
        serial = self.get_report(cov, squeeze=False, **kwargs)
        assert "mod00.py" in serial
        with force_threads():
            threaded = self.get_report(cov, squeeze=False, workers=4, **kwargs)
            threaded_again = self.get_report(cov, squeeze=False, workers=4, **kwargs)
            auto = self.get_report(cov, squeeze=False, workers=0, **kwargs)
        assert serial == threaded == threaded_again == auto

    def test_more_workers_than_files(self) -> None:
        cov = self.make_project()
        serial = self.get_report(cov, squeeze=False)
        with force_threads():
            threaded = self.get_report(cov, squeeze=False, workers=100)
        assert serial == threaded

    def test_json_identical(self) -> None:
        cov = self.make_project()

        def get_json(**kwargs: Any) -> str:
            cov.json_report(outfile="out.json", **kwargs)
            with open("out.json", encoding="utf-8") as f:
                data = json.load(f)
            del data["meta"]["timestamp"]
            return json.dumps(data, sort_keys=True)

        serial = get_json()
        with force_threads():
            threaded = get_json(workers=4)
        assert serial == threaded

    def test_xml_identical(self) -> None:
        cov = self.make_project()

        def get_xml(**kwargs: Any) -> str:
            cov.xml_report(outfile="out.xml", **kwargs)
            with open("out.xml", encoding="utf-8") as f:
                return re.sub(r'timestamp="\d+"', "", f.read())

        serial = get_xml()
        with force_threads():
            threaded = get_xml(workers=4)
        assert serial == threaded

    def test_lcov_identical(self) -> None:
        cov = self.make_project()

        def get_lcov(**kwargs: Any) -> str:
            cov.lcov_report(outfile="out.lcov", **kwargs)
            with open("out.lcov", encoding="utf-8") as f:
                return f.read()

        serial = get_lcov()
        with force_threads():
            threaded = get_lcov(workers=4)
        assert serial == threaded

    def test_html_identical(self) -> None:
        cov = self.make_project()

        def get_html(dirname: str, **kwargs: Any) -> dict[str, str]:
            cov.html_report(directory=dirname, **kwargs)
            contents = {}
            for fname in os.listdir(dirname):
                if fname.endswith(".html"):
                    with open(os.path.join(dirname, fname), encoding="utf-8") as f:
                        text = f.read()
                    contents[fname] = re.sub(
                        r"created at \d{4}-\d\d-\d\d \d\d:\d\d [-+]\d{4}", "", text
                    )
            return contents

        serial = get_html("htmlcov_serial")
        with force_threads():
            threaded = get_html("htmlcov_threads", workers=4)
        assert serial == threaded

    def test_gil_build_reports_serially_with_warning(self) -> None:
        cov = self.make_project()
        serial = self.get_report(cov, squeeze=False)
        warning_rx = r"Multi-threaded reporting needs a free-threaded Python"
        with mock.patch.object(report_core, "_threads_worthwhile", lambda: False):
            with pytest.warns(CoverageWarning, match=warning_rx):
                threaded = self.get_report(cov, squeeze=False, workers=4)
        assert serial == threaded


class WorkersEdgeCaseTest(CoverageTest):
    """Sharp edges of multi-threaded analysis."""

    def make_many_files(self, num: int = 25) -> dict[str, list[int]]:
        """Make `num` files, returning a {filename: lines} dict for a data file."""
        lines = {}
        for i in range(num):
            self.make_file(f"mod{i:02d}.py", "a = 1\nb = 2\nc = 3\n")
            lines[abs_file(f"mod{i:02d}.py")] = [1, 2]
        return lines

    def test_remapped_paths(self) -> None:
        # [paths] makes _prepare_data_for_reporting build an in-memory
        # CoverageData, which is empty when read from other threads unless
        # each worker gets its own copy of the data.
        lines = {}
        for i in range(25):
            src = "a = 1\nb = 2\nc = 3\n"
            self.make_file(f"src/mod{i:02d}.py", src)
            self.make_file(f"ver1/mod{i:02d}.py", src)
            lines[abs_file(f"ver1/mod{i:02d}.py")] = [1, 2]
        self.make_file(
            ".coveragerc",
            """\
            [paths]
            source =
                src
                ver1
            """,
        )
        self.make_data_file(lines=lines)

        def report(**kwargs: Any) -> str:
            cov = coverage.Coverage()
            cov.load()
            return self.get_report(cov, squeeze=False, **kwargs)

        serial = report()
        assert re.search(r"src[/\\]mod00\.py", serial)
        with force_threads():
            threaded = report(workers=4)
        assert serial == threaded

    def test_unparsable_file_raises(self) -> None:
        lines = self.make_many_files()
        self.make_file("bad.py", "if True:\n")
        lines[abs_file("bad.py")] = [1]
        self.make_data_file(lines=lines)
        cov = coverage.Coverage()
        cov.load()
        with force_threads():
            with pytest.raises(NotPython, match="Couldn't parse .* as Python source"):
                self.get_report(cov, workers=4)

    def test_unparsable_file_ignore_errors(self) -> None:
        lines = self.make_many_files()
        self.make_file("bad.py", "if True:\n")
        lines[abs_file("bad.py")] = [1]
        self.make_data_file(lines=lines)

        def report(**kwargs: Any) -> str:
            cov = coverage.Coverage()
            cov.load()
            with pytest.warns(CoverageWarning, match="Couldn't parse Python file"):
                return self.get_report(cov, squeeze=False, ignore_errors=True, **kwargs)

        serial = report()
        assert "bad.py" not in serial
        with force_threads():
            threaded = report(workers=4)
        assert serial == threaded

    def test_plugin_file_reporter(self) -> None:
        self.make_file(
            "wkr_plugin.py",
            """\
            from coverage import CoveragePlugin, FileReporter

            class MyFileReporter(FileReporter):
                def lines(self):
                    return {1, 2, 3}

            class Plugin(CoveragePlugin):
                def file_reporter(self, filename):
                    return MyFileReporter(filename)

            def coverage_init(reg, options):
                reg.add_file_tracer(Plugin())
            """,
        )
        self.make_file(
            ".coveragerc",
            """\
            [run]
            plugins = wkr_plugin
            """,
        )
        lines = self.make_many_files()
        self.make_file("weird.xyz", "1\n2\n3\n")
        lines[abs_file("weird.xyz")] = [1, 2]
        self.make_data_file(
            lines=lines,
            file_tracers={abs_file("weird.xyz"): "wkr_plugin.Plugin"},
        )

        def report(**kwargs: Any) -> str:
            cov = coverage.Coverage()
            cov.load()
            return self.get_report(cov, squeeze=False, **kwargs)

        serial = report()
        assert "weird.xyz" in serial
        with force_threads():
            threaded = report(workers=4)
        assert serial == threaded


class NumAnalysisWorkersTest(CoverageTest):
    """Tests of _num_analysis_workers."""

    run_in_temp_dir = False

    def num_workers(self, option: int, num_files: int, threads_ok: bool = True) -> int:
        """Get the number of workers decided for the given settings."""
        cov = coverage.Coverage()
        cov.set_option("report:workers", option)
        with mock.patch.object(report_core, "_threads_worthwhile", lambda: threads_ok):
            return report_core._num_analysis_workers(cov, num_files)

    def test_default_is_serial(self) -> None:
        assert self.num_workers(1, 1000) == 1

    def test_negative_is_an_error(self) -> None:
        with pytest.raises(ConfigError, match="workers must be non-negative"):
            self.num_workers(-1, 1000)

    def test_auto_with_few_files_is_serial(self) -> None:
        assert self.num_workers(0, report_core.WORKERS_FILE_CUTOFF - 1) == 1

    def test_auto_with_many_files_uses_cpus(self) -> None:
        expected = min(os.cpu_count() or 1, 1000)
        assert self.num_workers(0, 1000) == expected

    def test_explicit_capped_by_files(self) -> None:
        assert self.num_workers(8, 3) == min(3, os.cpu_count() or 1)

    def test_explicit_capped_by_cpus(self) -> None:
        expected = min(1000, os.cpu_count() or 1)
        assert self.num_workers(1000, 5000) == expected

    def test_gil_means_serial(self) -> None:
        with pytest.warns(CoverageWarning, match="needs a free-threaded Python"):
            assert self.num_workers(4, 1000, threads_ok=False) == 1

    def test_gil_warning_only_once(self) -> None:
        cov = coverage.Coverage()
        cov.set_option("report:workers", 4)
        with mock.patch.object(report_core, "_threads_worthwhile", lambda: False):
            with pytest.warns(CoverageWarning, match="needs a free-threaded Python") as recorded:
                report_core._num_analysis_workers(cov, 1000)
                report_core._num_analysis_workers(cov, 1000)
        assert len(recorded) == 1

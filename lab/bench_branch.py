# Compare coveragepy branch claude/benchmark-vs-main-q77b73 against main.
# Run as: .venv/bin/python bench_branch.py 3

import os
from pathlib import Path

import benchmark
from benchmark import (
    CoverageSource,
    ProjectMashumaro,
    Python,
    ShellSession,
    run_experiment,
)

SCRATCH = Path("/tmp/claude-0/-home-user-coveragepy/639025bb-a038-5b24-a5da-8d0c707c800e/scratchpad")

# The container routes outbound network through a local proxy configured by
# environment variables, but ShellSession strips the environment down to PATH.
# Pass the proxy/CA configuration through so git/pip work inside the harness.
PASS_THROUGH_VARS = [
    "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy",
    "NO_PROXY", "no_proxy",
    "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "PIP_CERT",
    "HOME",
]

_orig_init = ShellSession.__init__

def _patched_init(self, output_filename):
    _orig_init(self, output_filename)
    for var in PASS_THROUGH_VARS:
        value = os.getenv(var)
        if value is not None:
            self.env_vars[var] = value

ShellSession.__init__ = _patched_init

# The agent proxy returns 403 for requests.head() against github.com, but git
# clones work (they are rewritten through a local git proxy).  Skip the check.
benchmark.url_must_exist = lambda url: True

# Keep the working area in the session scratchpad.
benchmark.PERF_DIR = SCRATCH / "covperf"


class ProjectMashumaroReports(ProjectMashumaro):
    """Mashumaro suite generating term + xml + html reports from one run."""

    def __init__(self):
        super().__init__(
            more_pytest_args="--cov-report=term --cov-report=xml --cov-report=html"
        )
        self.slug = "mashreport"


run_experiment(
    py_versions=[
        Python(3, 12),
    ],
    cov_versions=[
        CoverageSource(str(SCRATCH / "coveragepy-main"), slug="main"),
        CoverageSource("/home/user/coveragepy", slug="branch"),
    ],
    projects=[
        ProjectMashumaro(),
        ProjectMashumaroReports(),
    ],
    rows=["proj", "pyver"],
    column="cov",
    ratios=[
        ("branch vs main", "branch", "main"),
    ],
)

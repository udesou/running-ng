"""End-to-end exercise of the FreeBSD counter path on any POSIX host, against
tests/fixtures/fake_pmcstat.py. Cannot tell whether events resolve on real hardware.
"""
import json
import os
import shutil
import stat
import subprocess
import sys

import pytest

from running import counters
from running.benchmark import BinaryBenchmark
from running.modifier import PerfAndOllyAttach
from pathlib import Path

FAKE = Path(__file__).parent / "fixtures" / "fake_pmcstat.py"

pytestmark = pytest.mark.skipif(
    os.name != "posix" or not Path("/bin/sleep").exists(),
    reason="needs a POSIX host with /bin/sleep")


@pytest.fixture
def fake_pmcstat_on_path(tmp_path, monkeypatch):
    """Put a stand-in `pmcstat` first on PATH and force the FreeBSD backend."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    target = bindir / "pmcstat"
    shutil.copy(FAKE, target)
    target.chmod(target.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", "{}{}{}".format(bindir, os.pathsep, os.environ["PATH"]))
    monkeypatch.setenv(counters.BACKEND_ENV_VAR, "freebsd-pmc")
    return target


def _run(events):
    bm = BinaryBenchmark(Path("/bin/sleep"), ["1"], suite_name="s", name="smoke")
    mod = PerfAndOllyAttach(name="pmc_grp1", type="PerfAndOllyAttach", val=events)
    _out, companion, status = bm._run_with_perf_and_olly(
        ["/bin/sleep", "1"], {}, None, mod)
    return json.loads(companion), status


def test_freebsd_path_collects_and_aliases_counters(fake_pmcstat_on_path):
    data, status = _run("instructions,unhalted-cycles")
    assert status.name == "Normal"
    assert data["counter_backend"] == "freebsd-pmc"
    by_name = {e["event"]: e["counter-value"] for e in data["perf"]}
    # "unhalted-cycles" is aliased onto perf's "cycles"
    assert set(by_name) == {"instructions", "cycles"}
    assert all(v > 0 for v in by_name.values())


def test_freebsd_path_still_reports_rusage(fake_pmcstat_on_path):
    data, _ = _run("instructions")
    # on FreeBSD rusage is the only CPU-time source
    assert set(data["rusage"]) >= {"user_time", "system_time", "minor_faults"}


def test_freebsd_path_has_no_task_clock_crosscheck(fake_pmcstat_on_path):
    data, _ = _run("instructions,unhalted-cycles")
    # absent, not silently passing: no task-clock on this backend
    assert "perf_incomplete" not in data


def test_benchmark_exit_status_is_the_benchmarks_own(fake_pmcstat_on_path):
    """`pmc stat` always returns 0; attaching keeps the benchmark's own exit status."""
    bm = BinaryBenchmark(Path("/bin/sh"), [], suite_name="s", name="crashy")
    mod = PerfAndOllyAttach(name="pmc_grp1", type="PerfAndOllyAttach", val="instructions")
    _out, _companion, status = bm._run_with_perf_and_olly(
        ["/bin/sh", "-c", "exit 42"], {}, None, mod)
    assert status.name == "Error"


def test_bad_event_list_degrades_without_failing_the_run(fake_pmcstat_on_path, caplog):
    """An unresolvable event name costs the invocation its counters, not the sweep."""
    bm = BinaryBenchmark(Path("/bin/sleep"), ["1"], suite_name="s", name="smoke")
    mod = PerfAndOllyAttach(name="pmc_grp1", type="PerfAndOllyAttach",
                            val="stalled-cycles-frontend")
    _out, companion, status = bm._run_with_perf_and_olly(
        ["/bin/sleep", "1"], {}, None, mod)
    data = json.loads(companion)
    assert status.name == "Normal"          # the benchmark itself was fine
    assert data.get("perf") in ([], None)   # but produced no counters
    assert "pmc list" in caplog.text

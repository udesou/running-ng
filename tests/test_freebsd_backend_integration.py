"""End-to-end exercise of the FreeBSD counter path, on any POSIX host.

Nobody has run running-ng on FreeBSD yet.  This drives the real code path
(backend selection, command construction, process lifecycle, output file
handling, table parsing, event aliasing) against tests/fixtures/fake_pmcstat.py,
which reproduces pmcstat(8)'s counting-mode output format from its source.

What this canNOT tell us: whether hwpmc is loaded, whether the event names
resolve on real hardware, or whether the counts mean anything.  Only a FreeBSD
box answers those.  It does mean that when one is available, the remaining
failures are about PMCs rather than about plumbing.
"""
import json
import os
import shutil
import stat
import subprocess
import sys

import pytest

from running import counters, suite  # noqa: F401  (suite first: see test_osinfo)
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
    # "unhalted-cycles" is aliased onto perf's "cycles" so the contract
    # vocabulary needs no change to accept FreeBSD counters.
    assert set(by_name) == {"instructions", "cycles"}
    assert all(v > 0 for v in by_name.values())


def test_freebsd_path_still_reports_rusage(fake_pmcstat_on_path):
    data, _ = _run("instructions")
    # rusage is the floor every backend keeps, and on FreeBSD it is also the
    # only CPU-time source, since pmcstat has no task-clock equivalent.
    assert set(data["rusage"]) >= {"user_time", "system_time", "minor_faults"}


def test_freebsd_path_has_no_task_clock_crosscheck(fake_pmcstat_on_path):
    data, _ = _run("instructions,unhalted-cycles")
    # The check must be absent rather than silently passing: there is no
    # task-clock on this backend to compare against rusage.
    assert "perf_incomplete" not in data


def test_benchmark_exit_status_is_the_benchmarks_own(fake_pmcstat_on_path):
    """The reason we attach instead of letting the tool launch.

    `pmc stat` always returns 0 (cmd_pmc_stat.c:481), so a crashed benchmark
    would look clean.  Attaching keeps the benchmark our direct child, so a
    non-zero exit still surfaces.
    """
    bm = BinaryBenchmark(Path("/bin/sh"), [], suite_name="s", name="crashy")
    mod = PerfAndOllyAttach(name="pmc_grp1", type="PerfAndOllyAttach", val="instructions")
    _out, _companion, status = bm._run_with_perf_and_olly(
        ["/bin/sh", "-c", "exit 42"], {}, None, mod)
    assert status.name == "Error"


def test_bad_event_list_degrades_without_failing_the_run(fake_pmcstat_on_path, caplog):
    """A wrong event name is the expected first failure on real hardware.

    libpmc has no portable aliases on modern x86, so the names in a config will
    not resolve until someone reads them off `pmc list`.  That must cost the
    invocation its counters, not the whole sweep.
    """
    bm = BinaryBenchmark(Path("/bin/sleep"), ["1"], suite_name="s", name="smoke")
    # The stand-in refuses to allocate an event that is neither a libpmc alias
    # nor a raw uppercase name, exactly as pmc_allocate does on real hardware.
    mod = PerfAndOllyAttach(name="pmc_grp1", type="PerfAndOllyAttach",
                            val="stalled-cycles-frontend")
    _out, companion, status = bm._run_with_perf_and_olly(
        ["/bin/sleep", "1"], {}, None, mod)
    data = json.loads(companion)
    assert status.name == "Normal"          # the benchmark itself was fine
    assert data.get("perf") in ([], None)   # but produced no counters
    assert "pmc list" in caplog.text

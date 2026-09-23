"""Tests for the host-OS abstraction; non-Linux behaviour is simulated by patching running.osinfo."""
import os
import shutil
import subprocess
import sys

import pytest

#: /bin/true on Linux, /usr/bin/true on FreeBSD and macOS.
TRUE_BIN = shutil.which("true")

from running import osinfo
from running.benchmark import pid_alive, pid_is_benchmark
from running.command import runbms


def test_probe_returns_empty_for_missing_command():
    # a probe the host does not ship must not raise (vmstat on macOS)
    assert osinfo.probe("running-ng-definitely-not-a-command") == ""


def test_probe_returns_empty_on_nonzero_exit():
    assert osinfo.probe("exit 3") == ""


def test_probe_captures_stdout():
    assert osinfo.probe("echo hello").strip() == "hello"


def test_core_count_is_positive():
    assert osinfo.core_count() > 0


def test_exactly_one_platform_flag():
    flags = [osinfo.IS_LINUX, osinfo.IS_DARWIN, osinfo.IS_FREEBSD]
    assert sum(flags) <= 1


def test_snapshot_commands_are_strings():
    assert isinstance(osinfo.memory_snapshot_cmd(), str)
    assert isinstance(osinfo.process_snapshot_cmd(), str)


@pytest.mark.skipif(not osinfo.EXE_LOOKUP_SUPPORTED,
                    reason="platform has no PID->exe lookup")
def test_pid_exe_name_resolves_self():
    assert osinfo.pid_exe_name(os.getpid())


@pytest.mark.skipif(not osinfo.EXE_LOOKUP_SUPPORTED,
                    reason="platform has no PID->exe lookup")
def test_pid_exe_name_resolves_a_known_child():
    p = subprocess.Popen(["sleep", "30"])
    try:
        assert osinfo.pid_exe_name(p.pid) == "sleep"
    finally:
        p.kill()
        p.wait()


def test_pid_exe_name_returns_none_for_bogus_pid():
    # above every platform's pid_max
    assert osinfo.pid_exe_name(2 ** 31 - 1) is None


def test_pid_is_benchmark_rejects_dead_pid():
    assert not pid_is_benchmark(2 ** 31 - 1)


def test_pid_alive_true_for_self():
    assert pid_alive(os.getpid())


@pytest.mark.skipif(not osinfo.EXE_LOOKUP_SUPPORTED,
                    reason="platform has no PID->exe lookup")
def test_pid_is_benchmark_rejects_build_tool(monkeypatch):
    monkeypatch.setattr(osinfo, "pid_exe_name", lambda pid: "ocamlfind")
    assert not pid_is_benchmark(os.getpid())


@pytest.mark.skipif(not osinfo.EXE_LOOKUP_SUPPORTED,
                    reason="platform has no PID->exe lookup")
def test_pid_is_benchmark_accepts_non_build_tool(monkeypatch):
    monkeypatch.setattr(osinfo, "pid_exe_name", lambda pid: "coq_bench")
    assert pid_is_benchmark(os.getpid())


def test_pid_is_benchmark_rejects_unreadable_exe_where_lookup_works(monkeypatch):
    """A failed lookup where lookups work means zombie/transient: reject, or dying subshells win the race."""
    monkeypatch.setattr(osinfo, "EXE_LOOKUP_SUPPORTED", True)
    monkeypatch.setattr(osinfo, "pid_exe_name", lambda pid: None)
    assert not pid_is_benchmark(os.getpid())


def test_pid_is_benchmark_degrades_to_alive_check_without_lookup(monkeypatch):
    """Without a PID->exe lookup, alive must be enough, or the olly attach never fires."""
    monkeypatch.setattr(osinfo, "EXE_LOOKUP_SUPPORTED", False)
    monkeypatch.setattr(osinfo, "pid_exe_name", lambda pid: None)
    assert pid_is_benchmark(os.getpid())
    assert not pid_is_benchmark(2 ** 31 - 1)


def test_hz_to_ghz_handles_unreadable_node():
    assert runbms.hz_to_ghz("") == "unknown"
    assert runbms.hz_to_ghz("3600000") == "3.60 GHz"


def test_cpu_frequency_info_empty_off_linux(monkeypatch):
    monkeypatch.setattr(osinfo, "IS_LINUX", False)
    assert runbms.cpu_frequency_info() == ""


def _prologue():
    from running.runtime import DummyRuntime
    from running.benchmark import BinaryBenchmark
    from pathlib import Path
    bm = BinaryBenchmark(Path(TRUE_BIN), [], suite_name="s", name="b")
    return runbms.get_log_prologue(DummyRuntime(""), bm)


def test_log_prologue_runs_on_this_host():
    out = _prologue()
    assert "running-ng v" in out
    assert "number of cores: " in out


def test_log_prologue_survives_a_host_with_no_probes(monkeypatch):
    """An OS where every probe is missing must not raise in the prologue."""
    monkeypatch.setattr(osinfo, "IS_LINUX", False)
    monkeypatch.setattr(osinfo, "memory_snapshot_cmd", lambda: "")
    monkeypatch.setattr(osinfo, "process_snapshot_cmd", lambda: "")
    monkeypatch.setattr(osinfo, "cpu_model", lambda: "")
    out = _prologue()
    assert "CPU: unknown" in out
    assert "number of cores: " in out


def test_benchmark_and_suite_import_in_either_order():
    """running.benchmark and running.suite import each other; either order must work."""
    for first in ("running.benchmark", "running.suite"):
        second = "running.suite" if first == "running.benchmark" else "running.benchmark"
        code = "import {}; import {}".format(first, second)
        r = subprocess.run([sys.executable, "-c", code],
                           capture_output=True, text=True)
        assert r.returncode == 0, "{} first: {}".format(first, r.stderr)

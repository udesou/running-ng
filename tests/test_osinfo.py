"""Tests for the host-OS abstraction and the two call sites step 1 fixed.

The point of most of these is the *non-Linux* behaviour, which CI cannot
exercise natively, so they simulate it by patching running.osinfo.
"""
import os
import subprocess

import pytest

from running import osinfo
# running.suite must be imported before running.benchmark: the two import each
# other, and only the suite-first order resolves (benchmark takes the module
# object, suite takes names from it).  Pre-existing, unrelated to this module —
# tests/test_modifier.py and tests/test_ocaml_built_binary.py hit the same
# thing when run in isolation.
from running import suite  # noqa: F401
from running.benchmark import pid_alive, pid_is_benchmark
from running.command import runbms


# --- osinfo probes ------------------------------------------------------------

def test_probe_returns_empty_for_missing_command():
    # The whole point: a probe the host does not ship must not raise.  This is
    # what aborted every run on macOS, where `vmstat` does not exist.
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
    # "" is the documented "no equivalent on this OS" value; callers skip it.
    assert isinstance(osinfo.memory_snapshot_cmd(), str)
    assert isinstance(osinfo.process_snapshot_cmd(), str)


# --- PID -> executable --------------------------------------------------------

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
    # 2**31-1 is above every platform's pid_max, so it can never be live.
    assert osinfo.pid_exe_name(2 ** 31 - 1) is None


# --- pid_is_benchmark ---------------------------------------------------------

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
    """A lookup that *could* work but failed means zombie/transient: reject.

    This is the behaviour the /proc implementation had, and it must survive
    the refactor: accepting here lets dying subshells win the race.
    """
    monkeypatch.setattr(osinfo, "EXE_LOOKUP_SUPPORTED", True)
    monkeypatch.setattr(osinfo, "pid_exe_name", lambda pid: None)
    assert not pid_is_benchmark(os.getpid())


def test_pid_is_benchmark_degrades_to_alive_check_without_lookup(monkeypatch):
    """The regression this whole step exists for.

    On a platform with no PID->exe lookup the old code rejected every PID,
    so the olly attach never fired and each invocation burned its full
    deadline producing no GC data.  Alive must now be enough.
    """
    monkeypatch.setattr(osinfo, "EXE_LOOKUP_SUPPORTED", False)
    monkeypatch.setattr(osinfo, "pid_exe_name", lambda pid: None)
    assert pid_is_benchmark(os.getpid())
    assert not pid_is_benchmark(2 ** 31 - 1)


# --- log prologue -------------------------------------------------------------

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
    bm = BinaryBenchmark(Path("/bin/true"), [], suite_name="s", name="b")
    return runbms.get_log_prologue(DummyRuntime(""), bm)


def test_log_prologue_runs_on_this_host():
    out = _prologue()
    assert "running-ng v" in out
    assert "number of cores: " in out


def test_log_prologue_survives_a_host_with_no_probes(monkeypatch):
    """Simulates an OS where every probe is missing.

    The prologue runs before *every* invocation, so anything that raises here
    kills the sweep rather than one benchmark.  Previously `vmstat` alone did
    exactly that on macOS.
    """
    monkeypatch.setattr(osinfo, "IS_LINUX", False)
    monkeypatch.setattr(osinfo, "memory_snapshot_cmd", lambda: "")
    monkeypatch.setattr(osinfo, "process_snapshot_cmd", lambda: "")
    monkeypatch.setattr(osinfo, "cpu_model", lambda: "")
    out = _prologue()
    assert "CPU: unknown" in out
    assert "number of cores: " in out

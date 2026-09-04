"""Host-OS abstraction: the few places running-ng must ask the kernel directly.

Everything here has a Linux implementation that matches the historical
behaviour exactly, plus macOS/FreeBSD equivalents and a documented
degraded mode when a platform offers nothing.  The rule is that a missing
capability must degrade (return None / empty string), never raise: these
helpers run once per invocation on the measurement path, and losing a
multi-thousand-invocation sweep to a probe is far worse than losing the
probe's output.
"""
import ctypes
import ctypes.util
import logging
import os
import platform
import subprocess
from typing import List, Optional

SYSTEM = platform.system()
IS_LINUX = SYSTEM == "Linux"
IS_DARWIN = SYSTEM == "Darwin"
IS_FREEBSD = SYSTEM == "FreeBSD"


# --- process executable lookup ------------------------------------------------
#
# Used by the olly attach path to tell the real benchmark apart from the
# short-lived build tools some benchmark wrapper scripts spawn.  Called in a
# 10 ms poll loop while the benchmark is starting, so implementations must
# avoid forking where the platform allows it: a fork per poll would perturb
# the very process we are about to measure.

def _exe_name_proc(pid: int) -> str:
    """Linux: /proc/<pid>/exe."""
    return os.path.basename(os.readlink("/proc/{}/exe".format(pid)))


def _exe_name_procfs_file(pid: int) -> str:
    """FreeBSD with procfs(5) mounted: /proc/<pid>/file."""
    return os.path.basename(os.readlink("/proc/{}/file".format(pid)))


_PROC_PIDPATHINFO_MAXSIZE = 4 * 1024


def _load_libproc():
    if not IS_DARWIN:
        return None
    try:
        return ctypes.CDLL(ctypes.util.find_library("proc") or "libproc.dylib",
                           use_errno=True)
    except OSError:
        return None


_libproc = _load_libproc()


def _exe_name_libproc(pid: int) -> str:
    """macOS: proc_pidpath(3).  Fork-free, but only for same-uid targets."""
    if _libproc is None:
        raise OSError("libproc unavailable")
    buf = ctypes.create_string_buffer(_PROC_PIDPATHINFO_MAXSIZE)
    n = _libproc.proc_pidpath(ctypes.c_int(pid), buf,
                              ctypes.c_uint32(_PROC_PIDPATHINFO_MAXSIZE))
    if n <= 0:
        raise OSError(ctypes.get_errno(), "proc_pidpath failed for {}".format(pid))
    return os.path.basename(buf.value.decode("utf-8", "replace"))


# FreeBSD sysctl mib for KERN_PROC_PATHNAME: {CTL_KERN, KERN_PROC,
# KERN_PROC_PATHNAME, pid}.  Stable ABI, so hard-coding the constants is safe;
# still guarded, because getting them wrong must degrade rather than crash.
_CTL_KERN = 1
_KERN_PROC = 14
_KERN_PROC_PATHNAME = 12

_libc = None
if IS_FREEBSD:
    try:
        _libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.7",
                            use_errno=True)
    except OSError:
        _libc = None


def _exe_name_sysctl(pid: int) -> str:
    """FreeBSD: sysctl(KERN_PROC_PATHNAME).  Fork-free."""
    if _libc is None:
        raise OSError("libc unavailable")
    mib = (ctypes.c_int * 4)(_CTL_KERN, _KERN_PROC, _KERN_PROC_PATHNAME, pid)
    size = ctypes.c_size_t(_PROC_PIDPATHINFO_MAXSIZE)
    buf = ctypes.create_string_buffer(_PROC_PIDPATHINFO_MAXSIZE)
    if _libc.sysctl(mib, 4, buf, ctypes.byref(size), None, ctypes.c_size_t(0)) != 0:
        raise OSError(ctypes.get_errno(), "sysctl kern.proc.pathname failed")
    return os.path.basename(buf.value.decode("utf-8", "replace"))


def _pick_exe_name_impl():
    if IS_LINUX:
        return _exe_name_proc
    if IS_DARWIN and _libproc is not None:
        return _exe_name_libproc
    if IS_FREEBSD:
        # procfs is not mounted by default on modern FreeBSD; prefer sysctl and
        # keep the symlink as a fallback for hosts that do mount it.
        if _libc is not None:
            return _exe_name_sysctl
        if os.path.isdir("/proc/self"):
            return _exe_name_procfs_file
    return None


_exe_name_impl = _pick_exe_name_impl()

#: False when this platform offers no way to resolve a PID's executable.
#: Callers must then fall back to a weaker check rather than rejecting
#: every PID, which is what the /proc-only implementation used to do.
EXE_LOOKUP_SUPPORTED = _exe_name_impl is not None

if not EXE_LOOKUP_SUPPORTED:
    logging.debug(
        "No PID->executable lookup on %s; consumers will degrade to weaker checks",
        SYSTEM)


def pid_exe_name(pid: int) -> Optional[str]:
    """Basename of the executable PID is running, or None.

    None means either "this platform cannot tell us" (see
    :data:`EXE_LOOKUP_SUPPORTED`) or "the lookup failed for this PID" — a
    zombie, a process that exited mid-call, or one we may not inspect.
    Callers that must distinguish the two check the flag.
    """
    if _exe_name_impl is None:
        return None
    try:
        return _exe_name_impl(pid)
    except (OSError, UnicodeError, ValueError):
        return None


# --- host description probes --------------------------------------------------
#
# Purely informational: these land in the per-invocation log prologue.

def probe(cmd: str) -> str:
    """Run a shell probe for the log prologue.  Never raises, never fails a run.

    Returns "" if the command is missing or errors.  Unlike util.system this
    deliberately does not check the exit status: a probe that does not exist on
    this OS is a missing log line, not a failed benchmark.
    """
    try:
        p = subprocess.run(cmd, shell=True, stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return ""
    return p.stdout.decode("utf-8", "replace")


def cpu_model() -> str:
    """Human-readable CPU model, or "" if we cannot determine it."""
    if IS_LINUX:
        try:
            with open("/proc/cpuinfo") as f:
                for line in f:
                    # x86 says "model name"; arm64 has neither, hence the
                    # "Model" fallback from /proc/device-tree consumers.
                    if line.startswith("model name"):
                        return line.split(":", 1)[1].strip()
        except OSError:
            pass
        return probe("lscpu 2>/dev/null | sed -n 's/^Model name: *//p'").strip()
    if IS_DARWIN:
        return probe("sysctl -n machdep.cpu.brand_string").strip()
    if IS_FREEBSD:
        return probe("sysctl -n hw.model").strip()
    return ""


def core_count() -> int:
    """Logical core count.  os.cpu_count() is correct on all three platforms."""
    return os.cpu_count() or 0


def memory_snapshot_cmd() -> str:
    """Shell command giving a virtual-memory/paging snapshot, or "" if none."""
    if IS_DARWIN:
        return "vm_stat"
    if IS_LINUX or IS_FREEBSD:
        return "vmstat 1 2"
    return ""


def process_snapshot_cmd() -> str:
    """Shell command listing the busiest processes, or "" if none."""
    if IS_LINUX:
        return "top -bcn 1 -w512 | head -n 12"
    if IS_DARWIN:
        # macOS top has no batch flag; -l 1 takes a single sample.
        return "top -l 1 -n 12 | head -n 20"
    if IS_FREEBSD:
        return "top -b -n 12"
    return ""

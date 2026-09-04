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
from typing import List, Optional, Sequence, Set, Tuple

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


# --- CPU topology and pinning --------------------------------------------------
#
# The pinning *mechanism* is per-OS (taskset on Linux, cpuset on FreeBSD, none
# on macOS).  The CPU *list* is per-machine and cannot be hardcoded per-OS: on
# one Ryzen 9 9950X, Linux enumerates SMT siblings as (0,16),(1,17)...(15,31)
# so one-thread-per-core is 0-15, while FreeBSD on the same silicon typically
# enumerates (0,1),(2,3)...(30,31) so the same policy is 0,2,4,...,30.  Hence
# detection at run time rather than a constant in a config file.


def _linux_sibling_groups() -> List[List[int]]:
    """SMT sibling sets from sysfs, one list per physical core."""
    groups, seen = [], set()
    base = "/sys/devices/system/cpu"
    try:
        entries = sorted(
            (int(n[3:]) for n in os.listdir(base)
             if n.startswith("cpu") and n[3:].isdigit()))
    except OSError:
        return []
    for cpu in entries:
        if cpu in seen:
            continue
        path = "{}/cpu{}/topology/thread_siblings_list".format(base, cpu)
        try:
            with open(path) as f:
                raw = f.read().strip()
        except OSError:
            # Offline CPU, or a kernel without topology info: treat it as its
            # own core rather than dropping it.
            raw = str(cpu)
        siblings = sorted(_parse_cpu_list(raw)) or [cpu]
        seen.update(siblings)
        groups.append(siblings)
    return groups


def _freebsd_sibling_groups() -> List[List[int]]:
    """SMT sibling sets from kern.sched.topology_spec.

    That sysctl emits an XML tree (sys/kern/sched_ule.c:3211-3250) where a
    group carrying the THREAD (or SMT) flag is exactly one physical core's
    hardware threads.  Groups without the flag are caches or NUMA nodes, so
    only the flagged leaves are sibling sets; with SMT off there are none and
    every CPU is its own core.
    """
    import xml.etree.ElementTree as ET
    xml = probe("sysctl -n kern.sched.topology_spec")
    if not xml.strip():
        return []
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        logging.warning("Could not parse kern.sched.topology_spec; not pinning")
        return []
    groups: List[List[int]] = []
    seen: Set[int] = set()
    for group in root.iter("group"):
        # Only this group's OWN flags. ElementTree's iter() recurses, so
        # asking a parent for "flag" would return its children's flags too and
        # make the whole package look like one SMT sibling set.
        flags_el = group.find("flags")
        flags = ({f.get("name") for f in flags_el.findall("flag")}
                 if flags_el is not None else set())
        if not ({"THREAD", "SMT"} & flags):
            continue
        cpu_el = group.find("cpu")
        if cpu_el is None or not cpu_el.text:
            continue
        cpus = sorted(_parse_cpu_list(cpu_el.text))
        if cpus and not (set(cpus) & seen):
            seen.update(cpus)
            groups.append(cpus)
    if not groups:
        # No SMT: every online CPU is its own physical core.
        groups = [[c] for c in range(core_count())]
    return groups


def _parse_cpu_list(text: str) -> List[int]:
    """Parse "0,16", "0-3", "0, 1, 2" and mixtures of those."""
    out: List[int] = []
    for part in text.replace(",", " ").split():
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, _, hi = part.partition("-")
            try:
                out.extend(range(int(lo), int(hi) + 1))
            except ValueError:
                continue
        else:
            try:
                out.append(int(part))
            except ValueError:
                continue
    return out


def sibling_groups() -> List[List[int]]:
    """One sorted list of logical CPUs per physical core, cores in order.

    Empty when the platform does not expose topology (macOS), which callers
    read as "cannot pin here".
    """
    if IS_LINUX:
        return _linux_sibling_groups()
    if IS_FREEBSD:
        return _freebsd_sibling_groups()
    return []


def partition_cpus(reserved_cores: int = 0) -> Tuple[List[int], List[int]]:
    """Split the machine into (benchmark CPUs, observer CPUs).

    The benchmark gets one hardware thread per physical core, which is the
    policy `pin_lavyek` encodes by hand today.  `reserved_cores` hands that
    many whole physical cores (both threads) to the observers instead.

    Reserving costs the benchmark cores, so it changes what is being measured:
    do not turn it on midway through a sweep that is meant to be comparable.
    The default of 0 reproduces today's behaviour exactly, leaving observers on
    the SMT siblings, which is weaker isolation than it looks since siblings
    share execution resources with the benchmark.

    Returns ([], []) where topology is unavailable.
    """
    groups = sibling_groups()
    if not groups:
        return [], []
    if reserved_cores < 0:
        raise ValueError("reserved_cores must be >= 0")
    # Never hand away so many cores that the benchmark has none left.
    reserved = min(reserved_cores, max(0, len(groups) - 1))
    if reserved != reserved_cores:
        logging.warning(
            "reserved_cores=%d would leave the benchmark %d cores; reserving %d instead",
            reserved_cores, len(groups) - reserved_cores, reserved)
    bench_groups = groups[:len(groups) - reserved] if reserved else groups
    observer_groups = groups[len(groups) - reserved:] if reserved else []

    bench = [g[0] for g in bench_groups]
    observers = [c for g in bench_groups for c in g[1:]]
    observers += [c for g in observer_groups for c in g]
    return sorted(bench), sorted(observers)


def format_cpu_list(cpus: Sequence[int]) -> str:
    """Render CPUs as the compact ranges both taskset and cpuset accept."""
    ordered = sorted(cpus)
    if not ordered:
        return ""
    out: List[Tuple[int, int]] = []
    run_start = prev = ordered[0]
    for c in ordered[1:]:
        if c == prev + 1:
            prev = c
            continue
        out.append((run_start, prev))
        run_start = prev = c
    out.append((run_start, prev))
    return ",".join(str(a) if a == b else "{}-{}".format(a, b) for a, b in out)


def pin_command(cpus: Sequence[int]) -> List[str]:
    """Command prefix confining a process to `cpus`, or [] if we cannot.

    macOS deliberately returns []: it exposes no CPU affinity API that binds a
    process to a core (only thread affinity *hints*, which the scheduler is
    free to ignore), so there is nothing honest to emit.
    """
    if not cpus:
        return []
    listing = format_cpu_list(cpus)
    if IS_LINUX:
        return ["taskset", "-c", listing]
    if IS_FREEBSD:
        return ["cpuset", "-l", listing]
    return []


def can_pin() -> bool:
    return bool(sibling_groups()) and bool(pin_command([0]))

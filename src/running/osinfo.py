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
import re
import shutil
import subprocess
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

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
    base = SYSFS_CPU_BASE
    try:
        entries = sorted(
            (int(n[3:]) for n in os.listdir(base)
             if n.startswith("cpu") and n[3:].isdigit()))
    except OSError:
        return []
    # Offline CPUs have no readable topology, and the fallback below would
    # turn each into a phantom single-thread "core" that the benchmark could
    # then be pinned to.  Seen on an 8-core box with SMT off: all 16 present
    # CPUs came back as 16 physical cores.
    online = set(online_cpus())
    for cpu in entries:
        if cpu in seen:
            continue
        if online and cpu not in online:
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
        if online:
            siblings = [c for c in siblings if c in online] or [cpu]
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


#: sysfs root for CPU topology and the kernel's CPU lists.  A module constant
#: so tests can point it at a fixture tree instead of the running machine.
SYSFS_CPU_BASE = "/sys/devices/system/cpu"


def _sysfs_cpu_list(name: str) -> List[int]:
    """Read <SYSFS_CPU_BASE>/<name>, which holds a list like "0,2-6"."""
    if SYSTEM != "Linux":
        return []
    try:
        with open("{}/{}".format(SYSFS_CPU_BASE, name)) as f:
            return sorted(_parse_cpu_list(f.read().strip()))
    except OSError:
        return []


def online_cpus() -> List[int]:
    """Logical CPUs the kernel will schedule on at all.

    Empty where the list is unavailable (non-Linux, or a kernel without the
    sysfs file), which callers read as "no information, assume every CPU".
    """
    return _sysfs_cpu_list("online")


def isolated_cpus() -> List[int]:
    """Logical CPUs the kernel has removed from scheduler load balancing.

    Populated by `isolcpus=` on the Linux cmdline (and on some kernels by
    `nohz_full=`).  Such a CPU still runs work pinned to it, but the scheduler
    will neither migrate anything onto it nor balance among the isolated set:
    a mask spanning several of them puts every thread on ONE and leaves it
    there.  Measured on an 8-core Xeon with isolcpus=4,6,8,10,12,14: six
    spinners sharing that mask reached 99% CPU, the same six pinned one per
    CPU reached 599%.

    So an isolated set suits a single-threaded benchmark, or one that pins its
    own threads (as lavyek_bench.ml does), and nothing else.  partition_cpus
    hands it to the benchmark; deciding how many of those CPUs a given
    benchmark may span is the caller's job.

    FreeBSD has no boot-time equivalent -- partitioning there is done at
    runtime with cpuset(1) -- and macOS cannot pin at all, so this is empty on
    both, and the topology policy applies unchanged.
    """
    return _sysfs_cpu_list("isolated")


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


def split_groups_by_node(groups: List[List[int]]
                         ) -> Tuple[List[List[int]], List[List[int]]]:
    """Split sibling groups into (one NUMA node's, everything else's).

    Returns (groups, []) unchanged on a single-node machine, or where the
    platform does not report NUMA topology.  The node kept is the one holding
    the most sibling groups; ties go to the lowest-numbered node so the choice
    is stable across runs on one machine.

    A group straddling nodes (which should not happen, but is not worth
    crashing over) counts as belonging to none and stays with the benchmark.
    """
    nodes = numa_nodes()
    if len(nodes) <= 1 or not groups:
        return groups, []
    node_of = {}
    for index, cpus in enumerate(nodes):
        for cpu in cpus:
            node_of[cpu] = index

    def node_for(group: List[int]) -> Optional[int]:
        seen = {node_of.get(c) for c in group}
        if len(seen) == 1:
            return seen.pop()
        return None

    by_node: Dict[Optional[int], List[List[int]]] = {}
    for group in groups:
        by_node.setdefault(node_for(group), []).append(group)
    real = {n: g for n, g in by_node.items() if n is not None}
    if not real:
        return groups, []
    chosen = max(sorted(real), key=lambda n: len(real[n]))
    kept = [g for g in groups if node_for(g) in (chosen, None)]
    rest = [g for g in groups if node_for(g) not in (chosen, None)]
    return kept, rest


def partition_cpus(reserved_cores: int = 0,
                   one_node: bool = False) -> Tuple[List[int], List[int]]:
    """Split the machine into (benchmark CPUs, observer CPUs).

    The benchmark gets one hardware thread per physical core, which is the
    policy `pin_lavyek` used to encode by hand.

    `reserved_cores` hands that many whole physical cores (both threads) to
    the observers instead.  The default of 0 reproduces the historical
    behaviour: the benchmark gets every physical core and the observers land
    on its SMT siblings, which is weaker isolation than it looks since
    siblings share execution resources with the benchmark threads.  Reserving
    costs the benchmark cores, so it changes what is being measured: not a
    mid-sweep decision.

    `one_node` confines the benchmark to a single NUMA node and gives every
    other node to the observers.  On a multi-socket machine that is usually
    the best arrangement available: the benchmark keeps a whole node's cores
    and the observers get a whole node of their own, rather than either giving
    cores up.  It also stops the benchmark's own memory traffic crossing the
    interconnect, which for GC work is a large source of run-to-run variance.
    No effect on a single-node machine, so a config carrying it stays
    portable.

    When it does take effect the benchmark's own SMT siblings are left IDLE
    rather than handed to the observers: with a whole spare node available the
    siblings buy nothing and would contend for the same physical cores.  So
    `one_node` genuinely means no SMT contention, whereas the default (where
    the siblings are the only spare CPUs there are) does not.

    Note `cpuset -l` sets CPU affinity but not the NUMA memory domain, so
    observer allocations can still land on either node.  `cpuset -n` is the
    knob if that ever matters.

    `reserved_cores` still applies within the chosen node if both are given,
    which is what you want when there is only one node to give.

    Where the kernel reports isolated CPUs (Linux `isolcpus=`), that split
    wins: the benchmark gets the isolated cores and the observers get the rest,
    with `reserved_cores` and `one_node` then applying within the isolated set.
    See isolated_cpus() for the load-balancing caveat that comes with it.

    Returns ([], []) where topology is unavailable.
    """
    groups = refine_groups(sibling_groups())
    if not groups:
        return [], []
    if reserved_cores < 0:
        raise ValueError("reserved_cores must be >= 0")
    # An `isolcpus=` boot line is the administrator having already drawn this
    # exact split: those cores are for the workload, the rest run the OS and
    # the interrupts (usually with a matching `irqaffinity=`).  Honour it in
    # preference to the topology policy, which knows about SMT but not about
    # which cores were set aside.
    #
    # Note what this does NOT fix: the scheduler does not balance among
    # isolated CPUs, so handing a multi-threaded benchmark this set confines
    # it to one of them unless it pins its own threads.  Choosing how many of
    # these CPUs to give a particular benchmark is the caller's decision.
    housekeeping: List[List[int]] = []
    isolated = set(isolated_cpus())
    if isolated:
        iso_groups = [g for g in groups if any(c in isolated for c in g)]
        other_groups = [g for g in groups if not any(c in isolated for c in g)]
        if iso_groups and other_groups:
            groups, housekeeping = iso_groups, other_groups
        elif iso_groups:
            logging.warning(
                "every core is isolated (%s); ignoring the isolation split, "
                "as it would leave the OS and the observers nowhere to run",
                format_cpu_list(sorted(isolated)))
    off_node: List[List[int]] = []
    if one_node:
        groups, off_node = split_groups_by_node(groups)
    # Never hand away so many cores that the benchmark has none left.
    reserved = min(reserved_cores, max(0, len(groups) - 1))
    if reserved != reserved_cores:
        logging.warning(
            "reserved_cores=%d would leave the benchmark %d cores; reserving %d instead",
            reserved_cores, len(groups) - reserved_cores, reserved)
    bench_groups = groups[:len(groups) - reserved] if reserved else groups
    observer_groups = groups[len(groups) - reserved:] if reserved else []

    bench = [g[0] for g in bench_groups]
    observers: List[int] = []
    # The benchmark's SMT siblings go to the observers only when there is
    # nothing better to give them. With a whole spare node available they buy
    # nothing and cost real contention, so they are left idle instead, and
    # that is what makes one_node's "no SMT contention" claim hold. Measured
    # on a 2-socket Xeon before this: every one of the benchmark's 10 cores
    # had its sibling in the observer set, which is precisely the interference
    # the physical-core split exists to avoid.
    if not off_node and not housekeeping:
        observers += [c for g in bench_groups for c in g[1:]]
    observers += [c for g in observer_groups for c in g]
    # Whole nodes the benchmark gave up go to the observers.
    observers += [c for g in off_node for c in g]
    # Non-isolated cores are where the OS already is, so the observers belong
    # there too.  They are also strictly better than the benchmark's own SMT
    # siblings, which is why the donation above is skipped when we have them.
    observers += [c for g in housekeeping for c in g]
    return sorted(bench), sorted(observers)


def _linux_numa_nodes() -> List[List[int]]:
    """CPUs per NUMA node from sysfs."""
    nodes = []
    base = "/sys/devices/system/node"
    try:
        names = sorted((n for n in os.listdir(base)
                        if n.startswith("node") and n[4:].isdigit()),
                       key=lambda n: int(n[4:]))
    except OSError:
        return []
    for name in names:
        try:
            with open("{}/{}/cpulist".format(base, name)) as f:
                cpus = _parse_cpu_list(f.read().strip())
        except OSError:
            continue
        if cpus:
            nodes.append(sorted(cpus))
    return nodes


def _linux_socket_count() -> Optional[int]:
    """Distinct physical_package_id values, or None if sysfs does not say."""
    base = "/sys/devices/system/cpu"
    packages = set()
    try:
        entries = [n for n in os.listdir(base)
                   if n.startswith("cpu") and n[3:].isdigit()]
    except OSError:
        return None
    for name in entries:
        try:
            with open("{}/{}/topology/physical_package_id".format(base, name)) as f:
                packages.add(f.read().strip())
        except OSError:
            continue
    return len(packages) or None


def _freebsd_numa_nodes() -> List[List[int]]:
    """CPUs per NUMA node from kern.sched.topology_spec's NODE-flagged groups."""
    import xml.etree.ElementTree as ET
    xml = probe("sysctl -n kern.sched.topology_spec")
    if not xml.strip():
        return []
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    nodes = []
    for group in root.iter("group"):
        flags_el = group.find("flags")
        flags = ({f.get("name") for f in flags_el.findall("flag")}
                 if flags_el is not None else set())
        if "NODE" not in flags:
            continue
        cpu_el = group.find("cpu")
        if cpu_el is not None and cpu_el.text:
            cpus = sorted(_parse_cpu_list(cpu_el.text))
            if cpus:
                nodes.append(cpus)
    return nodes


def numa_nodes() -> List[List[int]]:
    """CPUs per NUMA node, or [] where the platform does not say.

    Kernel-derived, so unlike the socket data from ocaml-processor this is
    available without any optional tool.  A benchmark CPU set that straddles a
    node boundary pays cross-socket memory traffic, which for GC work shows up
    as run-to-run variance; recording the boundary is the first step to being
    able to see that in the results.
    """
    if IS_LINUX:
        return _linux_numa_nodes()
    if IS_FREEBSD:
        return _freebsd_numa_nodes()
    return []


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


# --- optional refinement via ocaml-processor -----------------------------------
#
# https://github.com/haesbaert/ocaml-processor ships `ocaml-processor-dump`,
# which knows two things the kernel interfaces above do not surface uniformly:
# whether a core is a P-core or an E-core (hybrid Intel, Apple Silicon), and
# which socket it is on.  Pinning a benchmark across a P/E boundary or across
# sockets makes a nonsense of the measurement, so where that tool is present we
# use it to *narrow* the kernel's CPU set.
#
# Deliberately narrowing only, never replacing.  Its own README says that on
# anything but AMD64 and Apple it builds a fake topology where "each CPU will
# be its own core", and that its AMD64 path (pin the caller, run CPUID per CPU)
# is accurate only "as long as the process doesn't start in an already
# restricted affinity".  Both failure modes are silent.  Narrowing makes them
# harmless: a faked topology reports one socket and no E-cores, so it filters
# nothing and the kernel's view stands.

PROCESSOR_DUMP = "ocaml-processor-dump"

#: Set to a falsey value ("0", "no") to ignore ocaml-processor-dump entirely.
PROCESSOR_REFINE_ENV_VAR = "RUNNING_NG_USE_OCAML_PROCESSOR"

_CPU_LINE = re.compile(
    r"^cpu(?P<id>\d+):\s+smt=(?P<smt>\d+)\s+core=(?P<core>\d+)\s+"
    r"socket=(?P<socket>\d+)\s+kind=(?P<kind>\S+)")


def parse_processor_dump(text: str) -> List[Dict[str, Any]]:
    """Parse `ocaml-processor-dump` into one dict per logical CPU.

    Ignores the leading summary counters and anything unrecognised, so a new
    field or a new summary line upstream cannot break us.
    """
    cpus = []
    for line in text.splitlines():
        m = _CPU_LINE.match(line.strip())
        if not m:
            continue
        cpus.append({
            "id": int(m.group("id")),
            "smt": int(m.group("smt")),
            "core": int(m.group("core")),
            "socket": int(m.group("socket")),
            "kind": m.group("kind"),
        })
    return cpus


def processor_topology() -> List[Dict[str, Any]]:
    """Topology per ocaml-processor-dump, or [] if unavailable or disabled."""
    if os.environ.get(PROCESSOR_REFINE_ENV_VAR, "1").strip().lower() in (
            "0", "no", "false", ""):
        return []
    if shutil.which(PROCESSOR_DUMP) is None:
        return []
    return parse_processor_dump(probe(PROCESSOR_DUMP))


def _is_efficiency(kind: str) -> bool:
    return "e_core" in kind.strip().lower()


def refine_groups(groups: List[List[int]],
                  cpus: Optional[List[Dict[str, Any]]] = None
                  ) -> List[List[int]]:
    """Narrow sibling groups to one socket's performance cores.

    Returns `groups` unchanged when the extra topology says nothing useful
    (one socket, no E-cores), which is also what a faked topology looks like.
    Never returns empty: if filtering would remove everything, the unfiltered
    groups are better than no pinning at all.
    """
    if not groups:
        return groups
    if cpus is None:
        cpus = processor_topology()
    if not cpus:
        return groups
    by_id = {c["id"]: c for c in cpus}

    sockets = {c["socket"] for c in cpus}
    has_ecores = any(_is_efficiency(c["kind"]) for c in cpus)
    if len(sockets) <= 1 and not has_ecores:
        return groups

    def keep(group: List[int], socket: Optional[int]) -> bool:
        info = [by_id.get(c) for c in group]
        if any(i is None for i in info):
            # A CPU the tool did not describe: keep it rather than guess.
            return True
        if has_ecores and all(_is_efficiency(i["kind"]) for i in info):  # type: ignore[index]
            return False
        if socket is not None and any(i["socket"] != socket for i in info):  # type: ignore[index]
            return False
        return True

    chosen_socket = None
    if len(sockets) > 1:
        # Prefer the socket carrying the most performance cores; ties go to the
        # lowest id so the choice is stable across runs on one machine.
        def score(sock: int) -> Tuple[int, int]:
            n = sum(1 for c in cpus
                    if c["socket"] == sock and not _is_efficiency(c["kind"]))
            return (n, -sock)
        chosen_socket = max(sockets, key=score)

    refined = [g for g in groups if keep(g, chosen_socket)]
    if not refined:
        logging.warning(
            "ocaml-processor refinement would leave no CPUs; ignoring it")
        return groups
    if refined != groups:
        logging.info(
            "ocaml-processor narrowed the benchmark CPU set to %s (sockets=%d, "
            "E-cores present=%s)",
            format_cpu_list([c for g in refined for c in g]),
            len(sockets), has_ecores)
    return refined


def machine_topology_summary() -> Dict[str, Any]:
    """Topology facts worth recording alongside a result.

    Provenance only, so it is filled in as far as each source allows and stays
    silent about what it cannot determine.  Works on macOS too, where we can
    describe the machine but cannot pin on it.
    """
    summary: Dict[str, Any] = {}
    groups = sibling_groups()
    if groups:
        summary["physical_cores"] = len(groups)
        widths = {len(g) for g in groups}
        if len(widths) == 1:
            summary["threads_per_core"] = widths.pop()
    # Kernel-derived, so present even without ocaml-processor installed.
    nodes = numa_nodes()
    if nodes:
        summary["numa_nodes"] = len(nodes)
    if IS_LINUX:
        sockets = _linux_socket_count()
        if sockets:
            summary["sockets"] = sockets
    cpus = processor_topology()
    if cpus:
        # ocaml-processor knows sockets directly; prefer it where present.
        summary["sockets"] = len({c["socket"] for c in cpus})
        kinds: Dict[str, int] = {}
        for c in cpus:
            kinds[c["kind"]] = kinds.get(c["kind"], 0) + 1
        summary["cpu_kinds"] = kinds
        if "physical_cores" not in summary:
            summary["physical_cores"] = len({(c["socket"], c["core"])
                                             for c in cpus})
    return summary

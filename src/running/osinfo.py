"""Host-OS abstraction (Linux, macOS, FreeBSD). A missing capability must
degrade (None / empty), never raise: these run on the measurement path.
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
# Called in a 10 ms poll loop while the benchmark starts, so implementations
# must not fork where the platform allows it.

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
    """macOS: proc_pidpath(3); same-uid targets only."""
    if _libproc is None:
        raise OSError("libproc unavailable")
    buf = ctypes.create_string_buffer(_PROC_PIDPATHINFO_MAXSIZE)
    n = _libproc.proc_pidpath(ctypes.c_int(pid), buf,
                              ctypes.c_uint32(_PROC_PIDPATHINFO_MAXSIZE))
    if n <= 0:
        raise OSError(ctypes.get_errno(), "proc_pidpath failed for {}".format(pid))
    return os.path.basename(buf.value.decode("utf-8", "replace"))


# FreeBSD sysctl mib for KERN_PROC_PATHNAME (stable ABI).
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
    """FreeBSD: sysctl(KERN_PROC_PATHNAME)."""
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
        # procfs is not mounted by default on modern FreeBSD
        if _libc is not None:
            return _exe_name_sysctl
        if os.path.isdir("/proc/self"):
            return _exe_name_procfs_file
    return None


_exe_name_impl = _pick_exe_name_impl()

#: False when this platform cannot resolve a PID's executable; callers fall back to a weaker check.
EXE_LOOKUP_SUPPORTED = _exe_name_impl is not None

if not EXE_LOOKUP_SUPPORTED:
    logging.debug(
        "No PID->executable lookup on %s; consumers will degrade to weaker checks",
        SYSTEM)


def pid_exe_name(pid: int) -> Optional[str]:
    """Basename of the executable PID is running, or None (platform cannot tell,
    see :data:`EXE_LOOKUP_SUPPORTED`, or the lookup failed for this PID)."""
    if _exe_name_impl is None:
        return None
    try:
        return _exe_name_impl(pid)
    except (OSError, UnicodeError, ValueError):
        return None


# --- host description probes (for the log prologue) ---------------------------

def probe(cmd: str) -> str:
    """Run a shell probe; "" if the command is missing or fails. Never raises."""
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
                    # x86 says "model name"; arm64 may only have "Model"
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
    """Logical core count."""
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
        # macOS top has no batch flag
        return "top -l 1 -n 12 | head -n 20"
    if IS_FREEBSD:
        return "top -b -n 12"
    return ""


# --- CPU topology and pinning --------------------------------------------------
#
# The CPU list is detected at run time: Linux and FreeBSD enumerate SMT
# siblings differently on the same silicon.


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
    # Offline CPUs have no readable topology and would become phantom cores.
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
            # no topology info: treat the CPU as its own core
            raw = str(cpu)
        siblings = sorted(_parse_cpu_list(raw)) or [cpu]
        if online:
            siblings = [c for c in siblings if c in online] or [cpu]
        seen.update(siblings)
        groups.append(siblings)
    return groups


def _freebsd_sibling_groups() -> List[List[int]]:
    """SMT sibling sets from kern.sched.topology_spec: the XML groups flagged
    THREAD/SMT are one physical core each; unflagged groups are caches or nodes."""
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
        # find(), not iter(): iter() recurses into children's flags.
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
        # no SMT
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


#: sysfs root for CPU topology; tests point it at a fixture tree.
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
    """Online logical CPUs; empty where unavailable (callers assume every CPU)."""
    return _sysfs_cpu_list("online")


def isolated_cpus() -> List[int]:
    """CPUs isolated with `isolcpus=` (Linux only; empty elsewhere).

    The scheduler does not balance among isolated CPUs: a mask spanning
    several puts every thread on one. They suit single-threaded benchmarks
    or ones that pin their own threads.
    """
    return _sysfs_cpu_list("isolated")


def _parse_cpu_mask(text: str) -> List[int]:
    """Parse a procfs hex CPU mask: "0005", or "00000000,0000000f" past 32."""
    bits = text.strip().replace(",", "")
    if not bits:
        return []
    try:
        value = int(bits, 16)
    except ValueError:
        return []
    return [i for i in range(value.bit_length()) if (value >> i) & 1]


def irq_cpus() -> List[int]:
    """CPUs an `irqaffinity=` boot line routes interrupts to
    (/proc/irq/default_smp_affinity), or [] when untuned or not Linux."""
    if SYSTEM != "Linux":
        return []
    try:
        with open("/proc/irq/default_smp_affinity") as f:
            cpus = _parse_cpu_mask(f.read())
    except OSError:
        return []
    online = set(online_cpus())
    if online:
        cpus = [c for c in cpus if c in online]
        # a mask covering everything is the default, not a decision
        if len(cpus) >= len(online):
            return []
    return sorted(cpus)


def sibling_groups() -> List[List[int]]:
    """One sorted list of logical CPUs per physical core; empty where the platform
    exposes no topology (macOS), meaning "cannot pin here"."""
    if IS_LINUX:
        return _linux_sibling_groups()
    if IS_FREEBSD:
        return _freebsd_sibling_groups()
    return []


def split_groups_by_node(groups: List[List[int]]
                         ) -> Tuple[List[List[int]], List[List[int]]]:
    """Split sibling groups into (largest NUMA node's, everything else's);
    (groups, []) on a single-node machine. Ties go to the lowest node id so
    the choice is stable. A group straddling nodes stays with the benchmark."""
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
    """Split the machine into (benchmark CPUs, observer CPUs); ([], []) without topology.

    The benchmark gets one hardware thread per physical core. `reserved_cores`
    whole cores go to the observers instead (default 0: observers land on the
    benchmark's SMT siblings). `one_node` keeps the benchmark on one NUMA node
    and gives the other nodes to the observers, leaving the benchmark's
    siblings idle; a no-op on single-node machines. An `isolcpus=` split, or
    failing that an `irqaffinity=` one, takes precedence over topology, with
    the two options then applying within the isolated set.

    `cpuset -l` sets CPU affinity but not the NUMA memory domain (`cpuset -n`).
    """
    groups = refine_groups(sibling_groups())
    if not groups:
        return [], []
    if reserved_cores < 0:
        raise ValueError("reserved_cores must be >= 0")
    # isolcpus= is the administrator's own split; it wins over topology.
    # How many isolated CPUs a benchmark may span is the caller's decision.
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
    if not housekeeping:
        # irqaffinity= is the same statement in a weaker form.
        irq = set(irq_cpus())
        if irq:
            irq_groups = [g for g in groups if any(c in irq for c in g)]
            free_groups = [g for g in groups if not any(c in irq for c in g)]
            if irq_groups and free_groups:
                groups, housekeeping = free_groups, irq_groups
    off_node: List[List[int]] = []
    if one_node:
        groups, off_node = split_groups_by_node(groups)
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
    # nothing better; otherwise they stay idle to avoid contention.
    if not off_node and not housekeeping:
        observers += [c for g in bench_groups for c in g[1:]]
    observers += [c for g in observer_groups for c in g]
    observers += [c for g in off_node for c in g]
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
    """CPUs per NUMA node, or [] where the platform does not say."""
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
    """Command prefix confining a process to `cpus`, or [] if we cannot
    (macOS has no binding CPU affinity API, only hints)."""
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
# https://github.com/haesbaert/ocaml-processor's `ocaml-processor-dump` knows
# P/E core kind and socket, which the kernel does not surface uniformly. It is
# only ever used to narrow the kernel's CPU set, never replace it: on
# unsupported hardware it silently reports a fake flat topology, which then
# filters nothing.

PROCESSOR_DUMP = "ocaml-processor-dump"

#: Set to "0"/"no" to ignore ocaml-processor-dump.
PROCESSOR_REFINE_ENV_VAR = "RUNNING_NG_USE_OCAML_PROCESSOR"

_CPU_LINE = re.compile(
    r"^cpu(?P<id>\d+):\s+smt=(?P<smt>\d+)\s+core=(?P<core>\d+)\s+"
    r"socket=(?P<socket>\d+)\s+kind=(?P<kind>\S+)")


def parse_processor_dump(text: str) -> List[Dict[str, Any]]:
    """Parse `ocaml-processor-dump` into one dict per logical CPU, ignoring unrecognised lines."""
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
    """Narrow sibling groups to one socket's performance cores; never returns
    empty, and returns `groups` unchanged when there is nothing to narrow."""
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
            # undescribed CPU: keep rather than guess
            return True
        if has_ecores and all(_is_efficiency(i["kind"]) for i in info):  # type: ignore[index]
            return False
        if socket is not None and any(i["socket"] != socket for i in info):  # type: ignore[index]
            return False
        return True

    chosen_socket = None
    if len(sockets) > 1:
        # most performance cores; ties to the lowest id for stability
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


def isolation_tier() -> str:
    """How well this machine keeps other work off the benchmark's cores:
    "isolcpus", "irqaffinity", "topology" (SMT split only) or "none" (cannot pin).
    Recorded in the manifest; results at different tiers are not comparable.
    """
    if not sibling_groups():
        return "none"
    if isolated_cpus():
        return "isolcpus"
    if irq_cpus():
        return "irqaffinity"
    return "topology"


_warned_untuned = False


def warn_if_untuned() -> None:
    """Warn once per process when the machine is not tuned for isolation."""
    global _warned_untuned
    if _warned_untuned:
        return
    _warned_untuned = True
    tier = isolation_tier()
    if tier in ("isolcpus", "irqaffinity"):
        logging.info("CPU isolation: %s", tier)
        return
    if tier == "none":
        logging.warning(
            "This platform exposes no CPU topology, so nothing can be pinned: "
            "the benchmark, the OS and the observers all share every core. "
            "Expect run-to-run variance and do not compare these numbers with "
            "results from a pinned machine.")
        return
    logging.warning(
        "This machine is not tuned for benchmarking: neither `isolcpus=` nor "
        "`irqaffinity=` is set, so the kernel is free to schedule other work, "
        "and interrupts, onto the benchmark's cores. Pinning still separates "
        "the benchmark from the observers, but only onto SMT siblings of the "
        "same physical cores. Measured cost of leaving a machine untuned: a "
        "25.8s spread over six runs against 0.33s when tuned. Add "
        "`isolcpus=<cpus> irqaffinity=<others>` to the kernel cmdline (needs "
        "root and a reboot); results taken now are not comparable with results "
        "taken after.")


def machine_topology_summary() -> Dict[str, Any]:
    """Topology facts recorded alongside a result; omits what cannot be determined."""
    summary: Dict[str, Any] = {"cpu_isolation": isolation_tier()}
    groups = sibling_groups()
    if groups:
        summary["physical_cores"] = len(groups)
        widths = {len(g) for g in groups}
        if len(widths) == 1:
            summary["threads_per_core"] = widths.pop()
    nodes = numa_nodes()
    if nodes:
        summary["numa_nodes"] = len(nodes)
    if IS_LINUX:
        sockets = _linux_socket_count()
        if sockets:
            summary["sockets"] = sockets
    cpus = processor_topology()
    if cpus:
        summary["sockets"] = len({c["socket"] for c in cpus})
        kinds: Dict[str, int] = {}
        for c in cpus:
            kinds[c["kind"]] = kinds.get(c["kind"], 0) + 1
        summary["cpu_kinds"] = kinds
        if "physical_cores" not in summary:
            summary["physical_cores"] = len({(c["socket"], c["core"])
                                             for c in cpus})
    return summary

"""CPU topology detection and pinning.

The FreeBSD fixtures reproduce kern.sched.topology_spec as emitted by
sys/kern/sched_ule.c:3211-3250: nested <group> elements each carrying a <cpu>
list, with SMT sibling groups marked by a THREAD (and usually SMT) flag.
Nobody has run this on FreeBSD yet, so the fixtures are the specification.
"""
import shutil

import pytest

#: `true` lives in /bin on Linux and /usr/bin on FreeBSD and macOS, so the
#: path is looked up rather than written down.
TRUE_BIN = shutil.which("true")

from running import osinfo


# 4 physical cores, SMT on, FreeBSD's interleaved enumeration: (0,1) (2,3) ...
FREEBSD_SMT = """<groups>
 <group level="1" cache-level="3">
  <cpu count="8" mask="ff,0,0,0">0, 1, 2, 3, 4, 5, 6, 7</cpu>
  <flags><flag name="HTT">HTT group</flag></flags>
  <children>
   <group level="2" cache-level="2">
    <cpu count="2" mask="3,0,0,0">0, 1</cpu>
    <flags><flag name="THREAD">THREAD group</flag><flag name="SMT">SMT group</flag></flags>
   </group>
   <group level="2" cache-level="2">
    <cpu count="2" mask="c,0,0,0">2, 3</cpu>
    <flags><flag name="THREAD">THREAD group</flag><flag name="SMT">SMT group</flag></flags>
   </group>
   <group level="2" cache-level="2">
    <cpu count="2" mask="30,0,0,0">4, 5</cpu>
    <flags><flag name="THREAD">THREAD group</flag><flag name="SMT">SMT group</flag></flags>
   </group>
   <group level="2" cache-level="2">
    <cpu count="2" mask="c0,0,0,0">6, 7</cpu>
    <flags><flag name="THREAD">THREAD group</flag><flag name="SMT">SMT group</flag></flags>
   </group>
  </children>
 </group>
</groups>
"""

# SMT off: no THREAD-flagged group anywhere.
FREEBSD_NO_SMT = """<groups>
 <group level="1" cache-level="3">
  <cpu count="4" mask="f,0,0,0">0, 1, 2, 3</cpu>
 </group>
</groups>
"""


@pytest.fixture(autouse=True)
def _no_kernel_cpu_lists(request, monkeypatch):
    """Describe a machine where the kernel has isolated nothing.

    Both of these read the *running* machine's sysfs, so without stubbing them
    the host's own `isolcpus=` line would reach into every test that drives
    partition_cpus with a fake topology.  The tests that are about isolation
    override this fixture.  Empty online list means "no information", which is
    how sibling_groups reads it.
    """
    if request.node.get_closest_marker("host") or \
            request.node.get_closest_marker("cpu_lists"):
        return
    monkeypatch.setattr(osinfo, "isolated_cpus", lambda: [])
    monkeypatch.setattr(osinfo, "online_cpus", lambda: [])


@pytest.fixture
def as_freebsd(monkeypatch):
    monkeypatch.setattr(osinfo, "IS_LINUX", False)
    monkeypatch.setattr(osinfo, "IS_FREEBSD", True)
    monkeypatch.setattr(osinfo, "IS_DARWIN", False)


# --- cpu list parsing ----------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("0,16", [0, 16]),                       # Linux thread_siblings_list
    ("0-3", [0, 1, 2, 3]),                   # Linux range form
    ("0, 1, 2, 3", [0, 1, 2, 3]),            # FreeBSD comma-space form
    ("0-2,8", [0, 1, 2, 8]),                 # mixed
    ("", []),
    ("garbage", []),
])
def test_parse_cpu_list(text, expected):
    assert osinfo._parse_cpu_list(text) == expected


# --- FreeBSD topology ----------------------------------------------------------

def test_freebsd_smt_groups(as_freebsd, monkeypatch):
    monkeypatch.setattr(osinfo, "probe", lambda cmd: FREEBSD_SMT)
    assert osinfo.sibling_groups() == [[0, 1], [2, 3], [4, 5], [6, 7]]


def test_freebsd_interleaved_enumeration_differs_from_linux(as_freebsd, monkeypatch):
    """The reason the CPU list cannot be a per-OS constant.

    Same policy ("one thread per physical core"), same hardware, different
    answer: FreeBSD interleaves siblings so it is 0,2,4,6 where Linux on a
    (0,4)(1,5)... enumeration would be 0-3.
    """
    monkeypatch.setattr(osinfo, "probe", lambda cmd: FREEBSD_SMT)
    bench, observers = osinfo.partition_cpus()
    assert osinfo.format_cpu_list(bench) == "0,2,4,6"
    assert osinfo.format_cpu_list(observers) == "1,3,5,7"


def test_freebsd_without_smt_treats_each_cpu_as_a_core(as_freebsd, monkeypatch):
    monkeypatch.setattr(osinfo, "probe", lambda cmd: FREEBSD_NO_SMT)
    monkeypatch.setattr(osinfo, "core_count", lambda: 4)
    assert osinfo.sibling_groups() == [[0], [1], [2], [3]]
    bench, observers = osinfo.partition_cpus()
    assert bench == [0, 1, 2, 3]
    assert observers == []


def test_freebsd_unparseable_topology_degrades(as_freebsd, monkeypatch):
    monkeypatch.setattr(osinfo, "probe", lambda cmd: "<groups")
    assert osinfo.sibling_groups() == []
    assert osinfo.partition_cpus() == ([], [])


def test_freebsd_missing_sysctl_degrades(as_freebsd, monkeypatch):
    monkeypatch.setattr(osinfo, "probe", lambda cmd: "")
    assert osinfo.sibling_groups() == []


# --- partitioning policy -------------------------------------------------------

def _fake_groups(monkeypatch, groups):
    monkeypatch.setattr(osinfo, "sibling_groups", lambda: groups)


def test_default_partition_matches_current_pin_lavyek_policy(monkeypatch):
    """Reproduces the hand-written `taskset -c 0-15` on the calibration box.

    Linux enumerates that machine as (0,16),(1,17)...(15,31), so one thread per
    physical core is 0-15. Changing this default would silently change what
    every existing lavyek number means.
    """
    _fake_groups(monkeypatch, [[i, i + 16] for i in range(16)])
    bench, observers = osinfo.partition_cpus()
    assert osinfo.format_cpu_list(bench) == "0-15"
    assert osinfo.format_cpu_list(observers) == "16-31"


def test_reserving_cores_gives_observers_whole_cores(monkeypatch):
    _fake_groups(monkeypatch, [[i, i + 16] for i in range(16)])
    bench, observers = osinfo.partition_cpus(reserved_cores=2)
    # Cores 14 and 15 go entirely to observers, both their threads.
    assert osinfo.format_cpu_list(bench) == "0-13"
    assert {14, 15, 30, 31} <= set(observers)
    assert not (set(bench) & set(observers))


def test_reserving_never_starves_the_benchmark(monkeypatch, caplog):
    _fake_groups(monkeypatch, [[0, 2], [1, 3]])
    bench, observers = osinfo.partition_cpus(reserved_cores=99)
    assert bench, "benchmark must keep at least one core"
    assert "reserving" in caplog.text


def test_negative_reserve_is_an_error(monkeypatch):
    _fake_groups(monkeypatch, [[0], [1]])
    with pytest.raises(ValueError):
        osinfo.partition_cpus(reserved_cores=-1)


def test_partition_sets_are_disjoint_and_complete(monkeypatch):
    groups = [[i, i + 8] for i in range(8)]
    _fake_groups(monkeypatch, groups)
    for reserve in (0, 1, 3):
        bench, observers = osinfo.partition_cpus(reserve)
        assert not (set(bench) & set(observers))
        assert set(bench) | set(observers) == {c for g in groups for c in g}


# --- rendering and command shape -----------------------------------------------

@pytest.mark.parametrize("cpus,expected", [
    ([0, 1, 2, 3], "0-3"),
    ([0, 2, 4, 6], "0,2,4,6"),
    ([0, 1, 2, 5, 7, 8, 9], "0-2,5,7-9"),
    ([3], "3"),
    ([], ""),
])
def test_format_cpu_list(cpus, expected):
    assert osinfo.format_cpu_list(cpus) == expected


def test_pin_command_is_per_os(monkeypatch):
    monkeypatch.setattr(osinfo, "IS_LINUX", True)
    monkeypatch.setattr(osinfo, "IS_FREEBSD", False)
    assert osinfo.pin_command([0, 1, 2, 3]) == ["taskset", "-c", "0-3"]

    monkeypatch.setattr(osinfo, "IS_LINUX", False)
    monkeypatch.setattr(osinfo, "IS_FREEBSD", True)
    assert osinfo.pin_command([0, 2]) == ["cpuset", "-l", "0,2"]


def test_pin_command_empty_on_macos(monkeypatch):
    # macOS has no API that binds a process to a core, only thread affinity
    # hints the scheduler may ignore. Emitting nothing is the honest answer.
    monkeypatch.setattr(osinfo, "IS_LINUX", False)
    monkeypatch.setattr(osinfo, "IS_FREEBSD", False)
    monkeypatch.setattr(osinfo, "IS_DARWIN", True)
    assert osinfo.pin_command([0, 1]) == []


def test_pin_command_empty_for_empty_cpu_set():
    assert osinfo.pin_command([]) == []


# --- this host -----------------------------------------------------------------

@pytest.mark.host
@pytest.mark.skipif(not osinfo.IS_LINUX, reason="sysfs topology is Linux-only")
def test_linux_topology_is_self_consistent():
    groups = osinfo.sibling_groups()
    assert groups
    flat = [c for g in groups for c in g]
    assert len(flat) == len(set(flat)), "a CPU appears in two sibling groups"
    # Offline CPUs are left out, so this is the online count -- which is also
    # what os.cpu_count() reports.  A box with SMT disabled at boot has half
    # its CPUs offline and would fail an assertion against `present`.
    online = osinfo.online_cpus()
    assert len(flat) == (len(online) if online else osinfo.core_count())


# --- CpuPin modifier -----------------------------------------------------------

from running.benchmark import BinaryBenchmark  # noqa: E402
from running.modifier import CpuPin, PerfAndOllyAttach  # noqa: E402
from pathlib import Path  # noqa: E402


def _pin(monkeypatch, groups, val=None, linux=True):
    monkeypatch.setattr(osinfo, "sibling_groups", lambda: groups)
    monkeypatch.setattr(osinfo, "IS_LINUX", linux)
    monkeypatch.setattr(osinfo, "IS_FREEBSD", not linux)
    kwargs = {"name": "pin_bench", "type": "CpuPin"}
    if val is not None:
        kwargs["val"] = val
    return CpuPin(**kwargs)


def test_cpupin_reproduces_the_historical_lavyek_mask(monkeypatch):
    m = _pin(monkeypatch, [[i, i + 16] for i in range(16)])
    assert m.val == ["taskset", "-c", "0-15"]


def test_cpupin_same_policy_differs_by_os(monkeypatch):
    # Identical hardware, identical policy, different CPU numbers: exactly why
    # this cannot be a per-OS constant in a config file.
    linux = _pin(monkeypatch, [[i, i + 4] for i in range(4)], linux=True)
    freebsd = _pin(monkeypatch, [[2 * i, 2 * i + 1] for i in range(4)], linux=False)
    assert linux.val == ["taskset", "-c", "0-3"]
    assert freebsd.val == ["cpuset", "-l", "0,2,4,6"]


def test_cpupin_reserved_cores_shrinks_benchmark_set(monkeypatch):
    m = _pin(monkeypatch, [[i, i + 16] for i in range(16)], val="2")
    assert m.val == ["taskset", "-c", "0-13"]
    assert osinfo.format_cpu_list(m.observer_cpus) == "14-31"


def test_cpupin_rejects_non_numeric_val(monkeypatch):
    with pytest.raises(ValueError, match="whole number"):
        _pin(monkeypatch, [[0, 1]], val="lots")


def test_cpupin_is_inert_where_the_os_cannot_pin(monkeypatch, caplog):
    monkeypatch.setattr(osinfo, "sibling_groups", lambda: [])
    monkeypatch.setattr(osinfo, "IS_LINUX", False)
    monkeypatch.setattr(osinfo, "IS_FREEBSD", False)
    monkeypatch.setattr(osinfo, "IS_DARWIN", True)
    m = CpuPin(name="pin_bench", type="CpuPin")
    # A config carrying CpuPin must still run on macOS, just unpinned.
    assert m.val == []
    assert "cannot pin" in caplog.text


def test_cpupin_prepends_to_the_benchmark_command(monkeypatch):
    m = _pin(monkeypatch, [[i, i + 16] for i in range(16)])
    bm = BinaryBenchmark(Path(TRUE_BIN), [], suite_name="s", name="b")
    bm = bm.attach_modifiers([m])
    assert [str(x) for x in bm.get_full_args(None)][:3] == ["taskset", "-c", "0-15"]
    assert bm.cpu_pin is m


def test_cpupin_excludes_still_apply(monkeypatch):
    m = _pin(monkeypatch, [[0, 1]])
    m.excludes = {"s": ["b"]}
    bm = BinaryBenchmark(Path(TRUE_BIN), [], suite_name="s", name="b")
    bm = bm.attach_modifiers([m])
    # Excluded benchmarks must not be pinned, and must not record the modifier.
    assert bm.wrapper == []
    assert bm.cpu_pin is None


def test_no_cpupin_means_no_observer_pinning():
    bm = BinaryBenchmark(Path(TRUE_BIN), [], suite_name="s", name="b")
    bm = bm.attach_modifiers([PerfAndOllyAttach(
        name="perf_grp1", type="PerfAndOllyAttach", val="cycles")])
    assert bm.cpu_pin is None


# --- ocaml-processor refinement ------------------------------------------------
#
# https://github.com/haesbaert/ocaml-processor. The Ryzen fixture is real output
# captured from the calibration box; the hybrid and dual-socket ones are
# constructed, since no such machine was available.

REAL_RYZEN_DUMP = (Path(__file__).parent / "fixtures"
                   / "processor_dump_ryzen9950x.txt").read_text()


def _dump(cpus):
    """Render CPU dicts the way ocaml-processor-dump does."""
    head = "cpu_count: {}\ncore_count: {}\nsocket_count: {}\n".format(
        len(cpus), len({(c["socket"], c["core"]) for c in cpus}),
        len({c["socket"] for c in cpus}))
    return head + "".join(
        "cpu{}: smt={} core={} socket={} kind={}\n".format(
            c["id"], c["smt"], c["core"], c["socket"], c["kind"])
        for c in cpus)


# 4 P-cores with SMT (cpu0-7, interleaved) + 4 single-thread E-cores (cpu8-11).
HYBRID_CPUS = (
    [{"id": 2 * c + t, "smt": t, "core": c, "socket": 0, "kind": "P_core"}
     for c in range(4) for t in (0, 1)]
    + [{"id": 8 + c, "smt": 0, "core": 4 + c, "socket": 0, "kind": "E_core"}
       for c in range(4)]
)
HYBRID_GROUPS = [[0, 1], [2, 3], [4, 5], [6, 7], [8], [9], [10], [11]]

# 2 sockets, 2 cores each, SMT on: socket 0 owns cpu 0-3, socket 1 owns 4-7.
TWO_SOCKET_CPUS = [
    {"id": s * 4 + c * 2 + t, "smt": t, "core": c, "socket": s, "kind": "P_core"}
    for s in (0, 1) for c in range(2) for t in (0, 1)
]
TWO_SOCKET_GROUPS = [[0, 1], [2, 3], [4, 5], [6, 7]]


def test_parses_real_dump_from_the_calibration_box():
    cpus = osinfo.parse_processor_dump(REAL_RYZEN_DUMP)
    assert len(cpus) == 32
    assert cpus[0] == {"id": 0, "smt": 0, "core": 0, "socket": 0, "kind": "P_core"}
    assert {c["kind"] for c in cpus} == {"P_core"}
    assert len({(c["socket"], c["core"]) for c in cpus}) == 16


def test_parse_ignores_summary_lines_and_unknown_text():
    text = "cpu_count: 2\nsomething new upstream\ncpu0: smt=0 core=0 socket=0 kind=P_core\n"
    assert osinfo.parse_processor_dump(text) == [
        {"id": 0, "smt": 0, "core": 0, "socket": 0, "kind": "P_core"}]


def test_parse_empty_output():
    assert osinfo.parse_processor_dump("") == []


def test_refinement_is_inert_on_a_uniform_machine():
    # One socket, no E-cores: nothing to add, so the kernel's view stands.
    cpus = osinfo.parse_processor_dump(REAL_RYZEN_DUMP)
    groups = [[i, i + 16] for i in range(16)]
    assert osinfo.refine_groups(groups, cpus) == groups


def test_refinement_drops_efficiency_cores():
    cpus = osinfo.parse_processor_dump(_dump(HYBRID_CPUS))
    refined = osinfo.refine_groups(HYBRID_GROUPS, cpus)
    assert refined == [[0, 1], [2, 3], [4, 5], [6, 7]]
    # Pinning a benchmark across a P/E boundary measures two different CPUs.
    assert all(c < 8 for g in refined for c in g)


def test_refinement_keeps_to_one_socket():
    cpus = osinfo.parse_processor_dump(_dump(TWO_SOCKET_CPUS))
    assert osinfo.refine_groups(TWO_SOCKET_GROUPS, cpus) == [[0, 1], [2, 3]]


def test_refinement_prefers_the_socket_with_more_performance_cores():
    cpus = [dict(c) for c in TWO_SOCKET_CPUS]
    for c in cpus:
        if c["socket"] == 0:
            c["kind"] = "E_core"
    refined = osinfo.refine_groups(
        TWO_SOCKET_GROUPS, osinfo.parse_processor_dump(_dump(cpus)))
    assert refined == [[4, 5], [6, 7]]


def test_refinement_never_empties_the_cpu_set(caplog):
    cpus = [{"id": i, "smt": 0, "core": i, "socket": 0, "kind": "E_core"}
            for i in range(4)]
    groups = [[0], [1], [2], [3]]
    # All E-cores is not a reason to refuse to pin at all.
    assert osinfo.refine_groups(
        groups, osinfo.parse_processor_dump(_dump(cpus))) == groups


def test_refinement_keeps_cpus_the_tool_did_not_describe():
    # Partial data must not silently drop CPUs the kernel does know about.
    cpus = osinfo.parse_processor_dump(_dump(HYBRID_CPUS))
    groups = HYBRID_GROUPS + [[99]]
    assert [99] in osinfo.refine_groups(groups, cpus)


def test_refinement_noop_without_the_tool(monkeypatch):
    monkeypatch.setattr(osinfo, "processor_topology", lambda: [])
    groups = [[0, 1], [2, 3]]
    assert osinfo.refine_groups(groups) == groups


def test_refinement_can_be_disabled(monkeypatch):
    monkeypatch.setenv(osinfo.PROCESSOR_REFINE_ENV_VAR, "0")
    assert osinfo.processor_topology() == []


def test_faked_topology_cannot_corrupt_the_kernel_view():
    """ocaml-processor fakes topology off AMD64/Apple: every CPU its own core.

    Because refinement only ever narrows, and a faked topology reports one
    socket and no E-cores, it filters nothing and the kernel's real sibling
    groups survive intact. That is the whole reason it narrows rather than
    replaces.
    """
    fake = [{"id": i, "smt": 0, "core": i, "socket": 0, "kind": "P_core"}
            for i in range(32)]
    real_groups = [[i, i + 16] for i in range(16)]
    cpus = osinfo.parse_processor_dump(_dump(fake))
    assert osinfo.refine_groups(real_groups, cpus) == real_groups


# --- manifest summary ----------------------------------------------------------

def test_summary_reports_kinds_and_sockets(monkeypatch):
    monkeypatch.setattr(osinfo, "sibling_groups", lambda: HYBRID_GROUPS)
    monkeypatch.setattr(osinfo, "processor_topology",
                        lambda: osinfo.parse_processor_dump(_dump(HYBRID_CPUS)))
    s = osinfo.machine_topology_summary()
    assert s["sockets"] == 1
    assert s["cpu_kinds"] == {"P_core": 8, "E_core": 4}
    assert s["physical_cores"] == 8
    # Mixed group widths (SMT P-cores, single-thread E-cores): no single answer.
    assert "threads_per_core" not in s


def test_summary_degrades_without_the_tool(monkeypatch):
    # Without ocaml-processor the kernel-derived fields remain; only cpu_kinds,
    # which nothing else can supply, goes missing.
    monkeypatch.setattr(osinfo, "sibling_groups", lambda: [[i, i + 16] for i in range(16)])
    monkeypatch.setattr(osinfo, "processor_topology", lambda: [])
    monkeypatch.setattr(osinfo, "numa_nodes", lambda: [list(range(32))])
    monkeypatch.setattr(osinfo, "IS_LINUX", True)
    monkeypatch.setattr(osinfo, "_linux_socket_count", lambda: 1)
    s = osinfo.machine_topology_summary()
    assert s == {"physical_cores": 16, "threads_per_core": 2,
                 "numa_nodes": 1, "sockets": 1}
    assert "cpu_kinds" not in s


def test_summary_is_empty_where_nothing_is_knowable(monkeypatch):
    monkeypatch.setattr(osinfo, "sibling_groups", lambda: [])
    monkeypatch.setattr(osinfo, "processor_topology", lambda: [])
    monkeypatch.setattr(osinfo, "numa_nodes", lambda: [])
    monkeypatch.setattr(osinfo, "IS_LINUX", False)
    assert osinfo.machine_topology_summary() == {}


# --- NUMA and socket detection -------------------------------------------------
#
# Kernel-derived, so unlike the socket data from ocaml-processor these are
# available without any optional tool. The FreeBSD fixture models the
# 2-socket Xeon E5-2640 v4 the branch was validated on: 20 physical cores,
# 40 threads, node boundary at cpu 20.

FREEBSD_TWO_NODES = """<groups>
 <group level="1" cache-level="0">
  <cpu count="8" mask="ff">0, 1, 2, 3, 4, 5, 6, 7</cpu>
  <children>
   <group level="2" cache-level="3">
    <cpu count="4" mask="f">0, 1, 2, 3</cpu>
    <flags><flag name="NODE">NUMA node</flag></flags>
   </group>
   <group level="2" cache-level="3">
    <cpu count="4" mask="f0">4, 5, 6, 7</cpu>
    <flags><flag name="NODE">NUMA node</flag></flags>
   </group>
  </children>
 </group>
</groups>
"""


def test_freebsd_numa_nodes(as_freebsd, monkeypatch):
    monkeypatch.setattr(osinfo, "probe", lambda cmd: FREEBSD_TWO_NODES)
    assert osinfo.numa_nodes() == [[0, 1, 2, 3], [4, 5, 6, 7]]


def test_freebsd_numa_absent_when_not_flagged(as_freebsd, monkeypatch):
    # SMT groups are THREAD-flagged, not NODE-flagged: they must not be
    # mistaken for NUMA nodes.
    monkeypatch.setattr(osinfo, "probe", lambda cmd: FREEBSD_SMT)
    assert osinfo.numa_nodes() == []


def test_numa_nodes_empty_where_unknown(monkeypatch):
    monkeypatch.setattr(osinfo, "IS_LINUX", False)
    monkeypatch.setattr(osinfo, "IS_FREEBSD", False)
    assert osinfo.numa_nodes() == []


def test_summary_carries_numa_without_ocaml_processor(monkeypatch):
    monkeypatch.setattr(osinfo, "sibling_groups", lambda: [[i, i + 20] for i in range(20)])
    monkeypatch.setattr(osinfo, "numa_nodes",
                        lambda: [list(range(20)), list(range(20, 40))])
    monkeypatch.setattr(osinfo, "processor_topology", lambda: [])
    monkeypatch.setattr(osinfo, "IS_LINUX", True)
    monkeypatch.setattr(osinfo, "_linux_socket_count", lambda: 2)
    s = osinfo.machine_topology_summary()
    assert s["numa_nodes"] == 2
    assert s["sockets"] == 2
    assert s["physical_cores"] == 20


@pytest.mark.skipif(not osinfo.IS_LINUX, reason="sysfs NUMA is Linux-only")
def test_linux_numa_covers_every_cpu():
    nodes = osinfo.numa_nodes()
    if not nodes:
        pytest.skip("no NUMA info exposed")
    flat = [c for n in nodes for c in n]
    assert len(flat) == len(set(flat))
    assert len(flat) == osinfo.core_count()


# --- one-node pinning ----------------------------------------------------------
#
# Modelled on rosemary, the 2-socket Xeon E5-2640 v4 the FreeBSD work runs on:
# 2 sockets x 10 cores x 2 threads, siblings adjacent ([0,1],[2,3],...), NUMA
# boundary at cpu 20.

ROSEMARY_GROUPS = [[2 * i, 2 * i + 1] for i in range(20)]
ROSEMARY_NODES = [list(range(20)), list(range(20, 40))]


@pytest.fixture
def rosemary(monkeypatch):
    monkeypatch.setattr(osinfo, "sibling_groups", lambda: ROSEMARY_GROUPS)
    monkeypatch.setattr(osinfo, "refine_groups", lambda g, cpus=None: g)
    monkeypatch.setattr(osinfo, "numa_nodes", lambda: ROSEMARY_NODES)
    monkeypatch.setattr(osinfo, "IS_LINUX", False)
    monkeypatch.setattr(osinfo, "IS_FREEBSD", True)


def test_default_still_spans_both_nodes(rosemary):
    # The observed behaviour before one_node existed, kept as the default so
    # nothing changes under anyone mid-sweep.
    bench, _ = osinfo.partition_cpus()
    assert osinfo.format_cpu_list(bench) == \
        "0,2,4,6,8,10,12,14,16,18,20,22,24,26,28,30,32,34,36,38"
    assert len(bench) == 20


def test_one_node_confines_the_benchmark_to_a_single_node(rosemary):
    bench, observers = osinfo.partition_cpus(one_node=True)
    assert osinfo.format_cpu_list(bench) == "0,2,4,6,8,10,12,14,16,18"
    assert len(bench) == 10
    # Every benchmark cpu is on node 0.
    assert all(c in ROSEMARY_NODES[0] for c in bench)
    # The whole of node 1 goes to the observers rather than sitting idle.
    assert set(ROSEMARY_NODES[1]) <= set(observers)
    assert not (set(bench) & set(observers))


def test_one_node_deliberately_leaves_cpus_unassigned(rosemary):
    # Not a partition of every CPU any more: the benchmark's SMT siblings are
    # left idle on purpose, because a whole spare node is available for the
    # observers and a busy sibling contends with the benchmark thread.
    bench, observers = osinfo.partition_cpus(one_node=True)
    assert not (set(bench) & set(observers))
    assert set(bench) | set(observers) < set(range(40))


def test_one_node_composes_with_reserved_cores(rosemary):
    # reserved_cores applies within the chosen node, which is what you want
    # when there is only one node left to give.
    bench, observers = osinfo.partition_cpus(reserved_cores=2, one_node=True)
    assert osinfo.format_cpu_list(bench) == "0,2,4,6,8,10,12,14"
    assert len(bench) == 8
    assert set(ROSEMARY_NODES[1]) <= set(observers)


def test_one_node_is_a_noop_on_a_single_node_machine(monkeypatch):
    groups = [[i, i + 16] for i in range(16)]
    monkeypatch.setattr(osinfo, "sibling_groups", lambda: groups)
    monkeypatch.setattr(osinfo, "refine_groups", lambda g, cpus=None: g)
    monkeypatch.setattr(osinfo, "numa_nodes", lambda: [list(range(32))])
    monkeypatch.setattr(osinfo, "IS_LINUX", True)
    # Safe to leave on in a shared config precisely because of this.
    assert osinfo.partition_cpus() == osinfo.partition_cpus(one_node=True)


def test_one_node_is_a_noop_without_numa_information(monkeypatch):
    groups = [[0, 1], [2, 3]]
    monkeypatch.setattr(osinfo, "sibling_groups", lambda: groups)
    monkeypatch.setattr(osinfo, "refine_groups", lambda g, cpus=None: g)
    monkeypatch.setattr(osinfo, "numa_nodes", lambda: [])
    assert osinfo.split_groups_by_node(groups) == (groups, [])


def test_node_choice_prefers_the_larger_node_then_the_lower_id(monkeypatch):
    # Ties go to the lowest id so the choice is stable across runs.
    monkeypatch.setattr(osinfo, "numa_nodes", lambda: [[0, 1], [2, 3]])
    kept, rest = osinfo.split_groups_by_node([[0, 1], [2, 3]])
    assert kept == [[0, 1]] and rest == [[2, 3]]

    monkeypatch.setattr(osinfo, "numa_nodes", lambda: [[0, 1], [2, 3, 4, 5]])
    kept, rest = osinfo.split_groups_by_node([[0, 1], [2, 3], [4, 5]])
    assert kept == [[2, 3], [4, 5]] and rest == [[0, 1]]


def test_group_straddling_nodes_stays_with_the_benchmark(monkeypatch):
    # Should not happen, but is not worth crashing over.
    monkeypatch.setattr(osinfo, "numa_nodes", lambda: [[0], [1]])
    kept, rest = osinfo.split_groups_by_node([[0, 1]])
    assert kept == [[0, 1]] and rest == []


# --- CpuPin one_node option ----------------------------------------------------

def test_cpupin_accepts_one_node(rosemary):
    m = CpuPin(name="pin_bench", type="CpuPin", one_node=True)
    assert m.val == ["cpuset", "-l", "0,2,4,6,8,10,12,14,16,18"]
    assert m.one_node is True


@pytest.mark.parametrize("raw,expected", [
    (True, True), (False, False), ("true", True), ("yes", True),
    ("1", True), ("false", False), ("no", False), ("0", False),
])
def test_cpupin_one_node_accepts_yaml_bool_and_string(rosemary, raw, expected):
    # YAML hands this over as a bool or a string depending on quoting.
    assert CpuPin(name="p", type="CpuPin", one_node=raw).one_node is expected


def test_cpupin_rejects_a_non_boolean_one_node(rosemary):
    with pytest.raises(ValueError, match="one_node must be a boolean"):
        CpuPin(name="p", type="CpuPin", one_node="sometimes")


def test_cpupin_defaults_to_spanning_nodes(rosemary):
    # Unchanged default: turning this on is an explicit config decision.
    assert CpuPin(name="p", type="CpuPin").one_node is False


# --- one_node leaves the benchmark's SMT siblings idle --------------------------

def test_one_node_leaves_the_benchmarks_siblings_idle(rosemary):
    """The claim in partition_cpus's docstring, now enforced.

    With a whole spare node for the observers, the benchmark's own SMT
    siblings buy nothing and would contend for the same physical cores. Before
    this, every one of the benchmark's 10 cores had its sibling in the
    observer set, so "no SMT contention at all" was false.
    """
    bench, observers = osinfo.partition_cpus(one_node=True)
    siblings = {c + 1 for c in bench}          # adjacent enumeration on this box
    assert not (siblings & set(observers)), "a benchmark sibling is an observer"
    idle = set(range(40)) - set(bench) - set(observers)
    assert idle == siblings
    assert osinfo.format_cpu_list(observers) == "20-39"


def test_default_still_gives_observers_the_siblings(rosemary):
    # Without one_node there is no spare node, so the siblings are the only
    # spare CPUs there are and leaving them idle would buy nothing.
    bench, observers = osinfo.partition_cpus()
    assert set(bench) | set(observers) == set(range(40)), "no CPU left idle"
    assert {c + 1 for c in bench} <= set(observers)


def test_one_node_with_reserved_cores_still_idles_the_siblings(rosemary):
    bench, observers = osinfo.partition_cpus(reserved_cores=2, one_node=True)
    assert not ({c + 1 for c in bench} & set(observers))
    # The two reserved cores go over whole, both threads.
    assert {16, 17, 18, 19} <= set(observers)


def test_single_node_machine_leaves_nothing_idle(monkeypatch):
    groups = [[i, i + 16] for i in range(16)]
    monkeypatch.setattr(osinfo, "sibling_groups", lambda: groups)
    monkeypatch.setattr(osinfo, "refine_groups", lambda g, cpus=None: g)
    monkeypatch.setattr(osinfo, "numa_nodes", lambda: [list(range(32))])
    monkeypatch.setattr(osinfo, "IS_LINUX", True)
    for kwargs in ({}, {"one_node": True}):
        bench, observers = osinfo.partition_cpus(**kwargs)
        assert set(bench) | set(observers) == set(range(32))


# --- isolated CPUs (Linux isolcpus=) --------------------------------------------

@pytest.fixture
def obelisk(monkeypatch):
    """An 8-core Xeon booted `nosmt isolcpus=4,6,8,10,12,14 irqaffinity=0,2`.

    SMT off takes the odd CPUs offline, so sibling_groups sees eight
    single-thread cores, six of which the administrator set aside.
    """
    monkeypatch.setattr(osinfo, "sibling_groups",
                        lambda: [[c] for c in (0, 2, 4, 6, 8, 10, 12, 14)])
    monkeypatch.setattr(osinfo, "isolated_cpus",
                        lambda: [4, 6, 8, 10, 12, 14])
    monkeypatch.setattr(osinfo, "IS_LINUX", True)
    monkeypatch.setattr(osinfo, "IS_FREEBSD", False)


def test_isolated_cores_go_to_the_benchmark(obelisk):
    bench, observers = osinfo.partition_cpus()
    assert bench == [4, 6, 8, 10, 12, 14]
    assert observers == [0, 2]


def test_isolation_split_survives_the_modifier(obelisk):
    m = CpuPin(name="pin_bench", type="CpuPin")
    assert m.val == ["taskset", "-c", "4,6,8,10,12,14"]
    assert m.observer_cpus == [0, 2]


def test_no_isolation_falls_back_to_the_topology_policy(monkeypatch):
    # Same machine, no isolcpus: every core is the benchmark's, as before.
    monkeypatch.setattr(osinfo, "sibling_groups",
                        lambda: [[c] for c in (0, 2, 4, 6, 8, 10, 12, 14)])
    monkeypatch.setattr(osinfo, "isolated_cpus", lambda: [])
    bench, observers = osinfo.partition_cpus()
    assert bench == [0, 2, 4, 6, 8, 10, 12, 14]
    assert observers == []


def test_every_core_isolated_is_ignored(monkeypatch):
    # Leaving the OS and the observers nowhere to run is worse than ignoring
    # the split, so the topology policy applies and a warning is logged.
    monkeypatch.setattr(osinfo, "sibling_groups", lambda: [[0], [1], [2], [3]])
    monkeypatch.setattr(osinfo, "isolated_cpus", lambda: [0, 1, 2, 3])
    bench, observers = osinfo.partition_cpus()
    assert bench == [0, 1, 2, 3]
    assert observers == []


def test_isolation_does_not_hand_observers_the_benchmarks_siblings(monkeypatch):
    # SMT on, isolcpus naming both threads of four cores. The housekeeping
    # cores are strictly better for the observers than the benchmark's own
    # siblings, which share execution resources with it.
    monkeypatch.setattr(osinfo, "sibling_groups",
                        lambda: [[i, i + 8] for i in range(8)])
    monkeypatch.setattr(osinfo, "isolated_cpus",
                        lambda: [4, 5, 6, 7, 12, 13, 14, 15])
    bench, observers = osinfo.partition_cpus()
    assert bench == [4, 5, 6, 7]
    assert not ({c + 8 for c in bench} & set(observers)), "sibling given away"
    assert observers == [0, 1, 2, 3, 8, 9, 10, 11]


def test_reserved_cores_applies_within_the_isolated_set(obelisk):
    bench, observers = osinfo.partition_cpus(reserved_cores=2)
    assert bench == [4, 6, 8, 10]
    assert set(observers) == {0, 2, 12, 14}


# --- offline CPUs ---------------------------------------------------------------

@pytest.mark.cpu_lists
def test_offline_cpus_are_not_phantom_cores(monkeypatch, tmp_path):
    # An offline CPU has no readable thread_siblings_list, and the fallback
    # for that turned each one into its own single-thread "core".  A box with
    # SMT disabled at boot then reported twice the cores it has, and the
    # benchmark could be pinned to a CPU that will never run it.
    base = tmp_path / "cpu"
    for c in range(4):
        d = base / "cpu{}".format(c) / "topology"
        d.mkdir(parents=True)
        if c % 2 == 0:  # only the even CPUs are online
            (d / "thread_siblings_list").write_text("{}\n".format(c))
    (base / "online").write_text("0,2\n")
    monkeypatch.setattr(osinfo, "SYSFS_CPU_BASE", str(base))
    monkeypatch.setattr(osinfo, "IS_LINUX", True)
    monkeypatch.setattr(osinfo, "SYSTEM", "Linux")
    assert osinfo.online_cpus() == [0, 2]
    assert osinfo._linux_sibling_groups() == [[0], [2]]


@pytest.mark.cpu_lists
def test_isolated_cpus_read_from_sysfs(monkeypatch, tmp_path):
    base = tmp_path / "cpu"
    base.mkdir(parents=True)
    (base / "isolated").write_text("4,6,8-10\n")
    monkeypatch.setattr(osinfo, "SYSFS_CPU_BASE", str(base))
    monkeypatch.setattr(osinfo, "SYSTEM", "Linux")
    assert osinfo.isolated_cpus() == [4, 6, 8, 9, 10]


@pytest.mark.cpu_lists
def test_missing_kernel_cpu_lists_are_empty(monkeypatch, tmp_path):
    # A kernel without these files, which callers read as "no information".
    monkeypatch.setattr(osinfo, "SYSFS_CPU_BASE", str(tmp_path / "absent"))
    monkeypatch.setattr(osinfo, "SYSTEM", "Linux")
    assert osinfo.isolated_cpus() == []
    assert osinfo.online_cpus() == []


# --- bench_cores ----------------------------------------------------------------

def _cpupin(monkeypatch, groups, isolated=(), **kwargs):
    monkeypatch.setattr(osinfo, "sibling_groups", lambda: groups)
    monkeypatch.setattr(osinfo, "isolated_cpus", lambda: list(isolated))
    monkeypatch.setattr(osinfo, "IS_LINUX", True)
    monkeypatch.setattr(osinfo, "IS_FREEBSD", False)
    return CpuPin(name="pin_bench", type="CpuPin", **kwargs)


def test_bench_cores_one_gives_a_single_deterministic_core(monkeypatch):
    # A single-threaded benchmark needs one core, and pinning it to exactly one
    # is what makes the placement the same every run.
    m = _cpupin(monkeypatch, [[c] for c in (0, 2, 4, 6, 8, 10, 12, 14)],
                isolated=(4, 6, 8, 10, 12, 14), bench_cores="1")
    assert m.val == ["taskset", "-c", "4"]


def test_bench_cores_defaults_to_the_whole_set(monkeypatch):
    m = _cpupin(monkeypatch, [[c] for c in (0, 2, 4, 6, 8, 10, 12, 14)],
                isolated=(4, 6, 8, 10, 12, 14))
    assert m.val == ["taskset", "-c", "4,6,8,10,12,14"]


def test_bench_cores_all_is_the_default_spelled_out(monkeypatch):
    m = _cpupin(monkeypatch, [[c] for c in (0, 2, 4, 6)], isolated=(4, 6),
                bench_cores="all")
    assert m.val == ["taskset", "-c", "4,6"]


def test_unused_benchmark_cores_are_not_given_to_the_observers(monkeypatch):
    # They are reserved for benchmark work; putting olly there would undo the
    # separation the pinning exists to create.  Left idle instead.
    m = _cpupin(monkeypatch, [[c] for c in (0, 2, 4, 6, 8, 10, 12, 14)],
                isolated=(4, 6, 8, 10, 12, 14), bench_cores="1")
    assert m.benchmark_cpus == [4]
    assert m.observer_cpus == [0, 2]


def test_bench_cores_beyond_the_set_uses_what_there_is(monkeypatch):
    m = _cpupin(monkeypatch, [[0], [2], [4], [6]], isolated=(4, 6),
                bench_cores="8")
    assert m.val == ["taskset", "-c", "4,6"]


@pytest.mark.parametrize("bad", ["0", "-1", "two", "1.5"])
def test_bench_cores_rejects_nonsense(monkeypatch, bad):
    with pytest.raises(ValueError):
        _cpupin(monkeypatch, [[0], [2]], bench_cores=bad)

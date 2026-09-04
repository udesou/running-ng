"""CPU topology detection and pinning.

The FreeBSD fixtures reproduce kern.sched.topology_spec as emitted by
sys/kern/sched_ule.c:3211-3250: nested <group> elements each carrying a <cpu>
list, with SMT sibling groups marked by a THREAD (and usually SMT) flag.
Nobody has run this on FreeBSD yet, so the fixtures are the specification.
"""
import pytest

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

@pytest.mark.skipif(not osinfo.IS_LINUX, reason="sysfs topology is Linux-only")
def test_linux_topology_is_self_consistent():
    groups = osinfo.sibling_groups()
    assert groups
    flat = [c for g in groups for c in g]
    assert len(flat) == len(set(flat)), "a CPU appears in two sibling groups"
    assert len(flat) == osinfo.core_count()


# --- CpuPin modifier -----------------------------------------------------------

from running import suite  # noqa: E402,F401  (suite first: see test_osinfo)
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
    bm = BinaryBenchmark(Path("/bin/true"), [], suite_name="s", name="b")
    bm = bm.attach_modifiers([m])
    assert [str(x) for x in bm.get_full_args(None)][:3] == ["taskset", "-c", "0-15"]
    assert bm.cpu_pin is m


def test_cpupin_excludes_still_apply(monkeypatch):
    m = _pin(monkeypatch, [[0, 1]])
    m.excludes = {"s": ["b"]}
    bm = BinaryBenchmark(Path("/bin/true"), [], suite_name="s", name="b")
    bm = bm.attach_modifiers([m])
    # Excluded benchmarks must not be pinned, and must not record the modifier.
    assert bm.wrapper == []
    assert bm.cpu_pin is None


def test_no_cpupin_means_no_observer_pinning():
    bm = BinaryBenchmark(Path("/bin/true"), [], suite_name="s", name="b")
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
    monkeypatch.setattr(osinfo, "sibling_groups", lambda: [[i, i + 16] for i in range(16)])
    monkeypatch.setattr(osinfo, "processor_topology", lambda: [])
    s = osinfo.machine_topology_summary()
    assert s == {"physical_cores": 16, "threads_per_core": 2}


def test_summary_is_empty_where_nothing_is_knowable(monkeypatch):
    monkeypatch.setattr(osinfo, "sibling_groups", lambda: [])
    monkeypatch.setattr(osinfo, "processor_topology", lambda: [])
    assert osinfo.machine_topology_summary() == {}

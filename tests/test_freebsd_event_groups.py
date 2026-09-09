"""Constraints on the FreeBSD PMC event groups.

pmcstat allocates all-or-nothing: one unresolvable event name makes it exit 71
and write nothing, so the whole group yields no counters. That failure is loud
but total, and it cannot be caught on Linux. These tests pin the group
definitions against what was actually verified on the hardware, so a plausible
but unverified event name fails here rather than silently costing a sweep its
counters.

Verified on rosemary: FreeBSD 15.1, Xeon E5-2640 v4 (Broadwell-EP), against a
real GC-heavy OCaml workload, as complete groups rather than event by event.
"""
from pathlib import Path

import pytest
import yaml

CONFIG_DIR = Path(__file__).parent.parent / "src" / "running" / "config" / "base" / "ocaml"
BASE_CONFIGS = ["micro_base.yml", "macro_base.yml"]

#: Every event name confirmed on the hardware to resolve AND return a non-zero
#: count. A FreeBSD group may use nothing else. Extend only with names checked
#: on a real FreeBSD host, never from a datasheet: an unresolvable name costs
#: the entire group, not just itself.
VERIFIED_EVENTS = {
    # instructions, on a fixed-function counter
    "instructions", "inst_retired.any", "inst_retired.any_p",
    "inst_retired.prec_dist",
    # cycles, on fixed-function counters
    "unhalted-cycles", "cpu_clk_unhalted.thread",
    "cpu_clk_unhalted.thread_p", "cpu_clk_unhalted.ref_tsc",
    # last-level cache
    "longest_lat_cache.miss", "longest_lat_cache.reference", "llc-misses",
    "mem_load_uops_retired.l3_miss", "mem_load_uops_retired.l1_miss",
    "l1d.replacement", "l2_rqsts.all_demand_miss",
    # branches
    "br_inst_retired.all_branches", "br_misp_retired.all_branches",
    # TLB
    "dtlb_load_misses.walk_completed", "dtlb_store_misses.walk_completed",
    "itlb_misses.walk_completed",
    # stalls and uops
    "resource_stalls.any", "uops_retired.retire_slots",
    "idq_uops_not_delivered.core",
    # soft PMCs (pmc.soft(3)); see SOFT_EVENTS for why they cost no
    # programmable counter. Measured on rosemary: PAGE_FAULT.ALL read 60,636
    # against rusage's 62,357, within 2.8%, and READ + WRITE summed exactly to
    # ALL (20 + 60,616). On GC workloads write faults are ~99.97% of the
    # total, the first-touch signature of a growing major heap.
    "PAGE_FAULT.ALL", "PAGE_FAULT.READ", "PAGE_FAULT.WRITE",
}

#: Events added on source evidence but NOT yet run on FreeBSD hardware, each
#: with the evidence and the consequence if it is wrong. This is a deliberate
#: and temporary state: pmcstat allocates all-or-nothing, so an event that
#: does not resolve costs its whole group every counter. Move an entry into
#: VERIFIED_EVENTS once measured, or take it out of the group.
PENDING_HARDWARE_VERIFICATION: dict = {}

#: Events that cost no programmable counter. Fixed-function hardware ones, and
#: soft PMCs, which are a separate class with their own 16 rows entirely.
SOFT_EVENTS = {"PAGE_FAULT.ALL", "PAGE_FAULT.READ", "PAGE_FAULT.WRITE",
               "CLOCK.HARD", "CLOCK.STAT", "CLOCK.PROF"}

#: Events that land on fixed-function counters, so cost no programmable slot.
FIXED_FUNCTION = {
    "instructions", "inst_retired.any", "inst_retired.any_p",
    "inst_retired.prec_dist",
    "unhalted-cycles", "cpu_clk_unhalted.thread",
    "cpu_clk_unhalted.thread_p", "cpu_clk_unhalted.ref_tsc",
}

#: 3 fixed-function + 4 programmable. Four, not eight, because SMT is on. An
#: 8th event fails with Invalid argument.
MAX_EVENTS = 7
MAX_PROGRAMMABLE = 4

#: Pairs that are the same underlying counter. Including both would spend a
#: programmable slot on a duplicate. Measured identical modulo the sampling
#: instant: 77,610,381 against 77,610,362 in one run.
SAME_COUNTER = [("llc-misses", "longest_lat_cache.miss")]


def _freebsd_groups(config):
    mods = yaml.safe_load((CONFIG_DIR / config).read_text())["modifiers"]
    return {name: spec["val"].split(",")
            for name, spec in mods.items() if name.endswith("_freebsd")}


@pytest.mark.parametrize("config", BASE_CONFIGS)
def test_the_three_groups_exist(config):
    groups = _freebsd_groups(config)
    assert set(groups) == {"perf_grp1_freebsd", "perf_grp2_freebsd",
                           "perf_grp3_freebsd"}


@pytest.mark.parametrize("config", BASE_CONFIGS)
def test_every_event_was_verified_on_hardware(config):
    for name, events in _freebsd_groups(config).items():
        allowed = VERIFIED_EVENTS | set(PENDING_HARDWARE_VERIFICATION)
        unverified = [e for e in events if e not in allowed]
        assert not unverified, (
            "{} in {} uses event(s) never verified on FreeBSD: {}. pmcstat "
            "allocates all-or-nothing, so one bad name costs the whole group "
            "its counters. Verify on real hardware and add to "
            "VERIFIED_EVENTS.".format(name, config, unverified))


@pytest.mark.parametrize("config", BASE_CONFIGS)
def test_groups_fit_the_counter_ceiling(config):
    for name, events in _freebsd_groups(config).items():
        assert len(events) <= MAX_EVENTS, "{}: {} events".format(name, len(events))
        programmable = [e for e in events
                        if e not in FIXED_FUNCTION and e not in SOFT_EVENTS]
        assert len(programmable) <= MAX_PROGRAMMABLE, (
            "{} in {} needs {} programmable counters, but SMT leaves only {}: "
            "{}".format(name, config, len(programmable), MAX_PROGRAMMABLE,
                        programmable))


@pytest.mark.parametrize("config", BASE_CONFIGS)
def test_no_group_spends_a_slot_on_a_duplicate_counter(config):
    for name, events in _freebsd_groups(config).items():
        for a, b in SAME_COUNTER:
            assert not (a in events and b in events), (
                "{} in {} lists both {} and {}, which are the same underlying "
                "counter".format(name, config, a, b)) 


@pytest.mark.parametrize("config", BASE_CONFIGS)
def test_no_group_repeats_an_event(config):
    for name, events in _freebsd_groups(config).items():
        assert len(events) == len(set(events)), name


@pytest.mark.parametrize("config", BASE_CONFIGS)
def test_no_group_carries_an_event_that_does_not_resolve(config):
    # The exact names the Linux groups use, which do NOT resolve on FreeBSD.
    # This is the mistake the whole exercise exists to prevent.
    linux_only = {"task-clock", "page-faults", "cycles", "branch-misses",
                  "cache-misses", "LLC-load-misses", "dTLB-load-misses",
                  "iTLB-load-misses", "stalled-cycles-frontend",
                  "stalled-cycles-backend"}
    for name, events in _freebsd_groups(config).items():
        clashes = set(events) & linux_only
        assert not clashes, "{} in {} uses Linux-only name(s) {}".format(
            name, config, clashes)


@pytest.mark.parametrize("config", BASE_CONFIGS)
def test_every_group_carries_instructions_and_cycles(config):
    # Two reasons. They are the only events that reach the contract
    # vocabulary, and the all-zero guard in PmcStatBackend.collect assumes
    # every group contains something that cannot legitimately read zero.
    for name, events in _freebsd_groups(config).items():
        assert "instructions" in events, name
        assert "unhalted-cycles" in events, name


@pytest.mark.parametrize("config", BASE_CONFIGS)
def test_alias_spellings_are_used_so_names_match_linux(config):
    # EVENT_ALIASES maps unhalted-cycles -> cycles, so recorded event names
    # come out identical to Linux perf's. The dotted spellings resolve too but
    # would break that.
    from running import counters
    assert counters.PmcStatBackend.EVENT_ALIASES["unhalted-cycles"] == "cycles"
    for name, events in _freebsd_groups(config).items():
        assert "cpu_clk_unhalted.thread" not in events, name
        assert "inst_retired.any" not in events, name


def test_both_base_configs_define_identical_groups():
    micro = _freebsd_groups("micro_base.yml")
    macro = _freebsd_groups("macro_base.yml")
    assert micro == macro, "the two base configs have drifted apart"


def test_linux_groups_are_untouched():
    # The Linux groups must keep working exactly as before; the FreeBSD ones
    # are additive.
    for config in BASE_CONFIGS:
        mods = yaml.safe_load((CONFIG_DIR / config).read_text())["modifiers"]
        assert mods["perf_grp1"]["val"] == \
            "task-clock,page-faults,cycles,instructions"
        assert mods["perf_grp2"]["val"] == \
            "task-clock,cycles,stalled-cycles-frontend,stalled-cycles-backend"
        assert mods["perf_grp3"]["val"] == (
            "task-clock,cycles,cache-misses,LLC-load-misses,"
            "dTLB-load-misses,iTLB-load-misses")


# --- the smoke config ----------------------------------------------------------

SMOKE = (Path(__file__).parent.parent / "src" / "running" / "config"
         / "examples" / "smoke_micro_freebsd.yml")


def test_smoke_config_uses_a_freebsd_group():
    d = yaml.safe_load(SMOKE.read_text())
    assert d["configs"] == ["ocaml-5.4.1|perf_grp1_freebsd"]


def test_smoke_config_differs_from_the_linux_one_only_in_the_group():
    # It exists to exercise the FreeBSD counter path, so anything else
    # diverging would make the two smoke runs incomparable.
    linux = yaml.safe_load(
        (SMOKE.parent / "smoke_micro.yml").read_text())
    freebsd = yaml.safe_load(SMOKE.read_text())
    differing = {k for k in set(linux) | set(freebsd)
                 if linux.get(k) != freebsd.get(k)}
    assert differing == {"configs"}


def test_group_names_are_legal_modifier_names():
    # Modifier rejects "-" in a name, reserving it for value options, so a
    # group named perf-grp1-freebsd would be accepted by YAML and then blow up
    # at config-resolution time.
    from running.modifier import PerfAndOllyAttach
    for name, events in _freebsd_groups("micro_base.yml").items():
        m = PerfAndOllyAttach(name=name, type="PerfAndOllyAttach",
                              val=",".join(events))
        assert m.name == name
        # split_quoted splits on whitespace, so the whole comma-separated list
        # arrives as one element; split_event_list is what fans it out per -p.
        from running import counters
        assert counters.split_event_list(m.perf_events) == events


def test_pending_events_carry_their_evidence():
    # An entry here bypasses the hardware-verified allowlist, so it must say
    # what the evidence is and what breaks if it is wrong. Otherwise the
    # allowlist quietly becomes a rubber stamp.
    for event, reason in PENDING_HARDWARE_VERIFICATION.items():
        assert len(reason) > 80, "{} needs a real justification".format(event)


@pytest.mark.parametrize("config", BASE_CONFIGS)
def test_pending_events_are_confined_to_group_1(config):
    # Deliberate blast-radius limit: an unresolvable name takes its whole
    # group down, so nothing unverified goes near grp2 or grp3.
    for name, events in _freebsd_groups(config).items():
        pending = set(events) & set(PENDING_HARDWARE_VERIFICATION)
        if pending:
            assert name == "perf_grp1_freebsd", (
                "{} carries unverified event(s) {}".format(name, pending))


@pytest.mark.parametrize("config", BASE_CONFIGS)
def test_soft_pmcs_do_not_consume_programmable_counters(config):
    # Soft PMCs are a separate hwpmc class with their own 16 rows, so grp1 can
    # carry PAGE_FAULT.ALL without touching the 4 programmable counters SMT
    # leaves. If this stopped holding, grp3 would be the one to break first.
    for name, events in _freebsd_groups(config).items():
        hardware = [e for e in events if e not in SOFT_EVENTS]
        assert len(hardware) <= MAX_EVENTS


def test_page_fault_alias_reaches_the_contract_metric():
    # The whole reason for the alias: PAGE_FAULT.ALL -> page-faults ->
    # page_faults, so FreeBSD regains that contract metric with no vocabulary
    # change.
    from running import counters
    from running.contract import vocab
    canonical = counters.PmcStatBackend.EVENT_ALIASES["PAGE_FAULT.ALL"]
    assert canonical == "page-faults"
    assert vocab.PERF_EVENT_MAP[canonical] == "page_faults"

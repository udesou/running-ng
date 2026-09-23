"""Constraints on the FreeBSD PMC event groups. pmcstat allocates
all-or-nothing (one unresolvable name: exit 71, no counters), and that cannot
be caught on Linux, so the groups are pinned to names verified on hardware
(FreeBSD 15.1, Xeon E5-2640 v4).
"""
import re
from pathlib import Path

import pytest
import yaml

CONFIG_DIR = Path(__file__).parent.parent / "src" / "running" / "config" / "base" / "ocaml"
BASE_CONFIGS = ["micro_base.yml", "macro_base.yml"]

#: Event names confirmed on hardware to resolve and count non-zero. Extend only
#: from a real FreeBSD host, never from a datasheet.
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
    # soft PMCs (pmc.soft(3)); PAGE_FAULT.ALL agreed with rusage within 3%
    "PAGE_FAULT.ALL", "PAGE_FAULT.READ", "PAGE_FAULT.WRITE",
}

#: Events added on source evidence, not yet run on FreeBSD hardware, each with
#: its evidence. Move into VERIFIED_EVENTS once measured.
PENDING_HARDWARE_VERIFICATION: dict = {}

#: Soft PMCs: a separate class with its own rows, so no programmable counter.
SOFT_EVENTS = {"PAGE_FAULT.ALL", "PAGE_FAULT.READ", "PAGE_FAULT.WRITE",
               "CLOCK.HARD", "CLOCK.STAT", "CLOCK.PROF"}

#: Fixed-function counters, so no programmable slot.
FIXED_FUNCTION = {
    "instructions", "inst_retired.any", "inst_retired.any_p",
    "inst_retired.prec_dist",
    "unhalted-cycles", "cpu_clk_unhalted.thread",
    "cpu_clk_unhalted.thread_p", "cpu_clk_unhalted.ref_tsc",
}

#: 3 fixed-function + 4 programmable (SMT halves the programmable ones).
MAX_EVENTS = 7
MAX_PROGRAMMABLE = 4

#: Pairs that are the same underlying counter (measured identical).
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
    # the Linux spellings, which do not resolve on FreeBSD
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
    # the all-zero guard in PmcStatBackend.collect assumes every group has one
    for name, events in _freebsd_groups(config).items():
        assert "instructions" in events, name
        assert "unhalted-cycles" in events, name


@pytest.mark.parametrize("config", BASE_CONFIGS)
def test_alias_spellings_are_used_so_names_match_linux(config):
    # the dotted spellings resolve too, but would not alias onto perf's names
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

EXAMPLES = (Path(__file__).parent.parent / "src" / "running" / "config"
            / "examples")
SMOKE = EXAMPLES / "smoke_micro.yml"

#: Configs that must work on FreeBSD as well as Linux: every group they name
#: must define `val_freebsd`, or the run produces no counters at all.
PORTABLE_CONFIGS = ["smoke_micro.yml", "all_micro.yml",
                    "smoke_macro.yml", "all_macro.yml"]


def test_group_names_are_legal_modifier_names():
    # Modifier rejects "-" in a name (reserved for value options)
    from running.modifier import PerfAndOllyAttach
    for name, events in _freebsd_groups("micro_base.yml").items():
        m = PerfAndOllyAttach(name=name, type="PerfAndOllyAttach",
                              val=",".join(events))
        assert m.name == name
        from running import counters
        assert counters.split_event_list(m.perf_events) == events


def test_pending_events_carry_their_evidence():
    # an entry bypasses the allowlist, so it must carry its evidence
    for event, reason in PENDING_HARDWARE_VERIFICATION.items():
        assert len(reason) > 80, "{} needs a real justification".format(event)


@pytest.mark.parametrize("config", BASE_CONFIGS)
def test_pending_events_are_confined_to_group_1(config):
    # nothing unverified goes near grp2 or grp3
    for name, events in _freebsd_groups(config).items():
        pending = set(events) & set(PENDING_HARDWARE_VERIFICATION)
        if pending:
            assert name == "perf_grp1_freebsd", (
                "{} carries unverified event(s) {}".format(name, pending))


@pytest.mark.parametrize("config", BASE_CONFIGS)
def test_soft_pmcs_do_not_consume_programmable_counters(config):
    # soft PMCs do not use the 4 programmable counters
    for name, events in _freebsd_groups(config).items():
        hardware = [e for e in events if e not in SOFT_EVENTS]
        assert len(hardware) <= MAX_EVENTS


def test_page_fault_alias_reaches_the_contract_metric():
    # PAGE_FAULT.ALL -> page-faults -> page_faults
    from running import counters
    from running.contract import vocab
    canonical = counters.PmcStatBackend.EVENT_ALIASES["PAGE_FAULT.ALL"]
    assert canonical == "page-faults"
    assert vocab.PERF_EVENT_MAP[canonical] == "page_faults"




def test_coverage_config_runs_every_micro_benchmark_once():
    # one invocation is enough to find what fails to build or run
    d = yaml.safe_load((EXAMPLES / "all_micro.yml").read_text())
    assert d["invocations"] == 1
    assert "benchmarks" not in d.get("overrides", {}), \
        "overrides would REPLACE the base's set, defeating the coverage point"


# --- coverage config: ring size and the OxCaml suite ---------------------------

def test_coverage_config_carries_a_larger_runtime_events_ring():
    """The default ring overflows on GC-dense benchmarks and olly's stats become unreliable."""
    d = yaml.safe_load((EXAMPLES / "all_micro.yml").read_text())
    entry = d["configs"][0]
    assert "re-25" in entry, entry
    assert "md-2" in entry, entry


def test_coverage_config_disables_the_oxcaml_suite_without_listing_the_rest():
    # top-level `benchmarks:` merges key by key; `overrides:` would replace the whole block
    d = yaml.safe_load((EXAMPLES / "all_micro.yml").read_text())
    assert d["benchmarks"] == {"oxcaml-prefetch": []}
    assert "benchmarks" not in d.get("overrides", {})


def test_coverage_config_still_tracks_every_other_suite():
    base = yaml.safe_load(
        (EXAMPLES.parent / "base" / "ocaml" / "micro_base.yml").read_text())
    d = yaml.safe_load((EXAMPLES / "all_micro.yml").read_text())
    merged = dict(base["benchmarks"])
    merged.update(d["benchmarks"])
    disabled = [s for s, b in merged.items() if not b]
    assert disabled == ["oxcaml-prefetch"], disabled
    for suite, benches in base["benchmarks"].items():
        if suite != "oxcaml-prefetch":
            assert merged[suite] == benches, suite


# --- macro configs -------------------------------------------------------------

BASE_DIR = EXAMPLES.parent / "base" / "ocaml"


@pytest.mark.parametrize("config", PORTABLE_CONFIGS)
def test_portable_configs_name_groups_that_carry_freebsd_events(config):
    d = yaml.safe_load((EXAMPLES / config).read_text())
    base_name = "micro_base.yml" if "micro" in config else "macro_base.yml"
    base = yaml.safe_load((BASE_DIR / base_name).read_text())["modifiers"]
    for entry in d["configs"]:
        groups = [t for t in entry.split("|") if t.startswith("perf_grp")]
        assert groups, "{} names no counter group".format(config)
        for g in groups:
            assert g in base, "{} names undefined modifier {}".format(config, g)
            assert base[g].get("val_freebsd") or g.endswith("_freebsd"), (
                "{} uses {}, which carries no val_freebsd. On FreeBSD it would "
                "fall back to the Linux events, of which only `instructions` "
                "resolves under hwpmc, and pmcstat allocates all-or-nothing, "
                "so the run would yield NO counters at all.".format(config, g))


@pytest.mark.parametrize("config", PORTABLE_CONFIGS)
def test_every_named_suite_exists_in_the_base(config):
    """A misspelled suite key is silently added as an empty suite, not rejected."""
    d = yaml.safe_load((EXAMPLES / config).read_text())
    base_name = d["includes"][0].split("/")[-1]
    base = yaml.safe_load((BASE_DIR / base_name).read_text())["benchmarks"]
    named = dict(d.get("benchmarks", {}))
    named.update(d.get("overrides", {}).get("benchmarks", {}))
    unknown = sorted(set(named) - set(base))
    assert not unknown, (
        "{} names suite(s) absent from {}: {}. A typo here disables nothing "
        "and reports no error.".format(config, base_name, unknown))


def test_all_macro_enables_every_suite_the_base_defines():
    """No suite is disabled, and the count is pinned so a silently smaller run shows up."""
    d = yaml.safe_load((EXAMPLES / "all_macro.yml").read_text())
    assert "benchmarks" not in d, (
        "all_macro.yml disables suites again; if that is deliberate, say which "
        "and why here")
    assert "benchmarks" not in d.get("overrides", {}), (
        "overrides.benchmarks would REPLACE the base's block, not update it")

    base = yaml.safe_load((BASE_DIR / "macro_base.yml").read_text())["benchmarks"]
    live = {s_: v for s_, v in base.items() if v}
    assert sum(len(v) for v in live.values()) == 95
    # lavyek and merlin are empty in the base, so 21 suites carry the 95
    assert len(live) == 21
    assert {s_ for s_, v in base.items() if not v} == {
        "macro-merlin", "macro-lavyek-monorepo"}

# --- the configs must actually load -------------------------------------------
# yaml.safe_load never exercises the `includes:` merge, so parse the file the
# way running-ng does. from_file + validate only: resolve_class() would
# provision opam switches.
@pytest.mark.parametrize("config", [
    "all_micro.yml",
    "all_macro.yml",
    "smoke_micro.yml",
    "smoke_macro.yml",
])
def test_freebsd_example_configs_load_through_the_real_merge(config):
    from running.config import Configuration

    c = Configuration.from_file(EXAMPLES, config)
    c.validate()


def test_tier1_overrides_reach_the_merged_config():
    # the scalars belong under `overrides:`; dropped, the run silently takes macro_base's
    from running.config import Configuration

    c = Configuration.from_file(EXAMPLES, "all_macro.yml")
    assert c.get("invocations") == 1
    assert c.get("heap_range") == 6
    assert c.get("spread_factor") == 1
    assert c.get("minheap_multiplier") == 1.0
    assert c.get("compress_logs") is False


# --- the documented command must actually select something -------------------
# Apply the RUNNING_TAG from each config's usage block (or runbms's default_run
# fallback) and require that benchmarks remain. Only a command line (ending in
# a backslash continuation) counts; prose mentioning RUNNING_TAG= does not.
_TAG_IN_USAGE = re.compile(r"^#\s+RUNNING_TAG=([A-Za-z0-9_,]+)\s*\\\s*$", re.M)


@pytest.mark.parametrize("config", [
    "all_micro.yml",
    "all_macro.yml",
    "smoke_micro.yml",
    "smoke_macro.yml",
])
def test_the_documented_command_selects_benchmarks(config):
    from running.config import Configuration

    c = Configuration.from_file(EXAMPLES, config)
    m = _TAG_IN_USAGE.search((EXAMPLES / config).read_text())
    if m:
        tags = m.group(1).split(",")
    elif "default_run" in (c.get("tags") or {}):
        tags = ["default_run"]          # runbms' fallback for a bare run
    else:
        tags = []                       # no tags block: no filter at all
    if tags:
        c.apply_tag_filter(tags)
    kept = sum(len(v) for v in (c.get("benchmarks") or {}).values())
    assert kept, (
        "{} documents RUNNING_TAG={} but that selects no benchmarks; the "
        "command in its usage block cannot run.".format(config, tags or "(unset)"))

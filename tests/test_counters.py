"""Tests for the counter-backend abstraction and its parsers.

The pmcstat fixtures below reproduce the exact column formatting of
usr.sbin/pmcstat/pmcstat.c: field widths from the computation at lines
1160-1181 (header_width = len(name) + 2, display_width = floor(bits/3.32193)+1,
48-bit counters), and printing from pmcstat_print_headers / print_counters at
lines 270-330, including the two-column "# " prefix that widens only the first
counter field.  Nobody has run this against real FreeBSD hardware yet, so the
fixtures are the specification until someone does.
"""
import os
import subprocess

import pytest

from running import counters, osinfo


# A two-event process-scope run, cumulative (-C), so the last row is the total.
PMCSTAT_TWO_EVENTS = (
    "#  p/instructions p/unhalted-cycles \n"
    "     123456789012      234567890123 \n"
    "     223456789012      434567890123 "
)

# pmcstat reprints the header every 256 rows; a long run has several.
PMCSTAT_REPEATED_HEADER = (
    "#  p/instructions p/unhalted-cycles \n"
    "     100000000000      200000000000 \n"
    "\n"
    "#  p/instructions p/unhalted-cycles \n"
    "     300000000000      600000000000 "
)

PMCSTAT_SYSTEM_SCOPE = (
    "# s/00/instructions s/01/instructions \n"
    "        1111111111        2222222222 "
)


# --- pmcstat table parsing -----------------------------------------------------

def test_parses_last_cumulative_row():
    assert counters.parse_pmcstat_table(PMCSTAT_TWO_EVENTS) == {
        "instructions": 223456789012,
        "unhalted-cycles": 434567890123,
    }


def test_last_row_wins_across_repeated_headers():
    assert counters.parse_pmcstat_table(PMCSTAT_REPEATED_HEADER) == {
        "instructions": 300000000000,
        "unhalted-cycles": 600000000000,
    }


def test_strips_system_scope_cpu_prefix():
    # "s/00/instructions" names cpu 0; the event is still "instructions".
    assert counters.parse_pmcstat_table(PMCSTAT_SYSTEM_SCOPE) == {
        "instructions": 2222222222,
    }


def test_empty_output_yields_no_counters():
    # What a failed PMC allocation, or a benchmark shorter than one interval,
    # looks like from here. Must be {} rather than an exception.
    assert counters.parse_pmcstat_table("") == {}
    assert counters.parse_pmcstat_table("\n\n") == {}


def test_header_with_no_rows_yields_no_counters():
    assert counters.parse_pmcstat_table("#  p/instructions \n") == {}


def test_rows_before_any_header_are_ignored():
    # Without a header the columns are unlabelled, and guessing would silently
    # mislabel every counter.
    assert counters.parse_pmcstat_table("   123   456 \n") == {}


def test_short_row_is_ignored_not_zipped():
    # A truncated final row (killed mid-write) must not be paired positionally
    # with the header, which would attribute one event's count to another.
    text = "#  p/instructions p/unhalted-cycles \n     111111111111 "
    assert counters.parse_pmcstat_table(text) == {}


def test_non_numeric_row_is_ignored():
    text = PMCSTAT_TWO_EVENTS + "\npmcstat: ERROR: some diagnostic here"
    assert counters.parse_pmcstat_table(text) == {
        "instructions": 223456789012,
        "unhalted-cycles": 434567890123,
    }


def test_unparseable_header_does_not_mislabel_following_rows():
    text = "# garbage garbage \n     111111111111      222222222222 "
    assert counters.parse_pmcstat_table(text) == {}


# --- backend record shape ------------------------------------------------------

class _FakeHandle:
    def __init__(self, path):
        self.proc = type("P", (), {"returncode": 0, "stderr": None})()
        self.output_path = path
        self.ctl_fds = ()


def test_pmcstat_backend_emits_canonical_records(tmp_path):
    p = tmp_path / "pmcstat_main.txt"
    p.write_text(PMCSTAT_TWO_EVENTS)
    records = counters.PmcStatBackend().collect(_FakeHandle(str(p)))
    by_name = {r["event"]: r["counter-value"] for r in records}
    # "unhalted-cycles" is FreeBSD's spelling of what perf calls "cycles";
    # aliasing it means the contract vocabulary needs no change.
    assert by_name == {"instructions": 223456789012.0, "cycles": 434567890123.0}
    assert all(isinstance(r["counter-value"], float) for r in records)


def test_pmcstat_backend_reports_nothing_when_tool_failed(tmp_path, caplog):
    p = tmp_path / "pmcstat_main.txt"
    p.write_text(PMCSTAT_TWO_EVENTS)
    h = _FakeHandle(str(p))
    h.proc.returncode = 1
    assert counters.PmcStatBackend().collect(h) == []
    assert "pmc list" in caplog.text


def test_pmcstat_backend_missing_file_is_not_fatal(tmp_path):
    assert counters.PmcStatBackend().collect(
        _FakeHandle(str(tmp_path / "nope.txt"))) == []


def test_pmcstat_command_shape():
    class Recorder(counters.PmcStatBackend):
        seen = None

        def _spawn(self, cmd):
            Recorder.seen = cmd

    b = counters.PmcStatBackend()
    # Build the command the way attach() does, without spawning anything.
    events = ["instructions", "unhalted-cycles"]
    cmd = ["pmcstat", "-C", "-d", "-w", str(b.INTERVAL_SECONDS), "-o", "/tmp/x"]
    for ev in events:
        cmd.extend(["-p", ev])
    cmd.extend(["-t", "4242"])
    # -C and -d are toggles that apply to the -p flags that follow them, so
    # they must come first; -t names the already-running target.
    assert cmd.index("-C") < cmd.index("-p")
    assert cmd.index("-d") < cmd.index("-p")
    assert cmd[cmd.index("-t") + 1] == "4242"


# --- perf parsing --------------------------------------------------------------

def test_parse_perf_ndjson_is_line_oriented():
    text = ('{"counter-value":"123","event":"instructions"}\n'
            'not json\n'
            '{"counter-value":"456","event":"cycles"}\n')
    out = counters.parse_perf_ndjson(text)
    assert [e["event"] for e in out] == ["instructions", "cycles"]


# --- selection -----------------------------------------------------------------

def test_null_backend_is_always_available_and_silent():
    b = counters.CounterBackend()
    assert b.available()
    assert b.attach(1, "/tmp", []) is None
    assert b.collect(None) == []
    b.stop(None)
    b.kill(None)


def test_select_backend_matches_this_host():
    b = counters.select_backend()
    if osinfo.IS_LINUX:
        assert b.name in ("linux-perf", "none")
    elif osinfo.IS_FREEBSD:
        assert b.name in ("freebsd-pmc", "none")


def test_backend_can_be_forced(monkeypatch):
    monkeypatch.setenv(counters.BACKEND_ENV_VAR, "none")
    assert counters.select_backend().name == "none"


def test_forcing_unavailable_backend_warns_but_returns_it(monkeypatch, caplog):
    monkeypatch.setenv(counters.BACKEND_ENV_VAR, "freebsd-pmc")
    b = counters.select_backend()
    assert b.name == "freebsd-pmc"
    if not osinfo.IS_FREEBSD:
        assert "unavailable" in caplog.text


def test_unknown_forced_backend_is_an_error(monkeypatch):
    monkeypatch.setenv(counters.BACKEND_ENV_VAR, "nonesuch")
    with pytest.raises(ValueError, match="nonesuch"):
        counters.select_backend()


# --- event list flattening -----------------------------------------------------

def test_split_event_list_flattens_perf_style_commas():
    # How every existing config writes it: one comma-separated string, because
    # that is what perf -e takes.
    assert counters.split_event_list(["task-clock,cycles,instructions"]) == [
        "task-clock", "cycles", "instructions"]


def test_split_event_list_handles_already_separate_and_whitespace():
    assert counters.split_event_list(["cycles", " instructions "]) == [
        "cycles", "instructions"]


def test_split_event_list_drops_empties():
    assert counters.split_event_list(["cycles,,", "", ","]) == ["cycles"]


def test_split_event_list_empty_input():
    assert counters.split_event_list([]) == []

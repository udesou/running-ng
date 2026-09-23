"""Tests for the counter-backend abstraction and its parsers. The pmcstat
fixtures reproduce the column formatting of usr.sbin/pmcstat/pmcstat.c.
"""
import os
import time
import subprocess

import pytest

from running import counters, osinfo


PMCSTAT_TWO_EVENTS = (
    "#  p/instructions p/unhalted-cycles \n"
    "     123456789012      234567890123 \n"
    "     223456789012      434567890123 "
)

# pmcstat reprints the header every 256 rows
PMCSTAT_REPEATED_HEADER = (
    "#  p/instructions p/unhalted-cycles \n"
    "     100000000000      200000000000 \n"
    "\n"
    "#  p/instructions p/unhalted-cycles \n"
    "     300000000000      600000000000 "
)

# Real pmcstat output (FreeBSD 15.1) ends without a trailing newline, and the
# padding after "#" varies with the event name's length.
PMCSTAT_REAL_NO_TRAILING_NEWLINE = (
    "# p/unhalted-cycles \n"
    "                  0 \n"
    "         8077883251 \n"
    "        37037553393 "
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
    assert counters.parse_pmcstat_table(PMCSTAT_SYSTEM_SCOPE) == {
        "instructions": 2222222222,
    }


def test_empty_output_yields_no_counters():
    # a failed PMC allocation looks like this
    assert counters.parse_pmcstat_table("") == {}
    assert counters.parse_pmcstat_table("\n\n") == {}


def test_header_with_no_rows_yields_no_counters():
    assert counters.parse_pmcstat_table("#  p/instructions \n") == {}


def test_rows_before_any_header_are_ignored():
    assert counters.parse_pmcstat_table("   123   456 \n") == {}


def test_short_row_is_ignored_not_zipped():
    # a truncated row must not be paired positionally with the header
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


def test_parses_output_with_no_trailing_newline():
    assert counters.parse_pmcstat_table(PMCSTAT_REAL_NO_TRAILING_NEWLINE) == {
        "unhalted-cycles": 37037553393,
    }


def test_intermediate_rows_are_ignored_in_favour_of_the_last():
    """Intermediate rows are stale snapshots (hwpmc saves the counter only on
    context switch); only the exit row is the total."""
    table = counters.parse_pmcstat_table(PMCSTAT_REAL_NO_TRAILING_NEWLINE)
    assert table["unhalted-cycles"] == 37037553393
    assert 8077883251 not in table.values()


def test_header_padding_width_does_not_affect_parsing():
    one = counters.parse_pmcstat_table(PMCSTAT_REAL_NO_TRAILING_NEWLINE)
    two = counters.parse_pmcstat_table(PMCSTAT_TWO_EVENTS)
    assert set(one) == {"unhalted-cycles"}
    assert set(two) == {"instructions", "unhalted-cycles"}


# --- backend record shape ------------------------------------------------------

class _FakeHandle:
    def __init__(self, path):
        self.proc = type("P", (), {"returncode": 0, "stderr": None})()
        self.output_path = path
        self.ctl_fds = ()
        self.killed = False
        self.ready = True


def test_pmcstat_backend_emits_canonical_records(tmp_path):
    p = tmp_path / "pmcstat_main.txt"
    p.write_text(PMCSTAT_TWO_EVENTS)
    records = counters.PmcStatBackend().collect(_FakeHandle(str(p)))
    by_name = {r["event"]: r["counter-value"] for r in records}
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
    events = ["instructions", "unhalted-cycles"]
    cmd = ["pmcstat", "-C", "-d", "-w", str(b.INTERVAL_SECONDS), "-o", "/tmp/x"]
    for ev in events:
        cmd.extend(["-p", ev])
    cmd.extend(["-t", "4242"])
    # -C and -d apply to the -p flags that follow them
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
    assert counters.split_event_list(["task-clock,cycles,instructions"]) == [
        "task-clock", "cycles", "instructions"]


def test_split_event_list_handles_already_separate_and_whitespace():
    assert counters.split_event_list(["cycles", " instructions "]) == [
        "cycles", "instructions"]


def test_split_event_list_drops_empties():
    assert counters.split_event_list(["cycles,,", "", ","]) == ["cycles"]


def test_split_event_list_empty_input():
    assert counters.split_event_list([]) == []


# --- killed counter tool -------------------------------------------------------

def test_killed_pmcstat_yields_no_counters_rather_than_stale_ones(tmp_path, caplog):
    """A killed pmcstat leaves only a stale, plausible-looking row; discard rather than publish."""
    p = tmp_path / "pmcstat_main.txt"
    p.write_text(PMCSTAT_REAL_NO_TRAILING_NEWLINE)
    h = _FakeHandle(str(p))
    h.killed = True
    assert counters.PmcStatBackend().collect(h) == []
    assert "stale" in caplog.text


def test_stop_marks_the_handle_when_it_has_to_kill(caplog):
    import subprocess as sp
    proc = sp.Popen(["sleep", "30"])
    h = counters.CounterHandle(proc, "/tmp/unused")
    try:
        counters.CounterBackend().stop(h, timeout=0.2)
        assert h.killed is True
        assert "killing it" in caplog.text
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_stop_does_not_mark_a_tool_that_exited_on_its_own():
    import subprocess as sp
    proc = sp.Popen(["true"])
    h = counters.CounterHandle(proc, "/tmp/unused")
    counters.CounterBackend().stop(h, timeout=10)
    assert h.killed is False


# --- readiness handshake -------------------------------------------------------

def test_pmcstat_wait_ready_returns_true_once_the_header_appears(tmp_path):
    out = tmp_path / "pmcstat_main.txt"
    proc = subprocess.Popen(["sleep", "5"])
    h = counters.CounterHandle(proc, str(out))
    try:
        out.write_text("#  p/instructions \n")
        assert counters.PmcStatBackend().wait_ready(h, timeout=2.0) is True
        assert h.ready is True
    finally:
        proc.kill()
        proc.wait()


def test_pmcstat_wait_ready_returns_false_promptly_if_the_tool_died(tmp_path):
    proc = subprocess.Popen(["false"])
    proc.wait()
    h = counters.CounterHandle(proc, str(tmp_path / "never_written.txt"))
    started = time.monotonic()
    assert counters.PmcStatBackend().wait_ready(h, timeout=5.0) is False
    assert time.monotonic() - started < 2.0


def test_pmcstat_wait_ready_times_out_without_raising(tmp_path, caplog):
    proc = subprocess.Popen(["sleep", "5"])
    h = counters.CounterHandle(proc, str(tmp_path / "never_written.txt"))
    try:
        assert counters.PmcStatBackend().wait_ready(h, timeout=0.2) is False
        assert h.ready is False
        assert "did not start counting" in caplog.text
    finally:
        proc.kill()
        proc.wait()


def test_pmcstat_wait_ready_handles_no_handle():
    assert counters.PmcStatBackend().wait_ready(None) is True


def test_base_wait_ready_is_a_noop_so_linux_is_unchanged(tmp_path):
    # perf arms itself inside attach(); a second wait here would add latency to every invocation
    assert counters.CounterBackend().wait_ready(None) is True
    assert counters.PerfBackend().wait_ready(None) is True
    assert counters.PerfBackend.wait_ready is counters.CounterBackend.wait_ready
    h = _FakeHandle(str(tmp_path / "x"))
    started = time.monotonic()
    assert counters.PerfBackend().wait_ready(h, timeout=5.0) is True
    assert time.monotonic() - started < 0.1


# --- zero and partial results --------------------------------------------------

def test_all_zero_table_is_treated_as_missing_data(tmp_path, caplog):
    # pmcstat exits 0 here, so no other guard fires
    p = tmp_path / "pmcstat_main.txt"
    p.write_text("#  p/instructions p/unhalted-cycles \n"
                 "                0                 0 ")
    assert counters.PmcStatBackend().collect(_FakeHandle(str(p))) == []
    assert "zero for every counter" in caplog.text


def test_partly_zero_table_is_still_published(tmp_path):
    p = tmp_path / "pmcstat_main.txt"
    p.write_text("#  p/instructions p/unhalted-cycles \n"
                 "       123456789012                 0 ")
    records = counters.PmcStatBackend().collect(_FakeHandle(str(p)))
    assert {r["event"]: r["counter-value"] for r in records} == {
        "instructions": 123456789012.0, "cycles": 0.0}


def test_unready_handle_discards_its_totals(tmp_path, caplog):
    p = tmp_path / "pmcstat_main.txt"
    p.write_text(PMCSTAT_TWO_EVENTS)
    h = _FakeHandle(str(p))
    h.ready = False
    assert counters.PmcStatBackend().collect(h) == []
    assert "never confirmed to be counting" in caplog.text


# --- unreliable GC stats -------------------------------------------------------

def test_unreliable_gc_stats_are_warned_about(caplog):
    """An overflowed ring is marked in olly's own output but the invocation still passes."""
    from running.benchmark import _warn_if_gc_stats_unreliable
    _warn_if_gc_stats_unreliable("globroots_mp", {
        "stats_reliable": False, "lost_words": 7698197, "gc_time": 1.0})
    assert "stats_reliable=false" in caplog.text
    assert "globroots_mp" in caplog.text
    assert "re-25" in caplog.text, "the message should say how to fix it"


def test_reliable_gc_stats_are_silent(caplog):
    from running.benchmark import _warn_if_gc_stats_unreliable
    _warn_if_gc_stats_unreliable("almabench", {"stats_reliable": True})
    _warn_if_gc_stats_unreliable("almabench", {})
    assert caplog.text == ""


def test_unreliable_check_tolerates_a_non_dict():
    # must not raise on the measurement path
    from running.benchmark import _warn_if_gc_stats_unreliable
    _warn_if_gc_stats_unreliable("x", None)
    _warn_if_gc_stats_unreliable("x", "not a dict")


# --- task-clock unit handling ---------------------------------------------------

from running.benchmark import counter_seconds  # noqa: E402


#: A real task-clock entry from an invocation whose rusage said 36.908s of CPU.
REAL_TASK_CLOCK = {
    "counter-value": "36769.670031",
    "unit": "msec",
    "event": "task-clock",
    "event-runtime": 36769670031,
    "pcnt-running": 100.0,
    "metric-value": "0.992784",
    "metric-unit": "CPUs utilized",
}


def test_task_clock_is_read_in_its_stated_unit():
    assert counter_seconds(REAL_TASK_CLOCK) == pytest.approx(36.7696, rel=1e-4)


def test_a_faithful_counter_agrees_with_rusage():
    # the downstream check is task_clock < 0.8 * cpu_time
    cpu_time = 36.065 + 0.843
    assert counter_seconds(REAL_TASK_CLOCK) > 0.8 * cpu_time


def test_a_genuine_undercount_is_still_detectable():
    # perf attached late and saw one thread of many
    entry = dict(REAL_TASK_CLOCK, **{"counter-value": "10500.0",
                                     "event-runtime": 10500000000})
    assert counter_seconds(entry) < 0.8 * 322.0


@pytest.mark.parametrize("unit,value,expected", [
    ("msec", "1500", 1.5),
    ("ms", "1500", 1.5),
    ("sec", "1.5", 1.5),
    ("usec", "1500000", 1.5),
    ("nsec", "1500000000", 1.5),
])
def test_known_units(unit, value, expected):
    assert counter_seconds({"counter-value": value, "unit": unit}) == pytest.approx(expected)


def test_unknown_unit_falls_back_to_event_runtime_nanoseconds():
    e = {"counter-value": "1500", "unit": "furlongs", "event-runtime": 1500000000}
    assert counter_seconds(e) == pytest.approx(1.5)


def test_missing_unit_falls_back_to_event_runtime():
    assert counter_seconds({"counter-value": "1500",
                            "event-runtime": 1500000000}) == pytest.approx(1.5)


def test_unreadable_entry_is_none():
    assert counter_seconds({"unit": "msec"}) is None
    assert counter_seconds({"counter-value": "n/a", "unit": "msec"}) is None
    assert counter_seconds({"counter-value": "1500", "unit": "furlongs"}) is None

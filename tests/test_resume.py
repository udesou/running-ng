"""--resume: what counts as a completed cell, and the contract it leaves."""
import json

from running.command import runbms
from running.contract.native import NativeEmitter

CFG_A = "ocaml-a|perf_grp1"
CFG_B = "ocaml-b|perf_grp1"
RUNTIMES = {"ocaml-a": {"type": "OCaml", "version": "5.4.1"},
            "ocaml-b": {"type": "OCaml", "version": "5.5.0"}}
OLLY = json.dumps({"olly": {"wall_time": 1.0, "cpu_time": 1.0}}).encode()


class _Bm:
    def __init__(self, name, suite="s"):
        self.name = name
        self.suite_name = suite


def _rows(contract):
    p = contract / "measurements" / "olly.ndjson"
    return [json.loads(line) for line in p.read_text().splitlines()]


def _emitter(contract, resume=False):
    e = NativeEmitter(contract, "run-1", RUNTIMES, resume=resume)
    e.set_config_strings([CFG_A, CFG_B])
    return e


def test_a_resumed_contract_keeps_the_earlier_sessions_measurements(tmp_path):
    first = _emitter(tmp_path)
    first.record(_Bm("x"), CFG_A, OLLY)
    first.record(_Bm("y"), CFG_B, OLLY)
    # killed: no finalize; the next session resumes
    second = _emitter(tmp_path, resume=True)
    second.record(_Bm("z"), CFG_A, OLLY)
    second.finalize()
    rows = _rows(tmp_path)
    assert [r["benchmark"]["name"] for r in rows] == ["x", "y", "z"]
    man = json.loads((tmp_path / "manifest.json").read_text())
    assert sorted(b["name"] for b in man["benchmarks"]) == ["x", "y", "z"]
    assert len(man["configs"]) == 2


def test_a_new_run_in_the_same_dir_starts_empty(tmp_path):
    _emitter(tmp_path).record(_Bm("x"), CFG_A, OLLY)
    _emitter(tmp_path).record(_Bm("y"), CFG_A, OLLY)
    assert [r["benchmark"]["name"] for r in _rows(tmp_path)] == ["y"]


def test_a_rerun_cell_replaces_its_partial_rows(tmp_path):
    first = _emitter(tmp_path)
    first.record(_Bm("x"), CFG_A, OLLY)
    first.record(_Bm("x"), CFG_B, OLLY)
    second = _emitter(tmp_path, resume=True)
    second.discard(_Bm("x"), CFG_A)
    second.record(_Bm("x"), CFG_A, OLLY)
    rows = _rows(tmp_path)
    assert len(rows) == 2
    assert all(r["invocation"] == 0 for r in rows)


def test_invocations_continue_from_the_resumed_rows(tmp_path):
    _emitter(tmp_path).record(_Bm("x"), CFG_A, OLLY)
    second = _emitter(tmp_path, resume=True)
    second.record(_Bm("x"), CFG_A, OLLY)
    assert [r["invocation"] for r in _rows(tmp_path)] == [0, 1]


def test_completed_cells_round_trip(tmp_path):
    assert runbms.read_complete_cells(tmp_path) is None
    runbms.mark_cell_complete(tmp_path, "x.0.0.a.s.log")
    assert runbms.read_complete_cells(tmp_path) == {"x.0.0.a.s.log"}


def test_a_cut_off_cell_is_cleared_before_it_reruns(tmp_path, monkeypatch):
    monkeypatch.setattr(runbms, "_native_emitter", None)
    monkeypatch.setattr(runbms, "get_filename_no_ext",
                        lambda bm, hfac, size, c: "x.0.0.a.s")
    for name in ("x.0.0.a.s.log", "olly_x.0.0.a.s.json", "perf_x.0.0.a.s.json",
                 "memtrace_x.0.0.a.s.0.trace", "y.0.0.a.s.log"):
        (tmp_path / name).write_text("partial")
    runbms.discard_partial_cell(tmp_path, _Bm("x"), None, None, CFG_A)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["y.0.0.a.s.log"]

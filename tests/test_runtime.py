"""Switch provisioning contract for version/commit-pinned OCaml runtimes:
constructing one provisions its switch eagerly (so Configuration.resolve_class()
creates and destroys switches), wiping a leftover unless RUNNING_REUSE_SWITCHES is set.
"""
import shutil

import pytest

from running.runtime import OCaml

#: Not "/bin/true", which is Linux-only.
TRUE_BIN = shutil.which("true")


@pytest.fixture
def no_real_opam(monkeypatch, tmp_path):
    """Let __init__ run in full while doing no actual opam work."""
    bin_dir = tmp_path / "switch-bin"
    bin_dir.mkdir()
    (bin_dir / "ocaml").write_text("#!/usr/bin/env bash\nexit 0\n")
    (bin_dir / "ocaml").chmod(0o755)

    calls = {"ensure_switch": []}
    monkeypatch.setattr(
        OCaml, "_ensure_switch",
        staticmethod(lambda kwargs, switch_name: calls["ensure_switch"].append(switch_name)))
    monkeypatch.setattr(OCaml, "_find_opam", staticmethod(lambda: TRUE_BIN))

    class _Result:
        stdout = str(bin_dir)
    monkeypatch.setattr("running.runtime.subprocess.run",
                        lambda *a, **k: _Result())
    return calls, bin_dir


def test_construction_provisions_the_switch_eagerly(no_real_opam):
    calls, bin_dir = no_real_opam
    runtime = OCaml(name="ocaml-v5.3", version="5.3.0")
    assert calls["ensure_switch"] == ["running-ng-ocaml-v5.3"]
    assert runtime.get_executable() == (bin_dir / "ocaml").absolute()
    # a plain accessor; never provisions again
    runtime.get_executable()
    assert len(calls["ensure_switch"]) == 1


def test_switch_name_is_derived_from_the_runtime_name(no_real_opam):
    calls, _ = no_real_opam
    runtime = OCaml(name="ocaml-trunk-abc123", commit="abc123")
    assert calls["ensure_switch"] == ["running-ng-ocaml-trunk-abc123"]
    assert runtime.get_switch_name() == "running-ng-ocaml-trunk-abc123"


def test_executable_mode_provisions_nothing(no_real_opam, tmp_path):
    calls, _ = no_real_opam
    exe = tmp_path / "prebuilt-ocaml"
    exe.write_text("#!/usr/bin/env bash\nexit 0\n")
    exe.chmod(0o755)
    runtime = OCaml(name="ocaml-local", executable=str(exe))
    assert calls["ensure_switch"] == []
    assert runtime.get_switch_name() is None


def test_requires_a_version_commit_or_executable(no_real_opam):
    with pytest.raises(KeyError):
        OCaml(name="ocaml-nothing")


def test_version_and_commit_are_mutually_exclusive(no_real_opam):
    with pytest.raises(ValueError):
        OCaml(name="ocaml-both", version="5.3.0", commit="abc123")


@pytest.fixture
def claim(monkeypatch):
    """Drive _claim_switch with every side effect recorded rather than done."""
    state = {"exists": False, "removed": [], "dry_run": False}
    monkeypatch.setattr(OCaml, "_switches_created_this_run", set())
    monkeypatch.setattr(OCaml, "_acquire_opam_lock", staticmethod(lambda: None))
    monkeypatch.setattr(OCaml, "_save_active_switch", staticmethod(lambda: None))
    monkeypatch.setattr(OCaml, "_assert_switch_usable", staticmethod(lambda n: None))
    monkeypatch.setattr(OCaml, "_switch_exists",
                        staticmethod(lambda n: state["exists"]))
    monkeypatch.setattr(OCaml, "_remove_switch",
                        staticmethod(lambda n: state["removed"].append(n)))
    monkeypatch.setattr("running.suite.is_dry_run", lambda: state["dry_run"])
    return state


def test_absent_switch_is_created_without_removing_anything(claim):
    claim["exists"] = False
    assert OCaml._claim_switch("running-ng-ocaml-v5.3") is True
    assert claim["removed"] == []


def test_existing_switch_is_wiped_then_recreated(claim):
    claim["exists"] = True
    assert OCaml._claim_switch("running-ng-ocaml-v5.3") is True
    assert claim["removed"] == ["running-ng-ocaml-v5.3"]


def test_reuse_switches_env_var_keeps_an_existing_switch(claim, monkeypatch):
    monkeypatch.setenv("RUNNING_REUSE_SWITCHES", "1")
    claim["exists"] = True
    assert OCaml._claim_switch("running-ng-ocaml-v5.3") is False
    assert claim["removed"] == []


@pytest.mark.parametrize("value,reuses", [
    ("1", True), ("yes", True), ("0", False), ("", False),
])
def test_reuse_switches_accepts_any_non_zero_value(claim, monkeypatch, value, reuses):
    monkeypatch.setenv("RUNNING_REUSE_SWITCHES", value)
    claim["exists"] = True
    assert OCaml._claim_switch("running-ng-ocaml-v5.3") is (not reuses)
    assert bool(claim["removed"]) is (not reuses)


def test_a_switch_made_earlier_in_this_run_is_reused(claim):
    OCaml._switches_created_this_run.add("running-ng-ocaml-v5.3")
    claim["exists"] = True
    assert OCaml._claim_switch("running-ng-ocaml-v5.3") is False
    assert claim["removed"] == []


def test_dry_run_never_removes_a_switch(claim):
    claim["exists"] = True
    claim["dry_run"] = True
    assert OCaml._claim_switch("running-ng-ocaml-v5.3") is False
    assert claim["removed"] == []

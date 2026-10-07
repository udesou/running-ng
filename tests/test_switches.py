"""running-ng's own opam switches: declaration, state, drift detection and
command plan. Nothing here creates a switch.
"""
import json
import os
import sys
import subprocess

import pytest

from running import switches


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv(switches.STATE_ENV_VAR, str(tmp_path / "state"))
    return tmp_path / "state" / "switches.json"


# --- the declaration -----------------------------------------------------------

def test_olly_has_no_switch():
    """olly is built per runtime (running.olly), not in a switch of its own."""
    assert list(switches.SWITCHES) == [switches.TOOLS_SWITCH]


def test_only_the_tools_switch_registers_the_plugin():
    registers = [n for n, s in switches.SWITCHES.items() if s["registers_plugin"]]
    assert registers == [switches.TOOLS_SWITCH]


# --- command plan --------------------------------------------------------------

def test_tools_switch_commands():
    cmds = switches.build_commands(switches.TOOLS_SWITCH, compiler="5.4.0")
    assert cmds[0][:3] == ["opam", "switch", "create"]
    install = [c for c in cmds if c[1] == "install"][0]
    assert "--switch" in install and switches.TOOLS_SWITCH in install
    assert {"dune", "ocamlfind", "opam-compiler"} <= set(install)


def test_pins_are_applied_before_installing():
    cmds = switches.build_commands(switches.TOOLS_SWITCH, compiler="5.4.0")
    pins = switches.SWITCHES[switches.TOOLS_SWITCH]["pins"]
    pin_cmds = [c for c in cmds if c[1] == "pin"]
    assert [c[-2:] for c in pin_cmds] == [[p, u] for p, u in pins.items()]
    assert all("--no-action" in c for c in pin_cmds)
    first_install = next(i for i, c in enumerate(cmds) if c[1] == "install")
    assert all(cmds.index(c) < first_install for c in pin_cmds)


def test_every_install_names_its_switch_explicitly():
    # without --switch, opam installs into whichever switch is current
    for name in switches.SWITCHES:
        for cmd in switches.build_commands(name):
            if "install" in cmd:
                assert "--switch" in cmd, cmd
                assert cmd[cmd.index("--switch") + 1] == name, cmd


# --- state file ----------------------------------------------------------------

def test_missing_state_reads_as_nothing_known(state):
    assert switches.load_state() == {"version": 1, "switches": {}}


def test_corrupt_state_is_not_fatal(state):
    os.makedirs(state.parent, exist_ok=True)
    state.write_text("{ this is not json")
    assert switches.load_state() == {"version": 1, "switches": {}}


def test_state_round_trips(state):
    switches.save_state({"version": 1, "switches": {"x": {"identity": {"a": "1"}}}})
    assert switches.load_state()["switches"]["x"]["identity"] == {"a": "1"}
    assert json.loads(state.read_text())["version"] == 1


def test_state_path_honours_the_env_var(tmp_path, monkeypatch):
    monkeypatch.setenv(switches.STATE_ENV_VAR, str(tmp_path / "elsewhere"))
    assert switches.state_path().startswith(str(tmp_path / "elsewhere"))


def test_state_is_not_written_into_the_repo(monkeypatch):
    monkeypatch.delenv(switches.STATE_ENV_VAR, raising=False)
    repo = os.path.dirname(os.path.dirname(os.path.abspath(switches.__file__)))
    assert not switches.state_path().startswith(repo)


# --- invalidation --------------------------------------------------------------

def _fake(monkeypatch, exists, observed):
    monkeypatch.setattr(switches, "switch_exists", lambda opam, n: exists)
    monkeypatch.setattr(switches, "observe", lambda opam, n: observed)
    monkeypatch.setattr(switches, "missing_packages", lambda opam, n: [])


def _tools(**identity):
    """A tools-switch identity carrying the declared pins."""
    pins = switches.SWITCHES[switches.TOOLS_SWITCH]["pins"]
    return dict(identity, **{"pin:" + p: u for p, u in pins.items()})


def test_absent_switch_is_created(monkeypatch, state):
    _fake(monkeypatch, False, None)
    assert switches.plan("opam", switches.TOOLS_SWITCH) == "create"


def test_unchanged_switch_is_left_alone(monkeypatch, state):
    ident = _tools(ocaml="5.4.0", dune="3.24.0")
    _fake(monkeypatch, True, ident)
    switches.save_state({"version": 1, "switches": {
        switches.TOOLS_SWITCH: {"identity": ident}}})
    assert switches.plan("opam", switches.TOOLS_SWITCH) == "ok"


def test_changed_identity_triggers_a_rebuild(monkeypatch, state):
    _fake(monkeypatch, True, _tools(ocaml="5.4.0", dune="3.24.0"))
    switches.save_state({"version": 1, "switches": {
        switches.TOOLS_SWITCH: {"identity": _tools(ocaml="5.4.0", dune="3.22.1")}}})
    assert switches.plan("opam", switches.TOOLS_SWITCH) == "rebuild"


def test_a_missing_or_moved_pin_is_repaired_in_place(monkeypatch, state):
    unpinned = {"ocaml": "5.4.0", "dune": "3.24.0"}
    _fake(monkeypatch, True, unpinned)
    switches.save_state({"version": 1, "switches": {
        switches.TOOLS_SWITCH: {"identity": unpinned}}})
    assert switches.plan("opam", switches.TOOLS_SWITCH) == "repair"


def test_an_unrecorded_switch_without_the_pin_is_repaired(monkeypatch, state):
    _fake(monkeypatch, True, {"ocaml": "5.4.0"})
    assert switches.plan("opam", switches.TOOLS_SWITCH) == "repair"


def test_a_switch_we_did_not_build_is_adopted_not_destroyed(monkeypatch, state):
    # present but unrecorded: someone else's, or our state was lost
    _fake(monkeypatch, True, _tools(ocaml="5.4.0"))
    assert switches.plan("opam", switches.TOOLS_SWITCH) == "adopt"


# --- one place creates switches ------------------------------------------------

# not switches.__file__, which lives in src/
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHELL_SCRIPTS = ["install_deps_linux.sh", "install_deps_macos.sh",
                 "install_deps_freebsd.sh", "run_ocaml_bench_gc_sweep.sh"]


def _code_lines(path):
    """Code lines only, so a comment mentioning a command does not count."""
    out = []
    for line in open(os.path.join(REPO, path)):
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            out.append(stripped)
    return out


@pytest.mark.parametrize("script", SHELL_SCRIPTS)
def test_no_shell_script_creates_an_opam_switch(script):
    """switches.py is the only thing that provisions switches; scripts call
    `python3 -m running.switches ensure`."""
    offenders = [l for l in _code_lines(script)
                 if "switch create" in l or "--deps-only" in l]
    assert not offenders, (
        "{} provisions switches itself: {}".format(script, offenders))


@pytest.mark.parametrize("script", SHELL_SCRIPTS)
def test_no_shell_script_installs_opam_compiler(script):
    # without --switch it may land in whichever switch is current
    offenders = [l for l in _code_lines(script)
                 if "opam-compiler" in l and "install" in l]
    assert not offenders, (
        "{} installs opam-compiler itself: {}".format(script, offenders))


@pytest.mark.parametrize("script", ["install_deps_linux.sh",
                                    "install_deps_macos.sh",
                                    "install_deps_freebsd.sh"])
def test_installers_agree_on_the_tools_switch_name(script):
    decls = [l for l in _code_lines(script) if l.startswith("OPAM_SWITCH=")]
    assert decls, script
    assert switches.TOOLS_SWITCH in decls[0], decls


# --- the invocation actually works ---------------------------------------------
#
# `python3 -m running.switches` with no PYTHONPATH fails with
# ModuleNotFoundError on any machine using a virtualenv.

INVOKERS = ["install_deps_linux.sh", "install_deps_macos.sh",
            "install_deps_freebsd.sh", "run_ocaml_bench_gc_sweep.sh"]


@pytest.mark.parametrize("script", INVOKERS)
def test_every_invocation_sets_a_python_path(script):
    for line in _code_lines(script):
        if "-m running.switches" not in line:
            continue
        assert "PYTHONPATH" in line, (
            "{}: `{}` has no PYTHONPATH, so it fails with ModuleNotFoundError "
            "wherever running-ng is not importable system-wide".format(
                script, line))


@pytest.mark.parametrize("script", INVOKERS)
def test_advice_strings_are_runnable_too(script):
    # the recommended command must not fail the same way
    for line in _code_lines(script):
        if "running.switches status" in line:
            assert "PYTHONPATH" in line, (
                "{}: advice `{}` would fail the same way".format(script, line))


def test_installers_do_not_require_a_virtualenv():
    # an installer runs before running-ng is installed, so it cannot depend on a virtualenv
    for script in ["install_deps_linux.sh", "install_deps_macos.sh",
                   "install_deps_freebsd.sh"]:
        for line in _code_lines(script):
            if "-m running.switches" in line:
                assert '"$PYTHON"' not in line, script


def test_switches_module_runs_on_a_bare_interpreter(tmp_path):
    """Execute it the way an installer does: outside the repo, PYTHONPATH=src,
    nothing else making `running` importable."""
    env = {"PATH": os.environ.get("PATH", ""),
           "HOME": os.environ.get("HOME", ""),
           "PYTHONPATH": os.path.join(REPO, "src")}
    p = subprocess.run([sys.executable, "-m", "running.switches", "--help"],
                       cwd=str(tmp_path), env=env,
                       capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    assert "ensure" in p.stdout


def test_switches_module_is_not_importable_without_the_path(tmp_path):
    # the negative half of the test above
    env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "")}
    p = subprocess.run([sys.executable, "-m", "running.switches", "--help"],
                       cwd=str(tmp_path), env=env,
                       capture_output=True, text=True)
    if p.returncode == 0:
        pytest.skip("running-ng is importable system-wide here, so the failure "
                    "this guards against cannot be reproduced on this machine")
    # wording varies between "No module named 'running'" and "No module named running.switches"
    assert "No module named" in p.stderr


# --- the state file lives in running-ng's own opam root ---------------------------

def test_state_lives_in_the_tools_root(monkeypatch, tmp_path):
    monkeypatch.delenv(switches.STATE_ENV_VAR, raising=False)
    monkeypatch.setenv("RUNNING_OPAM_ROOTS", str(tmp_path / "roots"))
    assert switches.state_path() == str(tmp_path / "roots" / "running-ng" / "switches.json")


def test_separate_roots_dirs_get_separate_state(monkeypatch, tmp_path):
    monkeypatch.delenv(switches.STATE_ENV_VAR, raising=False)
    monkeypatch.setenv("RUNNING_OPAM_ROOTS", str(tmp_path / "a"))
    a = switches.state_path()
    monkeypatch.setenv("RUNNING_OPAM_ROOTS", str(tmp_path / "b"))
    assert a != switches.state_path()


def test_the_users_opamroot_does_not_move_the_state(monkeypatch, tmp_path):
    monkeypatch.delenv(switches.STATE_ENV_VAR, raising=False)
    monkeypatch.setenv("RUNNING_OPAM_ROOTS", str(tmp_path / "roots"))
    monkeypatch.setenv("OPAMROOT", str(tmp_path / "user-root"))
    assert switches.state_path().startswith(str(tmp_path / "roots"))


def test_explicit_state_dir_still_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("RUNNING_OPAM_ROOTS", str(tmp_path / "roots"))
    monkeypatch.setenv(switches.STATE_ENV_VAR, str(tmp_path / "explicit"))
    assert switches.state_path().startswith(str(tmp_path / "explicit"))


def test_locating_the_state_needs_no_opam(monkeypatch, tmp_path):
    monkeypatch.delenv(switches.STATE_ENV_VAR, raising=False)
    monkeypatch.delenv("RUNNING_OPAM_ROOTS", raising=False)
    monkeypatch.setattr(switches, "find_opam",
                        lambda: (_ for _ in ()).throw(RuntimeError("no opam")))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    assert switches.state_path().startswith(str(tmp_path / "cache"))


def test_tools_commands_never_use_the_users_root(monkeypatch, tmp_path):
    monkeypatch.setenv("RUNNING_OPAM_ROOTS", str(tmp_path / "roots"))
    monkeypatch.setenv("OPAMROOT", str(tmp_path / "user-root"))
    assert switches.tools_env()["OPAMROOT"] == str(tmp_path / "roots" / "running-ng")


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

def test_olly_and_the_plugin_are_never_in_the_same_switch():
    """olly needs cmdliner >= 2.0 and opam-compiler pins < 2.0; each corrupts the other's switch."""
    tools = switches.SWITCHES[switches.TOOLS_SWITCH]
    olly = switches.SWITCHES[switches.OLLY_SWITCH]
    assert "opam-compiler" in tools["packages"]
    assert "opam-compiler" not in olly["packages"]
    assert olly["source"] is not None, "olly's deps come from its own opam file"
    assert tools["source"] is None


def test_olly_deps_are_not_hand_listed():
    assert switches.SWITCHES[switches.OLLY_SWITCH]["packages"] == []


def test_only_the_tools_switch_registers_the_plugin():
    registers = [n for n, s in switches.SWITCHES.items() if s["registers_plugin"]]
    assert registers == [switches.TOOLS_SWITCH]


# --- command plan --------------------------------------------------------------

def test_tools_switch_commands():
    cmds = switches.build_commands(switches.TOOLS_SWITCH, compiler="5.4.0")
    assert cmds[0][:3] == ["opam", "switch", "create"]
    install = cmds[1]
    assert "--switch" in install and switches.TOOLS_SWITCH in install
    assert {"dune", "ocamlfind", "opam-compiler"} <= set(install)


def test_olly_switch_resolves_deps_from_its_own_opam_file():
    cmds = switches.build_commands(switches.OLLY_SWITCH)
    deps = cmds[-1]
    assert "--deps-only" in deps and deps[-1] == "."


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


def test_absent_switch_is_created(monkeypatch, state):
    _fake(monkeypatch, False, None)
    assert switches.plan("opam", switches.TOOLS_SWITCH) == "create"


def test_unchanged_switch_is_left_alone(monkeypatch, state):
    ident = {"ocaml": "5.4.0", "dune": "3.24.0"}
    _fake(monkeypatch, True, ident)
    switches.save_state({"version": 1, "switches": {
        switches.TOOLS_SWITCH: {"identity": ident}}})
    assert switches.plan("opam", switches.TOOLS_SWITCH) == "ok"


def test_changed_identity_triggers_a_rebuild(monkeypatch, state):
    _fake(monkeypatch, True, {"ocaml": "5.4.0", "dune": "3.24.0"})
    switches.save_state({"version": 1, "switches": {
        switches.TOOLS_SWITCH: {"identity": {"ocaml": "5.4.0", "dune": "3.22.1"}}}})
    assert switches.plan("opam", switches.TOOLS_SWITCH) == "rebuild"


def test_moving_the_olly_checkout_triggers_a_rebuild(monkeypatch, state):
    """A stale olly is silently wrong, not broken; the recorded SHA catches it."""
    _fake(monkeypatch, True, {"ocaml": "5.4.0", "source_sha": "bbbb"})
    switches.save_state({"version": 1, "switches": {
        switches.OLLY_SWITCH: {"identity": {"ocaml": "5.4.0", "source_sha": "aaaa"}}}})
    assert switches.plan("opam", switches.OLLY_SWITCH) == "rebuild"


def test_a_switch_we_did_not_build_is_adopted_not_destroyed(monkeypatch, state):
    # present but unrecorded: someone else's, or our state was lost
    _fake(monkeypatch, True, {"ocaml": "5.4.0"})
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
    # into the olly switch it downgrades cmdliner; without --switch it may do the same
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


# --- the state file follows the opam root --------------------------------------

def test_state_follows_opamroot(monkeypatch, tmp_path):
    monkeypatch.delenv(switches.STATE_ENV_VAR, raising=False)
    monkeypatch.setenv("OPAMROOT", str(tmp_path / "agent-root"))
    assert switches.state_path().startswith(str(tmp_path / "agent-root"))


def test_separate_roots_get_separate_state(monkeypatch, tmp_path):
    monkeypatch.delenv(switches.STATE_ENV_VAR, raising=False)
    monkeypatch.setenv("OPAMROOT", str(tmp_path / "a"))
    a = switches.state_path()
    monkeypatch.setenv("OPAMROOT", str(tmp_path / "b"))
    b = switches.state_path()
    assert a != b, "two opam roots must not share one state file"


def test_a_root_that_does_not_exist_yet_still_gets_its_own_state(monkeypatch, tmp_path):
    """`opam var root` fails on a root not yet created; the state must not fall back to the shared cache."""
    monkeypatch.delenv(switches.STATE_ENV_VAR, raising=False)
    root = tmp_path / "not-created-yet"
    assert not root.exists()
    monkeypatch.setenv("OPAMROOT", str(root))
    assert switches.state_path().startswith(str(root))


def test_explicit_state_dir_still_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("OPAMROOT", str(tmp_path / "root"))
    monkeypatch.setenv(switches.STATE_ENV_VAR, str(tmp_path / "explicit"))
    assert switches.state_path().startswith(str(tmp_path / "explicit"))


def test_no_opam_at_all_is_not_fatal(monkeypatch, tmp_path):
    monkeypatch.delenv(switches.STATE_ENV_VAR, raising=False)
    monkeypatch.delenv("OPAMROOT", raising=False)
    monkeypatch.setattr(switches, "find_opam",
                        lambda: (_ for _ in ()).throw(RuntimeError("no opam")))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    assert switches.state_path().startswith(str(tmp_path / "cache"))

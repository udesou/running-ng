"""Construction contract for version/commit-pinned OCaml runtimes: constructing
one ensures its opam root eagerly (so Configuration.resolve_class() builds
compilers), keyed by the compiler's resolved identity.
"""
import pytest

from running import opam_roots
from running.runtime import OCaml, OCamlMMTk

SHA = "c" * 40


@pytest.fixture
def no_real_opam(monkeypatch, tmp_path):
    """Let __init__ run in full while building nothing."""
    monkeypatch.setattr(opam_roots, "resolve_ref", lambda repo, ref: SHA)
    calls = []

    def ensure(ident, build, opam="opam", base=None):
        calls.append(ident)
        root = opam_roots.Root(tmp_path / opam_roots.key(ident))
        (root.switch_prefix() / "bin").mkdir(parents=True, exist_ok=True)
        (root.switch_prefix() / "bin" / "ocaml").write_text("#!/bin/sh\n")
        return root
    monkeypatch.setattr(opam_roots, "ensure", ensure)
    monkeypatch.setattr(OCaml, "_find_opam", staticmethod(lambda: "opam"))
    return calls


def test_construction_ensures_the_root_eagerly(no_real_opam, tmp_path):
    runtime = OCaml(name="ocaml-v5.3", version="5.3.0")
    [ident] = no_real_opam
    assert ident["kind"] == "OCaml" and ident["ref"] == "5.3.0" and ident["sha"] == SHA
    assert ident["dune_version"] == OCaml.DUNE_VERSION
    assert runtime.get_switch_name() == opam_roots.SWITCH
    assert runtime.get_executable() == (
        tmp_path / opam_roots.key(ident) / opam_roots.SWITCH / "bin" / "ocaml")
    assert runtime.get_compiler_identity() == SHA


def test_identity_carries_build_settings(no_real_opam):
    OCaml(name="flambda", version="5.3.0", configure_args=["--enable-flambda"],
          dune_version="3.22.1", opam_repository="d" * 40)
    [ident] = no_real_opam
    assert ident["configure_args"] == ["--enable-flambda"]
    assert ident["dune_version"] == "3.22.1"
    assert ident["opam_repository"] == "d" * 40


def test_runtime_name_does_not_change_the_root(no_real_opam):
    a = OCaml(name="one", version="5.3.0")
    b = OCaml(name="two", version="5.3.0")
    assert a.get_switch_prefix() == b.get_switch_prefix()


def test_mmtk_installs_no_dune(no_real_opam):
    OCamlMMTk(name="mmtk", commit=SHA)
    [ident] = no_real_opam
    assert ident["kind"] == "OCamlMMTk" and ident["dune_version"] is None
    assert ident["repo"] == OCamlMMTk.DEFAULT_REPO


def test_executable_mode_provisions_nothing(no_real_opam, tmp_path):
    exe = tmp_path / "prebuilt-ocaml"
    exe.write_text("#!/bin/sh\n")
    runtime = OCaml(name="ocaml-local", executable=str(exe))
    assert no_real_opam == []
    assert runtime.get_switch_name() is None
    assert runtime.get_switch_prefix() is None
    assert runtime.get_compiler_identity().startswith("executable:")


def test_requires_a_version_commit_or_executable(no_real_opam):
    with pytest.raises(KeyError):
        OCaml(name="ocaml-nothing")


def test_version_and_commit_are_mutually_exclusive(no_real_opam):
    with pytest.raises(ValueError):
        OCaml(name="ocaml-both", version="5.3.0", commit="abc123")


def test_create_command_builds_the_resolved_sha(monkeypatch):
    monkeypatch.setattr(OCaml, "_opam_compiler_bin", staticmethod(lambda: "opam-compiler"))
    ident = {"repo": "https://github.com/ocaml/ocaml.git", "sha": SHA,
             "configure_args": ["--enable-flambda"]}
    assert OCaml._create_command(ident) == [
        "opam-compiler", "create", "ocaml/ocaml:" + SHA, "--switch", opam_roots.SWITCH,
        "--configure-command", "./configure --enable-flambda"]

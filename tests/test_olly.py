"""olly built per runtime: lock handling, source selection, where builds go.
Nothing here fetches or builds.
"""
import io
import tarfile
from pathlib import Path

import pytest

from running import olly, opam_roots

OPAM = """opam-version: "2.0"
depends: [
  "dune" {>= "3.18"}
  "ocaml" {>= "5.0.0~"}
  "hdr_histogram" {>= "0.0.6"}
  "cmdliner" {>= "2.0.0"}
  "ppx_deriving_jsont" {with-dev-setup & >= "0.2.1"}
  "alcotest" {with-test & >= "1.9.0"}
  "odoc" {with-doc}
  "trace"
]
"""


@pytest.fixture
def roots(tmp_path, monkeypatch):
    monkeypatch.setenv(opam_roots.ROOTS_ENV_VAR, str(tmp_path))
    for v in (olly.COMMIT_ENV_VAR, olly.DIR_ENV_VAR, olly.BIN_ENV_VAR):
        monkeypatch.delenv(v, raising=False)
    return tmp_path


def test_depends_drop_test_doc_and_dev_deps():
    assert olly.parse_depends(OPAM) == [
        '"dune" {>= "3.18"}', '"ocaml" {>= "5.0.0~"}',
        '"hdr_histogram" {>= "0.0.6"}', '"cmdliner" {>= "2.0.0"}', '"trace"']


def test_lock_input_leaves_out_overlays_and_constrains_dune():
    deps = olly.parse_depends(OPAM) + ['"jsont" {>= "0.4.0"}']
    text = olly.lock_input(deps)
    assert '"jsont"' not in text and '"ocaml"' not in text
    for c in olly.LOCK_CONSTRAINTS:
        assert c in text
    assert opam_roots.OPAM_REPOSITORY_COMMIT in text


def test_committed_lock_matches_the_pinned_commit_depends():
    """A COMMIT bump must re-lock (python -m running.olly lock <sha>)."""
    assert olly.lock_for(olly._committed_depends()) == olly.LOCK
    names = [d.split('"')[1] for d in olly._committed_depends()]
    assert set(olly.OVERLAY_SOURCES) <= set(names)


def test_every_vendored_archive_has_a_checksum():
    dirs = olly._duniverse_dirs(olly.LOCK.read_text())
    assert dirs and all(len(sha) == 64 for _, _, sha in dirs)


def test_changed_depends_relock_into_the_cache(roots, monkeypatch):
    calls = []
    monkeypatch.setattr(olly, "_relock", lambda deps, out: calls.append(out) or out.parent.mkdir(parents=True, exist_ok=True) or out.write_text("x"))
    deps = olly._committed_depends() + ['"newdep" {>= "1.0"}']
    lock = olly.lock_for(deps)
    assert lock.parent == roots / opam_roots.OLLY_CACHE / "locks"
    assert olly.lock_for(deps) == lock and len(calls) == 1


def test_an_overlay_constraint_change_is_refused(roots):
    deps = [d if not d.startswith('"jsont"') else '"jsont" {>= "0.5.0"}'
            for d in olly._committed_depends()] + ['"newdep"']
    with pytest.raises(RuntimeError, match="overlay"):
        olly._relock(deps, roots / "out")


def test_default_source_is_the_pinned_commit(roots):
    src = olly.Source()
    assert src.commit == olly.COMMIT and src.dir is None


def test_olly_commit_overrides_the_pin(roots, monkeypatch):
    monkeypatch.setenv(olly.COMMIT_ENV_VAR, "a" * 40)
    assert olly.Source().commit == "a" * 40


def test_abbreviated_olly_commit_is_refused(roots, monkeypatch):
    monkeypatch.setenv(olly.COMMIT_ENV_VAR, "abc123")
    with pytest.raises(RuntimeError, match="40-character"):
        olly.Source()


def test_olly_dir_and_commit_together_are_refused(roots, monkeypatch, tmp_path):
    monkeypatch.setenv(olly.COMMIT_ENV_VAR, "a" * 40)
    monkeypatch.setenv(olly.DIR_ENV_VAR, str(tmp_path))
    with pytest.raises(RuntimeError, match="not both"):
        olly.Source()


def test_olly_bin_is_used_for_every_runtime(roots, monkeypatch, tmp_path):
    exe = tmp_path / "olly"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    monkeypatch.setenv(olly.BIN_ENV_VAR, str(tmp_path))
    rt = type("R", (), {"name": "a"})()
    assert olly.ensure([rt]) == {"a": str(exe)}
    assert olly.binary_for(rt) == str(exe)


def test_build_goes_inside_the_runtime_root(roots):
    root = opam_roots.Root(roots / "ocaml-x")
    rt = type("R", (), {"name": "a", "get_root": lambda self: root})()
    assert olly._build_dir(rt, "k") == root.path / "olly" / "k"


def test_the_olly_cache_is_not_listed_as_a_root(roots):
    (roots / opam_roots.OLLY_CACHE).mkdir()
    assert opam_roots.list_roots(roots) == []


def test_extract_strips_the_top_directory(tmp_path):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        data = b"hello"
        info = tarfile.TarInfo("pkg-1.0/src/a.ml")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    olly._extract(buf.getvalue(), tmp_path / "out")
    assert (tmp_path / "out" / "src" / "a.ml").read_bytes() == b"hello"


def test_every_patch_and_overlay_is_shipped():
    assert sorted(p.name for p in olly.PATCHES.glob("*.patch")) == [
        "ctypes-oxcaml-eta.patch", "hdr_histogram-vendored-ctypes.patch",
        "olly-oxcaml-shim.patch"]
    for name in olly.OVERLAY_SOURCES:
        assert (olly.OVERLAYS / name / "dune-project").is_file()

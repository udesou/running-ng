import json
import os
import time

import pytest

from running import opam_roots
from running.opam_roots import Root, RootLock

SHA_A = "a" * 40
SHA_B = "b" * 40


@pytest.fixture
def ls_remote(monkeypatch):
    refs = {}
    monkeypatch.setattr(opam_roots, "_git_ls_remote", lambda repo, patterns: "".join(
        "{}\t{}\n".format(refs[p], p) for p in patterns if p in refs))
    return refs


@pytest.fixture
def fake_init(monkeypatch):
    calls = []

    def init(opam, root, opam_repository):
        root.path.mkdir(parents=True)
        calls.append(opam_repository)
    monkeypatch.setattr(opam_roots, "init_root", init)
    return calls


def ident(**kw):
    kw.setdefault("configure_args", [])
    return dict({"kind": "OCaml", "repo": "r", "ref": "5.4.1", "sha": SHA_A,
                 "dune_version": "3.24.0", "relocatable": False,
                 "opam_repository": SHA_B}, **kw)


def test_full_sha_needs_no_lookup(ls_remote):
    assert opam_roots.resolve_ref("r", SHA_A) == SHA_A


def test_annotated_tag_resolves_to_its_commit(ls_remote):
    ls_remote["refs/tags/5.4.1"] = SHA_B  # the tag object
    ls_remote["refs/tags/5.4.1^{}"] = SHA_A  # the commit
    assert opam_roots.resolve_ref("r", "5.4.1") == SHA_A


def test_branch_resolves(ls_remote):
    ls_remote["refs/heads/trunk"] = SHA_A
    assert opam_roots.resolve_ref("r", "trunk") == SHA_A


def test_abbreviated_sha_is_refused(ls_remote):
    with pytest.raises(ValueError, match="full 40-character"):
        opam_roots.resolve_ref("r", "abc1234")


def test_unknown_ref_is_refused(ls_remote):
    with pytest.raises(ValueError, match="no tag or branch"):
        opam_roots.resolve_ref("r", "nope")


def test_identity_records_the_pinned_opam_repository(ls_remote):
    ls_remote["refs/tags/5.4.1^{}"] = SHA_A
    i = opam_roots.identity("OCaml", "r", "5.4.1")
    assert i["sha"] == SHA_A
    assert i["opam_repository"] == opam_roots.OPAM_REPOSITORY_COMMIT


def test_key_changes_with_anything_that_changes_the_build():
    base = opam_roots.key(ident())
    assert opam_roots.key(ident()) == base
    assert base.startswith("ocaml-aaaaaaaaaaaa-")
    for change in ({"configure_args": ["--enable-flambda"]}, {"dune_version": "3.22.1"},
                   {"opam_repository": SHA_A}, {"relocatable": True},
                   {"kind": "OxCaml"}, {"sha": SHA_B}):
        assert opam_roots.key(ident(**change)) != base, change


def test_ensure_builds_once_then_reuses(tmp_path, fake_init):
    built = []
    build = lambda root: built.append(root.path)
    r1 = opam_roots.ensure(ident(), build, base=tmp_path)
    opam_roots.release_all()
    r2 = opam_roots.ensure(ident(), build, base=tmp_path)
    opam_roots.release_all()
    assert r1.path == r2.path and len(built) == 1
    assert fake_init == [SHA_B]
    rec = json.loads((r1.path / opam_roots.RECORD).read_text())
    assert rec["sha"] == SHA_A and "created" in rec
    assert (r1.path / opam_roots.LAST_USED).exists()


def test_incomplete_root_is_rebuilt(tmp_path, fake_init):
    path = tmp_path / opam_roots.key(ident())
    path.mkdir()
    (path / "leftover").write_text("half-built")
    opam_roots.ensure(ident(), lambda root: None, base=tmp_path)
    opam_roots.release_all()
    assert not (path / "leftover").exists()
    assert Root(path).complete()


def test_failed_build_leaves_no_record(tmp_path, fake_init):
    def boom(root):
        raise RuntimeError("compiler build failed")
    with pytest.raises(RuntimeError):
        opam_roots.ensure(ident(), boom, base=tmp_path)
    assert not Root(tmp_path / opam_roots.key(ident())).complete()


def test_gc_removes_old_unused_roots_only(tmp_path, fake_init):
    old = opam_roots.ensure(ident(), lambda r: None, base=tmp_path)
    opam_roots.release_all()
    new = opam_roots.ensure(ident(sha=SHA_B), lambda r: None, base=tmp_path)
    opam_roots.release_all()
    stale = time.time() - 10 * 86400
    os.utime(old.path / opam_roots.LAST_USED, (stale, stale))
    removed = opam_roots.gc(5, base=tmp_path)
    assert removed == [str(old.path)]
    assert new.path.exists() and not old.path.exists()


def test_gc_skips_a_root_in_use(tmp_path, fake_init):
    root = opam_roots.ensure(ident(), lambda r: None, base=tmp_path)
    stale = time.time() - 10 * 86400
    os.utime(root.path / opam_roots.LAST_USED, (stale, stale))
    try:
        assert opam_roots.gc(5, base=tmp_path) == []
        assert root.path.exists()
    finally:
        opam_roots.release_all()


def test_list_reports_incomplete_roots(tmp_path, fake_init):
    opam_roots.ensure(ident(), lambda r: None, base=tmp_path)
    opam_roots.release_all()
    (tmp_path / "ocaml-half").mkdir()
    (tmp_path / opam_roots.DOWNLOAD_CACHE).mkdir()
    listed = {e["key"]: e for e in opam_roots.list_roots(tmp_path)}
    assert listed["ocaml-half"]["complete"] is False
    assert listed[opam_roots.key(ident())]["sha"] == SHA_A
    assert opam_roots.DOWNLOAD_CACHE not in listed


def test_root_env_points_opam_at_the_root(tmp_path):
    env = Root(tmp_path / "r").env({"OPAMSWITCH": "x", "PATH": "/bin"})
    assert env["OPAMROOT"] == str(tmp_path / "r")
    assert "OPAMSWITCH" not in env


def test_lock_conflict_is_per_open_file(tmp_path):
    a, b = RootLock(tmp_path / "k.lock"), RootLock(tmp_path / "k.lock")
    a.acquire(exclusive=False)
    try:
        assert not b.try_exclusive()
    finally:
        a.release()
    assert b.try_exclusive()
    b.release()

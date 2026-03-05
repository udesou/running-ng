from pathlib import Path

from running.runtime import OCaml


def test_ocaml_version_runtime_is_resolved_lazily(monkeypatch, tmp_path):
    resolved_ocaml = tmp_path / "ocaml"
    resolved_ocaml.write_text("#!/usr/bin/env bash\nexit 0\n")
    resolved_ocaml.chmod(0o755)

    calls = []

    def fake_resolve_or_build(kwargs):
        calls.append(kwargs)
        return resolved_ocaml

    monkeypatch.setattr(
        OCaml,
        "_resolve_or_build_executable",
        staticmethod(fake_resolve_or_build),
    )

    runtime = OCaml(name="ocaml-v5.3", version="5.3.0")
    assert calls == []

    assert runtime.get_executable() == resolved_ocaml.resolve()
    assert len(calls) == 1

    # Repeated lookups should use the cached executable.
    assert runtime.get_executable() == resolved_ocaml.resolve()
    assert len(calls) == 1

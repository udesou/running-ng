from running.config import Configuration
import pytest


def test_override():
    c = Configuration({
        "a": {"b": 1, "c": 42},
        "d": ["foo", "bar"]
    })
    c.override("a.c", 43)
    c.override("d.1", "buzz")
    assert c.get("a")["b"] == 1
    assert c.get("a")["c"] == 43
    assert c.get("d") == ["foo", "buzz"]


def test_combine():
    c1 = Configuration({
        "a": {"b": 1, "c": 42},
        "d": ["foo", "bar"]
    })

    c2 = Configuration({
        "a": {"b": 2, "e": 43},
        "d": ["fizz", "buzz"],
        "f": 100
    })

    c = c1.combine(c2)
    assert c.get("a") == {"b": 2, "c": 42, "e": 43}
    assert c.get("d") == ["foo", "bar", "fizz", "buzz"]
    assert c.get("f") == 100


def test_combine_fail():
    c1 = Configuration({
        "a": "val1",
        "b": "b"
    })

    c2 = Configuration({
        "a": "val2",
        "c": "c"
    })

    with pytest.raises(TypeError):
        c1.combine(c2)


def test_resolve_suites():
    c = Configuration({"suites": {
        "dacapo2006": {
            "type": "DaCapo",
            "release": "2006",
            "path": "/usr/share/benchmarks/dacapo/dacapo-2006-10-MR2.jar",
            "timing_iteration": 3
        }
    }})
    c.resolve_class()
    dacapo2006 = c.get("suites")["dacapo2006"]
    assert dacapo2006.release == "2006"
    assert dacapo2006.path.stem == "dacapo-2006-10-MR2"


def test_resolve_modifiers():
    c = Configuration({"modifiers": {
        "ss": {
            "type": "EnvVar",
            "var": "MMTK_PLAN",
            "val": "SemiSpace"
        }
    }})
    c.resolve_class()
    ss = c.get("modifiers")["ss"]
    assert ss.var == "MMTK_PLAN"
    assert ss.val == "SemiSpace"


def test_resolve_jvms():
    c = Configuration({"runtimes": {
        "adoptopenjdk-8": {
            "type": "OpenJDK",
            "release": "8",
            "home": "/usr/lib/jvm/adoptopenjdk-8-hotspot-amd64"
        }
    }})
    c.resolve_class()
    jdk8 = c.get("runtimes")["adoptopenjdk-8"]
    assert str(
        jdk8.executable) == "/usr/lib/jvm/adoptopenjdk-8-hotspot-amd64/bin/java"
    assert jdk8.release == 8


def test_resolve_ocaml_runtime():
    c = Configuration({"runtimes": {
        "ocaml-system": {
            "type": "OCaml",
            "executable": "/usr/bin/ocaml"
        }
    }})
    c.resolve_class()
    ocaml = c.get("runtimes")["ocaml-system"]
    assert str(ocaml.executable) == "/usr/bin/ocaml"


def test_a_present_but_empty_key_is_not_an_override(tmp_path, caplog):
    """`benchmarks:` with nothing under it parses as None; it must act as no override and warn."""
    import logging

    base = tmp_path / "base.yml"
    base.write_text(
        "benchmarks:\n"
        "  suite_a: [one, two]\n"
        "  suite_b: [three]\n"
    )
    child = tmp_path / "child.yml"
    child.write_text('includes:\n  - "./base.yml"\n\nbenchmarks:\n')

    with caplog.at_level(logging.WARNING):
        c = Configuration.from_file(tmp_path, "child.yml")

    assert c.get("benchmarks") == {"suite_a": ["one", "two"], "suite_b": ["three"]}
    assert "present but empty" in caplog.text, \
        "an emptied key should warn, not pass silently"
    assert "benchmarks" in caplog.text, "the warning must name the key"


def test_an_explicit_empty_mapping_still_means_no_override(tmp_path):
    base = tmp_path / "base.yml"
    base.write_text("benchmarks:\n  suite_a: [one]\n")
    child = tmp_path / "child.yml"
    child.write_text('includes:\n  - "./base.yml"\n\nbenchmarks: {}\n')
    c = Configuration.from_file(tmp_path, "child.yml")
    assert c.get("benchmarks") == {"suite_a": ["one"]}

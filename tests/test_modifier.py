from running.benchmark import JavaBenchmark, OCamlBenchmark
from running.modifier import *
from running.config import Configuration
from running.runtime import OCaml


def test_jvm_arg():
    j = JVMArg(name="j", val="-Xms100M -D\"foo bar\"")
    assert j.val == ["-Xms100M", "-Dfoo bar"]


def test_jvm_classpath():
    j = JVMClasspath(name="j", val="/bin /foo \"/Users/John Citizen/\"")
    assert j.val == ["/bin", "/foo", "/Users/John Citizen/"]

    jb = JavaBenchmark(
        jvm_args=[], program_args=[], cp=["fizzbuzz"],
        suite_name="dacapo", name="fop"
    )
    jb = jb.attach_modifiers([j])
    assert jb.cp == ["fizzbuzz", "/bin", "/foo", "/Users/John Citizen/"]


def test_jvm_classpath_append():
    j = JVMClasspathAppend(name="j", val="/bin /foo \"/Users/John Citizen/\"")
    assert j.val == ["/bin", "/foo", "/Users/John Citizen/"]

    jb = JavaBenchmark(
        jvm_args=[], program_args=[], cp=["fizzbuzz"],
        suite_name="dacapo", name="fop"
    )
    jb = jb.attach_modifiers([j])
    assert jb.cp == ["fizzbuzz", "/bin", "/foo", "/Users/John Citizen/"]


def test_jvm_classpath_prepend():
    j = JVMClasspathPrepend(name="j", val="/bin /foo \"/Users/John Citizen/\"")
    assert j.val == ["/bin", "/foo", "/Users/John Citizen/"]

    jb = JavaBenchmark(
        jvm_args=[], program_args=[], cp=["fizzbuzz"],
        suite_name="dacapo", name="fop"
    )
    jb = jb.attach_modifiers([j])
    assert jb.cp == ["/bin", "/foo", "/Users/John Citizen/", "fizzbuzz"]


def test_program_arg():
    p = ProgramArg(name="p", val="/bin /foo \"/Users/John Citizen/\"")
    assert p.val == ["/bin", "/foo", "/Users/John Citizen/"]


def test_expand_value_opts():
    p = EnvVar(name="path", var="PATH", val="{0}:{1}")
    assert p.val == "{0}:{1}"
    p = p.apply_value_opts(value_opts=["/bin", "/sbin"])
    assert p.val == "/bin:/sbin"


def test_modifier_set():
    c = Configuration({
        "modifiers": {
            "a": {
                "type": "JVMArg",
                "val": "-XX:GC={0}"
            },
            "b": {
                "type": "EnvVar",
                "var": "FOO",
                "val": "BAR"
            },
            "c": {
                "type": "EnvVar",
                "var": "FIZZ",
                "val": "BUZZ"
            },
            "set": {
                "type": "ModifierSet",
                "val": "a-{0}|b"
            },
            "set_nested": {
                "type": "ModifierSet",
                "val": "set-{0}|c"
            }
        }
    })
    c.resolve_class()
    mods = c.get("modifiers")["set"].apply_value_opts(
        value_opts=["NoGC"]).flatten(c)
    mods = c.get("modifiers")["set_nested"].apply_value_opts(
        value_opts=["NoGC"]).flatten(c)
    assert len(mods) == 3


def test_ocaml_arg():
    o = OCamlArg(name="domains", val="-domain-count 4")
    assert o.val == ["-domain-count", "4"]


def test_ocamlrunparam():
    p = OCamlRunParam(name="s", val="s=262144")
    assert p.val == "s=262144"


def test_ocaml_benchmark_with_ocaml_modifiers():
    b = OCamlBenchmark(
        ocaml_args=["-I", "+unix", "unix.cma"],
        program="/tmp/binarytrees.ml",
        program_args=["12"],
        suite_name="ocaml-demo",
        name="binarytrees"
    )
    b = b.attach_modifiers([
        OCamlArg(name="domains", val="-domain-count 4"),
        OCamlRunParam(name="s", val="s=262144"),
        OCamlRunParam(name="o", val="o=80"),
    ])
    runtime = OCaml(name="ocaml-local", executable="/usr/bin/ocaml")
    cmd = b.to_string(runtime)
    assert "-domain-count 4" in cmd
    # smart_quote quotes the value because it contains a comma, which a
    # shell would otherwise not treat as one word.
    assert 'OCAMLRUNPARAM="s=262144,o=80"' in cmd


# --- includes scoping -----------------------------------------------------------

from pathlib import Path  # noqa: E402
from running.benchmark import BinaryBenchmark  # noqa: E402


def _bm(suite, name):
    return BinaryBenchmark(Path("/bin/true"), [], suite_name=suite, name=name)


def _wrapped(suite, name, m):
    return _bm(suite, name).attach_modifiers([m]).wrapper


def test_includes_scopes_to_the_listed_programs():
    m = Wrapper(name="w", type="Wrapper", val="taskset -c 4",
                includes={"suiteA": ["wanted"]})
    assert _wrapped("suiteA", "wanted", m) == ["taskset", "-c", "4"]


def test_includes_skips_everything_else():
    # Including its own suite: naming one program is the whole scope.
    m = Wrapper(name="w", type="Wrapper", val="taskset -c 4",
                includes={"suiteA": ["wanted"]})
    assert _wrapped("suiteA", "other", m) == []
    assert _wrapped("suiteB", "wanted", m) == []


def test_no_includes_leaves_existing_behaviour_alone():
    m = Wrapper(name="w", type="Wrapper", val="taskset -c 4")
    assert _wrapped("suiteA", "anything", m) == ["taskset", "-c", "4"]


def test_combining_includes_and_excludes_subtracts_the_whole_suite():
    # Not the composition you would expect, and the reason to use one or the
    # other.  The existing excludes handling drops a modifier for EVERY program
    # of a suite it names, not only the listed ones, so excluding "wanted" also
    # removes "also".  Documented rather than fixed: pin_lavyek, re_par and
    # md_par in macro_base.yml each name 18 partially-excluded suites and are
    # lavyek-only *because* of this behaviour.
    m = Wrapper(name="w", type="Wrapper", val="taskset -c 4",
                includes={"suiteA": ["wanted", "also"]},
                excludes={"suiteA": ["wanted"]})
    assert _wrapped("suiteA", "wanted", m) == []
    assert _wrapped("suiteA", "also", m) == []


def test_empty_includes_is_no_scope_at_all():
    m = Wrapper(name="w", type="Wrapper", val="taskset -c 4", includes={})
    assert _wrapped("suiteA", "anything", m) == ["taskset", "-c", "4"]

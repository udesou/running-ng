from running.command.runbms import spread, expand_configs
import pytest


def test_spread_0():
    spread_factor = 0
    N = 8
    for i in range(0, N + 1):
        assert spread(spread_factor, N, i) == i


def test_spread_1():
    spread_factor = 1
    N = 8
    for i in range(1, N + 1):
        left = pytest.approx(spread(spread_factor, N, i) -
                             spread(spread_factor, N, i - 1))
        right = pytest.approx(1 + (i-1) / 7)
        assert left == right


def test_expand_configs_cartesian_respects_fixed_modifiers():
    configs = [
        "ocaml-v5.4|time_stats|d-1|s-32768|o-40|i-32|a-1",
        "ocaml-v5.4|time_stats|d-1|s-32768|i-32|a-1",
        "ocaml-v5.4|time_stats|d-1|i-32|a-1",
    ]
    config_sweep = {
        "s": [32768, 65536],
        "o": [40, 60, 80],
    }

    expanded = expand_configs(configs, config_sweep)

    assert expanded == [
        "ocaml-v5.4|time_stats|d-1|s-32768|o-40|i-32|a-1",
        "ocaml-v5.4|time_stats|d-1|s-32768|i-32|a-1|o-40",
        "ocaml-v5.4|time_stats|d-1|s-32768|i-32|a-1|o-60",
        "ocaml-v5.4|time_stats|d-1|s-32768|i-32|a-1|o-80",
        "ocaml-v5.4|time_stats|d-1|i-32|a-1|s-32768|o-40",
        "ocaml-v5.4|time_stats|d-1|i-32|a-1|s-32768|o-60",
        "ocaml-v5.4|time_stats|d-1|i-32|a-1|s-32768|o-80",
        "ocaml-v5.4|time_stats|d-1|i-32|a-1|s-65536|o-40",
        "ocaml-v5.4|time_stats|d-1|i-32|a-1|s-65536|o-60",
        "ocaml-v5.4|time_stats|d-1|i-32|a-1|s-65536|o-80",
    ]


def test_expand_configs_without_sweep():
    configs = ["ocaml-v5.4|time_stats|d-1|s-32768|o-40|i-32|a-1"]
    assert expand_configs(configs, None) == configs


class _Runtime:
    def __init__(self, name):
        self.name = name


class _Bm:
    def __init__(self, name, fails=()):
        self.name, self.fails, self.calls = name, set(fails), []

    def prepare(self, runtime):
        self.calls.append(runtime.name)
        if runtime.name in self.fails:
            raise RuntimeError("boom")


def test_prebuild_reports_failures_and_builds_each_pair_once():
    from running.command.runbms import prebuild
    a, b = _Runtime("a"), _Runtime("b")
    ok, bad = _Bm("ok"), _Bm("bad", fails={"b"})
    # Two configs share runtime a: each (benchmark, runtime) is built once.
    configs = ["a|x", "a|y", "b|x"]
    by_config = {"a|x": a, "a|y": a, "b|x": b}
    failed = prebuild({"s": [ok, bad]}, {"s": None}, configs, by_config)
    assert failed == {("s", "bad", "b")}
    assert ok.calls == ["a", "b"]
    assert bad.calls == ["a", "b"]


def test_prebuild_rejects_unknown_suite():
    from running.command.runbms import prebuild
    with pytest.raises(KeyError):
        prebuild({"missing": [_Bm("x")]}, {}, ["a"], {"a": _Runtime("a")})

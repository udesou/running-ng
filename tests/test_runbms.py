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

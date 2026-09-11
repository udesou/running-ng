from pathlib import Path
from running.util import smart_quote, split_quoted


def test_split_quoted():
    assert split_quoted("123 \"foo bar\"") == ["123", "foo bar"]


def test_smart_quote():
    assert smart_quote(Path("/bin")/"123 456") == "\"/bin/123 456\""


# --- default_modifiers ----------------------------------------------------------

import pytest  # noqa: E402
from running.config import Configuration  # noqa: E402
from running.util import default_modifiers, parse_config_str  # noqa: E402


def _config(**items):
    c = Configuration(items)
    c.resolve_class()
    return c


MODS = {
    "pin_bench": {"type": "Wrapper", "val": "taskset -c 4"},
    "time_stats": {"type": "Wrapper", "val": "/usr/bin/time"},
}


def test_no_key_means_no_default_modifiers():
    # Every config predating this key must behave exactly as before.
    c = _config(modifiers=dict(MODS))
    assert default_modifiers(c, []) == []


def test_default_modifiers_are_applied_without_being_named():
    c = _config(modifiers=dict(MODS), default_modifiers=["pin_bench"])
    assert [m.name for m in default_modifiers(c, [])] == ["pin_bench"]


def test_naming_a_default_explicitly_does_not_apply_it_twice():
    # Two taskset prefixes would nest and the inner one would silently win.
    c = _config(modifiers=dict(MODS), default_modifiers=["pin_bench"])
    named = [c.get("modifiers")["pin_bench"]]
    assert default_modifiers(c, named) == []


def test_defaults_come_before_the_config_strings_own():
    c = _config(modifiers=dict(MODS), default_modifiers=["pin_bench"],
                runtimes={"rt": {"type": "NativeExecutable"}})
    _, mods = parse_config_str(c, "rt|time_stats")
    assert [m.name for m in mods] == ["pin_bench", "time_stats"]


def test_unknown_default_modifier_is_an_error():
    c = _config(modifiers=dict(MODS), default_modifiers=["nope"])
    with pytest.raises(KeyError, match="nope"):
        default_modifiers(c, [])

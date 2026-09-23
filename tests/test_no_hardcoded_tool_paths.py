"""No test may hardcode an absolute path to a utility that moves between
platforms (`/bin/true` is `/usr/bin/true` on FreeBSD); use shutil.which.
Checks string constants via ast, so comments do not trip it.
"""
import ast
import pathlib

import pytest

#: Utilities Linux and FreeBSD place in different directories.
MOVES_BETWEEN_PLATFORMS = {"true", "false"}

TESTS_DIR = pathlib.Path(__file__).parent


def _string_constants(path):
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.lineno, node.value


@pytest.mark.parametrize(
    "path", sorted(TESTS_DIR.glob("test_*.py")), ids=lambda p: p.name)
def test_no_hardcoded_path_to_a_utility_that_moves(path):
    bad = [
        (lineno, value)
        for lineno, value in _string_constants(path)
        if value.startswith(("/bin/", "/usr/bin/"))
        and value.rsplit("/", 1)[1] in MOVES_BETWEEN_PLATFORMS
    ]
    assert not bad, (
        "{} hardcodes a path to a utility that is not in the same place on "
        "every platform: {}. Use `shutil.which(\"<tool>\")` instead; FreeBSD "
        "puts these under /usr/bin and the test will fail there while passing "
        "here.".format(path.name, ["line %d: %s" % b for b in bad]))

"""No test may hardcode an absolute path to a utility whose location moves.

This is the third time the same bug has been written. `/bin/true` is a Linux
spelling: FreeBSD ships it at `/usr/bin/true`, and `BinaryBenchmark` asserts
`program.exists()`, so a hardcoded path fails the whole module there while
staying green on Linux, which is why it keeps surviving review.

The fix each time is the same, and two test modules already do it:

    TRUE_BIN = shutil.which("true")

Deliberately narrow. It checks only the utilities that genuinely move between
Linux and FreeBSD, not every absolute path: several tests legitimately use
/bin/sh, /bin/ls and /bin/sleep, which exist in the same place on both, and
others use fake paths like "/usr/bin/ocaml" as config placeholders that are
never executed. Flagging those would be noise, and a noisy test gets ignored.

Uses ast rather than a regex so it reads real string constants and does not
trip over a comment (such as the ones this rule prompted).
"""
import ast
import pathlib

import pytest

#: Utilities Linux and FreeBSD place in different directories. Extend this when
#: another one bites, rather than widening the rule to every absolute path.
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

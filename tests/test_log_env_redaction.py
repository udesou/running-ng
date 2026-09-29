from running.command.runbms import format_environment


def test_credentials_are_redacted():
    out = format_environment({
        "GITHUB_TOKEN": "ghp_x",
        "ANTHROPIC_API_KEY": "sk-x",
        "AWS_SECRET_ACCESS_KEY": "aws-x",
        "DB_PASSWORD": "pw",
        "PATH": "/usr/bin",
    })
    for secret in ("ghp_x", "sk-x", "aws-x", "pw\n"):
        assert secret not in out
    assert "\tGITHUB_TOKEN=<redacted>\n" in out
    assert "\tPATH=/usr/bin\n" in out


def test_everything_else_is_kept_in_order():
    out = format_environment({"OCAMLRUNPARAM": "s=4M", "HOME": "/h"})
    assert out == "\tHOME=/h\n\tOCAMLRUNPARAM=s=4M\n"


def test_command_lines_in_probe_output_are_redacted():
    from running.command.runbms import redact
    top = (
        "  101 u  20 0 python3 app.py --token abc123 --api-key=sk_live_x\n"
        "  102 u  20 0 curl -H Authorization: Bearer ghp_0123456789abcdefghijABCDEF\n"
        "  103 u  20 0 git clone https://me:hunter2@github.com/x/y\n"
        "  104 u  20 0 ocamlrunparam=s=4M ./bench 1500\n"
    )
    out = redact(top)
    for secret in ("abc123", "sk_live_x", "ghp_0123456789abcdefghijABCDEF",
                   "hunter2"):
        assert secret not in out
    assert "ocamlrunparam=s=4M ./bench 1500" in out


def test_secret_values_under_innocent_names_are_redacted():
    out = format_environment({
        "GIT_REMOTE": "https://me:hunter2@example.com/r",
        "NOTES": "ghp_0123456789abcdefghijABCDEF",
    })
    assert "hunter2" not in out and "ghp_0123" not in out

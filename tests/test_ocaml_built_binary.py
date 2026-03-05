from pathlib import Path

from running.benchmark import OCamlBuiltBinaryBenchmark
from running.runtime import OCaml


def test_ocaml_built_binary_uses_existing_binary_without_runtime_resolution(tmp_path):
    bench_dir = tmp_path / "infer"
    bench_dir.mkdir()
    existing_binary = bench_dir / "infer-bin"
    existing_binary.write_text("#!/usr/bin/env bash\nexit 0\n")
    existing_binary.chmod(0o755)

    class NoRuntimeResolutionOCaml(OCaml):
        def get_executable(self) -> Path:
            raise AssertionError("Runtime executable must not be resolved")

    runtime = NoRuntimeResolutionOCaml(name="ocaml-v5.3", version="5.3.0")
    bm = OCamlBuiltBinaryBenchmark(
        benchmark_name="infer",
        benchmark_dir=bench_dir,
        build_script=None,
        binary=None,
        existing_binary=str(existing_binary),
        program_args=["fallback-arg"],
        existing_program_args=["analyze", "--no-report", "-j", "16"],
        build_args=[],
        build_env={},
        always_build=False,
        suite_name="ocaml-binarytrees",
        name="infer",
    )

    bm.prepare(runtime)
    cmd = [str(part) for part in bm.get_full_args(runtime)]
    assert cmd == [
        str(existing_binary.resolve()),
        "analyze",
        "--no-report",
        "-j",
        "16",
    ]


def test_ocaml_built_binary_falls_back_to_build_script_when_existing_missing(tmp_path):
    bench_dir = tmp_path / "infer"
    bench_dir.mkdir()
    build_script = bench_dir / "infer.build.sh"
    build_script.write_text(
        "#!/usr/bin/env bash\n"
        "set -eu\n"
        "touch \"$RUNNING_OCAML_OUTPUT\"\n"
        "chmod +x \"$RUNNING_OCAML_OUTPUT\"\n"
    )
    build_script.chmod(0o755)

    fake_ocaml = tmp_path / "ocaml"
    fake_ocaml.write_text("#!/usr/bin/env bash\nexit 0\n")
    fake_ocaml.chmod(0o755)
    runtime = OCaml(name="ocaml-local", executable=str(fake_ocaml))

    bm = OCamlBuiltBinaryBenchmark(
        benchmark_name="infer",
        benchmark_dir=bench_dir,
        build_script=None,
        binary=None,
        existing_binary=str(bench_dir / "missing-infer"),
        program_args=["--build-fallback"],
        existing_program_args=["--existing"],
        build_args=[],
        build_env={},
        always_build=False,
        suite_name="ocaml-binarytrees",
        name="infer",
    )

    bm.prepare(runtime)
    built_binary = bench_dir / "infer-ocaml-local"
    assert built_binary.exists()

    cmd = [str(part) for part in bm.get_full_args(runtime)]
    assert cmd == [str(built_binary.resolve()), "--build-fallback"]


def test_ocaml_built_binary_can_skip_runtime_executable_resolution(tmp_path):
    bench_dir = tmp_path / "infer"
    bench_dir.mkdir()
    build_script = bench_dir / "infer.build.sh"
    build_script.write_text(
        "#!/usr/bin/env bash\n"
        "set -eu\n"
        "if [ -n \"${OCAML_EXECUTABLE:-}\" ]; then\n"
        "  echo \"OCAML_EXECUTABLE should not be set\" >&2\n"
        "  exit 1\n"
        "fi\n"
        "touch \"$RUNNING_OCAML_OUTPUT\"\n"
        "chmod +x \"$RUNNING_OCAML_OUTPUT\"\n"
    )
    build_script.chmod(0o755)

    class NoRuntimeResolutionOCaml(OCaml):
        def get_executable(self) -> Path:
            raise AssertionError("Runtime executable must not be resolved")

    runtime = NoRuntimeResolutionOCaml(name="ocaml-v5.3", version="5.3.0")
    bm = OCamlBuiltBinaryBenchmark(
        benchmark_name="infer",
        benchmark_dir=bench_dir,
        build_script=build_script,
        binary=None,
        existing_binary=None,
        program_args=["--build-fallback"],
        existing_program_args=[],
        build_args=[],
        build_env={},
        use_runtime_executable=False,
        always_build=False,
        suite_name="ocaml-binarytrees",
        name="infer",
    )

    bm.prepare(runtime)
    built_binary = bench_dir / "infer-ocaml-v5.3"
    assert built_binary.exists()

    cmd = [str(part) for part in bm.get_full_args(runtime)]
    assert cmd == [str(built_binary.resolve()), "--build-fallback"]

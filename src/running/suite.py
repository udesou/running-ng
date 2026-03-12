from pathlib import Path
from typing import Any, Dict, Optional, Union
from running.benchmark import JavaBenchmark, BinaryBenchmark, Benchmark, JavaScriptBenchmark, OCamlBenchmark, OCamlBuiltBinaryBenchmark
import logging
from running.util import register, split_quoted
import os

__DRY_RUN = False
__DEFAULT_MINHEAP = 4096


def is_dry_run():
    global __DRY_RUN
    return __DRY_RUN


def set_dry_run(val: bool):
    global __DRY_RUN
    __DRY_RUN = val


def parse_timing_iteration(t: Optional[str], suite_name: str) -> Union[str, int]:
    if not t:
        raise KeyError(
            "You need to specify the timing_iteration for a {} suite".format(suite_name))
    assert t is not None
    try:
        t_parsed = int(t)
        return t_parsed
    except ValueError:
        return t


class BenchmarkSuite(object):
    CLS_MAPPING: Dict[str, Any]
    CLS_MAPPING = {}

    def __init__(self, name: str, **kwargs):
        self.name = name

    def __str__(self) -> str:
        return "Benchmark Suite {}".format(self.name)

    @staticmethod
    def from_config(name: str, config: Dict[str, str]) -> Any:
        return BenchmarkSuite.CLS_MAPPING[config["type"]](name=name, **config)

    def get_benchmark(self, _bm_spec: Union[str, Dict[str, Any]]) -> Any:
        raise NotImplementedError()

    def get_minheap(self, _bm: Benchmark) -> int:
        raise NotImplementedError

    def is_passed(self, _output: bytes) -> bool:
        raise NotImplementedError


@register(BenchmarkSuite)
class BinaryBenchmarkSuite(BenchmarkSuite):
    def __init__(self, programs: Dict[str, Dict[str, str]], **kwargs):
        super().__init__(**kwargs)
        self.programs: Dict[str, Dict[str, Any]]
        self.programs = {
            k: {
                'path': Path(os.path.expandvars(v['path'])),
                'args': split_quoted(os.path.expandvars(v['args']))
            }
            for k, v in programs.items()
        }
        self.timeout = kwargs.get("timeout")

    def get_benchmark(self, bm_spec: Union[str, Dict[str, Any]]) -> 'BinaryBenchmark':
        assert type(bm_spec) is str
        bm_name = bm_spec
        return BinaryBenchmark(
            self.programs[bm_name]['path'],
            self.programs[bm_name]['args'],
            suite_name=self.name,
            name=bm_name,
            timeout=self.timeout
        )

    def get_minheap(self, bm: Benchmark) -> int:
        logging.warning("minheap is not respected for BinaryBenchmarkSuite")
        assert isinstance(bm, BinaryBenchmark)
        return 0

    def is_passed(self, _output: bytes) -> bool:
        # FIXME no generic way to know
        return True


class JavaBenchmarkSuite(BenchmarkSuite):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    def get_minheap(self, _bm: Benchmark) -> int:
        raise NotImplementedError()


@register(BenchmarkSuite)
class DaCapo(JavaBenchmarkSuite):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.release: str
        self.release = kwargs["release"]
        if self.release not in ["2006", "9.12", "evaluation"]:
            raise ValueError(
                "DaCapo release {} not recongized".format(self.release))
        self.path: Path
        self.path = Path(kwargs["path"])
        if not self.path.exists():
            logging.warning("DaCapo jar {} not found".format(self.path))
        self.minheap: Optional[str]
        self.minheap = kwargs.get("minheap")
        self.minheap_values: Dict[str, Dict[str, int]]
        self.minheap_values = kwargs.get("minheap_values", {})
        if not isinstance(self.minheap_values, dict):
            raise TypeError(
                "The minheap_values of {} should be a dictionary".format(self.name))
        if self.minheap:
            if not isinstance(self.minheap, str):
                raise TypeError(
                    "The minheap of {} should be a string that selects from a minheap_values".format(self.name))
            if self.minheap not in self.minheap_values:
                raise KeyError(
                    "{} is not a valid entry of {}.minheap_values".format(self.name, self.name))
        self.timing_iteration = parse_timing_iteration(
            kwargs.get("timing_iteration"), "DaCapo")
        if isinstance(self.timing_iteration, str) and self.timing_iteration != "converge":
            raise TypeError("The timing iteration of the DaCapo benchmark suite `{}` is {}, which neither an integer nor 'converge'".format(
                self.path,
                repr(self.timing_iteration)
            ))
        self.callback: Optional[str]
        self.callback = kwargs.get("callback")
        self.timeout: Optional[int]
        self.timeout = kwargs.get("timeout")
        self.wrapper: Optional[Union[Dict[str, str], str]]
        self.wrapper = kwargs.get("wrapper")
        self.companion: Optional[Union[Dict[str, str], str]]
        self.companion = kwargs.get("companion")
        self.size: str
        self.size = kwargs.get("size", "default")

    def __str__(self) -> str:
        return "{} DaCapo {} {}".format(super().__str__(), self.release, self.path)

    @staticmethod
    def parse_timing_iteration(v: Any):
        try:
            timing_iteration = int(v)
        except ValueError:
            if v != "converge":
                raise TypeError("The timing iteration {} is neither an integer nor 'converge'".format(
                    repr(v)
                ))
            timing_iteration = v
        return timing_iteration

    def get_benchmark(self, bm_spec: Union[str, Dict[str, Any]]) -> 'JavaBenchmark':
        timing_iteration = self.timing_iteration
        timeout = self.timeout
        size = self.size
        if type(bm_spec) is str:
            bm_name = bm_spec
            name = bm_spec
        else:
            assert type(bm_spec) is dict
            if "bm_name" not in bm_spec or "name" not in bm_spec:
                raise KeyError(
                    "When a dictionary is used to speicfy a benchmark, you need to provide both `name` and `bm_name`")
            bm_name = bm_spec["bm_name"]
            name = bm_spec["name"]
            if "timing_iteration" in bm_spec:
                timing_iteration = DaCapo.parse_timing_iteration(
                    bm_spec["timing_iteration"])
            if "size" in bm_spec:
                size = bm_spec["size"]
            if "timeout" in bm_spec:
                timeout = bm_spec["timeout"]

        if self.callback:
            cp = [str(self.path)]
            program_args = ["Harness", "-c", self.callback]
        else:
            cp = []
            program_args = ["-jar", str(self.path)]
        # Timing iteration
        if type(timing_iteration) is int:
            program_args.extend(["-n", str(timing_iteration)])
        else:
            assert timing_iteration == "converge"
            if self.release == "2006":
                program_args.append("-converge")
            else:
                program_args.append("--converge")
        # Input size
        program_args.extend(["-s", size])
        # Name of the benchmark
        program_args.append(bm_name)
        return JavaBenchmark(
            jvm_args=[],
            program_args=program_args,
            cp=cp,
            wrapper=self.get_wrapper(bm_name),
            companion=self.get_companion(bm_name),
            suite_name=self.name,
            name=name,
            timeout=timeout
        )

    def get_minheap(self, bm: Benchmark) -> int:
        assert isinstance(bm, JavaBenchmark)
        name = bm.name
        if not self.minheap:
            logging.warning(
                "No minheap_value of {} is selected".format(self))
            return __DEFAULT_MINHEAP
        minheap = self.minheap_values[self.minheap]
        if name not in minheap:
            logging.warning(
                "Minheap for {} of {} not set".format(name, self))
            return __DEFAULT_MINHEAP
        return minheap[name]

    def is_passed(self, output: bytes) -> bool:
        return b"PASSED" in output

    def get_wrapper(self, bm_name: str) -> Optional[str]:
        if self.wrapper is None:
            return None
        elif type(self.wrapper) == str:
            return self.wrapper
        elif type(self.wrapper) == dict:
            return self.wrapper.get(bm_name)
        else:
            raise TypeError("wrapper of {} must be either null, "
                            "a string (the same wrapper for all benchmarks), "
                            "or a dictionary (different wrappers for"
                            "differerent benchmarks)".format(self.name))

    def get_companion(self, bm_name: str) -> Optional[str]:
        if self.companion is None:
            return None
        elif type(self.companion) == str:
            return self.companion
        elif type(self.companion) == dict:
            return self.companion.get(bm_name)
        else:
            raise TypeError("companion of {} must be either null, "
                            "a string (the same companion for all benchmarks), "
                            "or a dictionary (different companions for"
                            "differerent benchmarks)".format(self.name))


@register(BenchmarkSuite)
class SPECjbb2015(JavaBenchmarkSuite):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.release: str
        self.release = kwargs["release"]
        if self.release not in ["1.03"]:
            raise ValueError(
                "SPECjbb2015 release {} not recongized".format(self.release))
        self.path: Path
        self.path = Path(kwargs["path"]).resolve()
        self.propsfile = (self.path / ".." / "config" /
                          "specjbb2015.props").resolve()
        if not self.path.exists():
            logging.info("SPECjbb2015 jar {} not found".format(self.path))

    def __str__(self) -> str:
        return "{} SPECjbb2015 {} {}".format(super().__str__(), self.release, self.path)

    def get_benchmark(self, bm_spec: Union[str, Dict[str, Any]]) -> 'JavaBenchmark':
        assert type(bm_spec) is str
        if bm_spec != "composite":
            raise ValueError("Only composite mode is supported for now")

        program_args = [
            "-jar", str(self.path),
            "-p", str(self.propsfile),
            "-m", "COMPOSITE",
            "-skipReport"
        ]
        return JavaBenchmark(
            jvm_args=[],
            program_args=program_args,
            cp=[],
            suite_name=self.name,
            name="composite"
        )

    def get_minheap(self, _bm: Benchmark) -> int:
        return 2048  # SPEC recommends running with minimum 2GB of heap

    def is_passed(self, output: bytes) -> bool:
        # FIXME
        return True


@register(BenchmarkSuite)
class Octane(BenchmarkSuite):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.path: Path
        self.path = Path(kwargs["path"]).resolve()
        if not self.path.exists():
            logging.info("Octane folder {} not found".format(self.path))
        self.wrapper: Path
        self.wrapper = Path(kwargs["wrapper"]).resolve()
        if not self.wrapper.exists():
            logging.info("Octane folder {} not found".format(self.wrapper))
        timing_iteration = parse_timing_iteration(
            kwargs.get("timing_iteration"), "Octane")
        self.timing_iteration: int
        if isinstance(timing_iteration, str):
            raise TypeError(
                "timing_iteration for Octane has to be an integer")
        else:
            self.timing_iteration = timing_iteration
        self.minheap: Optional[str]
        self.minheap = kwargs.get("minheap")
        self.minheap_values: Dict[str, Dict[str, int]]
        self.minheap_values = kwargs.get("minheap_values", {})
        if not isinstance(self.minheap_values, dict):
            raise TypeError(
                "The minheap_values of {} should be a dictionary".format(self.name))
        if self.minheap:
            if not isinstance(self.minheap, str):
                raise TypeError(
                    "The minheap of {} should be a string that selects from a minheap_values".format(self.name))
            if self.minheap not in self.minheap_values:
                raise KeyError(
                    "{} is not a valid entry of {}.minheap_values".format(self.name, self.name))
        self.timeout: Optional[int]
        self.timeout = kwargs.get("timeout")

    def __str__(self) -> str:
        return "{} Octane {}".format(super().__str__(), self.path)

    def get_benchmark(self, bm_spec: Union[str, Dict[str, Any]]) -> 'JavaScriptBenchmark':
        assert type(bm_spec) is str

        program_args = [
            str(self.path),
            bm_spec,
            str(self.timing_iteration)
        ]
        return JavaScriptBenchmark(
            js_args=[],
            program=str(self.wrapper),
            program_args=program_args,
            suite_name=self.name,
            name=bm_spec,
            timeout=self.timeout
        )

    def get_minheap(self, bm: Benchmark) -> int:
        assert isinstance(bm, JavaScriptBenchmark)
        name = bm.name
        if not self.minheap:
            logging.warning(
                "No minheap_value of {} is selected".format(self))
            return __DEFAULT_MINHEAP
        minheap = self.minheap_values[self.minheap]
        if name not in minheap:
            logging.warning(
                "Minheap for {} of {} not set".format(name, self))
            return __DEFAULT_MINHEAP
        return minheap[name]

    def is_passed(self, output: bytes) -> bool:
        return b"PASSED" in output


@register(BenchmarkSuite)
class OCamlBenchmarkSuite(BenchmarkSuite):
    def __init__(self, programs: Dict[str, Dict[str, str]], **kwargs):
        super().__init__(**kwargs)
        always_build_default_raw = kwargs.get("always_build", False)
        if not isinstance(always_build_default_raw, bool):
            raise TypeError("OCamlBenchmarkSuite always_build must be boolean")
        self.always_build_default = always_build_default_raw
        self.programs: Dict[str, Dict[str, Any]]
        self.programs = {}
        for k, v in programs.items():
            build_env_raw = v.get("build_env", {})
            if not isinstance(build_env_raw, dict):
                raise TypeError(
                    "OCaml benchmark {} build_env must be a dictionary".format(k))
            always_build_raw = v.get("always_build", self.always_build_default)
            if not isinstance(always_build_raw, bool):
                raise TypeError(
                    "OCaml benchmark {} always_build must be boolean".format(k))
            entry: Dict[str, Any] = {
                "path": str(Path(os.path.expandvars(v["path"]))),
                "ocaml_args": split_quoted(v.get("ocaml_args", "")),
                "args": split_quoted(os.path.expandvars(v.get("args", ""))),
                "build_args": split_quoted(v.get("build_args", "")),
                "build_env": {str(env_k): str(env_v) for env_k, env_v in build_env_raw.items()},
                "always_build": always_build_raw,
            }
            if "build_script" in v:
                benchmark_root = Path(os.path.expandvars(v["path"])).resolve()
                if benchmark_root.is_file():
                    benchmark_root = benchmark_root.parent
                build_script = Path(v["build_script"])
                if not build_script.is_absolute():
                    build_script = benchmark_root / build_script
                entry["build_script"] = str(build_script.resolve())
            if "binary" in v:
                entry["binary"] = v["binary"]
            self.programs[k] = entry
        self.timeout = kwargs.get("timeout")

    def get_benchmark(self, bm_spec: Union[str, Dict[str, Any]]) -> 'OCamlBenchmark':
        timeout = self.timeout
        if type(bm_spec) is str:
            bm_name = bm_spec
            name = bm_spec
        else:
            assert type(bm_spec) is dict
            if "bm_name" not in bm_spec or "name" not in bm_spec:
                raise KeyError(
                    "When a dictionary is used to specify an OCaml benchmark, you need to provide both `name` and `bm_name`")
            bm_name = bm_spec["bm_name"]
            name = bm_spec["name"]
            if "timeout" in bm_spec:
                timeout = bm_spec["timeout"]
        p = self.programs[bm_name]
        program_path = Path(p["path"]).resolve()
        use_build_mode = (
            bool(p.get("build_script")) or
            bool(p.get("binary")) or
            bool(p.get("always_build")) or
            program_path.is_dir()
        )
        if use_build_mode:
            build_script = p.get("build_script")
            return OCamlBuiltBinaryBenchmark(
                benchmark_name=bm_name,
                benchmark_dir=Path(p["path"]).resolve(),
                build_script=Path(build_script).resolve() if build_script else None,
                binary=p.get("binary"),
                program_args=p["args"],
                build_args=p["build_args"],
                build_env=p["build_env"],
                always_build=p["always_build"],
                suite_name=self.name,
                name=name,
                timeout=timeout
            )
        return OCamlBenchmark(
            ocaml_args=p["ocaml_args"],
            program=p["path"],
            program_args=p["args"],
            suite_name=self.name,
            name=name,
            timeout=timeout
        )

    def get_minheap(self, bm: Benchmark) -> int:
        assert isinstance(bm, (OCamlBenchmark, OCamlBuiltBinaryBenchmark))
        logging.warning(
            "minheap is not supported for OCamlBenchmarkSuite; use runbms sweeps with OCaml-specific modifiers")
        return 0

    def is_passed(self, _output: bytes) -> bool:
        return True


@register(BenchmarkSuite)
class OCamlMulticoreBenchmarkSuite(OCamlBenchmarkSuite):
    """Like OCamlBenchmarkSuite but enforces OCaml >= 5 at build/run time."""

    def get_benchmark(self, bm_spec: Union[str, Dict[str, Any]]) -> 'OCamlBenchmark':
        bm = super().get_benchmark(bm_spec)
        if isinstance(bm, OCamlBuiltBinaryBenchmark):
            bm.min_ocaml_major = 5
        return bm


@register(BenchmarkSuite)
class OCamlOxcamlBenchmarkSuite(OCamlMulticoreBenchmarkSuite):
    """Like OCamlMulticoreBenchmarkSuite but requires an OxCaml runtime.

    Benchmarks in this suite use OxCaml-specific APIs (e.g. Domain.Safe,
    prefetch intrinsics) that are not available in stock OCaml. Builds will
    fail with a clear error if the runtime is not 'type: OxCaml'.
    """

    def get_benchmark(self, bm_spec: Union[str, Dict[str, Any]]) -> 'OCamlBenchmark':
        bm = super().get_benchmark(bm_spec)
        if isinstance(bm, OCamlBuiltBinaryBenchmark):
            bm.required_runtime_hint = (
                "an OxCaml runtime (uses OxCaml-specific APIs like Domain.Safe)"
            )
        return bm


@register(BenchmarkSuite)
class SPECjvm98(JavaBenchmarkSuite):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.release: str
        self.release = kwargs["release"]
        if self.release not in ["1.03_05"]:
            raise ValueError(
                "SPECjvm98 release {} not recongized".format(self.release))
        self.path: Path
        self.path = Path(kwargs["path"]).resolve()

        if not self.path.exists():
            logging.info("SPECjvm98 {} not found".format(self.path))
        if not (self.path / "SpecApplication.class").exists():
            logging.info(
                "SpecApplication.class not found under SPECjvm98 {}".format(self.path))
        timing_iteration = parse_timing_iteration(
            kwargs.get("timing_iteration"), "SPECjvm98")
        self.timing_iteration: int
        if isinstance(timing_iteration, str):
            raise TypeError(
                "timing_iteration for SPECjvm98 has to be an integer")
        else:
            self.timing_iteration = timing_iteration

    def __str__(self) -> str:
        return "{} SPECjvm98 {} {}".format(super().__str__(), self.release, self.path)

    def get_benchmark(self, bm_spec: Union[str, Dict[str, Any]]) -> 'JavaBenchmark':
        assert type(bm_spec) is str
        program_args = [
            "SpecApplication",
            "-i{}".format(self.timing_iteration),
            bm_spec
        ]
        return JavaBenchmark(
            jvm_args=[],
            program_args=program_args,
            cp=[str(self.path)],
            suite_name=self.name,
            name=bm_spec,
            override_cwd=self.path
        )

    def get_minheap(self, _bm: Benchmark) -> int:
        # FIXME allow user to measure and specify minimum heap sizes
        return 32  # SPEC recommends running with minimum 32MB of heap

    def is_passed(self, output: bytes) -> bool:
        # FIXME
        return b"**NOT VALID**" not in output

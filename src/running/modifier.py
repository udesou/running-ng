from typing import Any, Dict, List, Optional, TYPE_CHECKING
from running.util import register, smart_quote, split_quoted, parse_modifier_strs
import copy
import logging
from running import osinfo
if TYPE_CHECKING:
    from running.config import Configuration


class Modifier(object):
    CLS_MAPPING: Dict[str, Any]
    CLS_MAPPING = {}

    def __init__(self, value_opts=None, **kwargs):
        self.name = kwargs["name"]
        self.value_opts = value_opts
        if "-" in self.name:
            raise ValueError(
                "Modifier {} has - in its name. - is reserved for value options.".format(self.name))
        self.__original_kwargs = kwargs
        self._kwargs = copy.deepcopy(kwargs)
        self.excludes = kwargs.get("excludes", {})
        # When non-empty, the modifier applies only to the programs listed.
        # Use includes or excludes, not both: a suite in excludes drops every
        # program of it, so combining them removes more than the names suggest.
        self.includes = kwargs.get("includes", {})
        if self.value_opts:  # Neither None nor empty
            for k, v in kwargs.items():
                if type(v) is not str:
                    continue
                try:
                    self._kwargs[k] = v.format(*value_opts)
                except IndexError:
                    pass

    @staticmethod
    def from_config(name: str, config: Dict[str, str]) -> Any:
        return Modifier.CLS_MAPPING[config["type"]](name=name, **config)

    def apply_value_opts(self, value_opts):
        return type(self)(value_opts=value_opts, **self.__original_kwargs)

    def __str__(self) -> str:
        return "Modifier {}".format(self.name)


@register(Modifier)
class ModifierSet(Modifier):
    def __init__(self, value_opts=None, **kwargs):
        super().__init__(value_opts, **kwargs)
        self.val = self._kwargs["val"].split("|")

    def flatten(self, configuration: 'Configuration') -> List[Modifier]:
        return parse_modifier_strs(configuration, self.val)

    def __str__(self) -> str:
        return "{} ModifierSet {}".format(super().__str__(), "|".join(self.val))


@register(Modifier)
class JVMArg(Modifier):
    def __init__(self, value_opts=None, **kwargs):
        super().__init__(value_opts, **kwargs)
        self.val = split_quoted(self._kwargs["val"])

    def __str__(self) -> str:
        return "{} JVMArg {}".format(super().__str__(), self.val)


@register(Modifier)
class JVMClasspathAppend(Modifier):
    def __init__(self, value_opts=None, **kwargs):
        super().__init__(value_opts, **kwargs)
        self.val = split_quoted(self._kwargs["val"])

    def __str__(self) -> str:
        return "{} JVMClasspathAppend {}".format(super().__str__(), self.val)


@register(Modifier)
class JVMClasspath(JVMClasspathAppend):
    # backward compatibility
    pass


@register(Modifier)
class JVMClasspathPrepend(Modifier):
    def __init__(self, value_opts=None, **kwargs):
        super().__init__(value_opts, **kwargs)
        self.val = split_quoted(self._kwargs["val"])

    def __str__(self) -> str:
        return "{} JVMClasspathPrepend {}".format(super().__str__(), self.val)


@register(Modifier)
class EnvVar(Modifier):
    def __init__(self, value_opts=None, **kwargs):
        super().__init__(value_opts, **kwargs)
        if "var" not in self._kwargs:
            raise ValueError(
                "Please specify the name of the environment variable for modifier {}".format(self.name))
        if "val" not in self._kwargs:
            raise ValueError(
                "Please specify the value for the environment variable for modifier {}".format(self.name))
        self.var = self._kwargs["var"]
        self.val = self._kwargs["val"]

    def __str__(self) -> str:
        return "{} EnvVar {}={}".format(super().__str__(), self.var, smart_quote(self.val))


@register(Modifier)
class ProgramArg(Modifier):
    def __init__(self, value_opts=None, **kwargs):
        super().__init__(value_opts, **kwargs)
        self.val = split_quoted(self._kwargs["val"])

    def __str__(self) -> str:
        return "{} ProgramArg {}".format(super().__str__(), self.val)


@register(Modifier)
class Wrapper(Modifier):
    def __init__(self, value_opts=None, **kwargs):
        super().__init__(value_opts, **kwargs)
        self.val = split_quoted(self._kwargs["val"])

    def __str__(self) -> str:
        return "{} Wrapper {}".format(super().__str__(), self.val)


@register(Modifier)
class JSArg(Modifier):
    def __init__(self, value_opts=None, **kwargs):
        super().__init__(value_opts, **kwargs)
        self.val = split_quoted(self._kwargs["val"])

    def __str__(self) -> str:
        return "{} JSArg {}".format(super().__str__(), self.val)


@register(Modifier)
class OCamlArg(Modifier):
    def __init__(self, value_opts=None, **kwargs):
        super().__init__(value_opts, **kwargs)
        self.val = split_quoted(self._kwargs["val"])

    def __str__(self) -> str:
        return "{} OCamlArg {}".format(super().__str__(), self.val)


@register(Modifier)
class OCamlRunParam(Modifier):
    def __init__(self, value_opts=None, **kwargs):
        super().__init__(value_opts, **kwargs)
        self.val = self._kwargs["val"]

    def __str__(self) -> str:
        return "{} OCamlRunParam {}".format(super().__str__(), self.val)


@register(Modifier)
class Companion(Modifier):
    def __init__(self, value_opts=None, **kwargs):
        super().__init__(value_opts, **kwargs)
        self.val = split_quoted(self._kwargs["val"])

    def __str__(self) -> str:
        return "{} Companion {}".format(super().__str__(), self.val)


@register(Modifier)
class PerfAndOllyAttach(Modifier):
    """Attach perf stat and olly gc-stats to the benchmark process.

    The child is frozen with SIGSTOP after fork so both tools attach before any
    code runs. Requires olly on PATH and perf installed.

    `val`: perf stat -e events, e.g. "cycles,instructions".
    `val_freebsd`: the same list in hwpmc's vocabulary, used instead of `val` on
    FreeBSD (task-clock has no hwpmc equivalent; stall/cache groups are only
    approximations of the Linux ones).
    """
    def __init__(self, value_opts=None, **kwargs):
        super().__init__(value_opts, **kwargs)
        val = self._kwargs.get("val", "")
        if osinfo.IS_FREEBSD and self._kwargs.get("val_freebsd"):
            val = self._kwargs["val_freebsd"]
        self.perf_events: list = split_quoted(val) if val else []

    def __str__(self) -> str:
        return "{} PerfAndOllyAttach events={}".format(super().__str__(), self.perf_events)


@register(Modifier)
class MemtraceAttach(Modifier):
    """Enable memtrace allocation tracing via the MEMTRACE env var.

    memtrace cannot attach to a running process: the binary must call
    `Memtrace.trace_if_requested ()` itself.
    `val`: MEMTRACE_RATE sampling rate (memtrace's default is 1e-6; traces are
    per invocation, so higher rates need disk budget).
    """
    def __init__(self, value_opts=None, **kwargs):
        super().__init__(value_opts, **kwargs)
        self.rate = self._kwargs.get("val")

    def __str__(self) -> str:
        return "{} MemtraceAttach rate={}".format(super().__str__(), self.rate)


@register(Modifier)
class CpuPin(Modifier):
    """Pin the benchmark to one hardware thread per physical core, with the
    CPU list derived from the running machine (SMT sibling numbering differs
    between Linux and FreeBSD, so a hand-written taskset mask is not portable).

    `val`: physical cores handed to olly/perf instead of the benchmark
    (default 0: observers land on the benchmark's SMT siblings). Changing it
    mid-sweep changes what is measured.
    `one_node`: confine the benchmark to one NUMA node, observers on the
    others; a no-op on single-node machines.
    `bench_cores`: how many benchmark CPUs to use (default all). Use 1 for
    single-threaded benchmarks: the scheduler does not balance among isolcpus
    CPUs, so a smaller set makes placement deterministic. Unpinned
    multi-domain benchmarks must be excluded: on an isolated set every domain
    is confined to one CPU.

    A no-op where the OS cannot pin (macOS).
    """

    def __init__(self, value_opts=None, **kwargs):
        super().__init__(value_opts, **kwargs)
        raw = self._kwargs.get("val", 0)
        try:
            self.reserved_cores = int(raw) if raw not in (None, "") else 0
        except (TypeError, ValueError):
            raise ValueError(
                "CpuPin modifier {}: val must be a whole number of reserved "
                "cores, got {!r}".format(self.name, raw))
        # YAML may hand this over as a bool or a string, depending on quoting.
        node_raw = self._kwargs.get("one_node", False)
        if isinstance(node_raw, str):
            node_raw = node_raw.strip().lower()
            if node_raw not in ("true", "false", "yes", "no", "1", "0", ""):
                raise ValueError(
                    "CpuPin modifier {}: one_node must be a boolean, got "
                    "{!r}".format(self.name, self._kwargs.get("one_node")))
            self.one_node = node_raw in ("true", "yes", "1")
        else:
            self.one_node = bool(node_raw)
        cores_raw = self._kwargs.get("bench_cores", None)
        if cores_raw in (None, "", "all"):
            self.bench_cores: Optional[int] = None
        else:
            try:
                self.bench_cores = int(cores_raw)
            except (TypeError, ValueError):
                raise ValueError(
                    "CpuPin modifier {}: bench_cores must be a whole number "
                    "of cores or \"all\", got {!r}".format(self.name, cores_raw))
            if self.bench_cores < 1:
                raise ValueError(
                    "CpuPin modifier {}: bench_cores must be >= 1, got "
                    "{!r}".format(self.name, cores_raw))
        self.benchmark_cpus, self.observer_cpus = osinfo.partition_cpus(
            self.reserved_cores, one_node=self.one_node)
        if self.bench_cores is not None and self.benchmark_cpus:
            if self.bench_cores > len(self.benchmark_cpus):
                logging.warning(
                    "CpuPin modifier %s asks for %d cores but only %d are "
                    "available for the benchmark; using all of them",
                    self.name, self.bench_cores, len(self.benchmark_cpus))
            # Unused benchmark cores stay idle (not given to observers) so the
            # isolation the pinning creates is kept. Take from the far end:
            # without isolcpus the front of the list is CPU 0, the busiest core.
            self.benchmark_cpus = self.benchmark_cpus[-self.bench_cores:]
        self.val = osinfo.pin_command(self.benchmark_cpus)
        if not self.val:
            logging.warning(
                "CpuPin modifier %s cannot pin on %s; the benchmark will run "
                "unpinned", self.name, osinfo.SYSTEM)

    def __str__(self) -> str:
        return ("{} CpuPin cpus={} reserved_cores={} one_node={} "
                "bench_cores={}").format(
            super().__str__(),
            osinfo.format_cpu_list(self.benchmark_cpus) or "none",
            self.reserved_cores, self.one_node,
            "all" if self.bench_cores is None else self.bench_cores)

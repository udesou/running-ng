from typing import Any, Dict, List, TYPE_CHECKING
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
        if self.value_opts:  # Neither None nor empty
            # Expand value opts
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
    """Attach both perf stat and olly gc-stats to the benchmark process.

    Uses SIGSTOP/SIGCONT to freeze the child after fork so both tools can
    attach before any code runs. Requires olly on PATH and perf installed.

    Optional `val`: extra perf stat -e events string, e.g. "cycles,instructions".
    """
    def __init__(self, value_opts=None, **kwargs):
        super().__init__(value_opts, **kwargs)
        val = self._kwargs.get("val", "")
        self.perf_events: list = split_quoted(val) if val else []

    def __str__(self) -> str:
        return "{} PerfAndOllyAttach events={}".format(super().__str__(), self.perf_events)


@register(Modifier)
class MemtraceAttach(Modifier):
    """Enable memtrace allocation tracing for the benchmark process.

    Unlike PerfAndOllyAttach, memtrace has no attach-to-running-process
    path: tracing only starts if the benchmark's own binary calls
    `Memtrace.trace_if_requested ()` at startup (linked against the
    memtrace library), so this modifier only needs to set env vars —
    the benchmark reads MEMTRACE (output path) on its own.

    Optional `val`: MEMTRACE_RATE sampling-rate override (proportion of
    allocated words sampled).  memtrace's own default is **1e-6**
    (`default_sampling_rate` in memtrace's src/memtrace.ml), so a rate is a
    multiplier on a very sparse baseline: on test_decompress, the default
    yields ~600 samples per invocation while `val: "0.001"` yields ~590,000
    (a ~950x increase, and a 6.7 MB raw trace for a ~1.7 s run).  Budget disk
    accordingly — traces are per-invocation, not per-config.
    """
    def __init__(self, value_opts=None, **kwargs):
        super().__init__(value_opts, **kwargs)
        self.rate = self._kwargs.get("val")

    def __str__(self) -> str:
        return "{} MemtraceAttach rate={}".format(super().__str__(), self.rate)


@register(Modifier)
class CpuPin(Modifier):
    """Confine the benchmark to one hardware thread per physical core.

    The portable replacement for a hand-written `taskset -c 0-15` Wrapper.
    That mask is correct only on the machine it was measured on: the *policy*
    ("one thread per physical core") is stable, but the CPU numbers realising
    it are not.  On one Ryzen 9 9950X, Linux enumerates SMT siblings as
    (0,16),(1,17)... so the policy is 0-15, while FreeBSD on the same silicon
    typically enumerates (0,1),(2,3)... so it is 0,2,4,...,30.  So the list is
    derived from the running machine instead of written down.

    Optional `val`: whole physical cores to hand to olly and the counter tool
    instead of the benchmark.  Default 0, which reproduces the historical
    behaviour exactly: the benchmark gets every physical core and the
    observers land on its SMT siblings.  Raising it improves isolation but
    takes cores away from the benchmark, so it changes what is being measured;
    do not change it partway through a sweep meant to be comparable.

    Contributes nothing where the OS cannot pin (macOS), so a config carrying
    it stays portable rather than failing.
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
        self.benchmark_cpus, self.observer_cpus = osinfo.partition_cpus(
            self.reserved_cores)
        self.val = osinfo.pin_command(self.benchmark_cpus)
        if not self.val:
            logging.warning(
                "CpuPin modifier %s cannot pin on %s; the benchmark will run "
                "unpinned", self.name, osinfo.SYSTEM)

    def __str__(self) -> str:
        return "{} CpuPin cpus={} reserved_cores={}".format(
            super().__str__(),
            osinfo.format_cpu_list(self.benchmark_cpus) or "none",
            self.reserved_cores)

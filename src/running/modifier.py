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
        # Narrows the scope: when non-empty the modifier applies ONLY to the
        # programs listed.  Use one or the other -- naming a suite in excludes
        # drops the modifier for every program of it, so combining them removes
        # more than the names suggest (tests/test_modifier.py).  For a modifier that
        # belongs to one or two benchmarks, listing those is the whole scope;
        # spelling the same thing as an exclude list means naming every other
        # benchmark and keeping that list current forever.  macro_base.yml's
        # non_lavyek_excludes shows how that ends: 19 suites, 29 programs, and
        # 18 of those suites are missing programs added since.
        self.includes = kwargs.get("includes", {})
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

    Optional `one_node`: confine the benchmark to a single NUMA node and give
    the others to the observers.  On a multi-socket machine this is usually
    the better trade than `val`, because the benchmark keeps a whole node's
    cores with no SMT contention at all instead of giving cores up, and its
    memory traffic stops crossing the interconnect.  Exactly a no-op on a
    single-node machine, so it is safe to leave on in a shared config.

    Optional `bench_cores`: how many of the benchmark's CPUs this benchmark may
    actually use, default all of them.  Set it to 1 for a single-threaded
    benchmark: it only ever needs one core, and confining it to one makes which
    core deterministic across runs.  That matters most where the reserved set
    is isolated (Linux `isolcpus=`), because the scheduler does not balance
    among isolated CPUs -- a single-threaded benchmark handed six of them lands
    on whichever one it is first placed on, so the extra five buy nothing and
    only make the placement vary.  Measured on an 8-core Xeon, sedlex_tokenize
    over six runs: unpinned on the housekeeping cores 51.46s mean with a 25.8s
    spread, pinned to one isolated core 46.47s mean with a 0.33s spread.

    A multi-domain benchmark wants the whole set, but only if it pins its own
    domains the way lavyek_bench.ml does.  Handing an unpinned multi-domain
    benchmark an isolated set confines every domain to one CPU: infer went from
    168% CPU on two housekeeping cores to 100% on six isolated ones, 72%
    slower.  Leave such benchmarks out of the modifier until they self-pin.

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
        # YAML may hand this over as a bool or as a string, depending on how
        # it was quoted.
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
            # The cores this benchmark does not take are left IDLE rather than
            # given to the observers.  They are the set reserved for benchmark
            # work -- on a machine using isolcpus they are the isolated ones --
            # and putting olly on them would undo the separation the pinning
            # exists to create.
            #
            # Taken from the far end of the set: where the machine declares
            # nothing (no isolcpus, no irqaffinity) the whole list is handed to
            # the benchmark and the front of it is CPU 0, which is the busiest
            # core on most machines.  Where it does declare a split this makes
            # no difference to quality, and it keeps the benchmark as far from
            # the housekeeping cores as the machine allows.
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

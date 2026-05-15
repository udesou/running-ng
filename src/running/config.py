from typing import Any, Dict
import yaml
from running.suite import BenchmarkSuite
from running.runtime import Runtime
from running.modifier import Modifier
from pathlib import Path
import functools
import copy
import logging
import os


def load_class(cls, config):
    return {k: cls.from_config(k, v) for (k, v) in config.items()}


KEY_CLASS_MAPPING = {
    "suites": BenchmarkSuite,
    "modifiers": Modifier,
    "runtimes": Runtime
}


class Configuration(object):
    def __init__(self, kv_pairs: Dict[str, Any]):
        assert "includes" not in kv_pairs
        assert "overrides" not in kv_pairs
        self.__items = kv_pairs

    def save_to_file(self, fd):
        yaml.dump(self.__items, fd)

    def resolve_class(self):
        """Resolve the values by instantiating instances of classes

        For example, self.values["suites"] is a Dict[str, Dict[str, str]],
        where in the inner dictionary contains the string representation of a
        benchmark suite.
        After this function returns, self.values["suites"] becomes a
        Dict[str, BenchmarkSuite].

        Change the KEY_CLASS_MAPPING to change which classes get resolved.
        """
        configs_list = self.__items.get("configs")
        if configs_list is not None:
            used_runtimes = {
                c.split('|')[0].strip()
                for c in configs_list
                if isinstance(c, str)
            }
        else:
            used_runtimes = None

        for cls_name, cls in KEY_CLASS_MAPPING.items():
            if cls_name in self.__items:
                if cls_name == "runtimes" and used_runtimes is not None:
                    self.__items[cls_name] = {
                        k: cls.from_config(k, v)
                        for k, v in self.__items[cls_name].items()
                        if k in used_runtimes
                    }
                else:
                    self.__items[cls_name] = load_class(
                        cls, self.__items[cls_name])
        if "benchmarks" in self.__items:
            for suite_name, bms in self.__items["benchmarks"].items():
                suite = self.__items["suites"][suite_name]
                benchmarks = []
                for b in bms:
                    benchmarks.append(suite.get_benchmark(b))
                self.__items["benchmarks"][suite_name] = benchmarks

    def get(self, name: str) -> Any:
        return self.__items.get(name)

    def validate_tags(self) -> None:
        """Cross-check the ``tags:`` block — every ``(suite, program)`` listed
        under any tag's ``exercised_by:`` or ``cold:`` must exist in
        ``suites:``.  Each tag entry must either name at least one
        ``exercised_by:`` program *or* carry a ``gap:`` note (so an empty
        tag is intentional, not a typo).

        Errors here mean the tags block has fallen out of sync with the
        suite definitions — typo, program rename, or new gap that wasn't
        annotated.  This method is a no-op when no ``tags:`` block is
        present.

        Must be called before :meth:`resolve_class` for the same reason
        :meth:`validate` is — the suite definitions are still raw dicts
        at that point.
        """
        tags = self.__items.get("tags") or {}
        if not tags:
            return
        suites = self.__items.get("suites") or {}

        errors: list = []
        for tag_name, tag_entry in tags.items():
            if not isinstance(tag_entry, dict):
                errors.append(
                    f"tag `{tag_name}` must be a mapping; got "
                    f"{type(tag_entry).__name__}"
                )
                continue
            for key in ("exercised_by", "cold"):
                ref = tag_entry.get(key) or {}
                if not isinstance(ref, dict):
                    errors.append(
                        f"tag `{tag_name}`.{key} must be a mapping of "
                        f"suite -> [programs]; got {type(ref).__name__}"
                    )
                    continue
                for suite_name, programs in ref.items():
                    if suite_name not in suites:
                        errors.append(
                            f"tag `{tag_name}`.{key} references unknown "
                            f"suite `{suite_name}`"
                        )
                        continue
                    suite_programs = (suites[suite_name] or {}).get("programs") or {}
                    if not isinstance(programs, list):
                        errors.append(
                            f"tag `{tag_name}`.{key}.{suite_name} must be "
                            f"a list of program names"
                        )
                        continue
                    for prog in programs:
                        if prog not in suite_programs:
                            errors.append(
                                f"tag `{tag_name}`.{key} references unknown "
                                f"program `{prog}` in suite `{suite_name}`"
                            )
            exercised = tag_entry.get("exercised_by") or {}
            gap = tag_entry.get("gap")
            if not exercised and not gap:
                errors.append(
                    f"tag `{tag_name}` has no `exercised_by:` programs and "
                    f"no `gap:` note. Add a `gap:` field if no benchmark "
                    f"exercises this tag (so the gap is documented), or "
                    f"populate `exercised_by:`."
                )

        if errors:
            raise ValueError(
                "Tag validation failed:\n  - " + "\n  - ".join(errors)
            )

    def apply_tag_filter(self, tag_names: list) -> None:
        """Restrict ``benchmarks:`` to the union of programs listed under
        the named tag(s) in the ``tags:`` block.  Intended to be driven
        by the ``RUNNING_TAG`` environment variable; comma-separated
        names are union'd.

        Semantics:

        * **Union across tags.**  A program is kept if it appears under
          ``exercised_by:`` of *any* named tag.
        * **Intersection with existing ``benchmarks:``.**  The filter
          never re-enables a program that is already excluded — if
          ``benchmarks.<suite>`` is ``[]`` (e.g. ``macro-merlin`` is
          disabled), no tag will revive it.  This matters for benches
          we keep tag-listed but disable for reasons unrelated to the
          runtime feature they exercise (upstream race, parked, etc.).
        * **``cold:`` is ignored** for filtering — it's documentation
          only, used by :meth:`validate_tags` to track presence-but-
          cold uses.

        Raises ``ValueError`` if:

        * the configuration has no ``tags:`` block;
        * any of ``tag_names`` is not defined under ``tags:``;
        * the filter result is empty across every suite (catches typos
          in tag names, all-gap tag sets, and cases where every tagged
          benchmark happens to be disabled in ``benchmarks:``).
        """
        tags = self.__items.get("tags")
        if not tags:
            raise ValueError(
                "RUNNING_TAG is set but the configuration has no `tags:` "
                "block.  Make sure you include a config that defines tags "
                "(e.g. base/ocaml/macro_base.yml)."
            )

        unknown = [t for t in tag_names if t not in tags]
        if unknown:
            available = ", ".join(sorted(tags.keys())) or "(none)"
            raise ValueError(
                "Unknown tag(s) in RUNNING_TAG: {}. Available tags: {}.".format(
                    unknown, available
                )
            )

        selected: dict = {}
        for t in tag_names:
            exercised = (tags[t] or {}).get("exercised_by") or {}
            for suite, programs in exercised.items():
                selected.setdefault(suite, set()).update(programs)

        existing = self.__items.get("benchmarks") or {}
        filtered: dict = {}
        for suite, programs in existing.items():
            wanted = selected.get(suite, set())
            filtered[suite] = [p for p in programs if p in wanted]
        self.__items["benchmarks"] = filtered

        total_kept = sum(len(v) for v in filtered.values())
        if total_kept == 0:
            gap_tags = [t for t in tag_names if not ((tags[t] or {}).get("exercised_by") or {})]
            if gap_tags == list(tag_names):
                raise ValueError(
                    "After applying RUNNING_TAG={!r}, no benchmarks remain: "
                    "all named tags are coverage gaps (`exercised_by:` is "
                    "empty).  See `gap:` notes in the tags block.".format(
                        tag_names
                    )
                )
            raise ValueError(
                "After applying RUNNING_TAG={!r}, no benchmarks remain.  "
                "Either the tag(s) reference programs that are excluded in "
                "the `benchmarks:` block, or the named tag(s) include "
                "coverage gaps with empty `exercised_by:`.".format(tag_names)
            )

        logging.info(
            "RUNNING_TAG=%s applied: kept %d program(s) across %d suite(s)",
            ",".join(tag_names),
            total_kept,
            sum(1 for v in filtered.values() if v),
        )

    def validate(self) -> None:
        """Cross-check ``runtimes:`` / ``configs:`` / ``comparisons:`` consistency.

        Raises ``ValueError`` if any of the following conditions hold:

        Structural problems in ``comparisons:``:
          * Block missing ``a`` or ``b`` keys.
          * Unknown ``mode`` (must be ``pairwise`` or ``cartesian``).
          * Pairwise length mismatch where neither side is a scalar.
          * Non-string entries in ``a`` / ``b`` lists.

        Cross-block consistency:
          * Runtime referenced by ``configs:`` but not declared in ``runtimes:``.
          * Runtime referenced by ``comparisons:`` but not declared in ``runtimes:``.
          * Runtime declared in ``runtimes:`` but not referenced by any
            ``configs:`` entry (dead declaration).
          * (When ``comparisons:`` is present) Runtime referenced by a
            comparison but not in any ``configs:`` entry — would produce
            no data.
          * (When ``comparisons:`` is present) Runtime in ``configs:`` not
            referenced by any comparison block — data would be collected
            but never rendered.

        Must be called *before* :meth:`resolve_class`, which filters
        ``runtimes`` to only those referenced by ``configs`` — calling
        after would mask the "declared but unused" check.
        """
        runtimes = self.__items.get("runtimes") or {}
        configs = self.__items.get("configs") or []
        comparisons = self.__items.get("comparisons") or []

        declared_runtimes = set(runtimes.keys())

        config_runtimes = set()
        for c in configs:
            if isinstance(c, str):
                config_runtimes.add(c.split('|')[0].strip())

        comparison_runtimes = set()
        errors: list = []

        for i, block in enumerate(comparisons):
            if not isinstance(block, dict):
                errors.append(
                    f"comparisons[{i}] must be a mapping; got {type(block).__name__}: {block!r}"
                )
                continue
            if "a" not in block or "b" not in block:
                errors.append(f"comparisons[{i}] missing 'a' or 'b': {block!r}")
                continue
            mode = block.get("mode", "pairwise")
            if mode not in ("pairwise", "cartesian"):
                errors.append(
                    f"comparisons[{i}] has unknown mode {mode!r}; "
                    f"expected 'pairwise' or 'cartesian'"
                )
            a, b = block["a"], block["b"]
            for side, v in (("a", a), ("b", b)):
                if isinstance(v, str):
                    comparison_runtimes.add(v)
                elif isinstance(v, list):
                    for x in v:
                        if not isinstance(x, str):
                            errors.append(
                                f"comparisons[{i}].{side} contains a non-string entry: {x!r}"
                            )
                        else:
                            comparison_runtimes.add(x)
                else:
                    errors.append(
                        f"comparisons[{i}].{side} must be a string or a list of strings; "
                        f"got {type(v).__name__}: {v!r}"
                    )
            if mode == "pairwise":
                la = 1 if isinstance(a, str) else (len(a) if isinstance(a, list) else 0)
                lb = 1 if isinstance(b, str) else (len(b) if isinstance(b, list) else 0)
                if la > 1 and lb > 1 and la != lb:
                    errors.append(
                        f"comparisons[{i}] pairwise lengths don't match: "
                        f"len(a)={la}, len(b)={lb}. Set `mode: cartesian` "
                        f"if you wanted the cross product."
                    )

        config_undeclared = config_runtimes - declared_runtimes
        comparison_undeclared = comparison_runtimes - declared_runtimes
        declared_unused = declared_runtimes - config_runtimes

        if config_undeclared:
            errors.append(
                f"Runtimes referenced in `configs:` but not declared in "
                f"`runtimes:` (typo?): {sorted(config_undeclared)}"
            )
        if comparison_undeclared:
            errors.append(
                f"Runtimes referenced in `comparisons:` but not declared in "
                f"`runtimes:` (typo?): {sorted(comparison_undeclared)}"
            )
        if declared_unused:
            errors.append(
                f"Runtimes declared in `runtimes:` but not referenced by any "
                f"`configs:` entry (dead declarations): {sorted(declared_unused)}"
            )

        if comparisons:
            comparison_no_data = comparison_runtimes - config_runtimes
            if comparison_no_data:
                errors.append(
                    f"Runtimes in `comparisons:` but not in any `configs:` "
                    f"entry — no data will be produced for these: "
                    f"{sorted(comparison_no_data)}"
                )

            config_uncovered = config_runtimes - comparison_runtimes
            if config_uncovered:
                errors.append(
                    f"Runtimes in `configs:` but never referenced by any "
                    f"`comparisons:` block — data would be collected but not "
                    f"rendered: {sorted(config_uncovered)}"
                )

        if errors:
            raise ValueError(
                "Configuration validation failed:\n  - " + "\n  - ".join(errors)
            )

    def override(self, selector: str, new_value: Any):
        current: Any  # Union[Dict[str, Any], List[Any]]
        current = self.__items
        parts = list(selector.split("."))
        for index, p in enumerate(parts):
            if index == len(parts) - 1:
                if p.isnumeric():
                    current[int(p)] = new_value
                else:
                    current[p] = new_value
            else:
                if p.isnumeric():
                    current = current[int(p)]
                else:
                    current = current[p]

    def combine(self, other: "Configuration") -> "Configuration":
        """Combine top-level items of self.values.

        Arrays are concatenated and dictionaries are updated.
        """
        new_values = copy.deepcopy(self.__items)
        for k, v in other.__items.items():
            if k in new_values:
                if type(new_values[k]) is list:
                    new_values[k].extend(copy.deepcopy(other.__items[k]))
                else:
                    if type(new_values[k]) is not dict:
                        raise TypeError(
                            "Key `{}` has been defined in one of the "
                            "included files, and the value of `{}`, {}, "
                            "is not an array or a dictionary. "
                            "Please use overrides instead.".format(
                                k, k, repr(v)
                            ))
                    new_values[k].update(copy.deepcopy(other.__items[k]))
            else:
                new_values[k] = copy.deepcopy(other.__items[k])
        return Configuration(new_values)

    @staticmethod
    def parse_file(path: Path) -> Any:
        with path.open("r") as fd:
            try:
                config = yaml.safe_load(fd)
                return config
            except yaml.YAMLError as e:
                raise SyntaxError(
                    "Not able to parse the configuration file, {}".format(e))

    @staticmethod
    def from_file(in_folder: Path, p: str) -> "Configuration":
        expand_p = os.path.expandvars(p)
        logging.info("Loading config {}, expanding to {}, relative to {}".format(
            p, expand_p, in_folder))
        path = Path(expand_p)
        if path.is_absolute():
            logging.info("    is absolute")
        else:
            path = in_folder.joinpath(p)
            logging.info("    resolved to {}".format(path))
        if not path.exists():
            raise ValueError(
                "Configuration not found at path '{}'".format(path))
        if not path.is_file():
            raise ValueError(
                "Configuration at path '{}' is not a file".format(path))
        with path.open("r") as fd:
            try:
                config = yaml.safe_load(fd)
            except yaml.YAMLError as e:
                raise SyntaxError(
                    "Not able to parse the configuration file, {}".format(e))
        if config is None:
            raise ValueError("Parsed configuration file is None")
        if "includes" in config:
            includes = [Configuration.from_file(
                path.parent, p) for p in config["includes"]]
            base = functools.reduce(
                lambda left, right: left.combine(right), includes)
            if "overrides" in config:
                for selector, new_value in config["overrides"].items():
                    base.override(selector, new_value)
                del config["overrides"]
            del config["includes"]
            final_config = Configuration(config)
            final_config = base.combine(final_config)
        else:
            if "overrides" in config:
                raise KeyError(
                    'You specified "overrides" but not "includes". This does not make sense.')
            final_config = Configuration(config)
        return final_config

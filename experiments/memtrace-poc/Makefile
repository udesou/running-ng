# memtrace proof-of-concept: runs the macro-decompress benchmark with
# memtrace tracing attached (MemtraceAttach / memtrace_grp1), producing a
# raw .trace and a JSON folded-stack sidecar per invocation.
# See PLAN-MEMTRACE-V2.md for the two-repo integration this demonstrates.

ROOT_DIR := $(abspath ../..)
RUNNING_MACRO_BENCH_DIR := /home/curche/git/macro-benches
OLLY_BIN := /home/curche/git/runtime_events_tools/_build/install/default/bin
CONFIG := $(ROOT_DIR)/src/running/config/experiments/memtrace_poc.yml
LOG_DIR := $(abspath logs)

export RUNNING_MACRO_BENCH_DIR
export PYTHONPATH := $(ROOT_DIR)/src
export PATH := $(OLLY_BIN):$(PATH)

.PHONY: validate run summarize clean

validate:
	cd $(ROOT_DIR) && python3 -c "\
from pathlib import Path; \
from running.config import Configuration; \
c = Configuration.from_file(Path('.'), 'src/running/config/experiments/memtrace_poc.yml'); \
c.validate(); \
c.validate_tags(); \
print('OK - configs:', len(c.get('configs'))); \
[print(' ', x) for x in c.get('configs')]"

run: validate
	mkdir -p $(LOG_DIR)
	cd $(ROOT_DIR) && python3 -m running runbms $(LOG_DIR) $(CONFIG) \
		2>&1 | tee $(LOG_DIR)/run_stdout.log

summarize:
	@latest=$$(ls -dt $(LOG_DIR)/turing-* 2>/dev/null | head -1); \
	if [ -z "$$latest" ]; then echo "No run directory found under $(LOG_DIR)"; exit 1; fi; \
	echo "Summarizing $$latest"; \
	python3 $(ROOT_DIR)/experiments/memtrace-poc/summarize_json.py "$$latest"

clean:
	rm -rf $(LOG_DIR)/turing-* $(LOG_DIR)/run_stdout.log

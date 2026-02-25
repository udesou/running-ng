#!/usr/bin/env python3
import sys
import os
import shutil
import gzip
import re
import glob
import subprocess
import itertools

ADVICE_EXTS = ["ca", "dc", "ec"]
advice_folder = sys.argv[1]


JikesRVM_HEADER = "============================ MMTk Statistics Totals ============================"
JikesRVM_FOOTER = "------------------------------ End MMTk Statistics -----------------------------"


def extract_blocks(lines, header, footer):
    lines = [l.decode("ascii") for l in lines]
    i = 0
    blocks = []
    while i < len(lines):
        if header in lines[i]:
            i += 1
            block = []
            while footer not in lines[i]:
                block.append(lines[i])
                i += 1
            blocks.append(block)
        i += 1
    return blocks


def cleanse(filename):
    sed_pattern = 's/{urls[^}]*}//g'
    if sys.platform == "linux" or sys.platform == "linux2":
        cmd = ["sed", "-i", sed_pattern, filename]
    elif sys.platform == "darwin":
        cmd = ["sed", "-i", "", sed_pattern, filename]
    print("run({})".format(" ".join(cmd)))
    subprocess.run(cmd)


def parse_scenario(filename):
    if filename.endswith(".log.gz"):
        return filename[:-7]
    if filename.endswith(".log"):
        return filename[:-4]
    raise ValueError("Unexpected log filename {}".format(filename))


def get_log_path(scenario):
    gz = os.path.join(advice_folder, "{}.log.gz".format(scenario))
    if os.path.exists(gz):
        return gz
    plain = os.path.join(advice_folder, "{}.log".format(scenario))
    if os.path.exists(plain):
        return plain
    raise FileNotFoundError("Cannot find log file for scenario {}".format(scenario))


def select_best_invocation(scenario):
    filename = get_log_path(scenario)
    metrics = []
    opener = gzip.open if filename.endswith(".gz") else open
    with opener(filename, "rb") as log_file:
        stats_blocks = extract_blocks(
            log_file, JikesRVM_HEADER, JikesRVM_FOOTER)
        for stats_block in stats_blocks:
            stats = dict(zip(stats_block[0].split(
                "\t"), stats_block[1].split("\t")))
            metrics.append(float(stats["time.gc"]) + float(stats["time.mu"]))
    if not metrics:
        print("No metric is found")
        return -1
    print("Metrics for {}: {}".format(scenario, metrics))
    _, idx = min([(val, idx) for (idx, val) in enumerate(metrics)])
    return idx


def select_advice_file(scenario, best_invocation):
    if not best_invocation >= 0:
        return
    benchmark_name = scenario.split(".")[0]
    for ext in ADVICE_EXTS:
        src = "{}.{}.{}".format(scenario, best_invocation, ext)
        src = os.path.join(advice_folder, src)
        dst = "{}.{}".format(benchmark_name, ext)
        dst = os.path.join(advice_folder, dst)
        print("Copying {} to {}".format(src, dst))
        shutil.copyfile(src, dst)
        cleanse(dst)


def main():
    scenario_logs = glob.glob(os.path.join(advice_folder, "*.log"))
    scenario_logs.extend(glob.glob(os.path.join(advice_folder, "*.log.gz")))
    scenarios = sorted(set([parse_scenario(os.path.basename(s))
                            for s in scenario_logs]))
    print("Found scenarios {}".format(scenarios))
    for scenario in scenarios:
        print("Processing scenario {}".format(scenario))
        best_invocation = select_best_invocation(scenario)
        print("Best invocation for scenario {} is {}".format(scenario,
                                                             best_invocation))
        select_advice_file(scenario, best_invocation)


if __name__ == "__main__":
    main()

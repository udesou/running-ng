
Task 1: Benchmarking automatic compaction with transparent huge pages support

Devise a plan for the following:

There is a new compactor mechanism that has been added to OxCaml with a port of it available
for it on OCaml in a fork repo written by Sadiq 
Link: https://github.com/sadiqj/ocaml/tree/new_compactor

In addition, oxcaml also has automatic compaction which was something present in OCaml 4.x but
had to be removed in OCaml 5.  
Refer to the commit: https://github.com/oxcaml/oxcaml/commit/e5b16002d2174d46dc5d15e415738763a4067163

With our running-ng and macro-benches benchmarking framework, I'd like to set it up such that we have
a yaml config that compares all of these against our available benchmarks.


Investigate whether running-ng is able to also output compaction count in the output csv
Add automatic compaction to our compactor and compare against these macro benchmarks
Plot wall times, max rss, and compaction counts between all possible combinations of the available
runtimes

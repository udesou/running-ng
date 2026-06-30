Task: Using dimensionality reduction to create a representative benchmarking suite

In the dacapo benchmark suite for java, the original research paper [1] discussed the
idea of selective representative benchmarks. For the same, they measured the sparsity
of benchmark on their output parameters with Principal Component Analysis. We would 
like to explore the same dimensionality reduction techniques as a way for reducing the
no of benchmarks while preserving all relevant behaviors as before. A representative 
benchmark suite would contain a small subset, be hopefully more maintainable, and exhibit
interesting properties (mainly GC behavior) which can be easily tracked across pull
requests. This would hopefully also be representative of real world programs, thereby
being a new addition of sandmark benchmark suite.

Understand the following running-ng benchmarking harness. There is also a macro-benches
repo available at /home/curche/macro-benches which contains the current set of chosen
benchmarks. Using these and another other information as required, design a strategy
for doing an dimensionality reduction to select a smaller no of benchmarks from the
macro benches. Try to make the setup extensible i.e. we can try out different clustering
techniques like k-means, PCA and other newer literature. An extensible setup should also
be easily updateable to include new features (new observability tooling for eg live object
analysis) and still run smoothly and present us updated results without requiring more 
changes. Similarly, it should also be able to add new benchmarks as input and run the pipeline
to give us updated results on whether the representative benchmarks are modified or not

[1]: https://www.dacapobench.org/assets/pdf/dacapo-oopsla-2006.pdf

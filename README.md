# Blast Radius test selector

Selects and schedules regression tests for SQLGlot changes using reachability measured on the pinned checkout.

## The problem

Your image gets a batch of code changes to [sqlglot](https://github.com/tobymao/sqlglot) at commit `eb11aec4be8cb2122afe713a4816662c3f0fb2fa`. For each change, print which tests to run and how to split them across up to 8 workers.

| | |
| --- | --- |
| `/data/commits/<id>.patch` | One unified diff against the pinned commit. Each stands alone: none builds on another. |
| `/data/tests.json` | Every test, numbered, with its file and the seconds it took us to run. |
| stdout | One JSON line a commit: `{"commit": "k7q2", "workers": [[12, 845, 901], [3, 77]]}` |

Each inner list is one worker's test numbers. `"workers": []` runs nothing, which is the right answer for a change that breaks nothing.

## Scoring

Lower is better. [SCORING.md](SCORING.md) has the exact rules; in short:

- A commit costs the recorded seconds of its busiest worker, plus 2 seconds for every worker.
- A commit where you left out a test that fails adds 300 seconds.
- The CPU seconds your program uses are added. Running sqlglot's tests while you decide is allowed, and you pay for the time.
- Your image runs 3 times and the middle total counts.

"Fails" means pytest reports a failure, an error or a timeout for the test, or the test's file cannot be imported. A test with subtests is one test: one failing subtest fails it.

## Try it

You need Docker, make and Python 3.9 or newer. The kit unpacks into the folder you are in, so unpack it into an empty one.

```sh
make sample   # build, run once on the 30 sample commits under the scorer's limits, and score
make check    # the output's format only
```

`make sample` prints your simulated seconds and misses, which are exact for that output. The board adds the CPU seconds your container used and takes the middle of 3 runs, so expect it a little higher. Your program never sees `sample/answers.json`: only the patches and `tests.json` are mounted.

`sample/answers.json` lists the tests each sample commit breaks and the kind of change it is. The practice and final commits are hidden, and so is everything your program prints when it runs on them. A rejected submission tells you which rule it broke and nothing more, so reproduce problems here.

## Approach

The image installs the exact labeling dependencies and runs the pinned test suite with
`SKIP_INTEGRATION=0` during the build. It records Python callable entry points, fixture
reads, and direct source-file reads. Process-pool tasks carry the submitting test's
identity so reused workers contribute coverage to each test they serve. Setup and
teardown observations are attributed to the test file.

For each patch the selector checks unified-diff context against the pinned files.
It narrows Python changes to observed callers when the edits stay inside existing
callable bodies and preserve the surrounding syntax and callable metadata. It
uses observed readers for fixture edits and schedules the full suite when parsing,
collection, or the edit scope makes the affected tests uncertain. For each selection,
it tries one through eight workers with longest-first balancing and chooses the
lowest simulated cost.

### Validation

The hypothesis was that conservative reachability would preserve sample recall
and cost less than confirming candidates under pytest. Three runs under the
scorer's Docker flags produced identical output for all 30 sample commits, with
zero misses and 457.229 simulated seconds. Median container CPU was 0.629 seconds
and median Docker wall time was 1.213 seconds, for an estimated local score of
457.9. The pinned map build passed 1,620 tests with one skip.

A comparison run with candidate confirmation enabled scored 454.693 simulated
seconds with zero misses, but used 34.216 CPU seconds. The image defaults to
candidate selection. Pass `--env BLAST_CONFIRM=1` to Docker to reproduce the
optional rerun mode.

The synthetic run missed zero of four independently labeled patches. Full pytest
reported two failing tests for the dialect function change, 56 for the tokenizer
constant, zero for the source comment, and one for the test module's failed
import. The selector scored 78.120 simulated seconds for that batch.

Run `make synthetic` to regenerate the deterministic mutation dataset and its
full-suite labels under the scorer's container restrictions. The verifier writes
the patches, labels, and selector output under `.verification/`.

Full-suite fallbacks dominate the remaining sample cost. Baseline reachability
can miss interactions through state shared across tests or native code. The next
experiment would add coverage inside heavily used functions and validate that
narrowing on independent mutations.

## Target 150 experiment

This experiment starts from the branch that scored 457.229 simulated seconds
on the fixed 30-patch sample. It adds line events for replacements inside an
existing statement header and tracks the concrete classes, plus their base
classes, seen by the pinned test suite. Spawned workers carry the submitting
test's identity through argument decoding, execution, and result encoding.

An unchanged Python syntax tree still keeps tests that directly read the
changed file. Changes to the values of supported class attributes use
observed receivers and direct test references if the assignment keeps its
shape and the modified module imports successfully. For identity comparisons,
the build records whether every observed pair uses proven comparison semantics
that agree with identity. The table holds source locations and aggregate
observations. Unknown operands retain the execution-based selection.

The stop condition is three scorer-constrained sample runs with zero misses
and an estimated local median below 150 seconds, plus zero misses in
independent full-suite labels. Run `make synthetic VERIFY_SUITE=scope` to
reproduce the class attribute, parser dispatch, and comparison verification
cases.

The experiment reached 129.410 simulated seconds with zero misses in each
of three scorer-constrained sample runs. Output was identical across runs.
Median container CPU was 3.315 seconds and median Docker wall time was 4.003
seconds, for an estimated local score of 132.7. The instrumented build passed
1,620 tests with one skip. The scorer reported zero misses across nine
independently labeled synthetic patches, including failing dialect settings,
parser dispatch, and nested tokenizer cases.

The sample and synthetic labels verify the local result. Receiver tracking
can still miss class values propagated through unrelated global state or
native code. The public board uses its own patches and runner.

## Near-floor experiment

The official score for the previous public commit was 283.8 seconds against a
132.7-second local estimate. The ratio is 2.14. At that ratio a 60-second
official score would correspond to about 28.1 seconds locally. The public
sample has a 94.351-second zero-miss minimum before CPU. For each patch
with failures, the lower bound is the minimum over worker counts of the larger
of the longest failing test and the average failing-test load, plus worker
overhead. A failing-tests-only schedule attains that bound for all sample
patches. The private scoring batch can have a different minimum.

The selector now intersects observed class receivers with coverage of direct
scalar reads and the numeric escape dictionary's value consumers. It also
handles directly read module-level scalar constants and comparison-boundary
edits where all observed primitive operands stayed off the boundary. Patched
imports reuse checked-hash bytecode for unchanged modules.

Three scorer-constrained sample runs produced identical output with zero
misses, 111.872 simulated seconds, median 3.118 container CPU seconds and
median 3.828 Docker wall seconds. The estimated local score was 115.0.
Full-suite labels for all 11 synthetic patches yielded zero misses. A
Snowflake module constant changed from 36.817 to 3.974 simulated seconds on
its independent patch. Candidate reruns were measured again and left
disabled by default because the CPU cost outweighed the simulated savings.

The previous official/local ratio would project roughly 246 seconds for
this candidate. It is a one-run planning reference. An official run on the
private scoring batch is required to determine the actual result.

## Submit

Push to a public GitHub repository with the `Dockerfile` at its top, then enter the repository and the commit hash on the track page. The final scores the commit you pick. If you pick none, your best practice submission counts.

## Rules

- Submit your own team's repository. A submission with the same repository, commit or archive as another team's is disqualified, the later one.
- Your build may download only from public package registries: PyPI, npm, Docker Hub, GitHub releases at a fixed tag, and sqlglot’s own repository at the pinned commit. Nothing from an address your team controls. The final rebuilds every image after the cutoff, so anything else could change a submission after the deadline.
- No answers worked out ahead of time and looked up by patch: no table from a patch, or from a kind of edit, to the tests it breaks. Maps built from the pinned commit (coverage, imports, fixtures) are allowed. Top entries are read.
- Only the final counts. The practice board carries no prize.

## How your image runs

- The build has the network, 4 cores, 8 GB of memory and 10 minutes. It may clone sqlglot at the pinned commit, install it and run its tests to make coverage or import maps.
- Your repository may hold 20 MB. Git submodules and Git LFS files are not fetched: clone sqlglot in the `Dockerfile` and commit maps as plain files.
- The run has no network, 2 cores, 4 GB of memory and 3 minutes for the whole batch (4.5 in the final, which has about one and a half times as many commits).
- The disk is read-only. `/tmp` holds 64 MB and is empty at the start of every run.
- The container runs as user `nobody`, with `HOME=/tmp` and at most 256 processes.
- stdout may hold 2 MB. More rejects the run.

## Things that will catch you

- Logs go to stderr. Any line on stdout that is not an answer rejects the run.
- `score.py` names a broken rule by its code, as in `Rejected: not_json on line 1`. The table in SCORING.md says what each code means.
- Files your program needs must be readable by `nobody`. A map written by `root` during the build with mode 600 is not.
- pytest 9 prints a test as `PASSED` beside `SUBFAILED` lines when one of its subtests fails. That test failed.
- sqlglot skips its executor tests unless `SKIP_INTEGRATION=0` is set. We labeled with it set, so those tests can fail.
- `requirements.lock` is every package in the environment we labeled in, on the image the `Dockerfile` starts from. To run sqlglot's tests the way we did, `pip install -r requirements.lock` and put the sqlglot checkout on `PYTHONPATH`; sqlglot's own `pip install -e .[dev]` picks newer versions.
- The scorer is linux/amd64. On an Apple Silicon Mac, build and run with `--platform linux/amd64`, as the Makefile does, or your timings and wheels will differ.
- A change that only adds a comment is not promised to be harmless.

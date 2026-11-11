# Token savings: evidence and accounting

## Estimated cost including memory generation

Both peaks in the same 24-task cohort come from case 456, with both runs passing: tokens 2,301,174 → 840,186 (**63.49% fewer**); estimated cost 33.52836 → 16.09925 points (**51.98% lower**). Both include memory generation; cost uses the cache extrapolation below.

On the same 24 tasks, solving plus memory generation uses 17,808,978 tokens versus 26,410,221 for the control: **32.57% fewer**. Apply the input-weighted observed cache-hit rate `821,504 / 846,962 = 96.994%` to 1,171,777 input tokens across 55 records with missing cache fields.

At the frozen rates of 100 points per million uncached input tokens, 10 per million cached input tokens, and 500 per million output tokens, memory generation is estimated at 37.27503 points. Adding 352.25020 solving points gives **389.52523**, versus **460.52434** for the control: **15.42% lower estimated cost**. Points are not currency or an actual bill.

This assumes prefix-extraction cache hits generalize to ordinary generation and may be optimistic. Without that assumption, solving plus memory generation costs 386.35531–491.81524 points, so a reduction is not established. Embedding, readiness, and other auxiliary costs are excluded.

[Estimate inputs](cost-estimate.json). Token totals are measured; costs are estimated. Missing fields remain unknown in the source data.


[Project README](../../../README.md) · [中文](README_CN.md) · [Per-task table](cases.md) · [Machine-readable evidence](results.json)

These are project-run SWE-bench Pro comparisons, not an official leaderboard submission. Export date: 2026-11-10.

## Same 24 tasks: original / frozen selections

| Configuration | Total tokens | Cached input | Uncached input | Output | Reduction | Passed |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| gateway_off | 26,410,221 | 25,199,744 | 991,779 | 218,698 | 0.00% | 16/24 |
| S | 19,247,973 | 18,231,296 | 825,099 | 191,578 | 27.12% | 15/24 |
| CS | 17,680,166 | 16,224,256 | 1,252,101 | 203,809 | 33.06% | 15/24 |
| MCS | 15,766,986 | 14,446,080 | 1,131,659 | 189,247 | 40.30% | 16/24 |

The cohort is the frozen intersection of 24 completed tasks used in the Memory comparison, not a random sample or the full 121-task set. It contains both successful and failed solutions. This is not all 28 completed MCS tasks. IDs:

`22, 87, 152, 202, 225, 238, 293, 308, 336, 374, 388, 406, 410, 456, 464, 468, 472, 482, 492, 501, 505, 627, 698, 706`

## All 121 tasks: original settled selections

| Configuration | Total tokens | Cached input | Uncached input | Output | Reduction | Passed |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| gateway_off | 141,869,214 | 135,236,608 | 5,534,586 | 1,098,020 | 0.00% | 83/121 |
| native_off | 136,121,008 | 129,904,640 | 5,141,899 | 1,074,469 | 4.05% | 81/121 |
| C | 156,807,398 | 147,259,648 | 8,417,464 | 1,130,286 | -10.53% | 78/121 |
| S | 91,284,652 | 86,003,456 | 4,331,293 | 949,903 | 35.66% | 81/121 |
| CS | 90,544,517 | 83,440,640 | 6,132,463 | 971,414 | 36.18% | 82/121 |

Gateway OFF is the reference for every reduction column, including native OFF. CodeGraph alone did not reduce tokens in this run. CS saves slightly more total tokens than S, but consumes more uncached input. Token totals cannot rank billing efficiency without the cache and auxiliary-call accounting.

## Conditional retry selections — a separate view

Previously selected failures that another group solved were retried, up to three additional attempts. The first successful retry replaces the original record; if none succeeds, the last valid failed retry is selected. Untargeted cases retain their original record. These are selected-run totals, not Pass@1, uniform Pass@3, or the cost of all attempts.

### 121 tasks

| Configuration | Total tokens | Cached input | Uncached input | Output | Reduction | Passed |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| gateway_off | 144,023,044 | 137,392,000 | 5,534,903 | 1,096,141 | 0.00% | 87/121 |
| S | 90,197,496 | 84,905,216 | 4,345,852 | 946,428 | 37.37% | 87/121 |
| CS | 90,153,970 | 83,008,768 | 6,179,816 | 965,386 | 37.40% | 87/121 |

Across these three arms, 30 additional attempts were made. Initial selected records plus **all** additional attempts total 100,039,959 tokens for CS, 97,866,140 for S, and 159,303,478 for Gateway OFF. These totals retain replaced records and unsuccessful retries; earlier nonselected Gateway failures still have unknown usage.

### The same 24 tasks

| Configuration | Total tokens | Cached input | Uncached input | Output | Reduction | Passed |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| gateway_off | 26,410,221 | 25,199,744 | 991,779 | 218,698 | 0.00% | 16/24 |
| S | 19,063,917 | 18,041,984 | 832,610 | 189,323 | 27.82% | 16/24 |
| CS | 17,464,234 | 16,014,464 | 1,247,457 | 202,313 | 33.87% | 16/24 |
| MCS | 15,766,986 | 14,446,080 | 1,131,659 | 189,247 | 40.30% | 16/24 |

Only S/case 374 and CS/case 406 change in this subset. Gateway OFF and MCS stay unchanged. The top-level README uses the original/frozen view, so it does not mix success-selected retries with the Memory run.

## Memory overhead and what was actually reused

The MCS primary total is **15,766,986**. Its 74 recorded memory-generation calls add **2,018,739 input + 23,253 output = 2,041,992 tokens**. Together these two components total **17,808,978**, 32.57% below the OFF primary total. This is a partial component sum, not a complete system-cost comparison.

Additional observed business embedding input: 14,737 tokens for memory recall and 7,679 for memory indexing. Readiness, setup, embedding pricing, and some generation cache fields remain incomplete; full system billing is **unknown**, not zero. Do not add Scope or prefix crosschecks to the Core ledger again.

## Single-task peaks

| Scope | Arm | Case | OFF total | Arm total | Reduction | Both passed? |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| 121 original tasks | S | 450 | 1,833,811 | 412,215 | 77.52% | Yes |
| 121 original tasks | CS | 302 | 1,240,658 | 177,343 | 85.71% | Yes |
| Same 24 tasks | S | 202 | 1,286,399 | 475,243 | 63.06% | Yes |
| Same 24 tasks | CS | 464 | 505,046 | 156,916 | 68.93% | Yes |
| Same 24 tasks | MCS | 202 | 1,286,399 | 403,007 | 68.67% | Yes |

Every peak compares the **same task** in two arms. A peak is not a cohort average. Implementation and validation routes can differ; a single paired run does not isolate mechanism effects or establish statistical significance.

## Tested configuration

- Dataset: 121 selected tasks from the 731-task public v2 SWE-bench Pro file; repositories: NodeBB/NodeBB, navidrome/navidrome, tutao/tutanota. The Memory table is a 24-task completed subset.
- Agent: Codex CLI 0.162.1; model: `gpt-5.6-sol`; reasoning effort: `medium`; same original task prompts. No gold patch or grading input in prompts.
- C = PowerContext native CodeGraph (not the external colbymchenry/codegraph tool). S = the explicit [steering text](steering.txt), whose trimmed SHA-256 is `47335a057b37c59feb790c71531fb13fee9aa23f63611aaddcac74831b40c22f`. M = Memory enabled.
- Steering asks for targeted search, bounded reads, batched work, and focused validation. It also allows a compile/import check when tests cannot run; that can affect validation routes. OFF lacks this guidance.
- Gateway OFF keeps the gateway route with M/C/S disabled; native OFF is a separate no-gateway baseline. The C/S/CS/MCS figures here are compared to Gateway OFF.
- Every arm × task × model attempt uses a new DB, Scope, workspace, cache, and home. Technical completion recovery retains the same attempt and never replays a successful model to fix grading or flush.
- Host memory, plugins, and web search disabled; privacy inspection disabled. Resources: 2 CPUs, 6 GiB, 512 PIDs, 2,400-second task limit.
- Frozen development source: base `28ba11dc26865b4bae1bd6d9bb483248886c4c74` plus ported upstream fix `566834839821d026db1c116425ee9a9b5c0f4fe5`. Exact source, patch, dataset, and binary digests are in [results.json](results.json). This is not a claim about the default PyPI package configuration or later retrieval fixes.
- Historical OFF outcomes were reused from an earlier batch. Runs were not simultaneous randomized A/B trials. Model sampling and shared infrastructure can affect results.

## Verify the tables

From the repository root, with Python 3:

```bash
python3 docs/benchmarks/token-savings/verify.py
```

The verifier checks task alignment, record arithmetic, conditional selection, and published totals using [results.json](results.json). It makes no API calls, starts no agents, and reruns no graders. Primary usage comes from CLI `turn.completed`: `total = input + output`; cached input is already in input, and reasoning is already in output.

The export contains all 121 original records for the five baseline arms, S/CS/Gateway retry selections and extra attempts, and 24 MCS selections. Outcome digests identify the source records where supplied by the source report. Full prompts, raw traces, credentials, and local file paths are not bundled. Source hashes identify provenance; they do not make unavailable raw traces publicly auditable. Numerical verification is not independent grade reproduction.


## Configuration and result interpretation

In the detailed results, **C = CodeGraph, S = Steering, M = Memory**. Combined letters mean those features are enabled together. **Gateway OFF** is the control with all three disabled.

- On the same 24 tasks: S saves 27.12% (15/24 solved), CS 33.06% (15/24), MCS 40.30% (16/24); Gateway OFF solves 16/24. This is a completed-task intersection, not a random sample.
- MCS adds 2,041,992 post-task memory-generation tokens. Including them gives a 32.57% reduction, before other auxiliary work. No nonempty Memory injection was observed during solving; the savings do not establish cross-task memory benefits.
- Across 121 tasks, CodeGraph alone uses 10.53% more tokens. CS solves one fewer task than OFF. Implementation and validation routes vary; these runs do not guarantee savings on every task or equal quality.
- Conditional retry selections, all-attempt costs, cached and uncached tokens are separate in the evidence report. They are not mixed into the headline. MCS did not finish all 121 tasks.
- Tested with Codex CLI 0.162.1, GPT-5.6 Sol medium, a frozen development build and fresh databases per attempt. S is explicit guidance, not an installation default. OFF is an earlier frozen batch.

[Methodology](README.md) · [Data](results.json) · [Offline verifier](verify.py)

## Why 40.30% and 32.57% differ

40.30% covers only the solving model: 15,766,986 tokens. Adding 2,041,992 memory-generation tokens gives 17,808,978 tokens, or 32.57% below the same Gateway OFF baseline of 26,410,221. The README hero stacks both: the solving segment shows 40.30%, and the full bar, with memory generation, shows 32.57%. Both figures use the same 24 tasks and aggregate reductions, not the arithmetic mean of per-task percentage reductions.

## Where the savings come from

Same 24 tasks, solving only, split by input type:

| Component | Gateway OFF | MCS | Change |
| --- | ---: | ---: | ---: |
| Cached input (re-sent context) | 25,199,744 | 14,446,080 | −10,753,664 (−42.67%) |
| Uncached input (new content) | 991,779 | 1,131,659 | +139,880 (+14.10%) |
| Output | 218,698 | 189,247 | −29,451 (−13.47%) |
| Total | 26,410,221 | 15,766,986 | −10,643,235 (−40.30%) |

The drop in cached input is larger than the net saving: all of the saved tokens came from re-sent context, while new content rose slightly. The 121-task results point the same way. S alone cut cached input by 36.41%; C alone raised cached input by 8.89% and uncached input by 52.09%, so supplying more retrieved content does not by itself lower usage.

This also explains why the estimated cost reduction (23.51%) is smaller than the token reduction (40.30%): the saved tokens are mostly cheaper cached input, while pricier uncached input rose.

This export does not include per-task model call counts, so it cannot separate fewer calls from shorter context per call; both show up as less cached input.

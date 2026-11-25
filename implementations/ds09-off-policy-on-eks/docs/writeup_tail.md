### What I did not build, and what the results do and do not say
**Not built.**
- **Concurrent generation.** The engine and the trainer do not run at the same time, so staleness costs accuracy here but buys no throughput (S03 measured no saving). The design's reason to accept staleness, overlapping a slow engine with the trainer, is outside this build.
- **A separate log-probability worker, version spans for partial rollouts, and a store of old weight versions.** The proximal policy is computed in the trainer's own process at the moment of generation, when the right weights are in memory.
- **A teacher model.** The external pool is human-written solutions; the design's teacher-written solutions for the hardest tasks are not generated.
- **Sequence-level weights and distance-adaptive staleness.** The sequence-level ESS is computed and logged; no loss uses it. The staleness limit is in versions, not in KL.
- **Batch-composition caps by age, filters that model themselves as a policy, and hot spares for GPU nodes.**
- **Anything about the design's own scale.** One GPU per run, a 0.6B model (and one 1.7B check), answers of at most 256 tokens.
**What the results say, and how far to trust them.**
- The mechanism results (S02, S04, S05, S06, S10) are measurements of code paths and are stable: the two gaps behave as the design says, the limits hold, the decoupled loss is much more robust to engine mismatch than the naive one, and the queue orders by priority.
- The curves (S03, S07, S09) are one model, one task and two seeds per cell. A difference smaller than the seed spread printed beside it is not a finding, and S03 lists the three predictions I made before the sweep, all of which were wrong.
- The injected engine mismatch of S05 is noise on the engine's weights, not a real quantised engine; it shows what the losses do as the gap grows, not what an fp8 engine's gap is.
- The controller of S08 caught one kind of fault (a learning rate ten times too high) and correctly stayed quiet for a smaller one; it would not catch a fault that leaves the data close to the policy. One run per setting.
**Answers to the design's open questions, as far as this build can give them.**
- **Distance-adaptive staleness.** Not tested. The data say the behavior KL at a given age depends on the learning rate (S03), which is a reason to express the limit in KL; I did not build it.
- **Very old data.** Replayed batches up to 40 versions old, within a KL limit, were usable, but every share of replay or external data I tried ended below fresh-only data on the held-out set (S07), and the 40% external run ended far below. In this setting old data was not a free gain.
- **Filters as policies.** The drop rate moved with the task type and with training (S06), so a per-type report is needed; whether the filtered run does better is within the seed spread.

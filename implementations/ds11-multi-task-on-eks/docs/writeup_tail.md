### What I did not build, and what the results do and do not say
**Not built.**
- **The chat family.** The design's seventh family is open-ended chat, which needs a judge. The six families here have programmatic verifiers; the seventh I added (sorting) is of the same kind. How a matrix looks when one row is scored by a model is not measured.
- **Distillation.** The design folds specialists back by merging or by multi-teacher on-policy distillation. I built the merge. Distillation needs a student that samples and teachers that grade token by token, which is Design 06's loop with six teachers.
- **Conflict reducers.** Gradient surgery, adapters per domain and a larger or mixture-of-experts model are the design's tools once interference is measured. S07 found no module-level conflict to reduce, so I did not build them.
- **Automatic refreshes and live regression detection.** The refresh rule (any family fell by more than 1.5 points) is evaluated on the staged run and reported; nothing schedules a refresh by itself.
- **Anything at the scale of a real assistant.** One GPU per run, a 0.6B model, 20 to 120 steps, answers of at most 256 tokens.
**What the results say, and how far to trust them.**
- The mechanism results (S01, S02, S06, S09, S10) are measurements of code paths and are stable: the verifiers are exact on known answers, the token balance removes most of the imbalance, the gate refuses a family's regression that the average hides, and the pipeline orders its queued jobs and can be traced.
- The matrix, the mixture search and the orders (S03 to S05) are one model, six small families and a few seeds. In the matrix most off-diagonal cells are inside the noise; the ones outside it are few and I do not claim they hold elsewhere. The searched mixture raised the worst family and lowered the mean, and the gate refused it; whether a better search exists I did not test.
- The probe (S07) did not work as an instrument at this scale, and the scenario says so.
- The merge (S08) kept most of each specialist's gain in this setting, where the specialists were trained for forty small steps from one base. It is the setting in which merging is easiest.
**Answers to the design's open questions, as far as this build can give them.**
- **Scaling laws for mixtures.** Not tested: all weights were searched at one model size.
- **Capacity against mixture.** Not tested.
- **Merging RL-trained specialists.** In this setting, with small RL updates from one base, the specialists merged with little loss (S08); I did not train SFT specialists to compare.
- **Automatic refreshes.** The trigger of the design's own rule fired on the staged run (a family fell by more than 1.5 points in the focus stage), and the refresh that followed recovered both of the families that had fallen (maths 0.485 to 0.532, safety 0.853 to 0.922); scheduling that without a person was not built.

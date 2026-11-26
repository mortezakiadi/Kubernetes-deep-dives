"""Hand-written text for each scenario of the appendix: what it shows, why the result can be trusted, and why the output is what it is.
The numbers are in the evidence (commands, tables and checks); nothing here restates a measurement that could change from run to run.
"""
PROSE = {
    "S01": {
        "intro": "Before any experiment, I show what the experiments stand on: the GPU nodes the scheduler can hand out and the queue that admits work to them. Then the domain contract is checked: the registry is read back and compared with the code, every verifier is scored against answers known to be right and wrong, and every family's training prompts are compared with every family's held-out prompts. A first run goes through the queue and writes the records that every later scenario reads.",
        "trust": [
            "The node and GPU figures are read from the Kubernetes API, and the queue, its quota and its priorities are read back from the cluster after the tests applied them.",
            "The known-right and known-wrong answers are built from each example's own ground truth by code that does not use the verifier; a verifier cannot pass by agreeing with itself.",
            "The overlap check is a set intersection over the prompts (with their hidden tests or constraints) of all seven families, run in the pod that will train.",
        ],
        "why": [
            "The nodes are single-GPU instances of whichever type EC2 could supply; the pool lists several, and on-demand GPU capacity differs by type and zone.",
            "The registry's data versions must equal the version strings the generators carry, which is the cheapest way to notice that the data changed under a family without anyone recording it.",
            "The held-out parts are carved from each family's own lists before generation, so the overlap is zero by construction; the check exists so that a later edit to a generator cannot quietly break that.",
        ],
    },
    "S02": {
        "intro": "The untrained model is scored on every family's held-out set three times with different sampling seeds. The scores say where each family starts, the spread says how much a single evaluation moves by chance, and the answer lengths calibrate the token balance of S06.",
        "trust": [
            "Each score is the mean verifier reward over 100 held-out prompts with four sampled answers each, the same set and the same procedure in every later run.",
            "The three runs differ only in the engine's sampling seed.",
        ],
        "why": [
            "The families start between 0.35 and 0.85. I made the code and instruction families harder after a first look, because a family at its ceiling cannot show transfer and one at its floor cannot show interference.",
            "The spread is small because 400 sampled answers average out most of the chance; it is the noise of sampling only, not of training, which S03 measures separately.",
        ],
    },
    "S03": {
        "intro": "The design's matrix: train from the same start on an equal mixture, and on the same mixture with one family's weight raised by twenty points, and read the difference of held-out scores. Three baseline seeds give the noise, and a cell outside twice the standard error of a difference is the only one read as a finding. The seventh family is evaluated in every run, so its column comes for free.",
        "trust": [
            "Every cell is computed from the runs' own evaluation records, two seeds per boost and three for the baseline, all started from the same weights.",
            "The significance rule is stated and applied by code: cells inside the band are drawn grey in the chart and marked in the table.",
            "The noise is measured at the same number of steps and the same learning rate as the cells it judges.",
        ],
        "why": [
            "The diagonal is positive because a family given more of the batch learns more of what its verifier rewards.",
            "Most off-diagonal cells sit inside the noise because twenty points of weight over sixty small steps move a family little, and because several families saturate quickly. The few outside it are directional: instruction following and code pull against each other, in both directions and by different amounts.",
        ],
    },
    "S04": {
        "intro": "Fourteen proxy runs at random mixtures give a response model, the search picks the weights that lift the weakest family, and four mixtures are then confirmed on longer runs: natural proportions (the sizes each team happened to have), a temperature of two, uniform, and the searched weights.",
        "trust": [
            "The response model is fitted on the proxy runs' final scores and its residual is printed; the search uses the model's predictions only.",
            "The confirmation runs use different seeds from the proxies and report two seeds per mixture.",
            "Every mixture is shown as numbers, and the weights of the searched one are stored with the evidence.",
        ],
        "why": [
            "Natural proportions give maths more than half of the batch and safety less than half a percent, because that is how many examples each had.",
            "The searched mixture raises the worst family, as it was asked to, and pays for it in the mean: it gives the best-represented families less than the others do. The search optimises what it is told to.",
        ],
    },
    "S05": {
        "intro": "The same 120 steps in three orders: one family group after another, everything mixed, and a mixed base followed by a focus stage and a short refresh. After every stage every family is scored, so forgetting is visible as a drop in a family the stage did not train. The learning rate is three times the matrix's, so that the weights move far enough for forgetting to show.",
        "trust": [
            "Each stage is a separate run that starts from the previous run's saved weights, so the chain can be checked run by run.",
            "The scores after each stage are the held-out scores of all seven families, not only of the family just trained.",
            "One seed per order: the comparison is of the size of the effect, which is large, not of a few points.",
        ],
        "why": [
            "In the sequential order nothing holds earlier families in place while the weights move for the next one: code reaches 1.00 after its own stage, falls to 0.14 when the instruction and tool stage trains, comes back to 0.91 after the next stage and falls again to 0.24 by the end.",
            "The mixed and staged orders keep every family in the gradient, directly or through the focus stage's neighbours and the refresh, so no family is left to drift.",
        ],
    },
    "S06": {
        "intro": "In a mixed RL batch the families differ in answer length and in how often their groups carry a signal. The same equal mixture is run with the weights counted in prompts and counted in tokens, and the report says, per family, how many tokens the batch carried, how many answer groups had no learning signal, and how strong the signal of the rest was.",
        "trust": [
            "The token shares are computed from the per-family records of every generation of the run: groups times answers times mean tokens.",
            "The no-signal share counts groups whose four answers all scored the same, which is what makes their advantage zero.",
        ],
        "why": [
            "A maths or safety answer is hundreds of tokens and a tool call or a language answer a few dozen, so equal prompts are very unequal in tokens, and under token-level loss a family contributes in proportion to its tokens.",
            "Balancing by tokens removes most of the imbalance but not all: the answer lengths I used are those of the untrained model, and instruction answers grow during training.",
            "Families the model already does well on (tool calls) or cannot yet do at all produce groups of equal answers, and those groups teach nothing however large the family's weight.",
        ],
    },
    "S07": {
        "intro": "The gradient of each family's loss is taken on a probe batch, on the untrained model and on the model trained on the equal mixture, and the families' gradients are compared module by module. The whole-model cosine is one number; the design says to look per module, and to ask whether the probe agrees with the matrix.",
        "trust": [
            "The probe runs in the same pod type and with the same trainer code as the training runs; it only stops before the update.",
            "Each gradient is kept per parameter tensor and the cosines are computed on the GPU from the stored gradients, not estimated.",
            "The predictions about the probe were written down before it ran, and the two that failed are reported as failed.",
        ],
        "why": [
            "The gradients of two different families over 0.6 billion numbers are nearly orthogonal: whole-model cosines of a few hundredths.",
            "Per module the cosines are also small for the attention and MLP matrices; the large values come from the small normalisation vectors (cosines near plus or minus one in a spike I ran first), which I left out of the counts. At this scale the interference the matrix found is not concentrated in modules a probe of 32 prompts can name.",
        ],
    },
    "S08": {
        "intro": "One specialist per family is trained from the same base, then merged into one model in two ways (trimming and sign agreement, and plain averaged task vectors), and the merged models are scored against the specialists, the generalist and the untrained model. The merge and the evaluation are jobs that read their inputs from the bucket.",
        "trust": [
            "Every specialist's weights and the merged weights go through the bucket and are loaded back into the engine, so the scores are those of the files that would be released.",
            "The gain kept is computed against each specialist's own gain over the untrained model, for the families where that gain was at least 0.05.",
        ],
        "why": [
            "A specialist's task vector is its weights minus the base. I did not measure how much the vectors of different families overlap; the merge's result, not an overlap figure, is what the scenario tests.",
            "Trimming to the largest changes keeps most of each specialist's gain and drops what is likely noise; averaging all changes of all specialists dilutes each one by the others.",
        ],
    },
    "S09": {
        "intro": "Every model the earlier scenarios produced is judged against the current release by the design's gate, and the gate is tested on itself. Then a seventh family is admitted as the contract says: a proxy run at a small weight, the effect on the six families already there, and the same gate.",
        "trust": [
            "The candidates are read back from the bucket's records, not retyped.",
            "The tolerance of each family is its registry margin plus twice the standard deviation of its score over three baseline training seeds.",
            "The gate is tested on a model built to pass the average and fail one family, and on the current release against itself.",
        ],
        "why": [
            "A candidate can raise the mean by training some families much harder while another falls beyond its tolerance; the gate looks at each family against its own tolerance, so the mean cannot buy a regression.",
            "The tolerance of a family is wide when its held-out set is small or its score noisy (safety here), and narrow when it is stable (tool calls).",
            "Adding the seventh family needed one generator, one verifier and one registry entry, and one proxy run; the six families already there stayed inside their tolerances.",
        ],
    },
    "S10": {
        "intro": "The design's pipeline as an Argo Workflow: two specialists train at once, the merge waits for both, the evaluation waits for the merge. Each step creates a Kueue-managed Job and waits for it, so the pipeline queues like everything else. Afterwards the candidate is traced back to its inputs from the records the runs wrote.",
        "trust": [
            "The step times are read from the workflow object the controller maintains.",
            "Kueue's workload objects show that each step's job was admitted through the research queue.",
            "The lineage is read from the runs' own first records and the merge's own record, not written by hand.",
        ],
        "why": [
            "The two specialists have no dependency on each other and the queue's quota allows both, so they start in the same second; the merge needs their weights in the bucket, so it waits.",
            "The workflow engine only orders the steps. Admission, placement and the GPUs are still the queue's and the scheduler's.",
        ],
    },
}

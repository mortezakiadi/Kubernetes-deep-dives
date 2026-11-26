### 6. What I did not build, and why

**Scenario.** A build is only credible if it says where it stops. These are the parts of Design 10 that this implementation does not cover, with the reason for each.

| Not built | Why |
|---|---|
| **Real people reviewing** | The review loop is built end to end (S11): queues, label upload, agreement, adjudication, human gold labels, regression cases, a contract change and the gate. The labels themselves are assumed inputs: two checklists and the owner's decisions written before the run and applied by a script. They show what the platform does with labels, not how real reviewers would label these answers, and their agreement figures are a property of the two checklists. Elsewhere (S10) the evaluation team's verifier labels policy outputs, which is not a person either. |
| **Real tasks and real policy behaviour at scale** | The tasks are templated: 400 math problems, 8 code functions, 10 judge questions. The gold sets are labelled by construction, so their slices are the ones I thought of. The RL runs are 80 steps of a 0.5B model; the design's warning that hacking grows with compute is not tested. |
| **A learned reward model, judges trained on labels** | The judges are general instruction models with a rubric. Training a judge on review labels and fuzz outputs, an open question of the design, is not built. |
| **Model-based adversaries and judge adversarial suites per few hundred steps** | Fuzzing uses fixed mutations, and the nightly job builds them around the policy's own text. Prompting a model to write passing-but-wrong answers is not built, and the judges' adversarial suite is the judge gold set, run once, not re-run during training. |
| **Pausing a run through a controller** | The monitor pauses promotion by writing its decision and stops the run itself (S07). There is no separate controller, no promotion pipeline that reads the decision, and no paging. |
| **Re-scoring replay buffers and SFT datasets** | Re-scoring covers a run's stored rollouts (S09). Nothing here builds datasets from rollouts, so there is nothing else to re-score. |
| **Formal verification** | No proof assistant. The math contract is a narrow language checked with SymPy or exact fractions. |
| **A VM-level sandbox** | Code runs under gVisor in a restricted pod with no network, each test in a new limited process with an audit hook (S05). gVisor is a user-space kernel, not a hardware boundary. Only Python programs are supported. |
| **Separate images per verifier version** | Every verifier Deployment runs the same Python image with code from a ConfigMap; a version is pinned by the code hash of its class and files, which the launcher checks against the running service, not by a per-version image digest as in the design's YAML. |
| **Verifying agent processes** | The design's open question about verifying how a long trajectory got to its end state is covered for one setting in Appendix I, not here. |

### 7. Run it yourself, and take it down

**Scenario.** The point of Terraform is that anyone can rebuild this from the repository and remove it again. These are the exact steps.

**What you need on your machine.** Terraform 1.9 or later, the AWS CLI v2 signed in to the account you want to use, Helm 3, and Python 3 with `boto3`, Pillow and SymPy. `scripts/setup-tools.sh` downloads a matching `kubectl` into the project folder with a checksum check. Check the account's quota for running on-demand G and VT instances in us-east-2 first: the build needs up to 24 vCPU of it, depending on which instance types EC2 has.

**Build.**

```text
cd implementations/ds10-verification-on-eks
export AWS_PROFILE=<your profile>
./scripts/up.sh
```

`up.sh` writes small local files with your account ID and your public addresses (git-ignored), generates and checks the datasets, downloads the two wheels, builds the VPC, the cluster and the three node groups, then the platform, and waits until every verifier and both judges are ready.

**Test.**

```text
python3 tests/run_scenarios.py all
```

The whole run takes about an hour, most of it in the two RL runs (S07, S08) and the code verifiers' gold-set measurement (S03).

**Take it down.**

```text
./scripts/down.sh --yes
```

`down.sh` deletes the jobs in `training` and `rewards`, destroys the platform layer (the bucket is emptied with it), destroys the cluster layer, and then runs `verify-clean.sh`. That script looks, in us-east-2, for anything of this build: clusters, EC2 instances, Auto Scaling groups, launch templates, NAT gateways, addresses, VPCs and endpoints, security groups, volumes, network interfaces, buckets, roles and log groups. It prints `CLEAN` when nothing is left.

After the final run the cluster layer's destroy stopped twice: once on a network interface the VPC CNI had left behind for a terminated node, and then on the cluster security group EKS creates. Both were in this build's VPC (checked by its `Project` tag) and unattached; I deleted them by hand and re-ran `down.sh`. `down.sh` does not handle this case yet.

{{TEARDOWN}}

**Keeping it safe in a public repository.** Terraform state, plans, kubeconfig files, local variable files, generated data, wheels and scratch evidence are git-ignored. `scripts/check-no-secrets.sh --all` blocks any file containing a credential pattern, the account ID or one of the administrator's addresses, read from the local variable files. The test harness redacts those values, and the local user name, before anything is written.

### 8. Screenshots of the AWS Console

The terminal screenshots above are rendered from recorded command output. I could not capture the AWS Console from here. If you want console pictures, these pages show the same facts. Crop out the account number and your user name in the top bar before publishing.

- EKS, Clusters: `ds10-verify`, its Compute tab with the three node groups, and the Add-ons tab.
- EC2, Launch templates: `ds10-verify-sandbox-…` and its user data with the pinned gVisor release.
- EC2, Instances: the three GPU instances and their type.
- S3: the bucket with `data/`, `wheels/`, `registry/`, `runs/`, `fuzz/`, `evals/`, `rescore/`, `reviews/` and `gold/`.
- IAM, Roles: the `ds10-verify-*` roles for the cluster, the nodes and the store.

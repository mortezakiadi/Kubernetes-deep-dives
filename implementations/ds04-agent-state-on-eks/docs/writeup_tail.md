### 6. What I did not build, and why

**Scenario.** A portfolio piece is only credible if it says where it stops. These are the parts of Design 04 that this implementation does not cover, with the reason for each.

| Not built | Why |
|---|---|
| **A real language model** | The model is a scripted function of the prompt. A real model would make the failure tests non-repeatable and cost money, and the design is about what surrounds the model. The gateway, the retries, the budgets, the context rebuilding and the recording of every call are real. What is not tested is how a real model behaves when it is given a compacted context. |
| **A real Git host** | The Git host is a small service I wrote, so that it can lose its answer on demand. It honours an `Idempotency-Key` the way the design assumes real hosts and payment APIs do. I did not test any real provider's behaviour. |
| **Aurora PostgreSQL** | The engine's database is Multi-AZ RDS for PostgreSQL. It has the failover behaviour the tests need. Aurora's shared cluster storage, its faster failover and its read replicas are not built. S10 measures RDS's failover, not Aurora's. |
| **Loss of a whole Availability Zone** | S10 fails the database over to its standby and removes every engine pod, which is what a zone loss does to the engine's state. I did not remove a zone's worth of nodes, so the spreading of workers and engine pods over zones is built but not tested by taking a zone away. |
| **Region loss and a standby region** | The design lists an engine with a standby in another region, or replicated histories and snapshots. This build is one region. |
| **Pod-level checkpoint and restore, and process snapshots** | The design leaves them out on purpose, and so do I. A test that is running when its sandbox is lost starts again (S06). |
| **Dapr Workflow** | The design names it as an alternative to Temporal. I built the Temporal choice only. |
| **Temporal Cloud and Temporal's own worker versioning feature** | I self-hosted the engine, as the design describes. Versioning is done with separate task queues, which is the manual form of the idea, and with a replay test (S09). |
| **Multi-agent runs** | The design lists them as an open question. Each run here has one agent. |
| **Retention and deletion of histories, context logs and snapshots** | Another open question in the design. Nothing is deleted by the platform; the teardown deletes everything. |
| **Large payloads by reference** | The design says the history should hold an object key and a hash and not a large payload. The payloads here are a few hundred bytes, so I kept them in the history. |
| **A sandbox with real isolation** | The sandbox is a plain pod. [Design 01](#design-sandboxing) and [Appendix B](#appendix-design-01-eks) cover isolation. What is tested here is its lifecycle. |
| **A product UI** | The session API is called from a command line. A UI would use the same calls. |
| **Real autoscaling limits** | KEDA scales between 2 and 8 workers with six activity slots each. The numbers are small to make a modest load exceed them. |

### 7. Run it yourself, and take it down

**Scenario.** The point of Terraform is that anyone can rebuild this from the repository and remove it again. These are the exact steps.

**What you need on your machine.** Terraform 1.9 or later, the AWS CLI v2 signed in to the account you want to use, Helm 3, Docker with `buildx` (to build the `linux/amd64` image), and Python 3 with `boto3` and Pillow for the tests and screenshots. Nothing is installed system-wide: `scripts/setup-tools.sh` downloads a matching `kubectl` into the project folder with a checksum check.

**Build.**

```text
cd implementations/ds04-agent-state-on-eks
export AWS_PROFILE=<your profile>
./scripts/up.sh
```

`up.sh` writes small local files with your account ID and your public address (both git-ignored), builds the VPC, the cluster and the database, builds and pushes the image, then builds the platform. It takes roughly 35 minutes, most of it the database and the first image push.

**Test.**

```text
python3 tests/run_scenarios.py all
```

Expect about an hour and a half. The scenarios fail the database over (S10, about six minutes) and run 24 runs at once (S11).

**Take it down.**

```text
./scripts/down.sh
```

`down.sh` removes the workloads, destroys the platform layer, destroys the cluster layer, and then runs `verify-clean.sh`, which looks for anything tagged with this project: clusters, instances, NAT gateways, addresses, VPCs, security groups, volumes, network interfaces, buckets, repositories, database instances, roles and log groups. It prints `CLEAN` when nothing is left.

**Keeping it safe in a public repository.** Terraform state, plans, kubeconfig files, local variable files and raw evidence are git-ignored. `scripts/check-no-secrets.sh` runs before every commit and blocks any file containing a credential pattern, the account ID or the administrator's address. The test harness redacts those values before anything is written.

### 8. Screenshots of the AWS Console

The terminal screenshots above are rendered from recorded command output. I could not capture the AWS Console from here. If you want console pictures in your portfolio, these pages show the same facts. Crop out the account number and your user name in the top bar before publishing.

- EKS, Clusters: `ds04-agentstate`, with its Compute tab and the pods of the `temporal` and `agent-platform` namespaces.
- RDS, Databases: `ds04-agentstate-db`, showing Multi-AZ, the primary's zone and the standby's, and in Logs and events the failover that S10 caused.
- ECR: the `ds04-agentstate/agent` repository and its image tag.
- S3: the bucket with `snapshots/objects/` and `snapshots/manifests/`.
- IAM, Roles: the sandbox role and its policy, which allows nothing outside `snapshots/`.
- VPC: the VPC with its three private subnets and the NAT gateway.
- CloudWatch, Log groups: the cluster's API and audit logs.

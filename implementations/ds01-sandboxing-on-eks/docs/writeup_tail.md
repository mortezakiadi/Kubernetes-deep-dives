### 6. What I did not build, and why

**Scenario.** A portfolio piece is only credible if it says where it stops. These are the parts of Design 01 that this implementation does not cover, with the reason for each.

| Not built | Why |
|---|---|
| **GPU sandboxes** | The design lists this as an open question. AWS documents that Nitro Sandboxes do not support GPU or other device passthrough ([considerations](https://docs.aws.amazon.com/eks/latest/userguide/sandboxing-limitations.html)), so a GPU workload would need whole nodes without a sandbox runtime. A CPU sandbox proves the same isolation, network and credential design. An alternative is written down in section 10.9. |
| **gVisor (Tier 1) and Kata (Tier 2 alternative)** | EKS Auto Mode does not allow the shim gVisor needs, and Kata needs self-managed nodes with nested virtualization. Tiers 2 and 3 on Nitro Sandboxes are the design's default and are fully built. An alternative is written down in section 10.8. |
| **Confidential mode** | The design says agent sandboxes need isolated mode. Confidential mode is for regulated data and removes `exec`, which this design depends on. An alternative is written down in section 10.10. |
| **A persistent workspace volume, pause and resume, checkpoints** | The workspace is a size-limited scratch volume that lives and dies with the pod. The design's EBS-backed workspace and its pause and resume are not built. An alternative is written down in section 10.7. |
| **Snapshots and multi-cluster placement** | Open questions in the design itself. An alternative is written down in section 10.12. |
| **A separate certificate authority per sandbox** | One gateway CA is trusted by every sandbox. The design describes one per sandbox, discarded with it. An alternative is written down in section 10.5. |
| **A shared store for the tool gateway** | Approvals and rate counters are in memory, so the tool gateway runs as one replica. A production build keeps them in a shared store such as DynamoDB. An alternative is written down in section 10.1. |
| **An append-only audit store** | Audit lines go to each pod's log and are joined by a script. The design calls for an append-only store with its own retention. The EKS control-plane audit log does go to CloudWatch. An alternative is written down in section 10.2. |
| **A runtime-security agent on the nodes** | The design itself notes it sees nothing inside a microVM. The audit trail is built from the boundaries instead. An alternative is written down in section 10.3. |
| **Image signing and a private registry mirror** | The sandbox images are public images pulled through the NAT gateway. The design wants them pinned, signed and mirrored. An alternative is written down in section 10.4. |
| **A warm-pool autoscaler** | The pool sizes are fixed (2 and 1). An alternative is written down in section 10.6. |

### 7. Run it yourself, and take it down

**Scenario.** The point of Terraform is that anyone can rebuild this from the repository and remove it again. These are the exact steps.

**What you need on your machine.** Terraform 1.9 or later, the AWS CLI v2 signed in to the account you want to use, Docker running (it builds one image), and Python 3 with Pillow for the screenshots. Nothing is installed system-wide: `scripts/setup-tools.sh` downloads a matching `kubectl` into the project folder with a checksum check.

**Build.**

```text
cd implementations/ds01-sandboxing-on-eks
export AWS_PROFILE=<your profile>
./scripts/up.sh
```

`up.sh` writes two small local files with your account ID and your public address (both git-ignored), builds the cluster and the platform, and waits for the warm pools. It takes roughly 20 to 25 minutes.

**Test.**

```text
python3 tests/run_scenarios.py all
```

**Take it down.**

```text
./scripts/down.sh
```

`down.sh` ends live sessions, destroys the platform layer, destroys the cluster layer, and then runs `verify-clean.sh`, which looks for anything tagged with this project: clusters, instances, NAT gateways, addresses, VPCs, security groups, volumes, network interfaces, repositories, roles and log groups. It prints `CLEAN` when nothing is left.

**Evidence that teardown works.** The first build of this implementation was destroyed with the same Terraform layers before the final build: 87 platform resources and then 40 cluster resources, and `verify-clean.sh` reported that nothing from it remained. The final build was then created from nothing with `up.sh`, and every scenario above ran against it.

**Keeping it safe in a public repository.** Terraform state, plans, kubeconfig files, local variable files and raw evidence are git-ignored. `scripts/check-no-secrets.sh` runs before every commit and blocks any file containing a credential pattern, the account ID or the administrator's address. The test harness redacts those values before anything is written.

### 8. Screenshots of the AWS Console

The terminal screenshots above are rendered from recorded command output. I could not capture the AWS Console from here, because signing in needs credentials and a browser session I deliberately do not drive. If you want console pictures in your portfolio, these pages show the same facts and are quick to capture. Crop out the account number and your user name in the top bar before publishing.

- EKS, the cluster's overview: the Kubernetes version and "Auto Mode: enabled".
- EKS, Compute: the `sandbox` node pool and its nodes.
- EC2, Instances: the sandbox node (`c7i.xlarge`), and its Security tab showing the dedicated security group.
- VPC, Subnets: the two sandbox subnets with the `ds01/tier=sandbox` tag.
- IAM, Roles: the sandbox node role with its two policies.
- ECR: the `ds01-platform` repository.
### 9. An alternative egress gateway on Envoy

**Scenario.** The tested egress gateway runs on mitmproxy. That was a good fit for a test, because the policy is easy to write on it. It is a less obvious choice for a platform that other people will operate. mitmproxy describes itself as an interactive HTTPS proxy for debugging, testing and penetration testing, and it does not appear in the CNCF landscape data. [Envoy](https://www.envoyproxy.io) does, and the same data lists it as a graduated CNCF project ([landscape data](https://github.com/cncf/landscape/blob/master/landscape.yml), read on the day of writing). So I wrote the same gateway on Envoy, in case the platform ever needs it, and ran it in a lab.

**What I did and did not test.** I ran this in Docker on one machine, with test host names and a policy service that reads a table of sessions. It did not run on EKS, it did not call the real Manager or credential broker, and it is not part of the 12 scenarios or the 75 checks above. What it shows is that the plumbing works: Envoy can do the two jobs from section 1 (an untouched tunnel for ordinary hosts, and TLS ending for the one host that needs a key) and can hand every decision to a separate policy service.

**How the work is split.** Envoy carries the traffic. A small policy service makes the decisions. That is the main difference from the tested gateway, where one add-on did both.

| Job | In the tested gateway (mitmproxy add-on) | On Envoy |
|---|---|---|
| Accept the sandbox's request | An HTTP proxy port | A listener on port 3128 that accepts `CONNECT` and builds the tunnel |
| Find the session that owns the source address | The add-on asks the Manager | Envoy asks the policy service (the `ext_authz` filter, over gRPC) and passes it the source address. The service asks the Manager. |
| Check the host against the session's allowlist | The add-on | The policy service. A refusal becomes a 403 on the `CONNECT`. |
| Rate limit | Counters in the add-on | Counters in the policy service. A refusal becomes a 429. |
| Byte budget per session | Counted in the add-on | Not built in the lab. Envoy logs the bytes of every tunnel, but enforcing a budget would need another component. |
| Pass TLS through for ordinary hosts | The add-on tells mitmproxy to ignore the connection | A route to a dynamic forward proxy cluster, which resolves the host name and opens a plain TCP tunnel |
| End TLS for the one host that needs a key | The add-on terminates TLS and edits the request | A route sends that host's tunnel to a loopback-only listener that ends TLS. A PROXY protocol header on that hop keeps the sandbox's address, so the second policy check still knows the session. |
| Supply the credential | The add-on asks the broker and sets the header | The second policy check asks the broker and returns the `Authorization` header, and Envoy overwrites the sandbox's placeholder with it |
| Audit line with the session ID | The add-on writes it | Envoy's access log, with the session ID that the policy service returns as metadata |

![Egress gateway on Envoy: one listener, a pass-through tunnel, and a loopback listener that injects the credential](diagrams/appendix/a02-8.svg)

**What the picture shows.** The tunnel from a sandbox arrives at Envoy's listener (1). Envoy asks the policy service about the session (2). For an ordinary host the tunnel goes straight to the site (3). For the one internal host, the tunnel goes to a loopback listener that ends TLS (4), asks the policy service again for the credential (5), and calls the internal API (6).

**Commands I ran and what they printed** (the whole run is `lab/run-lab.sh`; output shortened).

```text
$ curl -x http://envoy:3128 --cacert ca.pem https://allowed.test/          # run in the sandbox container
{"server_host": "allowed.test", "authorization_seen": ""}
$ curl -x http://envoy:3128 --cacert ca.pem https://blocked.test/          # a host not on the session's list
tunnel status: 403
$ curl -x http://envoy:3128 --cacert ca.pem -H "Authorization: Bearer placeholder" https://metrics.test/v1/metrics
{"server_host": "metrics.test", "authorization_seen": "Bearer real-credential-for-sess-lab-0001"}
$ (same call from a container that owns no session)
tunnel status: 403
$ (eight calls in a row to the allowed host, with a lab limit of five a minute)
tunnel status: 200 for the first calls, then 429
$ curl https://envoy:8443/                                                  # the loopback listener, from another container
curl: exit code 7 (could not connect)
$ docker logs lab-envoy | grep egress.decision
{"component":"egress-gateway","detail":"ext_authz_denied","event":"egress.decision","host":"blocked.test:443","session_id":"sess-lab-0001","status":403,...}
{"component":"egress-gateway","event":"egress.request","host":"metrics.test","injected":"true","method":"GET","path":"/v1/metrics","session_id":"sess-lab-0001","status":200}
```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output |
|---|---|---|
| PASS | Envoy validates the configuration | Envoy's own validation mode accepts it. |
| PASS | an allowlisted host is reached through a tunnel, and the sandbox verifies the site's own certificate | The tunnel is plain TCP, so the TLS handshake runs between the sandbox and the site and Envoy never sees inside it. |
| PASS | a host that is not on the session's allowlist is refused with 403 | The policy service refuses it, and Envoy turns that into a 403 on the `CONNECT`, before any connection to the host is opened. |
| PASS | the internal host receives the real credential although the sandbox sent only a placeholder | The tunnel for that host ends in the loopback listener. The second policy check returns the real header, and Envoy replaces the sandbox's header with it. |
| PASS | a source address that owns no session is refused with 403 | The policy service finds no session for that address. |
| PASS | a session over its rate limit is refused with 429 | The policy service counts the session's tunnels in the last minute. |
| PASS | the TLS-terminating listener cannot be reached from the network | It listens on the loopback address only, so only Envoy itself can send traffic to it. |
| PASS | the audit log names the session on a refused request | The policy service returns the session ID as metadata with every answer, refusals included, and the log line carries it. |
| PASS | the audit log records the injected request | The loopback listener logs each decrypted request with `injected` set. |

**What I found.**

- **The HTTP form of the check does not work for `CONNECT`.** My first version used `ext_authz` over plain HTTP. Every tunnel failed with the error `ext_authz_error`, and the policy service logged that it had been sent a `CONNECT` request. Switching the same filter to gRPC fixed it with no other change. I did not find out why the HTTP form fails.
- **The source address needs the PROXY protocol on the hop to the loopback listener.** Without it, the second check would see Envoy's own address and could not tell which session is calling. The lab shows that with it, the second check sees the sandbox's address.
- **The list of hosts that get a credential is fixed in Envoy's configuration.** Each such host needs a route, a certificate and an upstream cluster. In the tested gateway the Manager supplies that list per session. On Envoy the per-session part stays in the policy service, which adds the header only when the session is entitled to it, but adding a new internal host means changing the configuration.
- **The rate limit has the same weakness as before.** The counters live in the policy service's memory. Two replicas would each see half the calls, as in section 4, so the Service in front of it needs the same session pinning, or the counters need a shared store.
- **Envoy warns that no limit on downstream connections is set.** The lab ran with that warning. A real deployment should set one.
- **A dynamic forward proxy needs a firewall around it.** Envoy's own documentation says dynamic forward proxy servers should be protected by network firewalls and default-deny rules. In this design the sandbox's network policy and the gateway's domain ceiling (S05, S06) play that part.

**What it would take to use it.** Replace the lab's session table in the policy service with the calls the tested gateway makes: ask the Manager which session owns an address, and ask the broker for a credential. Run Envoy as the gateway Deployment on the same address and port, behind the same Service. Keep the sandbox template unchanged, because it already points at that address. Then run S05, S06 and S07 against it. I have not done any of those steps.

**The configuration** (`alternatives/envoy-egress/envoy.yaml`, as tested with Envoy v1.40.2).

```yaml
{{ENVOY_CONFIG}}
```

**The files.** `alternatives/envoy-egress/` holds `envoy.yaml`, `policy_service.py` (the lab policy service, about 60 lines) and `lab/run-lab.sh`, which builds the lab, runs the nine checks and removes everything it started. It needs Docker and nothing else. `up.sh` does not use any of it.
### 10. Alternative implementations of what I did not build

**Scenario.** Section 6 lists what this implementation leaves out. Here each item is written down as an alternative: real files under `alternatives/`, each with a label for how far it was checked. None of it is deployed. Nothing in `platform/`, `k8s/`, `terraform/` or `tests/` changes, so the 12 scenarios and the 75 checks above are not affected.

**What the labels mean.** I use three, and I do not use a stronger word than the check supports.

| Label | Meaning |
|---|---|
| **Tested** | The logic ran in a test on my machine, with a positive control where a refusal is being tested. |
| **Checked** | The files passed a syntax or schema check (`terraform validate`, the Kubernetes schemas, or the CRD schemas of the pinned agent-sandbox release). Nothing was applied to an account or cluster. |
| **From documentation** | I wrote it from the vendor's documentation and never ran it. The page says which document. |


None of this ran on EKS. `alternatives/check-all.sh` runs every check that works on one machine with Docker, and it finished with no failure on the day of writing.

![The alternatives, each next to the part of the deployed design it extends](diagrams/appendix/a02-15.svg)

**What the picture shows.** The left column is the design as deployed, with the limit that section 6 names for each part. The right column is the alternative that addresses that limit, with its label. The arrows are not traffic. They show which deployed part each alternative would extend or replace.

| Section 6 item | Alternative | Label |
|---|---|---|
| A shared store for the tool gateway | 10.1 | Tested |
| An append-only audit store | 10.2 | Checked |
| A runtime-security agent on the nodes | 10.3 | Checked |
| Image signing and a private registry mirror | 10.4 | Tested (signing), checked (the rest) |
| A separate certificate authority per sandbox | 10.5 | Tested |
| A warm-pool autoscaler | 10.6 | Tested (the sizing rule), checked (the rest) |
| A persistent workspace, pause, resume, checkpoints | 10.7 | Tested against a fake cluster, checked against the schemas |
| gVisor and Kata | 10.8 | Checked (templates), from documentation (nodes) |
| GPU sandboxes | 10.9 | From documentation |
| Confidential mode | 10.10 | From documentation |
| Network gaps found while writing the defence-in-depth section | 10.11 | Checked |
| Snapshots and multi-cluster placement | 10.12 | A decision note, no files |


Where a patch to the deployed code is needed, the folder holds a `.patch` file that I applied to a copy of `platform/` and compiled. I did not apply any of them to the real files.

**10.1 A shared store for the tool gateway.**

**The gap.** The tool gateway keeps approvals and rate counters in the memory of one process. With two replicas, an approval could be used once on each, and each replica would count only its own calls. So it runs as one replica.

**What I built.** A small store class on DynamoDB, a patch that makes the tool gateway use it, the Terraform for the table and for a role that can only read and write that table, and a Deployment patch that sets two replicas. The two properties that matter are enforced by DynamoDB, not by Python. Consuming an approval is one conditional write, so only one caller can win. Counting a call is one atomic increment.

The highlighted lines, in order:

1. `ConditionExpression="attribute_not_exists(pk)"`: creating an approval cannot overwrite an existing one, even if two replicas picked the same random id.
2. `UpdateExpression="SET #s = :used"`: marks the approval used. This is the write that spends it.
3. `ConditionExpression="#s = :approved AND #sess = :session AND #k = :key"`: the write succeeds only if the approval is approved, belongs to this session, and was approved for these exact arguments. DynamoDB checks and writes in one step, so two replicas racing for one approval cannot both succeed.
4. `UpdateExpression="ADD n :one SET expires_at = :exp"`: adds one to this session's counter for the current minute. The add is atomic, so three replicas share one count.
5. `return int(out["Attributes"]["n"]) <= limit`: the decision uses the number DynamoDB returned from that same write, so there is no read-then-write gap.

```python
# from alternatives/tool-gateway-dynamodb/store.py
    def request_approval(self, session: str, tool: str, key: str, now: float | None = None) -> str:
        """Create a pending approval for this session, tool and argument hash. Returns its id."""
        now = now or time.time()
        approval_id = "apr-" + secrets.token_hex(4)
        self._table.put_item(
            Item={"pk": f"APPROVAL#{approval_id}", "sk": "A", "session": session, "key": key, "tool": tool,
                  "state": "pending", "expires_at": int(now) + APPROVAL_TTL_SECONDS},
            ConditionExpression="attribute_not_exists(pk)", #!
# ...
    def consume(self, approval_id: str, session: str, key: str) -> bool:
        """Use an approval. True for exactly one caller, however many replicas race for it."""
        try:
            self._table.update_item(
                Key={"pk": f"APPROVAL#{approval_id}", "sk": "A"},
                UpdateExpression="SET #s = :used", #!
                ConditionExpression="#s = :approved AND #sess = :session AND #k = :key", #!
                ExpressionAttributeNames={"#s": "state", "#sess": "session", "#k": "key"},
                ExpressionAttributeValues={":used": "used", ":approved": "approved", ":session": session, ":key": key},
            )
            return True
# ...
    def within_rate(self, session: str, limit: int, now: float | None = None) -> bool:
        """Count one call in the current one-minute window. False once the window already holds `limit` calls."""
        now = now or time.time()
        window = int(now // 60)
        out = self._table.update_item(
            Key={"pk": f"RATE#{session}", "sk": f"W#{window}"},
            UpdateExpression="ADD n :one SET expires_at = :exp", #!
            ExpressionAttributeValues={":one": 1, ":exp": (window + 2) * 60},
            ReturnValues="UPDATED_NEW",
        )
        return int(out["Attributes"]["n"]) <= limit #!
```

**How far it was checked.** Tested. `check.sh` starts DynamoDB Local in Docker and runs seven tests. Two of them are the point: 25 threads, each with its own client, race to consume one approval and exactly one wins; and three clients share one rate counter, so 20 of 25 calls pass and 5 are refused. The Terraform passes `terraform validate`. The patch applies to a copy of `platform/` and the result compiles.

**What it does not show.** I ran it against DynamoDB Local, not against the service. The table's expiry sweep (the `expires_at` attribute) is configured in Terraform and was not observed. The egress gateway has the same weakness in its rate counters, which section 4 handles by pinning each sandbox to one replica. I did not apply the store to it.

**10.2 An append-only audit store, and 10.3 watching the boundary.**

**The gap.** Audit lines go to each pod's log and are joined by a script (scenario S11). Anyone who can delete a pod log can change the record. And a runtime-security agent on the nodes would see nothing inside a microVM, so the design builds the trail from the boundaries instead. The deployed build does that from the platform's own lines. It does not record the network boundary itself.

**What I built.** Two Terraform folders and one picture. `audit-store/` is the bucket that nobody can edit, and the path into it. `boundary-monitoring/` adds two more sources of lines: every connection at the sandbox subnets (VPC Flow Logs) and every DNS question in the VPC (Route 53 Resolver query logs). Both feed the same bucket.

![Three sources go through CloudWatch Logs and Firehose into one S3 bucket with Object Lock](diagrams/appendix/a02-16.svg)

**What the picture shows.** Platform pod logs, flow logs and DNS query logs each land in a CloudWatch log group. A subscription filter sends each to one Kinesis Data Firehose stream, which writes batches to an S3 bucket under a date prefix. The platform filter passes only lines that contain a session ID. The other two pass everything. The bucket is the part that matters.

The highlighted lines, in order:

1. `object_lock_enabled = true`: turns Object Lock on for the bucket. It needs the versioning below.
2. `status = "Enabled"`: Object Lock needs versioning. A new write becomes a new version, and the old version stays.
3. `mode = "COMPLIANCE"`: in compliance mode, no user, including the account root user, can delete or overwrite a version before its retention ends ([S3 Object Lock](https://docs.aws.amazon.com/AmazonS3/latest/userguide/object-lock.html)). Governance mode would let a privileged user lift it.
4. `days = var.retention_days`: how long. The default is 365 days. A longer period costs storage and cannot be shortened for versions already written.
5. `kms_master_key_id = aws_kms_key.audit.arn`: objects are encrypted with a key of the account's own, with rotation on.

```hcl
# from alternatives/audit-store/terraform/main.tf
resource "aws_s3_bucket" "audit" {
  bucket              = "${var.project}-audit-${var.account_id}"
  object_lock_enabled = true #!
# ...
resource "aws_s3_bucket_versioning" "audit" {
  bucket = aws_s3_bucket.audit.id
  versioning_configuration {
    status = "Enabled" #!
# ...
resource "aws_s3_bucket_object_lock_configuration" "audit" {
  bucket = aws_s3_bucket.audit.id
  rule {
    default_retention {
      mode = "COMPLIANCE" #!
      days = var.retention_days #!
    }
  }
  depends_on = [aws_s3_bucket_versioning.audit]
# ...
      kms_master_key_id = aws_kms_key.audit.arn #!
```

The highlighted lines, in order:

1. `for_each = toset(var.sandbox_subnet_ids)`: one flow log per sandbox subnet, so every sandbox node is covered.
2. `traffic_type = "ALL"`: accepted and rejected traffic both. A rejected connection is the interesting one.
3. `max_aggregation_interval = 60`: records are grouped over one minute, the shortest choice, so a burst is visible while it still matters.
4. `resource_id = var.vpc_id`: the query log covers the whole VPC. It cannot be limited to the sandbox subnets.

```hcl
# from alternatives/boundary-monitoring/terraform/main.tf
resource "aws_flow_log" "sandbox" {
  for_each                 = toset(var.sandbox_subnet_ids) #!
  subnet_id                = each.value
  traffic_type             = "ALL" #!
  log_destination_type     = "cloud-watch-logs"
  log_destination          = aws_cloudwatch_log_group.flow.arn
  iam_role_arn             = aws_iam_role.flow.arn
  max_aggregation_interval = 60 #!
# ...
resource "aws_route53_resolver_query_log_config_association" "dns" {
  resolver_query_log_config_id = aws_route53_resolver_query_log_config.dns.id
  resource_id                  = var.vpc_id #!
```

**How far it was checked.** Checked. Both folders pass `terraform validate`. I did not apply them, so no line has travelled this path, and I did not see the Object Lock refuse a delete.

**What is missing.** Three things, stated plainly. The deployed design has no agent that ships pod output to CloudWatch, and the platform filter assumes one writes to the log group named in the variable. I did not build that shipper. Second, the flow logs record what reaches the VPC's network. I expect a connection that a network policy drops on the node not to appear there, but I did not test it. Third, the DNS log covers the VPC, so it also carries names asked by the platform's own pods. Neither source sees inside the microVM, which is the limit the design itself states.

**10.4 Image signing and a private mirror.**

**The gap.** The sandbox images are public images, pulled by tag through the NAT gateway. A tag can be moved to different contents. The design wants images pinned, signed and mirrored.

**What I built.** An ECR mirror of the two public registries the platform pulls from, with tags that cannot be moved. A KMS key that signs images, so the private half never leaves KMS. An admission rule that lets a pod in only if every image comes from the mirror and carries a signature made with that key.

![From public registries through the mirror and the signing key to the admission rule](diagrams/appendix/a02-18.svg)

**What the picture shows.** Images are pulled into the ECR mirror, signed once with the KMS key, and checked at admission by Kyverno against the matching public key. Kyverno also rewrites each image reference to its digest, so what was signed is exactly what runs. An unsigned image, an image signed with another key and an image from a public registry are all refused at admission.

The highlighted lines, in order:

1. `image_tag_mutability = "IMMUTABLE"`: a tag in a mirrored repository cannot be pushed again with different contents.
2. `key_usage = "SIGN_VERIFY"`: this key can sign and verify and cannot encrypt, so it has one job.
3. `customer_master_key_spec = "ECC_NIST_P256"`: the key type that cosign signs with through KMS.

```hcl
# from alternatives/image-trust/terraform/main.tf
resource "aws_ecr_repository_creation_template" "mirror" {
  for_each             = toset(["ecr-public", "k8s"])
  prefix               = each.value
  applied_for          = ["PULL_THROUGH_CACHE"]
  image_tag_mutability = "IMMUTABLE" #!
# ...
resource "aws_kms_key" "image_signing" {
  description              = "${var.project} image signing"
  key_usage                = "SIGN_VERIFY" #!
  customer_master_key_spec = "ECC_NIST_P256" #!
```

The highlighted lines, in order:

1. `failurePolicy: Fail`: if Kyverno cannot be reached, pods are refused rather than admitted unchecked.
2. `mutateDigest: true`: the image reference is rewritten to its digest, so the pod runs the bytes that were verified and not whatever the tag points to later.
3. `required: true`: an image with no signature is refused.
4. `publicKeys:`: the signature must verify against this key, the public half of the KMS key. A signature from any other key is refused.
5. `ignoreTlog: true`: no public transparency log is consulted. The key is the whole trust anchor, so who may use the KMS key matters.
6. `failureAction: Enforce`: in the second rule: it blocks, it does not just warn.
7. `image: "111122223333..."`: every container image must come from the account's own registry. This catches an image that was never put through the mirror.

```yaml
# from alternatives/image-trust/kyverno-policy.yaml
    failurePolicy: Fail #!
# ...
      verifyImages:
        - imageReferences:
            - "111122223333.dkr.ecr.us-east-1.amazonaws.com/*"
          failureAction: Enforce
          mutateDigest: true #!
          verifyDigest: true
          required: true #!
# ...
                    publicKeys: |- #!
# ...
                      ignoreTlog: true #!
# ...
      validate:
        failureAction: Enforce #!
# ...
              - image: "111122223333.dkr.ecr.us-east-1.amazonaws.com/*" #!
```

**How far it was checked.** The signing is tested; the rest is checked. `check.sh` starts a local registry and signs an image with cosign using a throwaway key. It verifies that the signed image passes, the same image fails against a second key, and an unsigned image fails. The two refusals only count if the first check passes, and the script is written that way. The Terraform passes `terraform validate`. The Kyverno policy parses, and its field names follow [Kyverno's documentation](https://kyverno.io/docs/policy-types/cluster-policy/verify-images/), but **I did not run it on a cluster**. The mirror follows the [ECR pull-through cache documentation](https://docs.aws.amazon.com/AmazonECR/latest/userguide/pull-through-cache.html), and I did not apply it.

**Not tested.** Signing used a local key pair, not the KMS key, so cosign's KMS path was not exercised. Signing the images that the mirror pulls is a step the build job has to do, and no job is written. The policy hard-codes a placeholder account and region, and the public key must be filled in.

**10.5 A separate certificate authority per sandbox.**

**The gap.** The gateway ends TLS for the one internal host that needs a credential, so every sandbox must trust a certificate authority that the gateway controls. In the tested build that is one authority shared by all sandboxes. If its key leaked, it could be used to impersonate any site to any sandbox.

**What I built.** A small module that creates one authority per session, limited so that a leaked key is of little use.

The highlighted lines, in order:

1. `ec.generate_private_key(ec.SECP256R1())`: a new key for every session. Nothing is shared between sessions.
2. `.not_valid_after(now + valid_for)`: the authority expires with the session. The default is two hours.
3. `BasicConstraints(ca=True, path_length=0)`: it is an authority that cannot create more authorities.
4. `NameConstraints(permitted_subtrees=[x509.DNSName(host)])`: marked critical. A client that honours it accepts a certificate from this authority only for this one host name.

```python
# from alternatives/session-ca/session_ca.py
    key = ec.generate_private_key(ec.SECP256R1()) #!
# ...
        .not_valid_before(now - datetime.timedelta(minutes=5)).not_valid_after(now + valid_for) #!
# ...
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True) #!
# ...
        .add_extension(x509.NameConstraints(permitted_subtrees=[x509.DNSName(host)], excluded_subtrees=None), critical=True) #!
```

**How far it was checked.** Tested, with the `cryptography` library and with the OpenSSL command line as a second opinion. Four tests: a certificate for the permitted host verifies; a certificate for any other host is refused even though it was signed with the authority's own key, which is the stolen-key case; another session's authority does not verify the certificate; and the authority certificate carries the limits above. Each refusal has a positive control, so a refusal cannot come from a broken test.

**Not tested.** Whether the clients inside real sandboxes enforce name constraints. OpenSSL and the `cryptography` verifier do. A client that ignores a critical extension would accept the certificate, so this has to be checked for each runtime in the sandbox image. The gateway code that would call this module is not written.

**10.6 A warm-pool autoscaler.**

**The gap.** The pool sizes are fixed at 2 and 1. A claim takes one booted pod at once, and the pool boots a replacement. Claims that arrive while the replacement boots get a cold start, which scenario S04 measured at about 10 seconds with room on a node and 60 seconds when a node had to be launched.

**What I built.** A sizing rule, a Manager patch that counts claims and reports a desired size, and KEDA objects that scale the pool to that size. A SandboxWarmPool has a scale subresource, and KEDA can scale any resource that has one ([KEDA](https://keda.sh/docs/latest/concepts/scaling-deployments/)).

The highlighted lines, in order:

1. `rate = claims_in_window / window_seconds`: claims per second over the recent window.
2. `max(floor, min(ceiling, math.ceil(rate * boot_seconds) + buffer))`: the pool must hold the claims that arrive during one boot of a replacement, plus a buffer, and stay between a floor and a ceiling.

```python
# from alternatives/warm-pool-autoscaler/pool_sizing.py
    rate = claims_in_window / window_seconds #!
    return max(floor, min(ceiling, math.ceil(rate * boot_seconds) + buffer)) #!
```

The highlighted lines, in order:

1. `kind: SandboxWarmPool`: KEDA's target is the pool itself, through its scale subresource.
2. `minReplicaCount: 2`: never below today's fixed size.
3. `maxReplicaCount: 20`: a ceiling, because every pooled pod costs a microVM that is idle.
4. `pollingInterval: 15`: KEDA asks the Manager every 15 seconds. How fast the pool shrinks is left to KEDA's defaults, which I did not tune.
5. `valueLocation: python-untrusted.desired`: KEDA reads the Manager's answer for this template.
6. `keyParamName: X-Internal-Token`: KEDA authenticates with the same internal token header that the gateways use, with a token of its own.
7. `resources: ["sandboxwarmpools/scale"]`: KEDA's service account may change the scale of pools and nothing else about them.

```yaml
# from alternatives/warm-pool-autoscaler/keda.yaml
  scaleTargetRef:
    apiVersion: extensions.agents.x-k8s.io/v1beta1
    kind: SandboxWarmPool #!
    name: python-untrusted-pool
# ...
  minReplicaCount: 2 #!
  maxReplicaCount: 20 #!
  pollingInterval: 15 #!
# ...
    - type: metrics-api
      metricType: AverageValue
      metadata:
        url: http://sandbox-manager.sandbox-platform.svc:8000/internal/pool-demand
        valueLocation: python-untrusted.desired #!
        targetValue: "1"
        authMode: apiKey
        method: header
        keyParamName: X-Internal-Token #!
# ...
    resources: ["sandboxwarmpools/scale"] #!
```

**How far it was checked.** The sizing rule is tested (six tests: the floor, the ceiling, the buffer, rounding up and bad input). The Manager patch applies to a copy of `platform/` and compiles. The Kubernetes kinds in `keda.yaml` pass the Kubernetes schemas. KEDA's own kinds were not schema-checked and **KEDA was never run**.

**Open points.** The label I used to let KEDA's operator reach the Manager (`app.kubernetes.io/name: keda-operator`) is what I expect from KEDA's chart, and I did not check it. The boot time that the rule assumes (60 seconds by default) is the cold node case from S04. A pool that is sized on claims from the last five minutes reacts after the burst starts, not before it.

**10.7 A persistent workspace, pause, resume and checkpoints.**

**The gap.** The workspace is a scratch volume that lives and dies with the pod. The design asks for a disk that survives, a way to pause a session and bring it back, and checkpoints.

**What AWS and agent-sandbox allow.** Three facts shape the design, and each has a source. A Nitro Sandbox can mount an EBS volume, one pod at a time, and it cannot be snapshotted, restored or moved while it runs ([AWS](https://docs.aws.amazon.com/eks/latest/userguide/sandboxing-limitations.html)). The agent-sandbox `Sandbox` has an `operatingMode`. `Suspended` deletes the pod and keeps the volume, and `Running` creates a new pod on the same volume. And a `SandboxTemplate` can declare `volumeClaimTemplates`, which gives every sandbox its own volume ([agent-sandbox](https://agent-sandbox.sigs.k8s.io/docs/volumes/volume-claim-template/)). So "pause" here means: keep the disk, drop the machine. The memory of a running process cannot be saved on Nitro Sandboxes.

![Running, suspended, running again, with a checkpoint and what ends the disk](diagrams/appendix/a02-17.svg)

**What the picture shows.** Suspending deletes the pod and leaves the volume. Resuming creates a new pod that mounts the same volume, so the files are back and the processes start again from them. A checkpoint is a snapshot of the volume. Deleting the claim deletes the volume with it, so a persistent session cannot use the delete-at-end-time policy that the deployed Manager sets.

The highlighted lines, in order:

1. `{"spec": {"operatingMode": mode}}, content_type="application/merge-patch+json"`: a merge patch that changes one field, Suspended or Running, and nothing else about the sandbox.
2. `persistentVolumeClaimName": f"{VOLUME_NAME}-{name}"`: the snapshot is of the volume that belongs to this session's sandbox, named by the pattern the controller uses.

```python
# from alternatives/persistent-workspace/workspace.py
def _set_mode(k8s, name, mode):
    code, resp = k8s.request("PATCH", f"{SANDBOXES}/{name}", {"spec": {"operatingMode": mode}}, content_type="application/merge-patch+json") #!
    if code >= 300:
        raise RuntimeError(f"could not set operatingMode {mode}: {code} {resp.get('message', '')}")
# ...
def checkpoint(k8s, session_id, label, snapshot_class="ds01-workspace"):
    """Snapshot the workspace volume. Returns the snapshot name."""
    name = sandbox_name(k8s, session_id)
    snap = f"{session_id}-{label}"
    manifest = {
        "apiVersion": "snapshot.storage.k8s.io/v1", "kind": "VolumeSnapshot",
        "metadata": {"name": snap, "namespace": NS, "labels": {"ds01/session": session_id}},
        "spec": {"volumeSnapshotClassName": snapshot_class, "source": {"persistentVolumeClaimName": f"{VOLUME_NAME}-{name}"}}, #!
```

The highlighted lines, in order:

1. `name: python-persistent`: generated from the real python-untrusted template. Only the name, one label, the workspace volume and the pool differ.
2. `accessModes: ["ReadWriteOncePod"]`: one pod at a time, which is what a sandbox can use for EBS.
3. `storageClassName: auto-ebs-sc`: an encrypted gp3 class that `storage.yaml` defines for the Auto Mode EBS driver.

```yaml
# from alternatives/persistent-workspace/sandbox-template-persistent.yaml
  name: python-persistent #!
# ...
  volumeClaimTemplates:
    - metadata:
        name: workspace                 # the PVC is named workspace-<sandbox name>
      spec:
        accessModes: ["ReadWriteOncePod"]   # one pod at a time; the only mode EKS Auto Mode supports for EBS in a sandbox #!
        storageClassName: auto-ebs-sc #!
        resources:
          requests: { storage: 2Gi }
```

**How far it was checked.** Checked and unit-tested, not run. Six unit tests use a fake Kubernetes: suspend sends one merge patch and deletes nothing, resume waits until the sandbox is Ready and times out otherwise, and a checkpoint names the right volume. `make_template.py` derives the persistent template from the real one, and `check.sh` fails if the committed file drifts from it. The template passes the CRD schemas of the pinned agent-sandbox release. That check is not decorative: it flags a misspelled field in a copy I broke on purpose, and it accepts the deployed templates. The Manager patch applies and compiles.

**Not built, and why.** Restoring a checkpoint into a new sandbox is not written. A warm-pool sandbox is already running, and Kubernetes cannot attach a volume to a running pod, which agent-sandbox's own GKE example works around with a privileged helper on each node ([agent-sandbox](https://agent-sandbox.sigs.k8s.io/docs/use-cases/examples/latebind-storage-gke-sandbox/)). A restore would need a cold start and a different path. Also not checked: what the `Retain` shutdown policy does to the volume when the end time passes, and whether a suspended session needs its own expiry, because a suspended session holds a disk that costs money.

**10.8 gVisor and Kata, 10.9 GPU sandboxes and 10.10 confidential mode.**

**The gap.** Section 6 gives one reason each. gVisor needs a containerd entry that Auto Mode does not allow. Kata needs nodes with nested virtualization. GPUs cannot pass into a Nitro Sandbox. Confidential mode removes `exec`. Each is real, so each alternative has to run on nodes that are not Auto Mode nodes, and says so.

**What I built.** Node pools and runtime classes for Kata, gVisor and gVisor with a GPU; one generated file of SandboxTemplates for the three; a Manager patch that lists them; and the written procedure for confidential mode. The templates are derived from the real `python-untrusted` template. Only the name, the runtime class, a label and (for the GPU one) a GPU limit differ. Everything that makes the sandbox safe is copied unchanged: no service account token, a read-only root, dropped capabilities, the one egress rule and the gateway's certificate.

| Alternative | Runs on | Source | Label |
|---|---|---|---|
| Kata | Self-managed Karpenter 1.13 or later, nodes with nested virtualization. Not Auto Mode, whose NodeClass has no such field. | [Karpenter blueprint](https://aws-samples.github.io/karpenter-blueprints/blueprints/nested-virtualization/) | From documentation |
| gVisor | Any current instance type, with the `runsc` binaries and a containerd entry added by user data | [gVisor](https://gvisor.dev/docs/user_guide/containerd/configuration/) | From documentation |
| GPU | gVisor with `nvproxy` on T4, A10G, L4, A100 and H100, with a driver version gVisor knows | [gVisor GPU](https://gvisor.dev/docs/user_guide/gpu/) | From documentation |
| Confidential | Nitro Sandbox runtime class `confidential-sandbox`, with a Cedar policy | [AWS](https://docs.aws.amazon.com/eks/latest/userguide/sandboxing-confidential-setup.html) | From documentation |

The highlighted lines, in order:

1. `nestedVirtualization: enabled`: two jobs in one field: it turns the feature on for the launched instance, and it makes Karpenter skip instance types that cannot do it ([Karpenter blueprint](https://aws-samples.github.io/karpenter-blueprints/blueprints/nested-virtualization/)).
2. `katacontainers.io/kata-runtime: "true"`: the Kata installer runs only on nodes with this label.
3. `startupTaints`: a new node is kept closed to sandbox pods until the Kata installer has finished and removed this taint.

```yaml
# from alternatives/isolation-tiers/kata/nodepool.yaml
  cpuOptions:
    nestedVirtualization: enabled      # sets the instance's CpuOptions and filters out families that cannot do it #!
# ...
        katacontainers.io/kata-runtime: "true"   # the kata-deploy DaemonSet installs Kata only where this label is set #!
# ...
      startupTaints: #!
        - { key: katacontainers.io/kata-runtime, effect: NoSchedule }
```

The highlighted lines, in order:

1. `runtime_type = "io.containerd.runsc.v1"`: tells containerd that a runtime named runsc is run through gVisor's shim. The runtime class's handler must carry the same name.
2. `sha512sum -c runsc.sha512`: the binary is checked against a checksum that comes from the same site, so this catches a damaged download and does not protect against a hostile site. Pin a release and bake the binary into the node image for real use.

```yaml
# from alternatives/isolation-tiers/gvisor/nodepool.yaml
            runtime_type = "io.containerd.runsc.v1" #!
# ...
    sha512sum -c runsc.sha512 #!
```

The highlighted lines, in order:

1. `ConfigPath = "/etc/containerd/runsc-gpu.toml"`: a second gVisor runtime, with its own settings file, so GPU nodes do not change the plain gVisor runtime.
2. `nvproxy = "true"`: turns on the proxy that forwards the sandbox's GPU driver calls to the host driver.
3. `karpenter.k8s.aws/instance-family: g4dn, g5, g6`: T4, A10G and L4 nodes, on gVisor's supported list.

```yaml
# from alternatives/isolation-tiers/gpu/nodepool.yaml
            ConfigPath = "/etc/containerd/runsc-gpu.toml" #!
# ...
    nvproxy = "true" #!
# ...
        - { key: karpenter.k8s.aws/instance-family, operator: In, values: ["g4dn", "g5", "g6"] } #!
```

**What is weaker.** gVisor is a user-space kernel, not a hardware boundary. With `nvproxy` the sandbox's GPU calls reach the host's NVIDIA driver, and gVisor's own documentation says it is much less effective against bugs in that driver, so driver updates are part of the security plan. The alternative to gVisor for a GPU is what AWS says: run GPU pods without a sandbox runtime class, on nodes used by one tenant at a time.

**Confidential mode.** `confidential/README.md` holds the procedure from the AWS page and a KMS key policy statement. The steps are: generate a Cedar policy from the exact final pod spec with `eks-nitro-sandbox-genpolicy`, attach it as an annotation, and put the predicted PCR17 value and the PCR8 service-name condition in the KMS key policy. The README lists why this does not drop into the deployed design as a template. The pod is built by the controller from a template and a pool, so the final spec is not in a file, and the policy must match it exactly. I did not check whether a canary pod read back from the cluster reproduces what the next pooled pod carries.

**How far it was checked.** The three templates pass the pinned CRD schemas, and the two runtime classes pass the Kubernetes schemas (`kubeconform`). The Manager patch applies and compiles. `make_templates.py` regenerates the templates and `check.sh` fails on drift. **The node pools, the Kata and gVisor installs, the GPU path and confidential mode were never run.** A named risk: whether the network policies on the sandbox namespace are enforced the same way for a Kata or gVisor pod as for a Nitro Sandbox pod. I did not check, and the deployed design depends on it.

**10.11 Narrower network rules.**

**The gap.** The defence-in-depth section explains that the security group rules between the node groups name groups and allow every port, and that the narrow control is the pod's network policy. Two things follow. The security groups can be narrower. And the platform pods have rules for who may call them, but none for where they may call, so a compromised Manager can open a connection to anything its node can reach.

The highlighted lines, in order:

1. `from_port = 10250`: the control plane may reach the kubelet and nothing else on the sandbox nodes, apart from sandboxd below.
2. `toset(["443", "3128"])`: a sandbox node may reach the cluster group on the API port and the gateway port only, instead of every port.
3. `from_port = 3128`: a security group of its own for the gateway pods: the proxy port, from sandbox nodes only, so the VPC refuses everything else before a network policy sees it.

```hcl
# from alternatives/network-gaps/terraform/main.tf
resource "aws_vpc_security_group_ingress_rule" "sandbox_kubelet" {
  security_group_id            = var.sandbox_sg_id
  referenced_security_group_id = var.cluster_sg_id
  ip_protocol                  = "tcp"
  from_port                    = 10250 #!
  to_port                      = 10250
# ...
resource "aws_vpc_security_group_egress_rule" "sandbox_to_cluster" {
  for_each                     = toset(["443", "3128"]) #!
  security_group_id            = var.sandbox_sg_id
  referenced_security_group_id = var.cluster_sg_id
  ip_protocol                  = "tcp"
  from_port                    = tonumber(each.value)
# ...
resource "aws_vpc_security_group_ingress_rule" "gateway_from_sandboxes" {
  security_group_id            = aws_security_group.gateway_pods.id
  referenced_security_group_id = var.sandbox_sg_id
  ip_protocol                  = "tcp"
  from_port                    = 3128 #!
  to_port                      = 3128
```

The highlighted lines, in order:

1. `port: 8000`: the tool gateway may call the Manager.
2. `port: 8443`: and the metrics API.
3. `port: 8001`: and the credential broker in the egress namespace.
4. `port: 53`: and DNS. Nothing else.
5. `egress: []`: the metrics API calls nobody, so it may call nobody.

```yaml
# from alternatives/network-gaps/egress-policies.yaml
metadata: { name: tool-gateway-egress, namespace: sandbox-platform }
spec:
  podSelector: { matchLabels: { app: tool-gateway } }
  policyTypes: [Egress]
  egress:
    - to: [{ podSelector: { matchLabels: { app: sandbox-manager } } }]
      ports: [{ protocol: TCP, port: 8000 }] #!
    - to: [{ podSelector: { matchLabels: { app: metrics-api } } }]
      ports: [{ protocol: TCP, port: 8443 }] #!
    - to:
        - namespaceSelector: { matchLabels: { kubernetes.io/metadata.name: sandbox-egress } }
          podSelector: { matchLabels: { app: credential-broker } }
      ports: [{ protocol: TCP, port: 8001 }] #!
    - to: [{ ipBlock: { cidr: 0.0.0.0/0 } }]
      ports: [{ protocol: UDP, port: 53 }, { protocol: TCP, port: 53 }] #!
# ...
metadata: { name: metrics-api-egress, namespace: sandbox-platform }
spec:
  podSelector: { matchLabels: { app: metrics-api } }
  policyTypes: [Egress]
  egress: [] #!
```
The file has one egress policy for each platform component (Manager, tool gateway, metrics API, router and orchestrator). I read each component's calls from its source and from `k8s/40-platform.yaml`. The credential broker and the gateway's own calls to the cloud are not covered.

**Security groups for individual pods.** `securitygrouppolicy.yaml` gives the gateway pods a security group of their own. This is an EKS Auto Mode feature for Kubernetes 1.36 and later ([AWS](https://docs.aws.amazon.com/eks/latest/userguide/auto-sgp.html)). Each matched pod gets a branch network interface, which lowers how many pods fit on a node, and a pod can match only one policy. I did not apply it to sandbox pods, because the AWS page does not say whether it reaches a pod inside a Nitro Sandbox.

**How far it was checked.** Checked. The Terraform passes `terraform validate`, and the five policies pass the Kubernetes schemas. **Not run.** Two risks: the security group ports are what the design is known to use, and a node needs more to join a cluster and pull images, so a missed port would show as nodes that never become Ready. Try it on a throwaway cluster first. And the egress rules allow the API server by the VPC's address range, because the control plane's own addresses are not exported by this implementation. The security group rules must replace the three deployed ones, because rules add up and the old, wider ones would still apply.

**10.12 Snapshots and multi-cluster placement: a decision note.** The design lists both as open questions. I did not build either, and I looked at what the platforms allow.

| Question | What I found | Decision |
|---|---|---|
| Snapshot a running sandbox, with its memory | AWS says Nitro Sandboxes do not support snapshot, restore or live migration ([limitations](https://docs.aws.amazon.com/eks/latest/userguide/sandboxing-limitations.html)). agent-sandbox has memory snapshots only on GKE with gVisor. | Not possible on this runtime. Section 10.7's checkpoint saves files, not memory. |
| Checkpoint the files | EBS snapshots work, and EKS Auto Mode is compatible with the snapshot controller but does not install it ([AWS](https://docs.aws.amazon.com/eks/latest/userguide/csi-snapshot-controller.html)). | Covered in 10.7. |
| Place sessions across clusters (CPU and GPU) | Out of scope for one cluster. | This is Design 02, Appendix C. |


**The files.** `alternatives/README.md` has the index with the label for each folder. `alternatives/check-all.sh` runs everything that can run on one machine. Two helpers are shared: `validate-terraform.sh` runs `terraform validate` in a throwaway copy, and `validate_agent_sandbox_crds.py` checks agent-sandbox YAML against the CRD schemas of the pinned release, after it verifies the download against the SHA-256 that Terraform pins.

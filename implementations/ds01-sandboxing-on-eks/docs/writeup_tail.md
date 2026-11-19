### 6. What I did not build, and why

**Scenario.** A portfolio piece is only credible if it says where it stops. These are the parts of Design 01 that this implementation does not cover, with the reason for each.

| Not built | Why |
|---|---|
| **GPU sandboxes** | The design lists this as an open question. AWS documents that Nitro Sandboxes do not support GPU or other device passthrough ([considerations](https://docs.aws.amazon.com/eks/latest/userguide/sandboxing-limitations.html)), so a GPU workload would need whole nodes without a sandbox runtime. A CPU sandbox proves the same isolation, network and credential design. |
| **gVisor (Tier 1) and Kata (Tier 2 alternative)** | EKS Auto Mode does not allow the shim gVisor needs, and Kata needs self-managed nodes with nested virtualization. Tiers 2 and 3 on Nitro Sandboxes are the design's default and are fully built. |
| **Confidential mode** | The design says agent sandboxes need isolated mode. Confidential mode is for regulated data and removes `exec`, which this design depends on. |
| **A persistent workspace volume, pause and resume, checkpoints** | The workspace is a size-limited scratch volume that lives and dies with the pod. The design's EBS-backed workspace and its pause and resume are not built. |
| **Snapshots and multi-cluster placement** | Open questions in the design itself. |
| **A separate certificate authority per sandbox** | One gateway CA is trusted by every sandbox. The design describes one per sandbox, discarded with it. |
| **A shared store for the tool gateway** | Approvals and rate counters are in memory, so the tool gateway runs as one replica. A production build keeps them in a shared store such as DynamoDB. |
| **An append-only audit store** | Audit lines go to each pod's log and are joined by a script. The design calls for an append-only store with its own retention. The EKS control-plane audit log does go to CloudWatch. |
| **A runtime-security agent on the nodes** | The design itself notes it sees nothing inside a microVM. The audit trail is built from the boundaries instead. |
| **Image signing and a private registry mirror** | The sandbox images are public images pulled through the NAT gateway. The design wants them pinned, signed and mirrored. |
| **A warm-pool autoscaler** | The pool sizes are fixed (2 and 1). |

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

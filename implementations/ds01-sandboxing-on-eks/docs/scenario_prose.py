"""Hand-written explanation for each test scenario. The commands, outputs and check results are NOT written here:
build_writeup.py takes those from the evidence recorded by the test run, so the document cannot drift from what happened.
"""

PROSE = {
    "S01": {
        "intro": "Before testing behaviour, I check that the cluster is built the way the design says: a dedicated pool of nodes that runs nothing but sandboxes, "
                 "in its own subnets, with its own security group and a minimal IAM role.",
        "trust": [
            "Each fact comes from the system that owns it: the Kubernetes API for pools and pods, the EC2 API for the instance, its subnet and metadata options, the IAM API for the role. None of it is read back from my own configuration files.",
            "The last check is a negative test. It tries to do the forbidden thing (put an ordinary pod on a sandbox node) and shows the scheduler refusing.",
        ],
        "why": [
            "The node carries the label for Nitro Sandboxes and the instance has Nitro Enclaves switched on because EKS Auto Mode launches sandbox-capable instances when a pod uses the sandbox runtime class. I did not configure that by hand.",
            "The taint and the subnet and security-group placement come from the NodePool and NodeClass I wrote (`k8s/10-sandbox-nodes.yaml`). The role has exactly two policies because Terraform attaches exactly two (`terraform/10-cluster/eks.tf`).",
        ],
    },
    "S02": {
        "intro": "This is the central claim of the design: a sandbox does not share a kernel with the node or with other pods. I test it by putting a sandbox next to an ordinary hardened container "
                 "and asking each one the same three questions: what kernel are you running, what machine are you, and how long have you been up.",
        "trust": [
            "Both measurements are taken from inside the workload, by reading `/proc/uptime` and the firmware product name. The workload's author cannot set either value.",
            "The comparison is built to fail in a specific way if isolation were absent: a container that shared the node's kernel would report the node's uptime, not its own pod's age.",
            "The two pods run on the same cluster at the same time, so there is no difference in environment to explain the result.",
        ],
        "why": [
            "The sandbox reports a product name ending in `.enclave` and an uptime that matches its pod's age because the pod's runtime class is `isolated-sandbox`: the pod was started as a Firecracker microVM with its own freshly booted kernel.",
            "Do not read anything into the kernel version strings. In this run the sandbox and the node report the same version (6.18.62), so the version cannot tell them apart. The test relies on the two things that can: the machine identity and the uptime.",
            "The ordinary container reports its node's uptime and the node's exact kernel version because a normal container is only a set of processes on the node's kernel. That is the shared-kernel risk in arrow 1 of the design's threat model.",
        ],
    },
    "S03": {
        "intro": "Isolation protects the node. This scenario checks the other half of the threat model: what a hostile process finds inside the sandbox, and what the sandbox's identity is allowed to do in the cluster.",
        "trust": [
            "Pod settings are read from the live pod object, not from my template file.",
            "Permissions are asked of the Kubernetes API itself (`kubectl auth can-i --as ...`), so the answer is what the cluster will actually enforce.",
            "The privileged-pod test submits a real forbidden request and records the real rejection.",
        ],
        "why": [
            "The container runs as user 1000 on a read-only root filesystem with no service-account directory because the SandboxTemplate sets `runAsUser`, `readOnlyRootFilesystem` and `automountServiceAccountToken: false`.",
            "The ServiceAccount can do nothing because I bound no Role to it. The platform identities can do only their jobs because each Role in `k8s/40-platform.yaml` lists only those verbs.",
            "The privileged pod is refused because the `sandboxes` namespace carries the Pod Security `restricted` label (`k8s/00-namespaces.yaml`).",
        ],
    },
    "S04": {
        "intro": "A sandbox that takes half a minute to start makes an agent feel broken. The design answers with warm pools: sandboxes booted in advance, handed out by a claim. I test the fast path, the empty-pool path and the refill.",
        "trust": [
            "The start times are measured by the Manager from the moment it creates the claim to the moment the claim reports Ready, and printed by it. I did not time them by hand.",
            "Three sessions are started at the same instant against a pool of two, so the third has to take the slow path. The test needs that difference to appear for its second check to mean anything.",
        ],
        "why": [
            "Two sessions are ready in well under a second because the controller adopts a pod that is already running and has already pulled its image. Nothing boots on that path.",
            "The third takes much longer because no warm pod is free, so a new sandbox has to be created. It still succeeds: an empty pool degrades, it does not fail.",
            "How much longer depends on capacity. In the first full run on this build the third session took 60 seconds: the sandbox node had no room left, so Auto Mode launched a second `c7i.xlarge`. Its node claim was created 3 seconds after the three claims, and the Manager's log recorded the session ready at 60,012 ms. In the run shown above the node had room, and the cold start took about 10 seconds, which is the time to boot a new microVM. Size the warm pool for the arrival rate, and treat a node launch as the worst case.",
            "The pool returns to its desired size because the controller starts a replacement for every pod a claim adopts.",
        ],
    },
    "S05": {
        "intro": "This is the scenario the design's network section is written for. Code running in a sandbox is hostile. From there I try to reach the Kubernetes API, another sandbox, the node's cloud credentials, the internet by address, the internet with the proxy settings removed, and DNS.",
        "trust": [
            "The attacks run inside the sandbox, through the same router path a real agent would use, so the source address, identity and network are the real ones.",
            "Each attack reports the exact error it got (a timeout, or a name-resolution failure). A pass is therefore a recorded failure to connect, not an absence of output.",
            "Two checks are positive controls elsewhere in the suite (an allowed host works, S06), so a 'blocked' result here cannot be explained by the network simply being down.",
            "The last check comes from a trusted pod, not from a sandbox, to show that ingress is closed even to the orchestrator.",
        ],
        "why": [
            "Every outbound attempt times out because the only egress rule the template generates is 'TCP 3128 to the gateway's address'. The generated NetworkPolicy is printed at the top of the evidence.",
            "No name resolves because the template has no DNS rule at all. The sandbox's proxy setting names the gateway by IP address, and the gateway resolves names for it. That closes DNS as a channel for smuggling data out.",
            "Unsetting the proxy variables does not help because the proxy is not what stops the traffic. The network policy is.",
            "The metadata service answers, but only with placement data. Credentials return 404 and there is no Pod Identity endpoint. That comes from how Nitro Sandboxes serve the metadata address, plus the fact that I created no Pod Identity association for the sandbox's ServiceAccount. The write-up explains in section 4 why this differs from the design's wording.",
            "Another sandbox is unreachable, and so is a sandbox from the orchestrator, because the ingress rule admits only the router's pods in the router's namespace.",
        ],
    },
    "S06": {
        "intro": "The gateway is the only way out, so it has to make the right decision for each session. This scenario tests an allowed host, a refused host, per-session differences, a mistake in an allowlist that the network must catch, a rate limit, and the audit record.",
        "trust": [
            "Allow and deny are both observed from inside the sandbox (the tunnel is refused with 403 or it opens) and from the gateway's own log, which is queried by session ID.",
            "The ceiling test is designed so that the gateway's decision and the network's decision can be told apart. The gateway log must show an allow for the host while the connection still fails.",
            "The rate-limit test counts the 429 responses itself; it does not infer them.",
        ],
        "why": [
            "Session A reaches `pypi.org` and is refused `api.github.com` and `example.com` because the gateway matches each CONNECT request against that session's allowlist, which the Manager stored on the claim when the session was created.",
            "Session B reaches `example.com` because it was created with that host approved. Same image, same network, different list.",
            "Session C names `github.com`. The gateway allows it (the log says so), yet the connection never completes. That is the network ceiling: the gateway's own network policy lists only three domains, so a wrong allowlist cannot open the internet.",
            "The 429s appear because the gateway allows 120 requests a minute per session. A Service setting pins each sandbox to one gateway replica so the count is exact.",
        ],
    },
    "S07": {
        "intro": "The most common way an agent leaks a secret is by reading it from its own environment. The design removes the secret from the sandbox. I check that the sandbox holds only a placeholder, that a call needing a real credential still succeeds, and that the credential is short-lived, verified, and never logged.",
        "trust": [
            "The success is end to end. The protected API checks the signature, audience and expiry of the credential it receives and names the session it was minted for. The sandbox cannot forge that.",
            "The negative tests (a forged credential, no credential) call the real API and record its real 401 responses.",
            "The log search uses this session's actual token as the search string. Zero matches across every platform log is a measured fact about this run.",
        ],
        "why": [
            "The sandbox's environment holds the word `placeholder` because that is the literal value in the template, and the namespace holds no Secrets.",
            "The call succeeds because the gateway terminated TLS for that one host, asked the broker for a credential for this session, and replaced the `Authorization` header on the way out. The credential never travelled back to the sandbox.",
            "The lifetime is at most 900 seconds because the broker caps it per audience, and the API rejects expired credentials.",
            "The token is absent from the logs because the shared audit function redacts any field named like a secret, and no component logs request headers.",
        ],
    },
    "S08": {
        "intro": "Agents also call tools: internal APIs and services. The design puts those behind a gateway that knows who is asking. I test tool visibility by agent, a read call, human approval for a risky tool, a forbidden tool, a rate limit, calling from inside a sandbox, and revocation.",
        "trust": [
            "Every call is a real JSON-RPC request to the gateway with a real session token; the responses printed are the gateway's own.",
            "The approval flow is tested adversarially: retry before approval, reuse after approval, call a tool the agent has no right to.",
            "Revocation is tested by ending the session and then calling again with the old, still unexpired, token.",
        ],
        "why": [
            "The two agents see different tool lists because the gateway filters by the agent name carried inside the signed session token.",
            "The risky tool pauses because it is marked high risk. The approval is bound to the session, the tool and the arguments, and is consumed by one call.",
            "The 429s appear after 20 calls a minute because that is the gateway's limit per session.",
            "The old token stops working at once because the gateway asks the Manager whether the session is still active on every call, rather than trusting the token's expiry alone.",
        ],
    },
    "S09": {
        "intro": "Some work needs no network at all, such as running an analysis on an uploaded file. Tier 3 of the design boots the microVM with no network device. I run a real analysis in one and confirm that nothing can leave it.",
        "trust": [
            "The analysis result is computed inside the sandbox and read back through the router, so a correct total proves the code ran there.",
            "The no-network check lists the interfaces the guest kernel actually has and attempts a connection. It does not rely on a configuration flag.",
        ],
        "why": [
            "Commands and files still work with no network because the router uses the Kubernetes exec channel for this tier, which reaches the guest over a virtual socket rather than over the network.",
            "No route out exists because the pod annotation makes the microVM boot without a network interface. There is nothing to filter and nothing to misconfigure.",
        ],
    },
    "S10": {
        "intro": "Abuse comes in three forms: too many sessions, sessions that never end, and code that eats memory. Each has its own control, and each is tested.",
        "trust": [
            "The quota test creates real sessions until the limit and records the real refusal; a second tenant is created afterwards to show the limit is per tenant.",
            "The end-time test creates a session, does nothing, and waits for the cluster to remove it.",
            "The memory test asks for more memory than the sandbox has, then checks the abuser's restart count, its neighbour's restart count, and the node's condition.",
        ],
        "why": [
            "The fourth session is refused because the Manager counts a tenant's active claims against a limit of three.",
            "The claim disappears on its own because the Manager stamped an end time and a delete policy on it, and the controller enforces both.",
            "The runaway process is contained because the memory limit is enforced by the guest kernel inside the sandbox's own microVM. The container is killed and restarted. The neighbour sandbox and the node are untouched, because they are separate kernels.",
        ],
    },
    "S11": {
        "intro": "A customer asks what the agent did with their data. The design answers with one query. I run a realistic session, then run that query.",
        "trust": [
            "The timeline is assembled from the components' own logs, sorted by their own timestamps. I do not add or edit lines.",
            "The checks look for specific events that only the right component could have written.",
        ],
        "why": [
            "One query finds records in every component because they all write JSON lines carrying the same session ID, and the query reads them by that ID.",
            "Credentials appear by ID and never by value because the audit function redacts secret-like fields.",
        ],
    },
    "S12": {
        "intro": "The design's failure table says how each part fails. I remove two parts of the running system and check that it fails the way the table says.",
        "trust": [
            "The same drill runs before, during and after, in the same sandbox, so the only variable is the component I removed.",
            "Components are removed and restored with ordinary Kubernetes commands that are shown in the evidence.",
        ],
        "why": [
            "With the gateway removed the sandbox loses outbound access because the gateway is its only route out. The system fails closed.",
            "With the controller removed a running session keeps working because the controller only reconciles claims. The sandbox and the router's path to it do not depend on it.",
        ],
    },
}

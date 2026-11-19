# Egress gateway on Envoy (alternative, tested in a lab only)

The platform's egress gateway runs on mitmproxy (`platform/ds01/gateway_addon.py`). This folder holds the same gateway written on Envoy, in case the platform ever needs it. `up.sh` does not use anything here.

**Status.** It ran in Docker on one machine, with test host names and a policy service that reads a table of sessions. It did not run on EKS, it does not call the real Manager or credential broker, and it is not part of the 12 scenarios or the 75 checks. The write-up is section 9 of Appendix B.

| File | What it is |
|---|---|
| `envoy.yaml` | The Envoy configuration, tested with Envoy v1.40.2. |
| `policy_service.py` | The lab policy service for Envoy's `ext_authz` filter (gRPC). It finds the session by source address, checks the allowlist and a rate limit, returns the session ID, and supplies the credential for the one injected host. |
| `lab/run-lab.sh` | Builds the lab, runs nine checks, prints PASS or FAIL for each and removes everything it started. Needs Docker and nothing else. |
| `lab/server.py` | A TLS server that plays the three lab hosts. |

Run it: `./lab/run-lab.sh`

**To use it for real.** In `policy_service.py`, replace the session table with the two calls the mitmproxy gateway makes: ask the Manager which session owns the source address, and ask the credential broker for a credential. Run Envoy as the gateway Deployment on the same address and port. Then run scenarios S05, S06 and S07 against it. None of that has been done.

**Known gaps against the mitmproxy gateway.** No per-session byte budget. The hosts that receive a credential are fixed in `envoy.yaml` (one route, one certificate and one upstream cluster each). The rate-limit counters live in the policy service's memory, so replicas need session pinning or a shared store.

# Confidential mode (not built)

What it is: the Nitro Sandbox runtime class `confidential-sandbox`. The parent instance is treated as untrusted, exec and stdin
are denied, the overlay is encrypted, and a Cedar policy pins what the sandbox may run. Source: the EKS documentation page
"Run a pod in confidential mode".

The steps, from that page:

1. Write the exact pod spec that will run, after every webhook has touched it.
2. `eks-nitro-sandbox-genpolicy --profile confidential manifest.yaml` prints a base64 policy and the predicted PCR17 value.
3. Put the policy in the pod's `sandbox.amazonaws.com/policy` annotation.
4. Put the PCR17 value in the KMS key policy of every key that holds a secret for this workload, with the PCR8 service-name
   condition (`kms-key-policy-statement.json`). Without this condition the measurement is recorded and nothing checks it.
5. Regenerate 2 to 4 whenever the pod spec changes, including a new image digest.

Why this does not drop into the deployed design as a template:

* The pod is built by the agent-sandbox controller from a template and a warm pool, so the final spec is not in a file. The policy
  has to be generated from a pod that was actually admitted, for example by reading a canary pod back with `kubectl get pod -o yaml`.
  Whether that reproduces byte-for-byte what the next pod from the pool will carry has not been checked.
* Exec is denied. The router talks to sandboxd over the network, so this should not matter, but it was not run.
* `--require-any-signer` replaces the digest pin with an AWS Signer signature. That needs network access from the sandbox to the
  Signer endpoint, which the deployed egress rule (one way out, to the gateway) does not allow. It cannot be combined with
  `sandbox.amazonaws.com/network-isolated`.

Status: the documentation was read; nothing was run, because it needs a cluster with the runtime installed.

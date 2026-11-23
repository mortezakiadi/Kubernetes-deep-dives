#!/usr/bin/env bash
# Fails if anything staged (or, with --all, anything tracked or untracked-but-not-ignored)
# looks like a credential or one of this implementation's private values.
# The private values (account ID, my IP) are read from the local, git-ignored tfvars,
# so this script itself contains no secrets.
set -uo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
MODE="${1:---staged}"
cd "$REPO_ROOT"

if [ "$MODE" = "--all" ]; then
  git ls-files --cached --others --exclude-standard -z
else
  git diff --cached --name-only --diff-filter=ACMR -z
fi | grep -z -v -E '(^|/)check-no-secrets\.sh$|\.(png|jpg|jpeg|gif|pdf)$' > /tmp/ds08-scan-files.$$

if [ ! -s /tmp/ds08-scan-files.$$ ]; then echo "check-no-secrets: nothing to scan"; rm -f /tmp/ds08-scan-files.$$; exit 0; fi

# Generic credential shapes, as one alternation so each file is read once.
PATTERN='AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16}|aws_secret_access_key|aws_session_token|AWS_SECRET_ACCESS_KEY|AWS_SESSION_TOKEN|-----BEGIN [A-Z ]*PRIVATE KEY-----|ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|xox[baprs]-[A-Za-z0-9-]{10,}|eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}|client-certificate-data|client-key-data|certificate-authority-data'

fail=0
# A Terraform line that only refers to the cluster CA attribute (ca_b64) holds no value, so it is not a hit.
hits=$(xargs -0 grep -I -H -E -e "$PATTERN" < /tmp/ds08-scan-files.$$ 2>/dev/null | grep -v -F 'ca_b64' | cut -d: -f1 | sort -u)
if [ -n "$hits" ]; then echo "BLOCKED: credential-shaped text in:"; echo "$hits" | sed 's/^/  /'; fail=1; fi

# Private values of this implementation, read from local files only.
LIT=/tmp/ds08-scan-literals.$$
: > "$LIT"
for f in "$REPO_ROOT"/implementations/*/terraform/*/local.auto.tfvars; do
  [ -f "$f" ] || continue
  sed -n 's/^[a-z_]*[[:space:]]*=[[:space:]]*"\(.*\)"/\1/p' "$f" | sed 's#/32$##' >> "$LIT"
done
if [ -s "$LIT" ]; then
  hits=$(xargs -0 grep -I -l -F -f "$LIT" < /tmp/ds08-scan-files.$$ 2>/dev/null)
  if [ -n "$hits" ]; then echo "BLOCKED: a private value from the local tfvars appears in:"; echo "$hits" | sed 's/^/  /'; fail=1; fi
fi
rm -f "$LIT" /tmp/ds08-scan-files.$$

if [ "$fail" -ne 0 ]; then
  echo "check-no-secrets: FAILED. Remove or redact the above before committing."
  exit 1
fi
echo "check-no-secrets: OK"

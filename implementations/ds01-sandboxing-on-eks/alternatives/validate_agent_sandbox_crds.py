"""Checks agent-sandbox YAML against the CRD schemas of the pinned release, with no cluster.

    python3 validate_agent_sandbox_crds.py file.yaml [file.yaml ...]

Downloads sandbox-with-extensions.yaml for the version Terraform installs, refuses it if its SHA-256 differs from the pin, and
checks every Sandbox, SandboxTemplate, SandboxClaim and SandboxWarmPool document in the files. Two kinds of fault are reported:
a value of the wrong type or outside an enum, and a field the schema does not have (a cluster would silently drop such a field).
${...} placeholders are replaced with dummy values first. Other kinds of document are skipped.
Needs: pyyaml, jsonschema.
"""
import hashlib
import re
import sys
import urllib.request

import jsonschema
import yaml

VERSION = "v1.0.10"
SHA256 = "7cc1eca2fb45844b0ec0c35fc975340dc01af2994301ffa8bf05db1ad1e397ee"
URL = f"https://github.com/kubernetes-sigs/agent-sandbox/releases/download/{VERSION}/sandbox-with-extensions.yaml"
DUMMY = {"gateway_ip": "10.20.1.10", "sandboxd_sha256": "0" * 64}


def load_schemas():
    raw = urllib.request.urlopen(URL, timeout=60).read()
    if hashlib.sha256(raw).hexdigest() != SHA256:
        sys.exit(f"the manifest at {URL} does not match the pinned SHA-256")
    schemas = {}
    for doc in yaml.safe_load_all(raw):
        if doc and doc.get("kind") == "CustomResourceDefinition":
            for version in doc["spec"]["versions"]:
                key = (f'{doc["spec"]["group"]}/{version["name"]}', doc["spec"]["names"]["kind"])
                schemas[key] = version["schema"]["openAPIV3Schema"]
    return schemas


def unknown_fields(value, schema, path=""):
    """Fields present in the document that a structural schema does not declare."""
    found = []
    if not isinstance(value, dict) or not isinstance(schema, dict):
        if isinstance(value, list) and isinstance(schema, dict) and "items" in schema:
            for i, item in enumerate(value):
                found += unknown_fields(item, schema["items"], f"{path}[{i}]")
        return found
    if schema.get("x-kubernetes-preserve-unknown-fields"):
        return found
    props = schema.get("properties", {})
    extra = schema.get("additionalProperties")
    for key, sub in value.items():
        if key in props:
            found += unknown_fields(sub, props[key], f"{path}.{key}")
        elif isinstance(extra, dict):
            found += unknown_fields(sub, extra, f"{path}.{key}")
        elif extra is True:
            continue
        elif props:
            found.append(f"{path}.{key}")
    return found


def check_file(name, schemas):
    text = open(name).read()
    text = re.sub(r"\$\{(\w+)\}", lambda m: DUMMY.get(m.group(1), "1"), text)
    problems, checked = [], 0
    for doc in yaml.safe_load_all(text):
        if not doc:
            continue
        schema = schemas.get((doc.get("apiVersion"), doc.get("kind")))
        if schema is None:
            continue
        checked += 1
        label = f'{doc["kind"]}/{doc["metadata"]["name"]}'
        for err in jsonschema.Draft7Validator(schema).iter_errors(doc):
            problems.append(f"{label}: {'.'.join(str(p) for p in err.absolute_path)}: {err.message[:120]}")
        for field in unknown_fields(doc.get("spec", {}), schema["properties"]["spec"], ".spec"):
            problems.append(f"{label}: unknown field {field}")
    return checked, problems


if __name__ == "__main__":
    schemas = load_schemas()
    failed = False
    for name in sys.argv[1:]:
        checked, problems = check_file(name, schemas)
        print(f"{name}: {checked} agent-sandbox documents checked, {len(problems)} problems")
        for p in problems:
            print("  ", p)
        failed = failed or bool(problems) or checked == 0
    sys.exit(1 if failed else 0)

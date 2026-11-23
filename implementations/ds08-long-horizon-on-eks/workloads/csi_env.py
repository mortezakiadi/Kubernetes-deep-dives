"""Environment state on a CSI volume, for the VolumeSnapshot branch path (Design 08, "Branching rollouts", the YAML).

    csi_env.py seed  --snapshot KEY --dir /workspace      write a recorded snapshot's files into the volume
    csi_env.py check --dir /workspace --expect SHA        hash the volume's files and compare with a recorded state

The seed pod writes the working copy onto an EBS volume; Kubernetes snapshots the volume (VolumeSnapshot); each branch
pod gets a NEW volume restored from that snapshot (an ephemeral volume whose dataSource is the snapshot) and checks
that it holds exactly the recorded state before any branch work would start.
"""
import argparse
import json
import pathlib
import time

from common import get
from sandbox import Workspace


def main():
    p = argparse.ArgumentParser()
    p.add_argument("mode", choices=["seed", "check"])
    p.add_argument("--snapshot")
    p.add_argument("--dir", required=True)
    p.add_argument("--expect")
    a = p.parse_args()
    root = pathlib.Path(a.dir) / "repo"
    t0 = time.time()
    if a.mode == "seed":
        ws = Workspace.restore(root, get(a.snapshot, raw=True))
        import os
        os.sync()
    else:
        ws = Workspace(root)
    sha = ws.tree_hash()[:16] if root.exists() else None
    print(json.dumps({"mode": a.mode, "files": len(ws.files()) if root.exists() else 0, "tree_sha256": sha,
                      "matches": (sha == a.expect) if a.expect else None, "seconds": round(time.time() - t0, 2)}), flush=True)


if __name__ == "__main__":
    main()

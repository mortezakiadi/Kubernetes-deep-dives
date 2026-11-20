#!/bin/bash
# Entry point the platform's training runtime gives every TrainJob. The Trainer controller sets the PET_* variables
# (number of nodes, this node's rank, the master address) that torchrun reads.
set -e
pip install -q --no-cache-dir boto3 2>&1 | tail -1 || true
exec torchrun /workloads/train.py "$@"

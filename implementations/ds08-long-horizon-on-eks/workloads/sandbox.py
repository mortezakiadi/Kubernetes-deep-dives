"""An agent's environment: a working copy of a task's repository and the tools the agent can call on it.

The environment is deterministic by contract (Design 08, "The trajectory as data"): the same files and the same
action always give the same observation. Nothing here reads a clock, a random number or the network, test output
is normalised (timings removed), and file listings are sorted. That is what lets a branch restored from a snapshot
continue exactly where the original trajectory was.

Snapshots are the whole working copy as a gzip tar with fixed metadata, so the same state always gives the same
bytes and the same hash.
"""
import gzip
import hashlib
import io
import json
import os
import pathlib
import re
import resource
import shutil
import subprocess
import sys
import tarfile

MAX_OBS = 1800          # characters of an observation the agent sees
TEST_TIMEOUT_S = 20


def _limits():
    # Linux applies all three; macOS (local self-test only) refuses RLIMIT_AS, so each is best effort.
    for res, val in ((resource.RLIMIT_CPU, 20), (resource.RLIMIT_AS, 1024 * 2**20), (resource.RLIMIT_FSIZE, 8 * 2**20)):
        try:
            resource.setrlimit(res, (val, val))
        except (ValueError, OSError):
            pass


class Workspace:
    def __init__(self, root):
        self.root = pathlib.Path(root)

    # ---- state ---------------------------------------------------------------------------------------------------
    @classmethod
    def from_files(cls, root, files):
        ws = cls(root)
        if ws.root.exists():
            shutil.rmtree(ws.root)
        ws.root.mkdir(parents=True)
        for p, c in files.items():
            ws.write_file(p, c)
        return ws

    def files(self):
        out = {}
        for p in sorted(self.root.rglob("*")):
            if p.is_file() and "__pycache__" not in p.parts:
                out[str(p.relative_to(self.root))] = p.read_text(errors="replace")
        return out

    def tree_hash(self):
        h = hashlib.sha256()
        for p, c in self.files().items():
            h.update(p.encode() + b"\0" + c.encode() + b"\0")
        return h.hexdigest()

    def snapshot(self):
        """Deterministic gzip tar of the working copy: same state, same bytes."""
        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode="w") as tar:
            for p, c in self.files().items():
                data = c.encode()
                info = tarfile.TarInfo(p)
                info.size, info.mtime, info.mode, info.uid, info.gid = len(data), 0, 0o644, 0, 0
                tar.addfile(info, io.BytesIO(data))
        return gzip.compress(raw.getvalue(), mtime=0)

    @classmethod
    def restore(cls, root, blob):
        files = {}
        with tarfile.open(fileobj=io.BytesIO(gzip.decompress(blob)), mode="r") as tar:
            for m in tar.getmembers():
                files[m.name] = tar.extractfile(m).read().decode()
        return cls.from_files(root, files)

    # ---- tools ---------------------------------------------------------------------------------------------------
    def _safe(self, path):
        p = (self.root / str(path)).resolve()
        if not str(p).startswith(str(self.root.resolve()) + os.sep):
            raise ValueError("path is outside the repository")
        return p

    def write_file(self, path, content):
        p = self._safe(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)

    def run_tests(self, module=None):
        args = [sys.executable, "-m", "unittest"] + (["-q", module] if module else ["discover", "-q", "-s", "tests", "-t", "."])
        try:
            r = subprocess.run(args, cwd=self.root, capture_output=True, text=True, timeout=TEST_TIMEOUT_S, preexec_fn=_limits,
                               env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONHASHSEED": "0"})
            out, code = r.stdout + r.stderr, r.returncode
        except subprocess.TimeoutExpired:
            out, code = "(tests timed out)", 124
        out = re.sub(r"Ran (\d+) tests? in [0-9.]+s", r"Ran \1 tests", out)   # remove timings: determinism
        out = out.replace(str(self.root), ".")
        return code, out

    def act(self, action):
        """Run one tool call. Returns the observation text and a few facts for the trajectory."""
        tool = action.get("tool")
        try:
            if tool == "ls":
                obs = "\n".join(self.files())
            elif tool == "cat":
                obs = self._safe(action["path"]).read_text()
            elif tool == "replace":
                p = self._safe(action["path"])
                text = p.read_text()
                old, new = action["old"], action["new"]
                if old not in text:
                    obs = "ERROR: the old text was not found in " + action["path"]
                else:
                    p.write_text(text.replace(old, new, 1))
                    obs = "replaced 1 occurrence in " + action["path"]
            elif tool == "write":
                self.write_file(action["path"], action["content"])
                obs = "wrote " + action["path"]
            elif tool == "run_tests":
                code, out = self.run_tests(action.get("module"))
                obs = ("TESTS PASSED\n" if code == 0 else "TESTS FAILED\n") + out
            elif tool == "submit":
                obs = "submitted"
            else:
                obs = f"ERROR: unknown tool {tool!r}; use ls, cat, replace, write, run_tests or submit"
        except FileNotFoundError:
            obs = "ERROR: no such file " + str(action.get("path"))
        except (KeyError, ValueError, IsADirectoryError) as e:
            obs = f"ERROR: {type(e).__name__}: {e}"
        if action.get("_ignored_actions"):
            obs = f"(only your first ACTION ran; {action['_ignored_actions']} more were ignored)\n" + obs
        if len(obs) > MAX_OBS:
            obs = obs[:MAX_OBS] + f"\n... ({len(obs) - MAX_OBS} more characters)"
        return obs

    def diff_against(self, original):
        """Files changed, added or deleted relative to the original task files."""
        now = self.files()
        changed = {p: now.get(p) for p in sorted(set(now) | set(original)) if now.get(p) != original.get(p)}
        lines = 0
        for p, c in changed.items():
            a, b = (original.get(p) or "").splitlines(), (c or "").splitlines()
            lines += sum(1 for x, y in zip(a, b) if x != y) + abs(len(a) - len(b))
        return changed, lines


def action_key(action):
    return json.dumps(action, sort_keys=True)

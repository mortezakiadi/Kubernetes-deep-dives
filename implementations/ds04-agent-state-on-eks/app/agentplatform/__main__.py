"""One image, several roles: `python -m agentplatform <role>`."""
import importlib
import sys

ROLES = {"session-api": "session_api", "model-gateway": "model_gateway", "tool-gateway": "tool_gateway",
         "sandbox-manager": "sandbox_manager", "executor": "executor", "git-host": "git_host", "worker": "worker"}

if len(sys.argv) < 2 or sys.argv[1] not in ROLES:
    sys.exit(f"usage: python -m agentplatform {{{'|'.join(ROLES)}}}")
importlib.import_module(f"agentplatform.{ROLES[sys.argv[1]]}").main()

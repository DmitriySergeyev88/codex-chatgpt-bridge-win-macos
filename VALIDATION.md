# Validation

Verified on macOS with Python 3.12, MCP SDK 1.26.0 and Codex CLI 0.160.0.

- 22 automated tests passed: scoped OAuth/PKCE, token refresh/revocation, project isolation, restricted file access, queue idempotency, FIX/ACCEPT, stale revisions and crash recovery.
- A live launchd service passed health and mandatory OAuth checks, permitted an approved README read and denied .env, traversal, foreign project access and shell. Temporary probe tokens were revoked.
- Launchd reboot/off/on semantics were exercised, including refusal to reboot an intentionally stopped instance.
- A real Codex execution created a Python sum function and passed four unit tests. A real ChatGPT Architect reviewed the supplied result and returned ACCEPT. The queue then unlocked another task, which Codex completed with the original tests still passing.
- Live review used explicitly authorized Codex desktop messaging. Task preparation and review ingestion used a local driver, not a ChatGPT MCP connection. This validates real execution/review/next-task gating, but does not establish an unattended consumer-ChatGPT loop.
- The tested deployment stayed local. Public HTTPS/tunnel, attaching MCP in an Architect chat and autonomous chat wakeups were not configured.
- One non-blocking MCP SDK/Pydantic warning concerned the forward reference for the lifespan settings field.

Run tests from the repository root: `python -m pytest -q` after installing `requirements.lock` in a virtual environment. Local user paths, chat IDs, generated LaunchAgents, credentials and deployment state are excluded from this public source tree.

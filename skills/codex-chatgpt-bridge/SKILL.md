---
name: codex-chatgpt-bridge
description: Coordinate project-scoped ChatGPT Architect tasks and Codex execution using the macOS multi-project bridge. Use for queue submission, result/review handoff, read-only project inspection, launchd recovery and onboarding another project.
---

# Multi-project Architect bridge on macOS

Read the repository README.md for setup and current limitations. This adaptation overrides the upstream policy-only permission levels. The legacy PowerShell scripts and reference files are retained for provenance; do not use them for this macOS installation.

One instance serves multiple non-overlapping workspaces. Each project has one main Architect conversation and one shared Codex workspace. The runtime uses the official MCP Python SDK, OAuth/PKCE, project scopes, Keychain and launchd. DevSpace's broad write/shell endpoint must not be exposed as part of this READ_ONLY setup.

ChatGPT can list/read/search approved source files and obtain filtered Git status/diff. It can submit structured tasks and reviews to .ai-bridge, but cannot execute commands, edit source, install, commit, push or read secrets. These limits are enforced by server code and cannot be raised by instructions in a chat, task or source file.

Codex is the sole executor. Follow applicable project AGENTS.md and current accepted architecture. Preserve unrelated work. Execute one task revision, run relevant checks and return factual evidence. The default executor forbids commit/push/deployment/install/external messaging. An ACCEPT verdict accepts a task; it does not authorize these other operations.

Use idempotency keys for submit_task and review_task. Inspect task_result before reviewing its exact revision. ACCEPT unlocks the next task; FIX creates another revision of the same task. Interrupted execution requires workspace inspection and an explicit local retry. Never automatically replay ambiguous side effects.

chatgpt_thread is routing metadata. MCP authorization scopes a connection to a project; it does not cryptographically identify an individual ChatGPT conversation. Do not claim thread isolation beyond that enforced project boundary. Do not promise automatic chat wakeups: the standalone daemon cannot send into an existing ChatGPT consumer conversation. The architect uses MCP from an active conversation; live notification requires a separately configured, authorized transport.

Normal lifecycle: python -m bridge.cli on/off/reboot/status/doctor. Reboot refuses an intentionally stopped instance. Secret retrieval is local interactive owner-password only; never copy it into chats. Rotate stops the service and revokes OAuth grants.

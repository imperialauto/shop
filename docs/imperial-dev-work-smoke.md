# Development worktree smoke

Run `py -3.12 scripts/dev_work_smoke.py` on Windows or
`python3 scripts/dev_work_smoke.py` on Linux from a repository checkout.
The smoke creates a detached writer worktree, commits a fixed synthetic
fixture, then checks the committed diff and content in a fresh reviewer
worktree. Both worktrees are removed on completion. A successful JSON report
covers the fixture check only. The candidate commit remains local and is
unreferenced after cleanup.

The draft implementing this check can run CI on Linux and Windows using the
pull request workflow. Those checks verify the harness change. Candidate CI,
agent review, provider calls, hosted claims, and hosted persistence require
separate evidence.

Reuse the development queue from #148 and reconcile claim/report behavior
with #149 before connecting a worker. The diagnostic node in #158 has its own
credentials and handles diagnostic drafts. Link development evidence to the
canonical task when #151 supplies the task linkage.

The synthetic smoke uses Git and Python 3.12. On this Windows machine the
Python launcher resolves 3.12, while the plain python command resolves to
the Store alias. Codex and Ollama executables were found. Claude Code and
OpenCode were absent from PATH. A home worker pilot still needs a dedicated
development execution environment, working CLI installations for the
providers selected for the pilot, a scoped development worker credential,
and a verified local model for OpenCode if it uses Ollama. This smoke
requires no credentials or desktop background service.

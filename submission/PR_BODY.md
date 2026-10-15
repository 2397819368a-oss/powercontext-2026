# Which issue or RFC does this PR close?

Link the bug issue created from `submission/ISSUE_DRAFT.md` here before opening the PR.

# Rationale for this change

Cursor signing keys are generated once and persisted. The current creation path assumes one `os.write()` writes the complete 32-byte key. A short write therefore raises while leaving a truncated key file behind; subsequent startups read that file and fail permanently until it is removed manually. An I/O failure after a partial write has the same recovery problem.

# What changes are included in this PR?

- Retry `os.write()` until the complete cursor signing key has been written.
- Treat a zero/negative write as a creation failure.
- Close the descriptor before cleanup and remove a partially created key after `OSError`, allowing the next startup to retry.
- Add regression coverage for short writes and partial-write failures/recovery.
- No API, configuration, storage-format, or documentation contract changes.

# Are there any user-facing changes?

Only failure recovery changes: transient short/failed writes no longer leave a truncated cursor key that breaks later Server startups. Normal successful creation and reuse are unchanged.

# How was this change tested?

- `python -m py_compile src/powercontext/server/cursor_secret.py tests/test_cursor_secret.py`
- Manual isolated regression check confirmed a simulated 7-byte first write is retried and produces the exact 32-byte key.
- Full focused pytest execution could not be completed in the preparation environment because dependency installation could not reach PyPI (`rfc8785` / `zstandard` downloads failed due network/DNS timeout). The two added pytest regressions are included for normal project CI.

# AI usage statement

GPT-6 was used to inspect the repository, identify the failure mode, implement the fix, and draft regression tests and submission text. The resulting change was reviewed against the repository's `AGENTS.md`, `REVIEW.md`, contribution guidance, and existing cursor-secret tests.

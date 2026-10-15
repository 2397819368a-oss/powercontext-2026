# Submission package

Suggested branch: `fix/cursor-secret-short-writes`

Suggested commit:

`fix(server): recover cursor secret short writes`

Files changed in the repository:

- `src/powercontext/server/cursor_secret.py`
- `tests/test_cursor_secret.py`

Recommended submission order:

1. Open the bug using `ISSUE_DRAFT.md` because PowerContext's contribution guide asks contributors to report bugs and its PR template expects an issue/RFC link.
2. Create a branch from current upstream `master`.
3. Copy the two changed files from `powercontext-master/`, or apply `0001-fix-cursor-secret-short-writes.patch` after adjusting paths if needed.
4. Run `make check` and `uv run pytest tests/test_cursor_secret.py -q` in a network-complete development environment.
5. Commit with `fix(server): recover cursor secret short writes`.
6. Open the PR with `PR_BODY.md`, replacing the issue placeholder with the actual issue number.

The package intentionally does not claim the full upstream test suite passed: dependency installation was blocked by the preparation environment's PyPI connectivity.

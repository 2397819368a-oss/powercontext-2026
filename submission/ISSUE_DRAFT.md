# bug: cursor signing key creation can leave a truncated key after a short/failed write

## Describe the bug

`powercontext.server.cursor_secret._load_or_create()` currently performs one `os.write()` call and assumes the complete 32-byte generated cursor signing key is written at once.

`os.write()` is allowed to return a short write. When that happens, PowerContext raises an error but leaves the partially written key file on disk. On the next Server startup, `_read()` finds the existing file and rejects it because it is not 32 bytes long. This turns a transient write problem into a persistent startup failure until the user manually removes the key file.

A write can also fail after some bytes have reached the file, with the same stale partial-file outcome.

## Expected behavior

* Key creation should retry short writes until all 32 bytes are persisted.
* If key creation fails before completion, the file created by that attempt should be removed so a later startup can retry.
* The file descriptor should be closed before cleanup so the behavior also works on platforms that cannot unlink an open file.
* Existing successful key reuse and `0600` creation behavior should remain unchanged.

## Proposed fix

Add a small `_write_all()` helper that advances over short writes and make `_load_or_create()` remove its newly created key file after an `OSError`.

Regression tests cover both a recoverable short write and a partial write followed by an I/O failure.

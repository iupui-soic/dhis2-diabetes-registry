"""Resumable progress that records outcomes rather than intentions.

The audit found every step-2 script doing this:

    send_batch(events)          # returns nothing, prints on failure
    completed.add(person_id)    # unconditional

A participant whose entire batch was rejected was recorded as done, and the
checkpoint that exists to make re-runs safe then skipped them forever. Mark
success only after the write is confirmed, and record failures so a re-run
can pick them up.

Writes go through a temporary file and os.replace, so an interrupted run
cannot leave a truncated checkpoint behind.
"""

import json
import os
import tempfile


class Checkpoint:
    def __init__(self, path, flush_every=1):
        """flush_every > 1 batches writes.

        The retinal import rewrote a 10.4 MB file after each of 118,480
        events. Raise flush_every for large runs, and call close() at the end.
        """
        self.path = path
        self.flush_every = max(1, flush_every)
        self._pending = 0
        self.data = {"completed": [], "failed": {}}
        if os.path.exists(path):
            with open(path) as fh:
                loaded = json.load(fh)
            self.data.update(loaded)
            self.data.setdefault("completed", [])
            self.data.setdefault("failed", {})
        self.completed = set(self.data["completed"])
        self.failed = dict(self.data["failed"])

    def is_done(self, key):
        return key in self.completed

    def pending(self, all_keys, retry_failed=True):
        """Keys still to process. Previously failed keys are retried."""
        if retry_failed:
            return [k for k in all_keys if k not in self.completed]
        return [k for k in all_keys if k not in self.completed and k not in self.failed]

    def mark_done(self, key, note=None):
        self.completed.add(key)
        self.failed.pop(key, None)
        if note is not None:
            self.data.setdefault("notes", {})[key] = note
        self._touch()

    def mark_failed(self, key, error):
        """Record a failure without marking the key complete, so it is retried."""
        self.failed[key] = str(error)[:1000]
        self.completed.discard(key)
        self._touch()

    def _touch(self):
        self._pending += 1
        if self._pending >= self.flush_every:
            self.save()

    def save(self):
        self.data["completed"] = sorted(self.completed)
        self.data["failed"] = self.failed
        directory = os.path.dirname(os.path.abspath(self.path)) or "."
        fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(self.data, fh)
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
        self._pending = 0

    def close(self):
        if self._pending:
            self.save()

    def summary(self):
        return f"{len(self.completed)} done, {len(self.failed)} failed"

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

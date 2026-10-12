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

Checkpoints written by the pre-refactor scripts used other key names
(fixed_events, completed_events, done_participants). They are read and folded
into "completed" on load, so a new script does not redo finished work.
"""

import json
import os
import tempfile


# Keys written by the scripts before Checkpoint existed, and what they meant.
# Reading these means a new backfill does not redo work the old run finished:
# the retinal backfill checkpoint alone records 42,054 fixed events.
_LEGACY_COMPLETED = ("fixed_events", "completed_events", "done_participants",
                     "completed_groups", "processed")
_LEGACY_FAILED = ("failed_events", "failed_participants")


class Checkpoint:
    def __init__(self, path, flush_every=1):
        """flush_every > 1 batches writes.

        The retinal import rewrote a 10.4 MB file after each of 118,480
        events. Raise flush_every for large runs, and call close() at the end.
        """
        self.path = path
        self.flush_every = max(1, flush_every)
        self._pending = 0
        self.data = {"completed": [], "failed": {}, "skipped": {}}
        if os.path.exists(path):
            with open(path) as fh:
                loaded = json.load(fh)
            self.data.update(self._migrate(loaded))
            self.data.setdefault("completed", [])
            self.data.setdefault("failed", {})
            self.data.setdefault("skipped", {})
        self.completed = set(self.data["completed"])
        self.failed = dict(self.data["failed"])
        self.skipped = dict(self.data["skipped"])

    @staticmethod
    def _migrate(loaded):
        """Fold pre-Checkpoint key names into completed / failed."""
        out = dict(loaded)
        completed = set(out.get("completed") or [])
        failed = dict(out.get("failed") or {})
        for key in _LEGACY_COMPLETED:
            value = out.pop(key, None)
            if value:
                completed.update(value if not isinstance(value, dict) else value.keys())
        for key in _LEGACY_FAILED:
            value = out.pop(key, None)
            if isinstance(value, dict):
                failed.update({k: str(v) for k, v in value.items()})
            elif value:
                failed.update({k: "failed in a previous run" for k in value})
        out["completed"] = sorted(completed)
        out["failed"] = failed
        return out

    def is_done(self, key):
        return key in self.completed

    def pending(self, all_keys, retry_failed=True):
        """Keys still to process. Failed and skipped keys are retried."""
        if retry_failed:
            return [k for k in all_keys if k not in self.completed]
        return [k for k in all_keys if k not in self.completed
                and k not in self.failed and k not in self.skipped]

    def mark_done(self, key, note=None):
        self.completed.add(key)
        self.failed.pop(key, None)
        self.skipped.pop(key, None)
        if note is not None:
            self.data.setdefault("notes", {})[key] = note
        self._touch()

    def mark_failed(self, key, error):
        """Record a failure without marking the key complete, so it is retried."""
        self.failed[key] = str(error)[:1000]
        self.completed.discard(key)
        self.skipped.pop(key, None)
        self._touch()

    def mark_skipped(self, key, reason):
        """Record that nothing could be done yet, without calling it done.

        Used for "no tracked entity": the participant is not in the registry
        today, so there is nothing to import, but marking them complete would
        hide them from every later run even after the core registry is fixed.
        Skipped keys are retried on the next run and do not count as failures.
        """
        self.skipped[key] = str(reason)[:1000]
        self.completed.discard(key)
        self.failed.pop(key, None)
        self._touch()

    def _touch(self):
        self._pending += 1
        if self._pending >= self.flush_every:
            self.save()

    def save(self):
        self.data["completed"] = sorted(self.completed)
        self.data["failed"] = self.failed
        self.data["skipped"] = self.skipped
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
        text = f"{len(self.completed)} done, {len(self.failed)} failed"
        if self.skipped:
            text += f", {len(self.skipped)} skipped (no tracked entity yet)"
        return text

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

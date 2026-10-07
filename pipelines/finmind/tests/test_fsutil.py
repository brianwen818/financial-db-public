"""Tests for the Windows-lock-tolerant filesystem primitives.

These guard a failure that actually occurred: during the first full backfill,
three tickers were dropped because the rate-limiter state write hit
PermissionError (WinError 5) from a transient antivirus lock.
"""

from __future__ import annotations

import os
from unittest import mock

import pytest

from finmind_pipeline.utils.fsutil import rename_with_retry, replace_with_retry


class TestReplaceWithRetry:
    def test_plain_replace_works(self, tmp_path):
        src, dst = tmp_path / "a", tmp_path / "b"
        src.write_text("new")
        dst.write_text("old")
        replace_with_retry(src, dst)
        assert dst.read_text() == "new"
        assert not src.exists()

    def test_recovers_from_transient_lock(self, tmp_path):
        """Two WinError-5 failures then success must still complete."""
        src, dst = tmp_path / "a", tmp_path / "b"
        src.write_text("new")
        real = os.replace
        calls = {"n": 0}

        def flaky(a, b):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise PermissionError(5, "Access is denied")
            return real(a, b)

        with mock.patch("finmind_pipeline.utils.fsutil.os.replace", side_effect=flaky):
            replace_with_retry(src, dst)

        assert calls["n"] == 3
        assert dst.read_text() == "new"

    def test_gives_up_and_raises_after_exhausting_attempts(self, tmp_path):
        """A permanent lock must surface, not be swallowed."""
        src, dst = tmp_path / "a", tmp_path / "b"
        src.write_text("new")
        with mock.patch(
            "finmind_pipeline.utils.fsutil.os.replace",
            side_effect=PermissionError(5, "Access is denied"),
        ):
            with pytest.raises(PermissionError):
                replace_with_retry(src, dst, attempts=3)

    def test_non_permission_errors_are_not_retried(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            replace_with_retry(tmp_path / "missing", tmp_path / "dst")


class TestRenameWithRetry:
    def test_directory_rename(self, tmp_path):
        src = tmp_path / "d1"
        src.mkdir()
        (src / "f.txt").write_text("x")
        rename_with_retry(src, tmp_path / "d2")
        assert (tmp_path / "d2" / "f.txt").read_text() == "x"

    def test_recovers_from_transient_lock(self, tmp_path):
        src = tmp_path / "d1"
        src.mkdir()
        real = os.rename
        calls = {"n": 0}

        def flaky(a, b):
            calls["n"] += 1
            if calls["n"] == 1:
                raise PermissionError(5, "Access is denied")
            return real(a, b)

        with mock.patch("finmind_pipeline.utils.fsutil.os.rename", side_effect=flaky):
            rename_with_retry(src, tmp_path / "d2")

        assert (tmp_path / "d2").is_dir()

#!/usr/bin/env python3
"""Unit tests for tfrefresh. Stdlib only: python3 test_tfrefresh.py"""

import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("tfrefresh",
                                              os.path.join(HERE, "tfrefresh.py"))
tf = importlib.util.module_from_spec(spec)
sys.modules["tfrefresh"] = tf
spec.loader.exec_module(tf)


class Base(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="tfrefresh-test-")
        self.card = os.path.join(self.root, "card")
        self.state = os.path.join(self.root, "state")
        os.makedirs(self.card)
        os.makedirs(self.state)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def write_card_file(self, name, data):
        path = os.path.join(self.card, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def read_card_file(self, name):
        with open(os.path.join(self.card, name), "rb") as f:
            return f.read()

    def run_cli(self, *argv):
        return tf.main(["run", self.card, "--state-dir", self.state,
                        "--buffer", "16M", "--force", "--quiet", *argv])

    def journal_records(self):
        files = {}
        for d in os.listdir(self.state):
            jp = os.path.join(self.state, d, "journal.jsonl")
            if not os.path.exists(jp):
                continue
            with open(jp) as f:
                for line in f:
                    ev = json.loads(line)
                    if ev.get("t") == "file":
                        files[ev["id"]] = dict(ev)
                    elif ev.get("t") == "state":
                        files[ev["id"]]["state"] = ev["state"]
        return files

    def states_by_path(self):
        return {r["path"]: r["state"] for r in self.journal_records().values()}


class TestSizes(Base):
    def test_parse_absolute(self):
        self.assertEqual(tf.parse_size("5G"), 5 * 1024**3)
        self.assertEqual(tf.parse_size("500m"), 500 * 1024**2)

    def test_resolve_percent(self):
        free = shutil.disk_usage(self.state).free
        got = tf.resolve_buffer("50%", self.state)
        self.assertAlmostEqual(got, free // 2, delta=1024**2)

    def test_resolve_percent_rejects_bad(self):
        with self.assertRaises(SystemExit):
            tf.resolve_buffer("150%", self.state)


class TestHappyPath(Base):
    def test_refresh_rewrites_identical_content(self):
        data = os.urandom(3 * 1024 * 1024 + 7)  # spans a copy buffer boundary
        self.write_card_file("a/b.bin", data)
        self.assertEqual(self.run_cli(), 0)
        self.assertEqual(self.read_card_file("a/b.bin"), data)
        self.assertEqual(self.states_by_path()["a/b.bin"], "done")

    def test_second_run_starts_fresh_pass(self):
        self.write_card_file("a.txt", b"hello")
        self.assertEqual(self.run_cli(), 0)
        # complete journal gets archived, everything is refreshed again
        self.assertEqual(self.run_cli(), 0)
        archived = [d for d in os.listdir(os.path.join(self.state,
                    os.listdir(self.state)[0])) if d.endswith(".done")]
        self.assertEqual(len(archived), 1)

    def test_symlink_skipped(self):
        self.write_card_file("real.txt", b"x")
        os.symlink("real.txt", os.path.join(self.card, "link.txt"))
        self.assertEqual(self.run_cli(), 0)
        self.assertEqual(self.states_by_path()["link.txt"], "skipped_type")


class TestIOErrorHandling(Base):
    def test_staging_ioerror_marks_error_and_continues(self):
        self.write_card_file("bad.bin", b"bad")
        self.write_card_file("zzz-good.bin", b"good")
        real = tf.copy_with_hash

        def flaky(src, dst, progress=None):
            if src.endswith("bad.bin"):
                raise OSError(5, "Input/output error")
            return real(src, dst, progress)

        with mock.patch.object(tf, "copy_with_hash", flaky):
            rc = self.run_cli()
        self.assertEqual(rc, 1)  # errors present -> nonzero exit
        states = self.states_by_path()
        self.assertEqual(states["bad.bin"], "error")
        self.assertEqual(states["zzz-good.bin"], "done")
        # the failed file's card original must be untouched
        self.assertEqual(self.read_card_file("bad.bin"), b"bad")

    def test_retry_errors_retries_only_intact_originals(self):
        self.write_card_file("bad.bin", b"bad")
        self.write_card_file("ok.bin", b"ok")
        real = tf.copy_with_hash
        with mock.patch.object(tf, "copy_with_hash",
                               lambda s, d, p=None: (_ for _ in ()).throw(
                                   OSError(5, "EIO")) if s.endswith("bad.bin")
                               else real(s, d, p)):
            self.assertEqual(self.run_cli(), 1)
        self.assertEqual(self.states_by_path()["bad.bin"], "error")
        # hardware "fixed": plain retry-errors run re-does only bad.bin
        self.assertEqual(self.run_cli("--retry-errors"), 0)
        self.assertEqual(self.states_by_path()["bad.bin"], "done")
        self.assertEqual(self.read_card_file("bad.bin"), b"bad")

    def test_retry_errors_skips_missing_original(self):
        self.write_card_file("gone.bin", b"data")
        self.assertEqual(self.run_cli(), 0)
        # simulate a write-back style error: no intact card original
        files = self.journal_records()
        rec = next(r for r in files.values() if r["path"] == "gone.bin")
        os.remove(os.path.join(self.card, "gone.bin"))
        jdir = os.path.join(self.state, os.listdir(self.state)[0])
        with open(os.path.join(jdir, "journal.jsonl"), "a") as f:
            f.write(json.dumps({"t": "state", "id": rec["id"],
                                "state": "error"}) + "\n")
            os.fsync(f.fileno())
        # journal is complete (error is terminal) -> archived, fresh pass;
        # gone.bin must NOT be resurrected from anywhere
        rc = self.run_cli("--retry-errors")
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(os.path.join(self.card, "gone.bin")))


class TestVerifyRead(Base):
    def test_verify_read_success(self):
        data = os.urandom(1024 * 1024)
        self.write_card_file("v.bin", data)
        self.assertEqual(self.run_cli("--verify-read"), 0)
        self.assertEqual(self.read_card_file("v.bin"), data)

    def test_verify_read_retries_then_succeeds(self):
        data = os.urandom(100000)
        self.write_card_file("v.bin", data)
        real = tf.hash_file
        calls = {"n": 0}

        def lying(path):
            calls["n"] += 1
            if calls["n"] <= 2:
                return "0" * 64
            return real(path)

        with mock.patch.object(tf, "hash_file", lying):
            rc = self.run_cli("--verify-read")
        self.assertEqual(rc, 0)
        self.assertEqual(calls["n"], 3)  # two bad reads + final good one
        self.assertEqual(self.read_card_file("v.bin"), data)

    def test_verify_read_persistent_failure_keeps_staged_copy(self):
        data = os.urandom(100000)
        self.write_card_file("v.bin", data)
        with mock.patch.object(tf, "hash_file", lambda p: "0" * 64):
            rc = self.run_cli("--verify-read")
        self.assertEqual(rc, 1)
        self.assertEqual(self.states_by_path()["v.bin"], "error")
        # staged buffer copy preserved, and it must contain the good data
        staged = []
        for d in os.listdir(self.state):
            sd = os.path.join(self.state, d, "staged")
            if os.path.isdir(sd):
                staged += [os.path.join(sd, f) for f in os.listdir(sd)]
        self.assertEqual(len(staged), 1)
        with open(staged[0], "rb") as f:
            self.assertEqual(f.read(), data)

    def test_no_verify_read_opts_out(self):
        data = os.urandom(100000)
        self.write_card_file("v.bin", data)
        with mock.patch.object(tf, "hash_file",
                               side_effect=AssertionError("must not be called")):
            rc = self.run_cli("--no-verify-read")
        self.assertEqual(rc, 0)
        self.assertEqual(self.read_card_file("v.bin"), data)


class TestEmuMMCFragmentCheck(Base):
    def test_parse_filefrag_extents(self):
        self.assertEqual(tf.parse_filefrag_extents(
            "a.bin: 1 extent found"), 1)
        self.assertEqual(tf.parse_filefrag_extents(
            "a.bin: 599 extents found"), 599)
        self.assertIsNone(tf.parse_filefrag_extents("garbage"))

    def test_no_emummc_dir_is_quiet(self):
        msgs = []
        self.assertEqual(
            tf.check_emummc_fragmentation(self.card, log=msgs.append), [])
        self.assertEqual(msgs, [])

    def test_contiguous_parts_pass(self):
        self.write_card_file("emuMMC/SD00/eMMC/00", b"x" * 100)
        msgs = []
        with mock.patch.object(tf, "file_extents", lambda p: 1):
            over = tf.check_emummc_fragmentation(self.card,
                                                 log=msgs.append)
        self.assertEqual(over, [])
        self.assertTrue(any("within the" in m for m in msgs))

    def test_over_limit_warns_and_lists(self):
        self.write_card_file("emuMMC/SD00/eMMC/12", b"x" * 100)
        msgs = []
        with mock.patch.object(tf, "file_extents", lambda p: 599):
            over = tf.check_emummc_fragmentation(self.card,
                                                 log=msgs.append)
        self.assertEqual(over, [("emuMMC/SD00/eMMC/12", 599)])
        self.assertTrue(any("WARNING" in m for m in msgs))

    def test_run_emits_warning(self):
        self.write_card_file("emuMMC/SD00/eMMC/12", b"x" * 100)
        import contextlib, io
        buf = io.StringIO()
        with mock.patch.object(tf, "file_extents", lambda p: 599):
            with contextlib.redirect_stdout(buf):
                rc = tf.main(["run", self.card, "--state-dir", self.state,
                              "--buffer", "16M", "--force"])
        self.assertEqual(rc, 0)  # warning only, never fails the run
        self.assertIn("599 fragments", buf.getvalue())


class TestCrashRecovery(Base):
    def _stage_and_crash(self):
        """Drive a file to CLEARED state, then stop before write-back."""
        data = os.urandom(500000)
        self.write_card_file("c.bin", data)
        r = tf.Refresher(self.card, self.state, 1024**2, quiet=True)
        try:
            r.scan()
            rec = next(r2 for r2 in r.journal.files.values()
                       if r2["path"] == "c.bin")
            with mock.patch.object(r, "_writeback",
                                   lambda rec2: (_ for _ in ()).throw(
                                       tf.StopRequested())):
                try:
                    r.process_file(rec)
                except tf.StopRequested:
                    pass
            return rec, data
        finally:
            r.close()

    def test_crash_after_clear_resumes_from_staged_copy(self):
        rec, data = self._stage_and_crash()
        self.assertEqual(rec["state"], "cleared")
        self.assertFalse(os.path.exists(os.path.join(self.card, "c.bin")))
        # resume in a fresh process (new Refresher): reconcile + write-back
        r2 = tf.Refresher(self.card, self.state, 1024**2, quiet=True)
        try:
            r2.reconcile()
            rec2 = r2.journal.files[rec["id"]]
            self.assertTrue(r2.process_file(rec2))
            self.assertEqual(rec2["state"], "done")
        finally:
            r2.close()
        self.assertEqual(self.read_card_file("c.bin"), data)

    def test_crash_mid_stage_leaves_original(self):
        data = os.urandom(500000)
        self.write_card_file("c.bin", data)
        real = tf.copy_with_hash

        def dying(src, dst, progress=None):
            real(src, dst, progress)
            if src.endswith(".bin") and dst.endswith(".tmp"):
                raise tf.StopRequested()
            return real(src, dst, progress)

        r = tf.Refresher(self.card, self.state, 1024**2, quiet=True)
        try:
            r.scan()
            rec = next(r2 for r2 in r.journal.files.values()
                       if r2["path"] == "c.bin")
            with mock.patch.object(tf, "copy_with_hash", dying):
                try:
                    r.process_file(rec)
                except tf.StopRequested:
                    pass
        finally:
            r.close()
        # original still on card, journaled pending, resume redoes cleanly
        self.assertEqual(rec["state"], "pending")
        r2 = tf.Refresher(self.card, self.state, 1024**2, quiet=True)
        try:
            r2.reconcile()
            rec2 = r2.journal.files[rec["id"]]
            self.assertTrue(r2.process_file(rec2))
        finally:
            r2.close()
        self.assertEqual(self.read_card_file("c.bin"), data)


if __name__ == "__main__":
    unittest.main(verbosity=2)

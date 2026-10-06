#!/usr/bin/env python3
"""
tfrefresh - refresh cold data on TF/SD cards.

Cold data on NAND flash (TF/SD cards) slowly loses read speed ("cold data
slowdown"). The cure is to rewrite the data so the flash controller stores it
in fresh cells. This tool does that file by file:

    copy file -> local buffer (verified, fsynced)
    delete file from card
    copy back to the same path (verified, fsynced)

Design goals:
  * Bounded buffer: never uses more local disk than --buffer (default 80%
    of the free space on the state disk), so any card size can be refreshed.
  * Crash-safe at ANY point (SIGKILL, power loss, card yanked): every state
    transition is journaled with fsync before the next mutation. Re-running
    the same command resumes exactly where it stopped. A file's data always
    exists in at least one complete, verified place: the card original XOR
    the staged buffer copy.
  * No dependencies: Python 3.8+ standard library only.

Usage:
    tfrefresh.py run    /Volumes/MYCARD [--buffer 80%|5G] [--state-dir DIR]
                        [--no-verify-read] [--retry-errors] [--force] [--quiet]
    tfrefresh.py status /Volumes/MYCARD
    tfrefresh.py scan   /Volumes/MYCARD

By default, each written-back file is re-read from the card (page cache
dropped, so the hash reflects what is actually on the device) and verified
against the staged copy; a mismatch triggers a rewrite, up to 3 attempts,
after which the file is marked "error" and the verified buffer copy is
kept. This catches silent write-path corruption (flaky reader or
controller). --no-verify-read skips this (~33% faster) at the risk of not
detecting such corruption. Neither mode protects against filesystem
metadata corruption if the device vanishes mid-write -- run fsck after
any such event.

Files that hit an I/O error are marked "error" (their card originals are
left untouched) and the run continues; after fixing the hardware, re-run
with --retry-errors to retry exactly those files.

State (journal + staged buffer copies) lives in
~/.tfrefresh/<volume-uuid>/, keyed per card, so refreshing a second card
needs no cleanup: it gets its own state directory automatically and the
first card's journal is untouched. On macOS the key is the real filesystem
UUID from diskutil. Elsewhere it falls back to a WEAK id (device number +
mount name, e.g. weak-2065-LX1TB): the same card re-plugged as a different
device looks like a new volume (a fresh pass starts; wasteful, never
unsafe), and two different cards sharing device number and mount name would
collide -- use a fresh --state-dir in that case.
"""

import argparse
import fcntl
import hashlib
import json
import os
import plistlib
import shutil
import signal
import subprocess
import sys
import time

JOURNAL_NAME = "journal.jsonl"
TMP_SUFFIX = ".tfresh-tmp"
COPY_BUFSIZE = 4 * 1024 * 1024
VERIFY_READ_ATTEMPTS = 3

# Journal states
PENDING = "pending"        # known, not yet staged
STAGED = "staged"          # complete verified copy in local buffer
CLEARED = "cleared"        # original deleted from card, only buffer copy exists
DONE = "done"              # rewritten on card and verified, buffer freed
SK_TOO_LARGE = "skipped_too_large"
SK_TYPE = "skipped_type"
SK_CHANGED = "skipped_changed"
ERROR = "error"
TERMINAL = {DONE, SK_TOO_LARGE, SK_TYPE, SK_CHANGED, ERROR}

STOP = False


def _handle_signal(signum, frame):
    global STOP
    STOP = True


def human(n):
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}B"
        n /= 1024


def resolve_buffer(spec, state_root):
    spec = spec.strip()
    if spec.endswith("%"):
        pct = float(spec[:-1])
        if not 0 < pct < 100:
            die(f"--buffer percentage must be between 0 and 100, got {spec!r}")
        os.makedirs(state_root, exist_ok=True)
        free = shutil.disk_usage(state_root).free
        return int(free * pct / 100)
    return parse_size(spec)


def parse_size(s):
    s = s.strip().upper()
    mult = 1
    for suffix, m in (("KIB", 1024), ("MIB", 1024**2), ("GIB", 1024**3),
                      ("KB", 1024), ("MB", 1024**2), ("GB", 1024**3),
                      ("K", 1024), ("M", 1024**2), ("G", 1024**3), ("B", 1)):
        if s.endswith(suffix):
            mult = m
            s = s[: -len(suffix)]
            break
    return int(float(s) * mult)


class StopRequested(Exception):
    pass


def check_stop():
    if STOP:
        raise StopRequested()


def fsync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def hash_file(path):
    """sha256 of a file, read back from storage (page cache dropped first,
    best effort, so the verify sees what is actually on the device)."""
    fd = os.open(path, os.O_RDONLY)
    try:
        if hasattr(os, "posix_fadvise"):
            try:
                os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
            except OSError:
                pass
        h = hashlib.sha256()
        while True:
            buf = os.read(fd, COPY_BUFSIZE)
            if not buf:
                break
            h.update(buf)
        return h.hexdigest()
    finally:
        os.close(fd)


def copy_with_hash(src, dst, progress=None):
    """Copy src -> dst in one pass, sha256 while copying, fsync dst."""
    h = hashlib.sha256()
    total = 0
    with open(src, "rb") as f, open(dst, "wb") as g:
        while True:
            check_stop()
            buf = f.read(COPY_BUFSIZE)
            if not buf:
                break
            g.write(buf)
            h.update(buf)
            total += len(buf)
            if progress:
                progress(len(buf))
    with open(dst, "rb") as g:
        os.fsync(g.fileno())
    return h.hexdigest(), total


def best_effort_meta(src, dst):
    """Preserve mode/times/xattrs; never fatal (exFAT etc. may refuse)."""
    try:
        shutil.copystat(src, dst)
    except OSError:
        try:
            st = os.stat(src)
            os.utime(dst, ns=(st.st_atime_ns, st.st_mtime_ns))
        except OSError:
            pass


# ---------------------------------------------------------------- journal

class Journal:
    """Append-only JSONL journal, fsynced per event. Replayed on startup."""

    def __init__(self, state_dir):
        self.path = os.path.join(state_dir, JOURNAL_NAME)
        self.meta = None
        self.files = {}        # id -> record dict
        self.by_path = {}      # relpath -> id
        self._next_id = 1
        self._events_since_compact = 0

    def load(self):
        if not os.path.exists(self.path):
            return
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                ev = json.loads(line)
                t = ev["t"]
                if t == "meta":
                    self.meta = ev
                elif t == "file":
                    rec = {"id": ev["id"], "path": ev["path"], "size": ev["size"],
                           "mtime_ns": ev["mtime_ns"], "state": ev.get("state", PENDING),
                           "sha256": None, "error": None}
                    self.files[rec["id"]] = rec
                    self.by_path[rec["path"]] = rec["id"]
                    self._next_id = max(self._next_id, int(rec["id"][1:]) + 1)
                elif t == "state":
                    rec = self.files.get(ev["id"])
                    if rec:
                        rec["state"] = ev["state"]
                        if "sha256" in ev:
                            rec["sha256"] = ev["sha256"]
                        if "error" in ev:
                            rec["error"] = ev["error"]

    def _append(self, ev):
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(ev, separators=(",", ":")) + "\n")
            f.flush()
            os.fsync(f.fileno())
        self._events_since_compact += 1

    def set_meta(self, meta):
        self.meta = meta
        ev = dict(meta)
        ev["t"] = "meta"
        self._append(ev)

    def add_file(self, relpath, size, mtime_ns):
        fid = f"f{self._next_id:08d}"
        self._next_id += 1
        rec = {"id": fid, "path": relpath, "size": size, "mtime_ns": mtime_ns,
               "state": PENDING, "sha256": None, "error": None}
        self.files[fid] = rec
        self.by_path[relpath] = fid
        self._append({"t": "file", "id": fid, "path": relpath,
                      "size": size, "mtime_ns": mtime_ns})
        return rec

    def set_state(self, rec, state, **kw):
        rec["state"] = state
        rec.update(kw)
        ev = {"t": "state", "id": rec["id"], "state": state}
        ev.update(kw)
        self._append(ev)
        if self._events_since_compact >= 2000:
            self.compact()

    def compact(self):
        tmp = self.path + ".compact"
        with open(tmp, "w", encoding="utf-8") as f:
            if self.meta:
                ev = dict(self.meta)
                ev["t"] = "meta"
                f.write(json.dumps(ev, separators=(",", ":")) + "\n")
            for rec in self.files.values():
                ev = {"t": "file", "id": rec["id"], "path": rec["path"],
                      "size": rec["size"], "mtime_ns": rec["mtime_ns"],
                      "state": rec["state"]}
                f.write(json.dumps(ev, separators=(",", ":")) + "\n")
                if rec["sha256"] or rec["error"]:
                    ev = {"t": "state", "id": rec["id"], "state": rec["state"]}
                    if rec["sha256"]:
                        ev["sha256"] = rec["sha256"]
                    if rec["error"]:
                        ev["error"] = rec["error"]
                    f.write(json.dumps(ev, separators=(",", ":")) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)
        fsync_dir(os.path.dirname(self.path))
        self._events_since_compact = 0

    def complete(self):
        return all(r["state"] in TERMINAL for r in self.files.values())

    def summary(self):
        out = {}
        for r in self.files.values():
            out[r["state"]] = out.get(r["state"], 0) + 1
        return out


# ---------------------------------------------------------------- volumes

def volume_info(mount):
    """Return dict with uuid/name/internal; uuid may be None."""
    info = {"uuid": None, "name": os.path.basename(mount.rstrip("/")) or mount,
            "internal": None}
    try:
        out = subprocess.run(["diskutil", "info", "-plist", mount],
                             capture_output=True, timeout=15)
        if out.returncode == 0:
            pl = plistlib.loads(out.stdout)
            info["uuid"] = pl.get("VolumeUUID") or pl.get("FilesystemUUID") or None
            info["name"] = pl.get("VolumeName") or info["name"]
            loc = pl.get("DeviceLocation")
            if loc:
                info["internal"] = (loc == "Internal")
            elif "Internal" in pl:
                info["internal"] = bool(pl["Internal"])
    except (OSError, subprocess.TimeoutExpired, ValueError):
        pass
    if not info["uuid"]:
        st = os.stat(mount)
        info["uuid"] = f"weak-{st.st_dev}-{info['name']}"
        info["weak"] = True
    return info


# ---------------------------------------------------------------- core

class Refresher:
    def __init__(self, mount, state_root, buffer_limit, quiet=False,
                 retry_errors=False, verify_read=False):
        self.mount = os.path.abspath(mount)
        self.buffer_limit = buffer_limit
        self.quiet = quiet
        self.retry_errors = retry_errors
        self.verify_read = verify_read
        self.vinfo = volume_info(self.mount)
        self.state_dir = os.path.join(state_root, self.vinfo["uuid"])
        self.staged_dir = os.path.join(self.state_dir, "staged")
        os.makedirs(self.staged_dir, exist_ok=True)
        self._lock_fd = os.open(os.path.join(self.state_dir, "lock"),
                                os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            die("another tfrefresh process is already working on this volume")
        self.journal = Journal(self.state_dir)
        self.journal.load()
        self.done_bytes = 0
        self.total_bytes = 0

    def close(self):
        os.close(self._lock_fd)

    def log(self, msg):
        if not self.quiet:
            print(msg, flush=True)

    # ---- scanning ----------------------------------------------------

    def scan(self):
        """Walk the card, adding new regular files to the journal."""
        self.log(f"scanning {self.mount} ...")
        n_new = 0
        for dirpath, dirnames, filenames in os.walk(self.mount, followlinks=False):
            check_stop()
            for name in filenames:
                if name.endswith(TMP_SUFFIX):
                    continue  # our own temp files; reconcile handles them
                full = os.path.join(dirpath, name)
                rel = os.path.relpath(full, self.mount)
                try:
                    st = os.lstat(full)
                except OSError:
                    continue
                if rel in self.journal.by_path:
                    continue
                rec = self.journal.add_file(rel, st.st_size, st.st_mtime_ns)
                if not os.path.isfile(full) or os.path.islink(full):
                    self.journal.set_state(rec, SK_TYPE)
                elif st.st_size > self.buffer_limit:
                    self.journal.set_state(rec, SK_TOO_LARGE)
                n_new += 1
        self.journal.compact()
        self.log(f"scan done: {n_new} new file(s), "
                 f"{len(self.journal.files)} total in journal")

    def retry_error_files(self):
        """Reset ERROR files to PENDING if the card original is intact.

        Only files whose card copy still matches the scan (size + mtime)
        qualify -- staging failures leave the original untouched. Write-back
        failures and conflicts keep their staged buffer copy for manual
        inspection and are not retried.
        """
        n = 0
        for rec in self.journal.files.values():
            if rec["state"] != ERROR:
                continue
            try:
                st = os.lstat(os.path.join(self.mount, rec["path"]))
            except OSError:
                continue
            if (st.st_size, st.st_mtime_ns) == (rec["size"], rec["mtime_ns"]):
                self.journal.set_state(rec, PENDING)
                n += 1
        if n:
            self.log(f"retry: reset {n} error file(s) to pending")

    # ---- crash recovery ----------------------------------------------

    def reconcile(self):
        """Bring in-flight files to a consistent state after a crash."""
        staged_tmp = None
        for rec in self.journal.files.values():
            st = rec["state"]
            staged = os.path.join(self.staged_dir, rec["id"] + ".bin")
            if st == PENDING:
                for p in (staged, staged + ".tmp"):
                    if os.path.exists(p):
                        os.remove(p)
            elif st == DONE:
                if os.path.exists(staged):
                    os.remove(staged)
            elif st in (STAGED, CLEARED):
                # Buffer copy is verified and durable; make sure the card
                # side has no half-written remains, then re-write back.
                card_path = os.path.join(self.mount, rec["path"])
                tmp_path = card_path + TMP_SUFFIX
                if os.path.lexists(tmp_path):
                    os.remove(tmp_path)
                    fsync_dir(os.path.dirname(tmp_path) or self.mount)
                if st == STAGED and os.path.lexists(card_path):
                    try:
                        cur = os.lstat(card_path)
                    except OSError:
                        continue
                    if (cur.st_size, cur.st_mtime_ns) != (rec["size"], rec["mtime_ns"]):
                        self.journal.set_state(
                            rec, ERROR,
                            error="conflict: card file changed while staged; "
                                  "buffer copy kept at " + staged)
                        self.log(f"CONFLICT (left untouched): {rec['path']}")
                        continue
                    os.remove(card_path)
                    fsync_dir(os.path.dirname(card_path) or self.mount)
                    self.journal.set_state(rec, CLEARED)
        if os.path.isdir(self.staged_dir):
            for name in os.listdir(self.staged_dir):
                if name.endswith(".tmp"):
                    staged_tmp = os.path.join(self.staged_dir, name)
                    os.remove(staged_tmp)
        self.journal.compact()

    # ---- per-file pipeline --------------------------------------------

    def _writeback(self, rec):
        """CLEARED -> DONE: copy staged buffer back onto the card."""
        staged = os.path.join(self.staged_dir, rec["id"] + ".bin")
        card_path = os.path.join(self.mount, rec["path"])
        tmp_path = card_path + TMP_SUFFIX
        os.makedirs(os.path.dirname(card_path), exist_ok=True)

        attempts = VERIFY_READ_ATTEMPTS if self.verify_read else 1
        for attempt in range(1, attempts + 1):
            try:
                digest, _ = copy_with_hash(staged, tmp_path)
            except OSError as e:
                if os.path.lexists(tmp_path):
                    os.remove(tmp_path)
                fsync_dir(os.path.dirname(tmp_path) or self.mount)
                self.journal.set_state(rec, ERROR,
                                       error=f"write-back I/O error: {e}; "
                                             "buffer copy kept")
                self.log(f"ERROR write-back {rec['path']}: {e}")
                return False
            if digest != rec["sha256"]:
                os.remove(tmp_path)
                fsync_dir(os.path.dirname(tmp_path) or self.mount)
                self.journal.set_state(rec, ERROR,
                                       error="write-back hash mismatch; "
                                             "buffer copy kept")
                self.log(f"ERROR write-back verify failed: {rec['path']}")
                return False
            best_effort_meta(staged, tmp_path)
            os.replace(tmp_path, card_path)
            fsync_dir(os.path.dirname(card_path) or self.mount)
            if not self.verify_read:
                break
            try:
                back = hash_file(card_path)
            except OSError as e:
                back = None
                self.log(f"verify-read I/O error (attempt {attempt}/"
                         f"{attempts}): {rec['path']}: {e}")
            if back == rec["sha256"]:
                break
            self.log(f"verify-read mismatch (attempt {attempt}/{attempts}), "
                     f"rewriting: {rec['path']}")
        else:
            # every attempt failed verification: stop, keep the good copy
            self.journal.set_state(rec, ERROR,
                                   error=f"verify-read failed {attempts}x; "
                                         "buffer copy kept")
            self.log(f"ERROR verify-read failed {attempts}x: {rec['path']}")
            return False

        self.journal.set_state(rec, DONE)
        os.remove(staged)
        fsync_dir(self.staged_dir)
        self.done_bytes += rec["size"]
        return True

    def process_file(self, rec):
        card_path = os.path.join(self.mount, rec["path"])
        staged = os.path.join(self.staged_dir, rec["id"] + ".bin")
        staged_tmp = staged + ".tmp"

        # resume mid-pipeline if needed
        if rec["state"] == CLEARED:
            return self._writeback(rec)
        if rec["state"] != PENDING:
            return False

        # 1. verify the card file is unchanged since the scan
        try:
            cur = os.lstat(card_path)
        except OSError:
            self.journal.set_state(rec, ERROR, error="vanished before staging")
            return False
        if (cur.st_size, cur.st_mtime_ns) != (rec["size"], rec["mtime_ns"]):
            self.journal.set_state(rec, SK_CHANGED)
            self.log(f"SKIP changed since scan: {rec['path']}")
            return False

        # 2. stage: card -> buffer (hashed, fsynced, atomic rename)
        try:
            digest, _ = copy_with_hash(card_path, staged_tmp)
        except OSError as e:
            if os.path.lexists(staged_tmp):
                os.remove(staged_tmp)
            fsync_dir(self.staged_dir)
            self.journal.set_state(rec, ERROR,
                                   error=f"staging I/O error: {e}; card original untouched")
            self.log(f"ERROR staging {rec['path']}: {e}")
            return False
        if digest is None:
            return False
        best_effort_meta(card_path, staged_tmp)
        os.replace(staged_tmp, staged)
        fsync_dir(self.staged_dir)
        self.journal.set_state(rec, STAGED, sha256=digest)

        # 3. delete original from card
        os.remove(card_path)
        fsync_dir(os.path.dirname(card_path) or self.mount)
        self.journal.set_state(rec, CLEARED)

        # 4. write back
        return self._writeback(rec)

    # ---- top level -----------------------------------------------------

    def run(self):
        j = self.journal
        if j.meta and j.meta.get("volume_uuid") != self.vinfo["uuid"]:
            die(f"journal in {self.state_dir} belongs to volume "
                f"{j.meta.get('volume_uuid')}, not {self.vinfo['uuid']}; "
                f"refusing to mix state (use a fresh --state-dir to override)")
        if self.retry_errors:
            # must happen before the complete() check: errors are terminal,
            # so a failed-but-finished journal would otherwise be archived
            # and a full fresh pass would start
            self.retry_error_files()
        if j.files and j.complete():
            # previous session finished: archive it and start a fresh pass
            os.rename(j.path, j.path + f".{int(time.time())}.done")
            self.journal = Journal(self.state_dir)

        if not self.journal.meta:
            self.journal.set_meta({
                "volume_uuid": self.vinfo["uuid"],
                "volume_name": self.vinfo["name"],
                "mount": self.mount,
                "buffer_limit": self.buffer_limit,
                "created": time.time(),
            })

        free = shutil.disk_usage(self.state_dir).free
        if free < self.buffer_limit:
            die(f"buffer dir has only {human(free)} free, less than "
                f"--buffer {human(self.buffer_limit)}")

        self.reconcile()
        self.scan()

        todo = [r for r in self.journal.files.values() if r["state"] in (PENDING, CLEARED)]
        self.total_bytes = sum(r["size"] for r in todo)
        self.log(f"to refresh: {len(todo)} file(s), {human(self.total_bytes)}; "
                 f"buffer limit {human(self.buffer_limit)}")

        t0 = time.time()
        n_done = 0
        try:
            for i, rec in enumerate(sorted(todo, key=lambda r: r["path"]), 1):
                check_stop()
                ok = self.process_file(rec)
                n_done += bool(ok)
                if not self.quiet:
                    pct = 100.0 * self.done_bytes / self.total_bytes if self.total_bytes else 100
                    rate = self.done_bytes / max(time.time() - t0, 1e-9)
                    print(f"[{i}/{len(todo)}] {pct:5.1f}% {human(rate)}/s  "
                          f"{rec['path']} ({human(rec['size'])})", flush=True)
        except StopRequested:
            self.journal.compact()
            print("\ninterrupted - all state saved. "
                  "Re-run the same command to resume.", file=sys.stderr)
            return 130

        self.journal.compact()
        summ = self.journal.summary()
        dt = time.time() - t0
        print(f"\nrefreshed {n_done} file(s), {human(self.done_bytes)} in {dt:.0f}s")
        for state in sorted(summ):
            print(f"  {state}: {summ[state]}")
        if summ.get(ERROR):
            print("there were errors; staged copies (if any) are kept in "
                  f"{self.staged_dir}", file=sys.stderr)
            return 1
        return 0

    def status(self):
        j = self.journal
        if not j.meta:
            print("no state for this volume (never refreshed)")
            return 0
        print(f"volume:  {j.meta.get('volume_name')} ({j.meta.get('volume_uuid')})")
        print(f"journal: {j.path}")
        print(f"files:   {len(j.files)}")
        for state, n in sorted(j.summary().items()):
            print(f"  {state}: {n}")
        print("complete: " + ("yes" if j.complete() else "no - run again to resume"))
        return 0


def die(msg, code=2):
    print(f"tfrefresh: error: {msg}", file=sys.stderr)
    sys.exit(code)


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="tfrefresh",
        description="Rewrite all files on a TF/SD card through a bounded local "
                    "buffer to cure cold-data slowdown. Crash-safe and resumable.")
    p.add_argument("--version", action="version", version="tfrefresh 1.1")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("mount", help="mount point of the card, e.g. /Volumes/MYCARD")
        sp.add_argument("--state-dir", default=os.path.expanduser("~/.tfrefresh"),
                        help="where journals and buffer live (default: ~/.tfrefresh)")

    sp = sub.add_parser("run", help="refresh (or resume refreshing) the card")
    common(sp)
    sp.add_argument("--buffer", default="80%",
                    help="max local buffer space: an absolute size (e.g. 5G, "
                         "500M) or a percentage of the free space on the state "
                         "disk, e.g. 80%% (default: 80%%)")
    sp.add_argument("--force", action="store_true",
                    help="allow running on a volume that is not external/removable")
    sp.add_argument("--quiet", action="store_true")
    sp.add_argument("--retry-errors", action="store_true",
                    help="reset files marked 'error' to pending and try them "
                         "again (only if the card original is intact)")
    sp.add_argument("--verify-read", dest="verify_read", action="store_true",
                    default=True,
                    help="after writing each file back, re-read it from the "
                         "card (page cache dropped) and verify the hash; "
                         "rewrite up to 3 times on mismatch, keeping the "
                         "buffer copy (default; this is the safe mode)")
    sp.add_argument("--no-verify-read", dest="verify_read",
                    action="store_false",
                    help="skip the read-back verification (~33%% faster); "
                         "silent write-path corruption would go undetected")

    sp = sub.add_parser("status", help="show refresh state for a card")
    common(sp)

    sp = sub.add_parser("scan", help="dry run: count files and sizes only")
    common(sp)

    args = p.parse_args(argv)

    if not os.path.isdir(args.mount):
        die(f"{args.mount} is not a mounted directory")

    if args.cmd == "scan":
        n = 0
        total = 0
        for dirpath, _dn, fnames in os.walk(args.mount, followlinks=False):
            for name in fnames:
                full = os.path.join(dirpath, name)
                try:
                    st = os.lstat(full)
                except OSError:
                    continue
                if os.path.isfile(full) and not os.path.islink(full):
                    n += 1
                    total += st.st_size
        print(f"{n} regular file(s), {human(total)} total")
        return 0

    if args.cmd == "run":
        vinfo = volume_info(args.mount)
        if vinfo["internal"] is True and not args.force:
            die(f"{args.mount} looks like an INTERNAL volume ({vinfo['name']}); "
                f"refusing. Use --force if you really mean it.")

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    r = Refresher(args.mount, os.path.abspath(args.state_dir),
                  resolve_buffer(args.buffer, os.path.abspath(args.state_dir))
                  if args.cmd == "run" else 0,
                  quiet=getattr(args, "quiet", False),
                  retry_errors=getattr(args, "retry_errors", False),
                  verify_read=getattr(args, "verify_read", False))
    try:
        if args.cmd == "run":
            return r.run()
        if args.cmd == "status":
            return r.status()
    finally:
        r.close()


if __name__ == "__main__":
    sys.exit(main())

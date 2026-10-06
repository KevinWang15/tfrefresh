# tfrefresh

Refresh cold data on TF/SD cards by rewriting every file through a bounded,
crash-safe local buffer.

## Why this is necessary

NAND flash — the storage inside every TF/microSD card — stores data as
electric charge trapped in floating-gate cells. That charge leaks away
slowly over months and years, faster at high temperatures and on worn
(heavily cycled) cells. As the charge in a cell drifts, the card's
controller has to work harder to read it: it re-reads the cell with
different reference voltages ("read retry") and leans on ECC to reconstruct
the data. The data is still correct, but reads that should take
microseconds start taking milliseconds.

This is **cold-data slowdown**: files written long ago — game installs,
photo archives, ROM collections, anything written once and left alone —
read at a fraction of the card's original speed, while freshly written
files are fast.

A typical real-world case: a Nintendo Switch with a large microSD card.
Games are installed to the card once and then only ever read. After six
months or so, games that used to load in seconds take minutes: long
loading screens, textures popping in late, stuttering when streaming
assets — while a newly installed game on the same card runs perfectly.
Benchmarks show the card's sequential read speed has collapsed from
~90 MB/s to single-digit MB/s, but only for the old data. Deleting and
reinstalling a game "fixes" that game, because reinstalling is really just
a rewrite — which is exactly what tfrefresh does to every file without
destroying your installs, saves, or directory layout. The same failure
hits Steam libraries moved onto SD cards, emulation handhelds, dashcams,
and Raspberry Pi systems: anything written once and read for months.

The charge keeps leaking. Left alone, read retries eventually fail and ECC
runs out of margin, at which point slow reads become *read errors*.

## Why rewriting fixes it

The controller's read problems come from cells whose charge has drifted.
Rewriting a file makes the controller program the data into cells again —
restoring full, sharply-defined charge levels (and in practice usually
different, fresher physical cells via wear leveling). Reads become
first-try fast again. No card vendor exposes a "refresh" command, so the
only way to do this from a host is to read each file and write it back.

tfrefresh does exactly that, file by file:

    copy file -> local buffer (SHA-256 verified, fsynced)
    delete file from card
    copy back to the same path (verified, fsynced, atomic rename)
    re-read from the card and verify against the buffer copy   [--verify-read]

The effect lasts until the new charge drifts — typically months to years,
after which you simply run it again.

## Usage

    tfrefresh.py run    /Volumes/MYCARD --verify-read [--buffer 80%|5G]
                        [--state-dir DIR]
    tfrefresh.py status /Volumes/MYCARD
    tfrefresh.py scan   /Volumes/MYCARD

**Use `--verify-read`.** It is the strictest form and the recommended one:
after each file is written back, tfrefresh re-reads it from the card (with
the OS page cache dropped, so the hash reflects what is actually on the
device) and verifies it against the known-good buffer copy. A mismatch
triggers a rewrite, up to 3 attempts; if all fail, the file is marked
`error` and the verified buffer copy is kept — the tool never lets go of
the only good copy of your data. This catches silent write-path corruption
from a flaky reader or controller, which is otherwise undetectable. The
cost is one extra read pass (~+50% runtime); on a job that runs for hours
unattended, that is cheap insurance. Only omit it if you fully trust the
card reader and are in a hurry.

- `scan` — dry run: counts files and sizes, touches nothing.
- `run` — refresh, or resume an interrupted refresh. Safe to Ctrl-C,
  kill, or power off at any point; re-run the same command to continue.
  I/O errors (e.g. a reader that locks up mid-run) mark just the affected
  files as `error` and the run continues; after fixing the hardware
  (re-plug the reader), re-run with `--retry-errors` to retry exactly
  those files. Files whose card original was already replaced stay
  untouched with their staged copy preserved for manual inspection.
- `status` — per-state progress (`pending/staged/cleared/done/skipped_*`).

`--buffer` caps local disk usage: an absolute size (`5G`, `500M`) or a
percentage of the free space on the state disk (default `80%`). Files
larger than the buffer are skipped with a warning — re-run with a bigger
buffer to include them.

State (journal + staged buffer copies) lives in
`~/.tfrefresh/<volume-uuid>/`, keyed per card, so refreshing a second card
needs no cleanup. On macOS the key is the real filesystem UUID from
diskutil; elsewhere it falls back to a weak id (device number + mount
name), which can change when a card is re-plugged — a fresh pass then
starts (wasteful, never unsafe).

## Safety properties

- **Verified rewrite**: the buffer copy is SHA-256-checked against the
  card read, the write-back against the buffer, and (with the recommended
  `--verify-read`) the card's stored copy is re-read and checked before
  the buffer copy is released.
- **Crash-safe at any point** (SIGKILL, power loss, card yanked): every
  state transition is journaled with fsync before the next mutation. A
  file's data always exists in at least one complete, verified place:
  the card original XOR the staged buffer copy.
- **Bounded buffer**: never uses more local disk than `--buffer`, so any
  card size can be refreshed; only the largest single file matters.
- **Careful skips**: symlinks and files that changed since the scan are
  skipped; modes/mtimes preserved on a best-effort basis; refuses to run
  on internal disks unless `--force`.

## Caveats

- Without `--verify-read`, the card's copy is not re-read after
  write-back, so a *silent* write-path fault (flaky reader or controller)
  would go undetected. This is why `--verify-read` is recommended.
- On journaling-less filesystems (exFAT, FAT32), a power cut or device
  removal mid-write can corrupt filesystem metadata even though file
  contents are protected. Keep the card powered and seated during the run,
  and run `fsck` (e.g. `fsck.exfat`) after any such event before trusting
  the card again.
- Loud failures (I/O errors, USB resets) mark only the affected files as
  `error` and the run continues; after fixing the hardware, re-run with
  `--retry-errors`.
- A file already silently corrupted before the run will be faithfully
  re-written as-is; refreshing fixes slow reads, not pre-existing rot.

## Requirements

Python 3.8+, standard library only. Runs on macOS and Linux. The card must
be mounted.

### Cards macOS can't mount (ext4, f2fs, btrfs, xfs)

Run tfrefresh inside a Linux VM (any minimal arm64/x86_64 distro; only
python3 is required) with the USB card reader passed through to the VM.
With Parallels: *Devices → USB & Bluetooth* → select the reader; if macOS
shows a "not readable" dialog for the card, click **Ignore** (never
"Initialize"). Then in the VM:

    lsblk -f                            # find the card, e.g. /dev/sda1
    sudo mount /dev/sda1 /mnt/tfcard
    python3 tfrefresh.py run /mnt/tfcard

Run it inside `tmux`/`screen` so closing the VM window doesn't interrupt
the view (the run itself survives that anyway). When done:
`sudo umount /mnt/tfcard`, then reassign the reader to the host.

## Troubleshooting

| Symptom | Action |
|---|---|
| "not a mounted directory" | mount the card first |
| "another tfrefresh process..." | stale lock after a crash — delete `~/.tfrefresh/<uuid>/lock` |
| "buffer dir has only X free" | free state-disk space or lower `--buffer` |
| `CONFLICT (left untouched)` | file changed on the card while staged; the staged copy is kept in the state dir — diff manually |
| "journal belongs to volume X" | state dir reused for a different card; use a fresh `--state-dir` |
| card disappears mid-run (USB reset) | re-attach, re-mount, re-run the same command |
| I/O errors mid-run / reader locks up | power-cycle the reader (unplug 10 s), make sure the host has stable power, re-mount, then `run --retry-errors`; the errored files' originals were left untouched |
| macOS metadata (`.Spotlight-V100`, `.fseventsd`) appears on exFAT cards | macOS writes it whenever the card is attached to the Mac; harmless — delete it after the run if the card lives in another device |
| `error:` entries in the summary | nothing was lost — verified staged copies are kept in `~/.tfrefresh/<uuid>/staged/`; inspect before deleting anything |

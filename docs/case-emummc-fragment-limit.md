# Case study: file-based emuMMC black screen after a refresh

**Summary:** refreshing an SD card that hosts a file-based Nintendo Switch
emuMMC can leave the emuMMC's eMMC part files fragmented past a hard limit
in Atmosphere's emuMMC driver. The console then shows the Atmosphere logo
and instantly goes black (reboots to RCM) when booting the emuMMC, while
everything else — hekate, sysMMC CFW, stock — still works. File contents
are byte-identical; only the physical layout changed.

Observed in the field, 2026-10: 512 GB exFAT card, 83% full, file-based
emuMMC at `emuMMC/SD00`. After a full tfrefresh pass, `eMMC/12` had 599
extents. Defragmenting it back to 1 extent restored booting immediately.

## Symptoms

- hekate boots fine; its emuMMC Info page shows the emuMMC as enabled and
  correctly configured.
- Booting the emuMMC: Atmosphere logo appears, then the screen goes black
  within a second (the console silently reboots to RCM; on modchip
  consoles this can look like a power-off or a boot loop).
- sysMMC (CFW and stock) boots normally.
- Every file's SHA-256 matches the pre-refresh state; `fsck` is clean;
  hekate's "Fix Archive Bit" reports nothing to do.
- No Atmosphere crash/fatal reports are written — see
  [Why there is no log](#why-there-is-no-log) below.

## Why there is no log

The silence is structural, and it is why this failure stayed a community
mystery for years:

1. **The abort fires before any logging infrastructure exists.**
   `_file_based_emmc_initialize()` runs inside `sdmmc_initialize()`, in the
   earliest phase of the Horizon OS boot. Atmosphere's report writers —
   the `fatal` sysmodule (`/atmosphere/fatal_reports`), crash reports, and
   `erpt_reports` — all require the FS sysmodule to be up and the SD card
   mounted through the normal (already-redirected) path. None of that
   exists yet at the moment of failure; there is literally no file-writing
   facility available.
2. **The error record is stashed in IRAM, not on disk.** `fatal_abort()`
   (emummc `source/utils/fatal.c`) fills an `atmosphere_fatal_error_ctx`
   and copies it to physical address `0x4003E000` (magic `"AFE1"`), then
   calls `smcRebootToRcm()`. The source even carries the telling comment
   `// Basic error storage for Atmosphere / TODO: Maybe include a small
   reboot2payload stub?`. The design intent is that a *warm* reboot back
   into Atmosphere would find the context and show the fatal-error screen.
3. **The warm-reboot handoff usually never completes.** Rebooting to RCM
   hands control to whatever injects next: on an RCM-exploit console the
   unit sits in RCM looking powered-off; on a modchip console the chip
   typically re-injects hekate, so the user lands back in the hekate menu
   (or loops) instead of Atmosphere's fatal screen. Either way the screen
   is never seen — and any power-off wipes IRAM, erasing the last trace.

If the fatal screen *is* ever captured for this failure (e.g. by
chainloading straight back into Atmosphere without power-cycling), the
fingerprint is: **Title ID `0100000000000000` (FS), Error Desc `0xE`
(14)** — `Fatal_FatfsMemExhaustion` in the current emummc source (the
enum value may shift across versions). Seeing that code on a file-based
emuMMC boot is this bug.

So the observable evidence reduces to: logo, black, no reports, and
nothing in `/atmosphere/crash_reports`, `/atmosphere/fatal_reports`, or
`erpt_reports` — exactly the "unsolvable" reports scattered across the
forums.

## Root cause

Atmosphere's emuMMC driver does not walk the filesystem to serve NAND
reads. For speed, at boot it opens each part file in `emuMMC/*/eMMC/`
(`BOOT0`, `BOOT1`, `00`, `01`, ...) and pre-builds a **cluster link map**
(a fragment list) in a fixed-size table:

- `emummc.h`: `EMUMMC_FP_CLMT_COUNT = 1024` table entries per file.
- FatFs `f_lseek(CREATE_LINKMAP)` stores **2 entries per fragment**
  (top + length), so the table holds at most **511 fragments**.
- `emummc.c` `_file_based_emmc_initialize()`: if `f_expand_cltbl()`
  fails, it calls `fatal_abort(Fatal_FatfsMemExhaustion)`, which reboots
  straight to RCM — no message, no log. Instant black screen.

hekate creates the part files fully contiguous (1 fragment each), so a
fresh emuMMC never hits this. But any tool that rewrites the files in
place — including tfrefresh — hands allocation back to the host OS's
exFAT/FAT32 driver, and on a nearly-full card free space is scattered, so
a rewritten 4 GB part file can come back in hundreds of pieces. The file
contents stay bit-identical, so pure hash verification does not catch it.

## Detection

On Linux, with the card mounted:

```sh
filefrag /path/to/card/emuMMC/*/eMMC/*
```

Any file with more than **511 extents** will fatally abort the emuMMC
boot. tfrefresh >= 1.2 runs this check automatically after every `run`
and prints a warning listing offenders (filefrag must be installed; on
macOS the check is skipped).

## Fix: defragment the part files

Rewrite each fragmented part file into one contiguous allocation. Do this
file by file, and never trust the copy blindly — hash-verify before
deleting the original:

```sh
cd /path/to/card/emuMMC/SD00/eMMC
for f in *; do
  n=$(filefrag "$f" | grep -o '[0-9]* extent' | cut -d' ' -f1)
  [ "$n" -le 1 ] && continue
  cp "$f" "$f.defrag-tmp" && sync
  m=$(filefrag "$f.defrag-tmp" | grep -o '[0-9]* extent' | cut -d' ' -f1)
  if [ "$m" -eq 1 ] && \
     [ "$(sha256sum < "$f")" = "$(sha256sum < "$f.defrag-tmp")" ]; then
    rm "$f" && mv "$f.defrag-tmp" "$f" && sync
    echo "$f: now contiguous, hash verified"
  else
    rm -f "$f.defrag-tmp"
    echo "$f: could not get a contiguous allocation, retry or free space"
  fi
done
```

Notes:

- You need free space at least as large as the biggest part file
  (~4 GB for a hekate split emuMMC).
- If a retry keeps coming back fragmented, free up space or move some
  large files off and back first to open up a contiguous region.
- A heavier but thorough alternative: copy the whole card to a computer,
  reformat, copy back — freshly written cards allocate contiguously.

## Lesson for tfrefresh

tfrefresh guarantees *content* integrity (every rewrite is SHA-256
verified and journaled), but not *layout* invariants — and file-based
emuMMC is a consumer with a hard layout requirement. Since v1.2 the tool
checks `emuMMC/*/eMMC/*` fragment counts after each run and warns with
the recipe above. The general principle: **any bootloader/driver that
maps file data via fixed-size fragment tables is fragile against
rewrite-induced fragmentation**; when in doubt, run `filefrag` on boot
partitions/images after refreshing a card.

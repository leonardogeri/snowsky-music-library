---
name: snowsky-music-library
description: "Audit, clean, organize, and prepare local music folders for Snowsky/Echo-style playback. Use when Codex needs to process music libraries containing MP3/FLAC/M4A files and archives such as .rar/.zip: extract archives, remove originals after verified extraction, normalize folders to Artist/Album/Track, standardize embedded album art to JPEG 600x600, fetch missing album covers from web sources with fallback, detect missing tracks, duplicate track numbers, inconsistent metadata, non-audio residue, or other readiness issues."
---

# Snowsky Music Library

Prepare a local music folder for Snowsky by making it predictable, browsable, and metadata-complete.

Use the bundled script first when possible:

```bash
python3 ~/.codex/skills/snowsky-music-library/scripts/music_library_tool.py audit /path/to/music
```

The script avoids destructive changes unless an action command is explicitly used. For archive deletion, only delete originals after extraction succeeds and the extracted files are visible in the audit.

## Workflow

1. **Inventory the folder**
   - Count audio files, archive files, image files, hidden files, and non-audio residue.
   - Treat `.mp3`, `.flac`, and `.m4a` as audio files.
   - Read MP3 ID3 tags, FLAC Vorbis comments/PICTURE blocks, and M4A/MP4 iTunes metadata directly if external tag tools are unavailable.
   - Report missing `artist`, `album`, `title`, `track`, missing embedded art, odd folder depth, duplicate track numbers, and sequence gaps.
   - Report missing `.lrc` sidecars and `.lrc` files that contain only metadata or empty `[00:00.00]` placeholders.

2. **Extract archives**
   - Scan for `.rar` and `.zip`.
   - Extract each archive into a temporary or named staging folder first.
   - Verify that extraction produced files and that audio files are readable.
   - Move extracted music into the library only after verification.
   - Delete the original archive only when the user requested deletion or the task explicitly says to remove originals.
   - Prefer `bsdtar` or `ditto` if available. If RAR support is missing, use `unrar` or `7z` when installed; otherwise ask before installing anything.

3. **Normalize folder layout**
   - Target exactly `Artist/Album/Music file`.
   - Prefer embedded metadata for `artist` and `album`.
   - Fall back to path names only when metadata is absent.
   - Sanitize path segments: replace `/` with `_`, remove control characters, trim whitespace, and avoid empty names.
   - Detect destination conflicts before moving. Do not overwrite existing files silently.
   - Move sidecar album art such as `cover.jpg` with the album when it belongs to that album.

4. **Normalize file names**
   - Keep the existing title text unless the user asks for deeper renaming.
   - Ensure filenames sort by track number: `01 Title.ext`, `02 Title.ext`.
   - For SoundCloud-style collections, order tracks by authoritative publish date when available.
   - Preserve extensions and avoid changing audio encoding unless explicitly requested.

5. **Normalize embedded cover art**
   - Target embedded cover: `image/jpeg`, `600x600`.
   - For albums that already have art, extract one representative cover, resize/convert with `sips`, and embed the same JPEG into every track of the album.
   - For MP3, use an ID3 `APIC` front-cover frame.
   - For FLAC, use a FLAC `PICTURE` metadata block.
   - For M4A, use an MP4/iTunes `covr` metadata item.
   - Re-audit after writing and require every audio file to report JPEG 600x600.

6. **Fetch missing album covers**
   - Preferred source: iTunes Search API / Apple artwork, because it provides stable square artwork URLs.
   - Fallback: MusicBrainz search plus Cover Art Archive.
   - For ambiguous local album names, add explicit aliases before applying:
     - `Blue (1999 Remastered Edition)` -> `Blue`
     - `Love And Hate [remastered 2011]` -> `Songs of Love and Hate`
     - `Man [remastered 2011]` -> `I'm Your Man`
   - For risky matches, print the chosen source/title/URL and inspect before embedding.
   - Never use a low-confidence search hit just because it is the first result.

7. **Detect album problems**
   - Report missing sequence numbers between `1` and the album max track number.
   - Report duplicate track numbers.
   - Report mixed album names within one folder, mixed artists where unexpected, and inconsistent cover hashes within one album.
   - Report non-audio files in album folders that are not expected sidecars.
   - Treat `.lrc` lyrics and sidecar cover images (`.jpg`, `.jpeg`, `.png`) as expected album sidecars.
   - Do not treat a blank `.lrc` placeholder as valid lyrics.
   - Report archives left in the root after extraction and whether they were intentionally kept.

8. **Lyrics sidecars**
   - A `.lrc` sidecar is valid only when it contains actual lyric/timed lyric content, not only metadata tags such as `[ar:]`, `[al:]`, `[ti:]`, `[offset:]`, or an empty `[00:00.00]` line.
   - Do not create blank `.lrc` placeholders as a substitute for lyrics.
   - Use `fetch-lrc-lrclib` to fill missing or blank `.lrc` sidecars from LRCLIB, the service used by the open source LRCGET client, when the user requests this provider.
   - Prefer synced lyrics. Do not print downloaded lyric text in terminal output or final responses; report only counts and matched metadata.
   - Before running `fetch-lrc-lrclib`, always ask the user for explicit confirmation. Explain that the command performs external network requests, can take several minutes on large libraries, writes many `.lrc` files, and can consume a large number of tokens if verbose output is enabled.
   - Before asking, run `audit` or otherwise summarize the exact scope: `MISSING_LRC`, `BLANK_LRC`, total audio count, and whether the run will overwrite only missing/blank `.lrc` files.
   - For approved large runs, use quiet output by default: `fetch-lrc-lrclib . --jobs 4 --quiet`. Use `--dry-run --limit N` only when the user approves a network test first.
   - If the user interrupts a `fetch-lrc-lrclib` run, check for a still-running process and stop it before proceeding unless the user explicitly asks to let it continue.

9. **Final verification**
   - Confirm every audio file is exactly three levels deep: `Artist/Album/File`.
   - Confirm no audio file lacks embedded art.
   - Confirm all embedded art is JPEG 600x600.
   - Confirm every audio file has a nonblank `.lrc` sidecar when lyrics are part of the requested readiness criteria.
   - Confirm archives and hidden files are either removed or intentionally reported.
   - Summarize counts changed: archives extracted/deleted, tracks moved, covers fetched, covers normalized, and unresolved warnings.

## Commands

Use these from the target music folder or pass an explicit path.

```bash
python3 ~/.codex/skills/snowsky-music-library/scripts/music_library_tool.py audit .
python3 ~/.codex/skills/snowsky-music-library/scripts/music_library_tool.py extract-archives . --delete-originals
python3 ~/.codex/skills/snowsky-music-library/scripts/music_library_tool.py organize .
python3 ~/.codex/skills/snowsky-music-library/scripts/music_library_tool.py normalize-existing-art .
python3 ~/.codex/skills/snowsky-music-library/scripts/music_library_tool.py fetch-missing-art .
python3 ~/.codex/skills/snowsky-music-library/scripts/music_library_tool.py fetch-lrc-lrclib . --jobs 4 --quiet
python3 ~/.codex/skills/snowsky-music-library/scripts/music_library_tool.py verify .
```

Prefer running `audit` before and after every mutating command.

## Safety Rules

- Treat archive deletion, overwrites, and bulk moves as destructive. Confirm intent unless the user explicitly requested that operation.
- Keep original audio encoding. Do not convert FLAC to MP3 or transcode MP3 unless asked.
- Work with dirty folders carefully: make a preflight plan, detect conflicts, then move.
- Use local tools first. If web access is needed for covers, cite the source family in the final answer.
- If metadata and path disagree, prefer metadata for organization but report the mismatch.

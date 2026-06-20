# Snowsky Music Library

Codex skill and local helper script for auditing and preparing personal music
folders for Snowsky/Echo-style playback.

The project helps make a local library predictable:

- audits audio metadata, embedded cover art, folder depth, archives, and LRC sidecars
- organizes tracks into `Artist/Album/File`
- normalizes embedded cover art to JPEG 600x600
- optionally fetches missing covers from public metadata/artwork services
- optionally fetches missing synchronized LRC sidecars from LRCLIB

This repository distributes tooling only. It does not include music files,
commercial cover art, downloaded lyrics, LRCLIB database dumps, or a personal
music library.

## Install As A Codex Skill

Clone the repository into your Codex skills directory:

```bash
git clone https://github.com/<your-user>/snowsky-music-library.git \
  ~/.codex/skills/snowsky-music-library
```

Then use the skill by referencing `$snowsky-music-library` in Codex.

## Direct Script Usage

Run commands from a music folder or pass a root path explicitly:

```bash
python3 scripts/music_library_tool.py audit /path/to/music
python3 scripts/music_library_tool.py extract-archives /path/to/music --delete-originals
python3 scripts/music_library_tool.py organize /path/to/music --dry-run
python3 scripts/music_library_tool.py normalize-existing-art /path/to/music
python3 scripts/music_library_tool.py fetch-missing-art /path/to/music
python3 scripts/music_library_tool.py fetch-lrc-lrclib /path/to/music --jobs 4 --quiet
python3 scripts/music_library_tool.py verify /path/to/music
```

Prefer running `audit` before and after any mutating command.

## Requirements

- Python 3.10 or newer
- macOS `sips` for artwork resizing
- `bsdtar`, `unrar`, or `7z` for RAR extraction when needed
- Network access only for optional cover and lyrics lookup commands

The script intentionally avoids third-party Python dependencies.

## Lyrics And Third-Party Content

`fetch-lrc-lrclib` uses the public LRCLIB API to fill missing or blank `.lrc`
sidecars in a local library. This is optional and writes files only on the
user's machine.

Do not commit downloaded `.lrc` files to this repository. Song lyrics may be
copyrighted independently from the software used to retrieve them. Attribution
to LRCLIB or LRCGET is appropriate, but attribution alone does not make
redistribution of lyrics safe.

Test fixtures in this repository are synthetic and do not contain real song
lyrics.

## Development

Run the lightweight test suite:

```bash
python3 -m unittest discover -s tests
```

Run the CLI smoke test:

```bash
python3 scripts/music_library_tool.py --help
```

Before publishing, check that no personal media or real lyrics are staged:

```bash
git status --short
rg -n "Users/|/Volumes/|lrclib-db-dumps|BEGIN [A-Z ]*PRIVATE|api[_-]?key|cookie" .
find . -type f \( -name '*.mp3' -o -name '*.flac' -o -name '*.m4a' -o -name '*.rar' -o -name '*.zip' \)
```

## License

Code in this repository is licensed under the MIT License. See `LICENSE`.

The MIT License applies only to this repository's code and documentation. It
does not grant rights to music, cover art, lyrics, metadata services, or other
third-party content fetched by users.

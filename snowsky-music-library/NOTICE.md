# Notices And Attribution

This repository contains original helper code and Codex skill instructions for
preparing a local music library. It does not vendor code from LRCLIB or LRCGET,
and it does not distribute lyrics downloaded from LRCLIB.

## LRCLIB

The optional `fetch-lrc-lrclib` command queries the public LRCLIB API:

- Site: https://lrclib.net
- Source: https://github.com/tranxuanthang/lrclib
- License for LRCLIB software: MIT

LRCLIB provides a service for finding and contributing synchronized lyrics. The
license for the LRCLIB server software is separate from any rights that may
apply to individual lyric records returned by the service.

## LRCGET

LRCGET is the official LRCLIB client and a relevant prior tool in the same
problem space:

- Source: https://github.com/tranxuanthang/lrcget
- License for LRCGET software: MIT

This repository does not include LRCGET code. It credits LRCGET because users
who want a dedicated app for mass-downloading LRC files should know the
official client exists.

## Cover And Metadata Sources

The optional artwork lookup command may use:

- Apple iTunes Search API / Apple artwork URLs
- MusicBrainz
- Cover Art Archive

Fetched artwork and metadata remain third-party content. Users are responsible
for complying with the terms and rights that apply to those sources.

## Repository Content Policy

Do not commit:

- music files
- downloaded `.lrc` files containing real lyrics
- commercial cover art
- LRCLIB database dumps
- personal library caches
- credentials, tokens, cookies, or private paths

Only synthetic fixtures created for testing should be stored in this
repository.

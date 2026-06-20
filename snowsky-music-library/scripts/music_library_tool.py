#!/usr/bin/env python3
"""Music library preparation helper for the snowsky-music-library skill.

This script intentionally uses only the Python standard library plus macOS
`sips` for image resizing when available. It reads and writes MP3 ID3v2.3/2.4
APIC frames, FLAC PICTURE blocks, and M4A/MP4 iTunes metadata directly.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path


AUDIO_EXTS = {".mp3", ".flac", ".m4a"}
ARCHIVE_EXTS = {".rar", ".zip"}
EXPECTED_SIDECAR_EXTS = {".jpg", ".jpeg", ".png", ".lrc"}
USER_AGENT = "SnowskyMusicLibrarySkill/1.0"


ALIASES = {
    ("Joni Mitchell", "Blue (1999 Remastered Edition)"): "Blue",
    ("Leonard Cohen", "Love And Hate [remastered 2011]"): "Songs of Love and Hate",
    ("Leonard Cohen", "Man [remastered 2011]"): "I'm Your Man",
}


MANUAL_URLS = {
    # Add high-confidence overrides here when automatic search is ambiguous.
    ("Leonard Cohen", "Love And Hate [remastered 2011]"): (
        "coverartarchive",
        "Songs of Love and Hate",
        "https://coverartarchive.org/release/28e1dddf-af36-3f55-a840-1ad283cc925a/front-500",
    ),
}


@dataclass
class AudioInfo:
    path: Path
    kind: str
    artist: str = ""
    album: str = ""
    title: str = ""
    track: str = ""
    has_art: bool = False
    art_mime: str = ""
    art_size: tuple[int, int] | None = None
    art_sha1: str = ""


def fail(message: str, code: int = 1) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(code)


def syncsafe_decode(raw: bytes) -> int:
    return (raw[0] << 21) | (raw[1] << 14) | (raw[2] << 7) | raw[3]


def syncsafe_encode(value: int) -> bytes:
    return bytes(
        [
            (value >> 21) & 0x7F,
            (value >> 14) & 0x7F,
            (value >> 7) & 0x7F,
            value & 0x7F,
        ]
    )


def frame_size_decode(raw: bytes, major: int) -> int:
    return syncsafe_decode(raw) if major == 4 else int.from_bytes(raw, "big")


def frame_size_encode(value: int, major: int) -> bytes:
    return syncsafe_encode(value) if major == 4 else value.to_bytes(4, "big")


def decode_id3_text(content: bytes) -> str:
    if not content:
        return ""
    encoding = content[0]
    raw = content[1:]
    codec = {0: "latin1", 1: "utf-16", 2: "utf-16-be", 3: "utf-8"}.get(
        encoding, "latin1"
    )
    return raw.decode(codec, "replace").rstrip("\x00")


def jpeg_size(data: bytes) -> tuple[int, int] | None:
    if not data.startswith(b"\xff\xd8"):
        return None
    pos = 2
    sof_markers = set(range(0xC0, 0xC4)) | set(range(0xC5, 0xC8)) | set(
        range(0xC9, 0xCC)
    ) | set(range(0xCD, 0xD0))
    while pos + 9 < len(data):
        if data[pos] != 0xFF:
            pos += 1
            continue
        while pos < len(data) and data[pos] == 0xFF:
            pos += 1
        if pos >= len(data):
            return None
        marker = data[pos]
        pos += 1
        if marker in (0xD8, 0xD9):
            continue
        if pos + 2 > len(data):
            return None
        seg_len = int.from_bytes(data[pos : pos + 2], "big")
        if seg_len < 2 or pos + seg_len > len(data):
            return None
        if marker in sof_markers:
            height = int.from_bytes(data[pos + 3 : pos + 5], "big")
            width = int.from_bytes(data[pos + 5 : pos + 7], "big")
            return width, height
        pos += seg_len
    return None


def png_size(data: bytes) -> tuple[int, int] | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    return None


def image_size(data: bytes) -> tuple[int, int] | None:
    return jpeg_size(data) or png_size(data)


@dataclass
class Atom:
    start: int
    kind: bytes
    header_size: int
    end: int
    large_size: bool = False

    @property
    def payload_start(self) -> int:
        return self.start + self.header_size

    @property
    def size(self) -> int:
        return self.end - self.start


def iter_atoms(data: bytes, start: int, end: int):
    pos = start
    while pos + 8 <= end:
        raw_size = int.from_bytes(data[pos : pos + 4], "big")
        kind = data[pos + 4 : pos + 8]
        header_size = 8
        large_size = False
        if raw_size == 1:
            if pos + 16 > end:
                break
            size = int.from_bytes(data[pos + 8 : pos + 16], "big")
            header_size = 16
            large_size = True
        elif raw_size == 0:
            size = end - pos
        else:
            size = raw_size
        if size < header_size or pos + size > end:
            break
        yield Atom(pos, kind, header_size, pos + size, large_size)
        pos += size


def find_child_atom(data: bytes, start: int, end: int, kind: bytes) -> Atom | None:
    for atom in iter_atoms(data, start, end):
        if atom.kind == kind:
            return atom
    return None


def find_m4a_ilst(data: bytes) -> list[Atom] | None:
    moov = find_child_atom(data, 0, len(data), b"moov")
    if not moov:
        return None
    udta = find_child_atom(data, moov.payload_start, moov.end, b"udta")
    if not udta:
        return None
    meta = find_child_atom(data, udta.payload_start, udta.end, b"meta")
    if not meta:
        return None
    ilst = find_child_atom(data, meta.payload_start + 4, meta.end, b"ilst")
    if not ilst:
        return None
    return [moov, udta, meta, ilst]


def make_atom(kind: bytes, payload: bytes) -> bytes:
    return (len(payload) + 8).to_bytes(4, "big") + kind + payload


def make_m4a_data_atom(data_kind: int, payload: bytes) -> bytes:
    return make_atom(b"data", data_kind.to_bytes(4, "big") + b"\x00\x00\x00\x00" + payload)


def extract_apic_image(content: bytes) -> tuple[str, bytes]:
    encoding = content[0]
    rest = content[1:]
    mime_raw, rest = rest.split(b"\x00", 1)
    mime = mime_raw.decode("latin1", "replace")
    rest = rest[1:]  # picture type
    if encoding in (1, 2):
        idx = rest.find(b"\x00\x00")
        image = rest[idx + 2 :] if idx >= 0 else rest
        if image.startswith(b"\x00"):
            image = image[1:]
    else:
        idx = rest.find(b"\x00")
        image = rest[idx + 1 :] if idx >= 0 else rest
    return mime, image


def parse_mp3(path: Path) -> AudioInfo:
    data = path.read_bytes()
    info = AudioInfo(path=path, kind="mp3")
    if len(data) < 10 or data[:3] != b"ID3":
        return info
    major = data[3]
    end = min(len(data), 10 + syncsafe_decode(data[6:10]))
    pos = 10
    frame_ids = {"TPE1": "artist", "TALB": "album", "TIT2": "title", "TRCK": "track"}
    while pos + 10 <= end:
        fid = data[pos : pos + 4].decode("latin1", "replace")
        if not fid.strip("\x00"):
            break
        size = frame_size_decode(data[pos + 4 : pos + 8], major)
        if size <= 0 or pos + 10 + size > len(data):
            break
        content = data[pos + 10 : pos + 10 + size]
        if fid in frame_ids:
            setattr(info, frame_ids[fid], decode_id3_text(content))
        elif fid == "APIC":
            info.has_art = True
            try:
                mime, image = extract_apic_image(content)
                info.art_mime = mime
                info.art_size = image_size(image)
                info.art_sha1 = hashlib.sha1(image).hexdigest()
            except Exception:
                pass
        pos += 10 + size
    return info


def parse_flac(path: Path) -> AudioInfo:
    data = path.read_bytes()
    info = AudioInfo(path=path, kind="flac")
    if not data.startswith(b"fLaC"):
        return info
    pos = 4
    last = False
    while not last and pos + 4 <= len(data):
        header = data[pos]
        last = bool(header & 0x80)
        block_type = header & 0x7F
        size = int.from_bytes(data[pos + 1 : pos + 4], "big")
        block = data[pos + 4 : pos + 4 + size]
        if block_type == 4 and len(block) >= 8:
            vendor_len = int.from_bytes(block[:4], "little")
            q = 4 + vendor_len
            if q + 4 <= len(block):
                count = int.from_bytes(block[q : q + 4], "little")
                q += 4
                for _ in range(count):
                    if q + 4 > len(block):
                        break
                    item_len = int.from_bytes(block[q : q + 4], "little")
                    q += 4
                    item = block[q : q + item_len].decode("utf-8", "replace")
                    q += item_len
                    if "=" not in item:
                        continue
                    key, value = item.split("=", 1)
                    key = key.lower()
                    if key == "artist":
                        info.artist = value
                    elif key == "album":
                        info.album = value
                    elif key == "title":
                        info.title = value
                    elif key in {"tracknumber", "track"}:
                        info.track = value
        elif block_type == 6:
            info.has_art = True
            try:
                q = 4
                mime_len = int.from_bytes(block[q : q + 4], "big")
                q += 4
                info.art_mime = block[q : q + mime_len].decode("latin1", "replace")
                q += mime_len
                desc_len = int.from_bytes(block[q : q + 4], "big")
                q += 4 + desc_len
                width = int.from_bytes(block[q : q + 4], "big")
                height = int.from_bytes(block[q + 4 : q + 8], "big")
                q += 16
                image_len = int.from_bytes(block[q : q + 4], "big")
                q += 4
                image = block[q : q + image_len]
                info.art_size = (width, height)
                info.art_sha1 = hashlib.sha1(image).hexdigest()
            except Exception:
                pass
        pos += 4 + size
    return info


def parse_m4a_text_data(payload: bytes) -> str:
    return payload.decode("utf-8", "replace").rstrip("\x00")


def parse_m4a_track(payload: bytes) -> str:
    if len(payload) >= 6:
        number = int.from_bytes(payload[2:4], "big")
        if number:
            return str(number)
    return ""


def parse_m4a(path: Path) -> AudioInfo:
    data = path.read_bytes()
    info = AudioInfo(path=path, kind="m4a")
    atoms = find_m4a_ilst(data)
    if not atoms:
        return info
    ilst = atoms[-1]
    text_tags = {
        b"\xa9ART": "artist",
        b"aART": "artist",
        b"\xa9alb": "album",
        b"\xa9nam": "title",
    }
    for item in iter_atoms(data, ilst.payload_start, ilst.end):
        data_atom = find_child_atom(data, item.payload_start, item.end, b"data")
        if not data_atom or data_atom.payload_start + 8 > data_atom.end:
            continue
        data_kind = int.from_bytes(data[data_atom.payload_start : data_atom.payload_start + 4], "big")
        payload = data[data_atom.payload_start + 8 : data_atom.end]
        if item.kind in text_tags:
            value = parse_m4a_text_data(payload)
            if value and (item.kind != b"aART" or not info.artist):
                setattr(info, text_tags[item.kind], value)
        elif item.kind == b"trkn":
            info.track = parse_m4a_track(payload)
        elif item.kind == b"covr":
            info.has_art = True
            if data_kind == 13:
                info.art_mime = "image/jpeg"
            elif data_kind == 14:
                info.art_mime = "image/png"
            else:
                info.art_mime = f"m4a/{data_kind}"
            info.art_size = image_size(payload)
            info.art_sha1 = hashlib.sha1(payload).hexdigest()
    return info


def parse_audio(path: Path) -> AudioInfo:
    suffix = path.suffix.lower()
    if suffix == ".mp3":
        return parse_mp3(path)
    if suffix == ".flac":
        return parse_flac(path)
    if suffix == ".m4a":
        return parse_m4a(path)
    raise ValueError(path)


def all_audio(root: Path) -> list[AudioInfo]:
    return [
        parse_audio(path)
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.suffix.lower() in AUDIO_EXTS
    ]


def clean_segment(value: str, fallback: str) -> str:
    value = (value or fallback).strip()
    value = value.replace("/", "_").replace(":", " _")
    value = re.sub(r"[\x00-\x1f]", "", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    return value or fallback


def infer_artist_album(info: AudioInfo, root: Path) -> tuple[str, str]:
    rel = info.path.relative_to(root)
    parts = rel.parts
    artist = info.artist or (parts[0] if len(parts) >= 3 else "Unknown Artist")
    album = info.album or (parts[1] if len(parts) >= 3 else "Unknown Album")
    return clean_segment(artist, "Unknown Artist"), clean_segment(album, "Unknown Album")


def track_number(value: str) -> int | None:
    match = re.match(r"\s*(\d+)", value or "")
    return int(match.group(1)) if match else None


def title_from_filename(path: Path) -> str:
    value = path.stem
    value = re.sub(r"^\s*\d+\s*[-._ ]\s*", "", value)
    return value.strip() or path.stem


def lrc_has_lyric_content(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    for line in text.splitlines():
        value = line.strip()
        if not value:
            continue
        if value.startswith(("[ar:", "[al:", "[ti:", "[offset:", "[by:", "[re:", "[ve:")):
            continue
        if value in {"[00:00.00]", "[00:00.000]"}:
            continue
        return True
    return False


def lrc_needs_fetch(info: AudioInfo) -> bool:
    path = info.path.with_suffix(".lrc")
    return not path.exists() or not lrc_has_lyric_content(path)


def album_groups(root: Path) -> dict[tuple[str, str], list[AudioInfo]]:
    groups: dict[tuple[str, str], list[AudioInfo]] = defaultdict(list)
    for info in all_audio(root):
        groups[infer_artist_album(info, root)].append(info)
    return groups


def print_audit(root: Path) -> dict[str, int]:
    audio = all_audio(root)
    archives = [p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in ARCHIVE_EXTS]
    hidden = [p for p in root.rglob("*") if p.is_file() and p.name.startswith(".")]
    non_audio = [
        p
        for p in root.rglob("*")
        if p.is_file()
        and p.suffix.lower() not in AUDIO_EXTS
        and p.suffix.lower() not in ARCHIVE_EXTS
        and p.suffix.lower() not in EXPECTED_SIDECAR_EXTS
        and not p.name.startswith(".")
    ]
    missing_art = [i for i in audio if not i.has_art]
    bad_art = [
        i for i in audio if i.has_art and (i.art_mime != "image/jpeg" or i.art_size != (600, 600))
    ]
    bad_depth = [i for i in audio if len(i.path.relative_to(root).parts) != 3]
    missing_lrc = [i for i in audio if not i.path.with_suffix(".lrc").exists()]
    blank_lrc = [
        i
        for i in audio
        if i.path.with_suffix(".lrc").exists()
        and not lrc_has_lyric_content(i.path.with_suffix(".lrc"))
    ]
    missing_tags = [
        i
        for i in audio
        if not (i.artist.strip() and i.album.strip() and i.title.strip() and i.track.strip())
    ]

    print(f"TOTAL_AUDIO {len(audio)}")
    print(f"ARCHIVES {len(archives)}")
    print(f"HIDDEN_FILES {len(hidden)}")
    print(f"NON_AUDIO_RESIDUE {len(non_audio)}")
    print(f"MISSING_ART {len(missing_art)}")
    print(f"BAD_ART_FORMAT_OR_SIZE {len(bad_art)}")
    print(f"BAD_FOLDER_DEPTH {len(bad_depth)}")
    print(f"MISSING_LRC {len(missing_lrc)}")
    print(f"BLANK_LRC {len(blank_lrc)}")
    print(f"MISSING_BASIC_TAGS {len(missing_tags)}")

    for label, items in [
        ("ARCHIVE", archives),
        ("MISSING_ART", [i.path for i in missing_art]),
        ("BAD_ART", [i.path for i in bad_art]),
        ("BAD_DEPTH", [i.path for i in bad_depth]),
        ("MISSING_LRC", [i.path.with_suffix(".lrc") for i in missing_lrc]),
        ("BLANK_LRC", [i.path.with_suffix(".lrc") for i in blank_lrc]),
        ("MISSING_TAGS", [i.path for i in missing_tags]),
    ]:
        for item in items[:50]:
            print(f"{label} {item.relative_to(root)}")
        if len(items) > 50:
            print(f"{label} ... {len(items) - 50} more")

    for (artist, album), infos in sorted(album_groups(root).items()):
        numbers: dict[int, list[Path]] = defaultdict(list)
        for info in infos:
            number = track_number(info.track) or track_number(info.path.name)
            if number is not None:
                numbers[number].append(info.path)
        duplicates = {k: v for k, v in numbers.items() if len(v) > 1}
        missing = []
        if len(numbers) >= 3:
            max_track = max(numbers)
            missing = [n for n in range(1, max_track + 1) if n not in numbers]
        if duplicates or missing:
            print(f"ALBUM_WARNING {artist} | {album}")
            if missing:
                print(f"  MISSING_TRACK_NUMBERS {missing}")
            for number, paths in duplicates.items():
                rels = [str(p.relative_to(root)) for p in paths]
                print(f"  DUPLICATE_TRACK_NUMBER {number}: {json.dumps(rels, ensure_ascii=False)}")

    return {
        "audio": len(audio),
        "archives": len(archives),
        "missing_art": len(missing_art),
        "bad_art": len(bad_art),
        "bad_depth": len(bad_depth),
        "missing_lrc": len(missing_lrc),
        "blank_lrc": len(blank_lrc),
        "missing_tags": len(missing_tags),
    }


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    for index in range(1, 1000):
        candidate = path.with_name(f"{stem} ({index}){suffix}")
        if not candidate.exists():
            return candidate
    fail(f"could not find unique path for {path}")


def archive_staging_dir(root: Path, archive: Path) -> Path:
    name = clean_segment(archive.stem, "extracted")
    return unique_path(root / name)


def extract_archive(root: Path, archive: Path, delete_originals: bool) -> bool:
    destination = archive_staging_dir(root, archive)
    destination.mkdir(parents=True, exist_ok=False)
    suffix = archive.suffix.lower()
    if suffix == ".zip":
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(destination)
    elif suffix == ".rar":
        if shutil.which("bsdtar"):
            subprocess.run(["bsdtar", "-xf", str(archive), "-C", str(destination)], check=True)
        elif shutil.which("unrar"):
            subprocess.run(["unrar", "x", "-o-", str(archive), str(destination)], check=True)
        elif shutil.which("7z"):
            subprocess.run(["7z", "x", str(archive), f"-o{destination}"], check=True)
        else:
            print(f"NO_RAR_TOOL {archive}")
            destination.rmdir()
            return False
    else:
        return False

    extracted_files = [p for p in destination.rglob("*") if p.is_file()]
    audio_files = [p for p in extracted_files if p.suffix.lower() in AUDIO_EXTS]
    print(f"EXTRACTED {archive.relative_to(root)} -> {destination.relative_to(root)}")
    print(f"  FILES {len(extracted_files)}")
    print(f"  AUDIO {len(audio_files)}")
    if extracted_files and delete_originals:
        archive.unlink()
        print(f"DELETED_ARCHIVE {archive.relative_to(root)}")
    return bool(extracted_files)


def command_extract_archives(args: argparse.Namespace) -> None:
    root = args.root.resolve()
    archives = [
        p for p in sorted(root.rglob("*")) if p.is_file() and p.suffix.lower() in ARCHIVE_EXTS
    ]
    if not archives:
        print("NO_ARCHIVES")
        return
    for archive in archives:
        extract_archive(root, archive, args.delete_originals)


def command_organize(args: argparse.Namespace) -> None:
    root = args.root.resolve()
    moves: list[tuple[Path, Path]] = []
    for info in all_audio(root):
        artist, album = infer_artist_album(info, root)
        target = root / artist / album / info.path.name
        if info.path != target:
            moves.append((info.path, target))

    conflicts = [
        (src, dst)
        for src, dst in moves
        if dst.exists() and src.resolve() != dst.resolve()
    ]
    if conflicts:
        print("CONFLICTS")
        for src, dst in conflicts:
            print(f"{src.relative_to(root)} -> {dst.relative_to(root)}")
        fail("resolve conflicts before organizing")

    for src, dst in moves:
        print(f"MOVE {src.relative_to(root)} -> {dst.relative_to(root)}")
    if args.dry_run:
        print(f"DRY_RUN moves={len(moves)}")
        return

    for src, dst in moves:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
    remove_empty_dirs(root)
    print(f"MOVED {len(moves)}")


def remove_empty_dirs(root: Path) -> None:
    for directory in sorted([p for p in root.rglob("*") if p.is_dir()], key=lambda p: len(p.parts), reverse=True):
        try:
            directory.rmdir()
        except OSError:
            pass


def extract_mp3_cover(path: Path) -> bytes:
    data = path.read_bytes()
    if len(data) < 10 or data[:3] != b"ID3":
        raise RuntimeError(f"{path}: no ID3 tag")
    major = data[3]
    end = min(len(data), 10 + syncsafe_decode(data[6:10]))
    pos = 10
    while pos + 10 <= end:
        fid = data[pos : pos + 4]
        if not fid.strip(b"\x00"):
            break
        size = frame_size_decode(data[pos + 4 : pos + 8], major)
        content = data[pos + 10 : pos + 10 + size]
        if fid == b"APIC":
            return extract_apic_image(content)[1]
        pos += 10 + size
    raise RuntimeError(f"{path}: no APIC frame")


def embed_mp3_cover(path: Path, cover: bytes) -> None:
    data = path.read_bytes()
    if len(data) < 10 or data[:3] != b"ID3":
        raise RuntimeError(f"{path}: no ID3 tag")
    major = data[3]
    tag_end = 10 + syncsafe_decode(data[6:10])
    pos = 10
    frames: list[bytes] = []
    while pos + 10 <= tag_end:
        fid = data[pos : pos + 4]
        if not fid.strip(b"\x00"):
            break
        size = frame_size_decode(data[pos + 4 : pos + 8], major)
        if size <= 0 or pos + 10 + size > tag_end:
            break
        if fid != b"APIC":
            frames.append(data[pos : pos + 10 + size])
        pos += 10 + size
    content = b"\x00image/jpeg\x00\x03\x00" + cover
    frame = b"APIC" + frame_size_encode(len(content), major) + b"\x00\x00" + content
    payload = b"".join(frames) + frame
    tmp = path.with_name(path.name + ".cover_tmp")
    tmp.write_bytes(b"ID3" + data[3:6] + syncsafe_encode(len(payload)) + payload + data[tag_end:])
    os.chmod(tmp, path.stat().st_mode)
    tmp.replace(path)


def extract_flac_cover(path: Path) -> bytes:
    data = path.read_bytes()
    if not data.startswith(b"fLaC"):
        raise RuntimeError(f"{path}: not FLAC")
    pos = 4
    last = False
    while not last and pos + 4 <= len(data):
        header = data[pos]
        last = bool(header & 0x80)
        block_type = header & 0x7F
        size = int.from_bytes(data[pos + 1 : pos + 4], "big")
        block = data[pos + 4 : pos + 4 + size]
        if block_type == 6:
            q = 4
            mime_len = int.from_bytes(block[q : q + 4], "big")
            q += 4 + mime_len
            desc_len = int.from_bytes(block[q : q + 4], "big")
            q += 4 + desc_len + 16
            image_len = int.from_bytes(block[q : q + 4], "big")
            q += 4
            return block[q : q + image_len]
        pos += 4 + size
    raise RuntimeError(f"{path}: no PICTURE block")


def embed_flac_cover(path: Path, cover: bytes) -> None:
    data = path.read_bytes()
    if not data.startswith(b"fLaC"):
        raise RuntimeError(f"{path}: not FLAC")
    pos = 4
    last = False
    blocks: list[tuple[int, bytes]] = []
    while not last and pos + 4 <= len(data):
        header = data[pos]
        last = bool(header & 0x80)
        block_type = header & 0x7F
        size = int.from_bytes(data[pos + 1 : pos + 4], "big")
        block = data[pos + 4 : pos + 4 + size]
        if block_type != 6:
            blocks.append((block_type, block))
        pos += 4 + size
    audio = data[pos:]
    mime = b"image/jpeg"
    picture = b"".join(
        [
            (3).to_bytes(4, "big"),
            len(mime).to_bytes(4, "big"),
            mime,
            (0).to_bytes(4, "big"),
            (600).to_bytes(4, "big"),
            (600).to_bytes(4, "big"),
            (24).to_bytes(4, "big"),
            (0).to_bytes(4, "big"),
            len(cover).to_bytes(4, "big"),
            cover,
        ]
    )
    insert_at = len(blocks)
    for index, (block_type, _) in enumerate(blocks):
        if block_type == 1:
            insert_at = index
            break
    blocks.insert(insert_at, (6, picture))
    metadata = bytearray()
    for index, (block_type, block) in enumerate(blocks):
        metadata.append((0x80 if index == len(blocks) - 1 else 0) | block_type)
        metadata.extend(len(block).to_bytes(3, "big"))
        metadata.extend(block)
    tmp = path.with_name(path.name + ".cover_tmp")
    tmp.write_bytes(b"fLaC" + bytes(metadata) + audio)
    os.chmod(tmp, path.stat().st_mode)
    tmp.replace(path)


def extract_m4a_cover(path: Path) -> bytes:
    data = path.read_bytes()
    atoms = find_m4a_ilst(data)
    if not atoms:
        raise RuntimeError(f"{path}: no ilst metadata")
    ilst = atoms[-1]
    for item in iter_atoms(data, ilst.payload_start, ilst.end):
        if item.kind != b"covr":
            continue
        data_atom = find_child_atom(data, item.payload_start, item.end, b"data")
        if data_atom and data_atom.payload_start + 8 <= data_atom.end:
            return data[data_atom.payload_start + 8 : data_atom.end]
    raise RuntimeError(f"{path}: no covr atom")


def write_atom_size(payload: bytearray, atom: Atom, size: int) -> None:
    if atom.large_size:
        payload[atom.start + 8 : atom.start + 16] = size.to_bytes(8, "big")
    else:
        if size > 0xFFFFFFFF:
            raise RuntimeError("atom grew past 32-bit size")
        payload[atom.start : atom.start + 4] = size.to_bytes(4, "big")


def embed_m4a_cover(path: Path, cover: bytes) -> None:
    data = path.read_bytes()
    atoms = find_m4a_ilst(data)
    if not atoms:
        raise RuntimeError(f"{path}: no ilst metadata")
    ilst = atoms[-1]
    item = None
    for candidate in iter_atoms(data, ilst.payload_start, ilst.end):
        if candidate.kind == b"covr":
            item = candidate
            break
    covr_payload = make_m4a_data_atom(13, cover)
    covr_item = make_atom(b"covr", covr_payload)
    if item:
        start, end = item.start, item.end
    else:
        start = end = ilst.end
    rewritten = bytearray(data[:start] + covr_item + data[end:])
    delta = len(covr_item) - (end - start)
    for atom in atoms:
        write_atom_size(rewritten, atom, atom.size + delta)
    tmp = path.with_name(path.name + ".cover_tmp")
    tmp.write_bytes(rewritten)
    os.chmod(tmp, path.stat().st_mode)
    tmp.replace(path)


def normalize_image(image: bytes, tmpdir: Path) -> bytes:
    sips = shutil.which("sips")
    if not sips:
        fail("sips is required to normalize artwork to JPEG 600x600")
    source = tmpdir / "source.img"
    output = tmpdir / "cover_600.jpg"
    source.write_bytes(image)
    subprocess.run(
        [sips, "-s", "format", "jpeg", "-z", "600", "600", str(source), "--out", str(output)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    cover = output.read_bytes()
    if jpeg_size(cover) != (600, 600):
        raise RuntimeError("normalized cover is not JPEG 600x600")
    return cover


def embed_cover(path: Path, cover: bytes) -> None:
    if path.suffix.lower() == ".mp3":
        embed_mp3_cover(path, cover)
    elif path.suffix.lower() == ".flac":
        embed_flac_cover(path, cover)
    elif path.suffix.lower() == ".m4a":
        embed_m4a_cover(path, cover)


def extract_cover(path: Path) -> bytes:
    if path.suffix.lower() == ".mp3":
        return extract_mp3_cover(path)
    if path.suffix.lower() == ".flac":
        return extract_flac_cover(path)
    if path.suffix.lower() == ".m4a":
        return extract_m4a_cover(path)
    raise RuntimeError(path)


def command_normalize_existing_art(args: argparse.Namespace) -> None:
    root = args.root.resolve()
    changed = 0
    with tempfile.TemporaryDirectory(prefix="snowsky_art_") as td_raw:
        tmpdir = Path(td_raw)
        for (artist, album), infos in sorted(album_groups(root).items()):
            if not infos or not any(i.has_art for i in infos):
                continue
            needs = [
                i
                for i in infos
                if i.art_mime != "image/jpeg"
                or i.art_size != (600, 600)
                or len({j.art_sha1 for j in infos if j.art_sha1}) > 1
            ]
            if not needs:
                continue
            representative = next(i for i in infos if i.has_art)
            cover = normalize_image(extract_cover(representative.path), tmpdir)
            for info in infos:
                embed_cover(info.path, cover)
                changed += 1
            print(f"NORMALIZED {artist} | {album} | {len(infos)}")
    print(f"ART_NORMALIZED_FILES {changed}")


def norm_search(value: str) -> str:
    value = value.lower().replace("&", "and")
    value = re.sub(r"\[[^\]]*\]|\([^\)]*\)", " ", value)
    value = re.sub(
        r"\b(remaster(ed)?|mono|version|edition|soundtrack|motion|picture|from|the|a|of|and|album|[0-9]{4}|2011|1999)\b",
        " ",
        value,
    )
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())


def open_json(url: str) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=25) as response:
        return json.load(response)


def resolve_itunes(artist: str, album: str) -> tuple[str, str, str] | None:
    import difflib

    term_album = ALIASES.get((artist, album), album)
    term = f"{artist} {term_album}"
    url = "https://itunes.apple.com/search?" + urllib.parse.urlencode(
        {"term": term, "entity": "album", "limit": 15, "country": "US"}
    )
    data = open_json(url)
    target = norm_search(term_album)
    best = None
    for result in data.get("results", []):
        if norm_search(result.get("artistName", "")) != norm_search(artist):
            continue
        collection = result.get("collectionName", "")
        collection_norm = norm_search(collection)
        score = difflib.SequenceMatcher(None, target, collection_norm).ratio()
        if target and (target in collection_norm or collection_norm in target):
            score += 0.4
        if best is None or score > best[0]:
            best = (score, result)
    if best and best[0] >= 0.65:
        result = best[1]
        art = result.get("artworkUrl100", "")
        art = re.sub(r"/[0-9]+x[0-9]+bb\.(jpg|png)$", "/600x600bb.jpg", art)
        return "itunes", result.get("collectionName", ""), art
    return None


def resolve_musicbrainz(artist: str, album: str) -> tuple[str, str, str] | None:
    alias = ALIASES.get((artist, album), album)
    query = f'artist:"{artist}" AND release:"{alias}"'
    url = "https://musicbrainz.org/ws/2/release/?" + urllib.parse.urlencode(
        {"query": query, "fmt": "json", "limit": 8}
    )
    data = open_json(url)
    for release in data.get("releases", []):
        try:
            score = int(release.get("score", "0"))
        except ValueError:
            score = 0
        if score >= 90:
            return (
                "coverartarchive",
                release.get("title", ""),
                f"https://coverartarchive.org/release/{release['id']}/front-500",
            )
    return None


def resolve_cover(artist: str, album: str) -> tuple[str, str, str] | None:
    if (artist, album) in MANUAL_URLS:
        return MANUAL_URLS[(artist, album)]
    for resolver in (resolve_itunes, resolve_musicbrainz):
        try:
            result = resolver(artist, album)
            if result:
                return result
        except Exception as exc:
            print(f"COVER_LOOKUP_WARNING {artist} | {album} | {exc}")
    return None


def download_bytes(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=35) as response:
        return response.read()


def command_fetch_missing_art(args: argparse.Namespace) -> None:
    root = args.root.resolve()
    updated = 0
    failed = []
    with tempfile.TemporaryDirectory(prefix="snowsky_fetch_art_") as td_raw:
        tmpdir = Path(td_raw)
        for (artist, album), infos in sorted(album_groups(root).items()):
            if not any(not i.has_art for i in infos):
                continue
            print(f"FETCH {artist} | {album} | {len(infos)} tracks")
            result = resolve_cover(artist, album)
            if not result:
                print("  NO_COVER_FOUND")
                failed.append((artist, album))
                continue
            source, found_title, url = result
            cover = normalize_image(download_bytes(url), tmpdir)
            for info in infos:
                embed_cover(info.path, cover)
                updated += 1
            print(f"  OK {source} | {found_title} | {url}")
            time.sleep(args.sleep)
    print(f"FETCHED_ART_FILES {updated}")
    print(f"FETCHED_ART_FAILED_ALBUMS {len(failed)}")
    for artist, album in failed:
        print(f"FAILED {artist} | {album}")


def lrclib_normalize(value: str) -> str:
    value = value.lower().replace("&", "and")
    value = re.sub(r"\([^)]*\)|\[[^]]*\]", " ", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())


def lrclib_score(result: dict, artist: str, album: str, title: str) -> float:
    import difflib

    target_artist = lrclib_normalize(artist)
    target_album = lrclib_normalize(album)
    target_title = lrclib_normalize(title)
    result_artist = lrclib_normalize(str(result.get("artistName", "")))
    result_album = lrclib_normalize(str(result.get("albumName", "")))
    result_title = lrclib_normalize(str(result.get("trackName") or result.get("name") or ""))
    if not target_title or not result_title:
        return 0.0
    title_score = difflib.SequenceMatcher(None, target_title, result_title).ratio()
    artist_score = difflib.SequenceMatcher(None, target_artist, result_artist).ratio()
    album_score = difflib.SequenceMatcher(None, target_album, result_album).ratio()
    if target_title == result_title:
        title_score += 0.25
    if target_artist == result_artist:
        artist_score += 0.15
    if target_album and target_album == result_album:
        album_score += 0.15
    return (title_score * 0.55) + (artist_score * 0.30) + (album_score * 0.15)


def resolve_lrclib_lrc(artist: str, album: str, title: str) -> tuple[dict, str] | None:
    params = {
        "artist_name": artist,
        "track_name": title,
        "album_name": album,
    }
    url = "https://lrclib.net/api/search?" + urllib.parse.urlencode(params)
    try:
        data = open_json(url)
    except Exception as exc:
        print(f"LRC_LOOKUP_WARNING {artist} | {album} | {title} | {exc}")
        return None
    if not isinstance(data, list):
        return None
    candidates = [
        (lrclib_score(result, artist, album, title), result)
        for result in data
        if result.get("syncedLyrics")
    ]
    if not candidates:
        return None
    score, result = max(candidates, key=lambda item: item[0])
    if score < 0.72:
        return None
    return result, str(result["syncedLyrics"]).rstrip() + "\n"


def command_fetch_lrclib_lrc(args: argparse.Namespace) -> None:
    root = args.root.resolve()
    targets = [info for info in all_audio(root) if lrc_needs_fetch(info)]
    if args.limit is not None:
        targets = targets[: args.limit]
    fetched = 0
    failed = []
    skipped = 0

    def process(info: AudioInfo) -> tuple[str, Path, str]:
        artist, album = infer_artist_album(info, root)
        title = info.title.strip() or title_from_filename(info.path)
        lrc_path = info.path.with_suffix(".lrc")
        result = resolve_lrclib_lrc(artist, album, title)
        if not result:
            return "failed", info.path, "NO_SYNCED_LRC_FOUND"
        metadata, synced_lrc = result
        if args.dry_run:
            return (
                "dry_run",
                info.path,
                f"id={metadata.get('id')} artist={metadata.get('artistName')} "
                f"album={metadata.get('albumName')} track={metadata.get('trackName')}",
            )
        lrc_path.write_text(synced_lrc, encoding="utf-8")
        return (
            "fetched",
            info.path,
            f"id={metadata.get('id')} artist={metadata.get('artistName')} "
            f"album={metadata.get('albumName')} track={metadata.get('trackName')}",
        )

    if args.jobs <= 1:
        results = []
        for info in targets:
            results.append(process(info))
            time.sleep(args.sleep)
    else:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        results = []
        with ThreadPoolExecutor(max_workers=args.jobs) as executor:
            futures = [executor.submit(process, info) for info in targets]
            for future in as_completed(futures):
                results.append(future.result())

    for status, path, message in results:
        if not args.quiet or status == "failed":
            print(f"LRC_FETCH {path.relative_to(root)}")
        if status == "failed":
            print(f"  {message}")
            failed.append(path)
        elif status == "dry_run":
            if not args.quiet:
                print(f"  WOULD_WRITE {message}")
            skipped += 1
        else:
            if not args.quiet:
                print(f"  OK {message}")
            fetched += 1
    print(f"LRC_FETCHED {fetched}")
    print(f"LRC_DRY_RUN_MATCHES {skipped}")
    print(f"LRC_FAILED {len(failed)}")
    for path in failed[:100]:
        print(f"LRC_FAILED_FILE {path.relative_to(root)}")
    if len(failed) > 100:
        print(f"LRC_FAILED_FILE ... {len(failed) - 100} more")


def command_verify(args: argparse.Namespace) -> None:
    counts = print_audit(args.root.resolve())
    failures = (
        counts["missing_art"]
        + counts["bad_art"]
        + counts["bad_depth"]
        + counts["missing_lrc"]
        + counts["blank_lrc"]
    )
    if failures:
        fail(f"verification failed with {failures} readiness issue(s)")
    print("VERIFY_OK")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Prepare music folders for Snowsky.")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("audit")
    p.add_argument("root", type=Path)
    p.set_defaults(func=lambda args: print_audit(args.root.resolve()))

    p = sub.add_parser("verify")
    p.add_argument("root", type=Path)
    p.set_defaults(func=command_verify)

    p = sub.add_parser("extract-archives")
    p.add_argument("root", type=Path)
    p.add_argument("--delete-originals", action="store_true")
    p.set_defaults(func=command_extract_archives)

    p = sub.add_parser("organize")
    p.add_argument("root", type=Path)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=command_organize)

    p = sub.add_parser("normalize-existing-art")
    p.add_argument("root", type=Path)
    p.set_defaults(func=command_normalize_existing_art)

    p = sub.add_parser("fetch-missing-art")
    p.add_argument("root", type=Path)
    p.add_argument("--sleep", type=float, default=0.35)
    p.set_defaults(func=command_fetch_missing_art)

    p = sub.add_parser("fetch-lrc-lrclib")
    p.add_argument("root", type=Path)
    p.add_argument("--sleep", type=float, default=0.35)
    p.add_argument("--limit", type=int)
    p.add_argument("--jobs", type=int, default=1)
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=command_fetch_lrclib_lrc)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

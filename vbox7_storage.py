"""Filesystem transactions and media validation for Vbox7 archives."""

from contextlib import contextmanager
import errno
import json
import os
from pathlib import Path
import re
import tempfile


VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi", ".flv", ".wmv", ".mpeg", ".mpg", ".3gp"}
MP4_EXTENSIONS = {".mp4", ".m4v", ".mov", ".3gp"}


class ArchiveError(Exception):
    """An error whose message can be displayed without exposing session data."""


@contextmanager
def staged_file(destination):
    descriptor, filename = tempfile.mkstemp(
        dir=destination.parent, prefix=f".{destination.name}.", suffix=".part"
    )
    os.close(descriptor)
    path = Path(filename)
    try:
        yield path
    finally:
        path.unlink(missing_ok=True)


def video_header_valid(header, suffix):
    if suffix in MP4_EXTENSIONS:
        box = header[4:8]
        return len(header) >= 8 and (box == b"ftyp" or (
            suffix == ".mov" and box in {b"moov", b"mdat", b"wide", b"free"}
        ))
    if suffix in {".webm", ".mkv"}:
        return header.startswith(b"\x1a\x45\xdf\xa3")
    if suffix == ".avi":
        return header.startswith(b"RIFF") and header[8:12] == b"AVI "
    if suffix == ".flv":
        return header.startswith(b"FLV\x01")
    if suffix == ".wmv":
        return header.startswith(bytes.fromhex("3026b2758e66cf11a6d900aa0062ce6c"))
    return suffix in {".mpeg", ".mpg"} and header.startswith(b"\x00\x00\x01\xba")


def valid_video(path, suffix=None):
    if not path.is_file():
        return False
    suffix = suffix or path.suffix.lower()
    with path.open("rb") as video:
        if not video_header_valid(video.read(16), suffix):
            return False
        return suffix not in MP4_EXTENSIONS or has_mp4_media_data(video)


def has_mp4_media_data(video):
    size = os.fstat(video.fileno()).st_size
    offset = 0
    has_media = False
    # Check top-level box sizes without loading the video payload.
    while offset + 8 <= size:
        video.seek(offset)
        header = video.read(8)
        if len(header) != 8:
            return False
        box_size = int.from_bytes(header[:4], "big")
        header_size = 8
        if box_size == 1:
            extra = video.read(8)
            if len(extra) != 8:
                return False
            box_size = int.from_bytes(extra, "big")
            header_size = 16
        elif box_size == 0:
            box_size = size - offset
        if box_size < header_size or offset + box_size > size:
            return False
        if header[4:] == b"mdat" and box_size > header_size:
            has_media = True
        offset += box_size
    return has_media and offset == size


def existing_video(record, video_dir):
    video_id = record["id"]
    filenames = [f"{video_id}.mp4"]
    filenames.extend(f"{video_id}{suffix}" for suffix in sorted(VIDEO_EXTENSIONS - {".mp4"}))
    saved = record.get("video_file")
    saved = re.split(r"[/\\]", saved)[-1] if isinstance(saved, str) else None
    if saved in filenames:
        filenames.remove(saved)
        filenames.insert(0, saved)
    placeholder = f"{video_id}.placeholder.mp4"
    if saved == placeholder and record.get("video_is_placeholder") is True:
        filenames.append(placeholder)
    return next((video_dir / name for name in filenames if valid_video(video_dir / name)), None)


def is_placeholder(path):
    return path is not None and path.name.endswith(".placeholder.mp4")


def valid_static(path):
    if path.is_file():
        with path.open("rb") as gif:
            return gif.read(6) in {b"GIF87a", b"GIF89a"}
    return False


def save_text(path, write):
    with staged_file(path) as temporary:
        with temporary.open("w", encoding="utf-8") as output:
            write(output)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(path)


def save_json(path, data):
    save_text(path, lambda output: json.dump(data, output, ensure_ascii=False, indent=2))


def save_viewer_data(path, catalog):
    # A classic script can load from disk in Chrome when fetch() of local JSON cannot.
    payload = json.dumps(catalog, ensure_ascii=False, separators=(",", ":"))
    literal = json.dumps(payload, ensure_ascii=False).replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    save_text(path, lambda output: output.write(f"globalThis.vbox7ViewerCatalog = JSON.parse({literal});"))


@contextmanager
def archive_lock(directory):
    directory.mkdir(parents=True, exist_ok=True)
    # Keep the lock file in place so every process locks the same file.
    with (directory / ".archive.lock").open("a+b") as lock:
        try:
            if os.name == "nt":
                import msvcrt

                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise ArchiveError("Another archiver is already writing to this output folder.") from None
            raise
        yield


def load_catalog(index, username):
    if not index.exists():
        return {"username": username, "uploads": {}}
    try:
        catalog = json.loads(index.read_text(encoding="utf-8"))
    except ValueError:
        raise ArchiveError("The archive catalog is not valid JSON. Restore the saved catalog before continuing.") from None
    if not isinstance(catalog, dict) or not isinstance(catalog.get("username"), str):
        raise ArchiveError("The archive catalog has no valid account owner.")
    if catalog["username"].casefold() != username.casefold():
        raise ArchiveError("This output folder belongs to another Vbox7 account. Choose a different --output.")
    uploads = catalog.get("uploads")
    if not isinstance(uploads, dict):
        raise ArchiveError("The archive catalog has no valid upload index.")
    for entry in uploads.values():
        if not isinstance(entry, dict) or (entry.get("comments") is not None and not isinstance(entry["comments"], list)):
            raise ArchiveError("The archive catalog contains an invalid upload entry.")
    return catalog

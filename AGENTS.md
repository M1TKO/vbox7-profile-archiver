# Agent instructions

## Project workflow

- `vbox7_archive.py` is the Python CLI entry point. It uses Playwright to open local Google Chrome for manual sign-in, then verifies the signed-in owner before scanning. `vbox7_storage.py` manages filesystem writes and validation.
- Follow the setup in `README.md` and install dependencies from `requirements.txt`. Maintain compatibility with Python 3.9 or newer and the documented Windows, macOS, and Linux commands.
- Preserve private, friends-only, then public discovery order and keep the first visibility for duplicate IDs. `--limit` applies to processing after discovery. Preserve already recognized uploads when a listing scan is incomplete.
- Keep `--dry-run` read-only. It uses the normal sign-in, discovery, selection, and saved-video checks to count planned downloads. Report incomplete listing scans with a nonzero exit status.
- Preserve resume behavior when changing archive storage. Reuse validated final video files in `videos/`, check account ownership of the catalog, and atomically update `misc/metadata.json` as the run progresses. Hold the output lock throughout a write run. Keep existing archives readable when changing the catalog format.
- Keep per-video download progress based on bytes received. Show a percentage when the server provides the total size, and report completion only after the video is saved to its final path.
- Save Vbox7's placeholder MP4 and static GIF only when the signed-in player explicitly returns `src: "blank"`. Preserve the upload's metadata, mark placeholder videos with `video_is_placeholder`, and count them separately from original videos.
- `--retry-placeholders` checks for original videos and keeps existing placeholders until a validated original is saved. Dry run counts the same planned attempts. Preserve previously saved comments when a refresh is incomplete.
- Keep generated archive data, private comments, and session data out of commits and test fixtures. Use synthetic fixtures for offline checks.

## Verification

- Use the project virtual environment to run `python -m py_compile vbox7_archive.py vbox7_storage.py` for a syntax check, `python vbox7_archive.py --help` for a CLI smoke check, and `python -m unittest discover -s tests -v` for offline regression checks.
- For a live smoke check, follow the one-upload example in `README.md` with an authorized account and a separate `--output` directory.

## Keep these instructions current

- Review `AGENTS.md` whenever you change the code. If the change affects something future agents need to know, update this file in the same change. Examples include changes to project structure, supported environments, dependencies, commands, verification steps, and archive formats or behavior that future changes must preserve.
- Update `README.md` when setup instructions, CLI usage, or documented archive behavior changes.

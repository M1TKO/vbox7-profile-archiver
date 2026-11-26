# Vbox7 Profile Archiver

Back up your own Vbox7 uploads to your computer. The archiver scans your private, friends-only, and public upload lists in that order and saves downloadable videos alongside a JSON catalog of their details and comments.

## Requirements

- Python 3.9 or newer.
- Google Chrome installed locally using the standard installer or Linux package. [Google Chrome installation instructions](https://support.google.com/chrome/answer/95346)
- A desktop session that can open Chrome for manual sign-in. Check that your Windows, macOS, or Linux version is supported. [Playwright system requirements](https://playwright.dev/python/docs/intro#system-requirements)
- Access to the Vbox7 account you want to archive.

## Setup

Clone or download this repository and open a terminal in the folder containing `vbox7_archive.py`.

**Windows (PowerShell)**

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --only-binary=:all: 'playwright<2' 'requests<3' 'beautifulsoup4<5'
```

**Linux and macOS**

```sh
python3 -m venv .venv
.venv/bin/python -m pip install --only-binary=:all: 'playwright<2' 'requests<3' 'beautifulsoup4<5'
```

On Debian or Ubuntu, install the `python3-venv` package if creating the virtual environment fails because `ensurepip` is unavailable.

## Run an archive

Replace `YOUR_USERNAME` with your Vbox7 username. Start with one upload to check that sign-in and downloads work.

**Windows (PowerShell)**

```powershell
.\.venv\Scripts\python.exe vbox7_archive.py YOUR_USERNAME --limit 1
```

**Linux and macOS**

```sh
.venv/bin/python vbox7_archive.py YOUR_USERNAME --limit 1
```

Sign in to that account on the Vbox7 page in the Chrome window that opens. Handle any notice shown by the site and keep the window open until the script finishes.

Omit `--limit` to process all discovered uploads.

During each direct download, the script reports how much data it has received. When the server provides the file size, it also shows the total size and percentage. If the script uses Chrome's download action, it shows when Chrome starts and how much data was saved at completion.

### Preview downloads

Add `--dry-run` to preview an archive after signing in. For example, with the project virtual environment active:

```sh
python vbox7_archive.py YOUR_USERNAME --dry-run --limit 10
```

The preview reports the total number of distinct uploads found, how many a normal run would process with the given limit, and how many of those would need a new download. It checks for reusable videos in the selected `--output` folder and leaves all archive files untouched. Download availability is checked during an actual archive. If a listing fails, the preview labels the counts as incomplete and exits with status 1.

### Command-line options

| Option | Behavior |
| --- | --- |
| `--limit N` | Scan all three upload lists and process up to `N` uploads, starting with private uploads. |
| `--dry-run` | Preview upload and planned download counts without writing archive files. |
| `--browser chrome` | Optional for compatibility with existing commands. The script always uses Google Chrome. |
| `--output PATH` | Save to a custom folder. The default is the folder containing the script. Use a separate output folder for each account. |
| `--help` | Show usage and options. |

## Archive files

| Path | Contents |
| --- | --- |
| `videos/` | Downloaded videos named by their Vbox7 video IDs, usually as MP4 files. |
| `misc/metadata.json` | A catalog keyed by video ID. Entries can include the title, uploader, upload date, visibility, page URL, view and comment counts, comment text, and relative path to the saved video. |

When the signed-in player reports a blank source, the archiver saves Vbox7's blank video as `videos/<id>.placeholder.mp4` and saves its animated static overlay once as `videos/vbox7-static.gif`. The catalog keeps the original upload's available metadata and comments, marks the video with `video_is_placeholder`, and links the overlay in `placeholder_static_file`. The run summary counts placeholders separately.

The archive can contain private videos and comments. Keep generated files out of public commits and releases.

## Check or resume an archive

After a run, check the `Videos saved` and `Entries to review` totals. In `misc/metadata.json`, `scan_complete` reports whether the upload lists were scanned successfully. Check `listing_errors` for scan failures and each upload's `errors` for problems retrieving the video, metadata, or comments.

To continue after an interruption or retry a failed download, run the script again with the same username and output folder, then sign in again. The script reuses existing video files and updates the catalog as it goes.

## License

[MIT License](LICENSE)

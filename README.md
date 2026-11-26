# Vbox7 Profile Archiver

Back up your own Vbox7 uploads to your computer. The archiver scans your private, friends-only, and public upload lists in that order and saves downloadable videos alongside a JSON catalog of their details and comments.

## Requirements

- Python 3.9 was used to test the script. The commands below are for Windows PowerShell.
- Microsoft Edge or Google Chrome installed locally.
- Access to the Vbox7 account you want to archive.

## Setup

Clone or download this repository, then open PowerShell in the folder containing `vbox7_archive.py`. Create and activate a virtual environment, then install the dependencies.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --only-binary=:all: 'playwright<2' 'requests<3' 'beautifulsoup4<5'
```

Activate the environment again when you open a new PowerShell session.

## Run an archive

Replace `YOUR_USERNAME` with your Vbox7 username. Start with one upload to check that sign-in and downloads work.

```powershell
python vbox7_archive.py YOUR_USERNAME --limit 1
```

Sign in to that account on the Vbox7 page in the browser window that opens. Handle any notice shown by the site and keep the window open until the script finishes.

Omit `--limit` to process all discovered uploads.

```powershell
python vbox7_archive.py YOUR_USERNAME
```

### Command-line options

| Option | Behavior |
| --- | --- |
| `--limit N` | Scan all three upload lists and process up to `N` uploads, starting with private uploads. |
| `--browser {msedge,chrome}` | Choose the installed browser for sign-in. The default is `msedge`. |
| `--output PATH` | Save to a custom folder. The default is the folder containing the script. Use a separate output folder for each account. |
| `--help` | Show usage and options. |

## Archive files

| Path | Contents |
| --- | --- |
| `videos/` | Downloaded videos named by their Vbox7 video IDs, usually as MP4 files. |
| `misc/metadata.json` | A catalog keyed by video ID. Entries can include the title, uploader, upload date, visibility, page URL, view and comment counts, comment text, and relative path to the saved video. |

The archive can contain private videos and comments. Keep generated files out of public commits and releases.

## Check or resume an archive

After a run, check the `Videos saved` and `Entries to review` totals. In `misc/metadata.json`, `scan_complete` reports whether the upload lists were scanned successfully. Check `listing_errors` for scan failures and each upload's `errors` for problems retrieving the video, metadata, or comments.

To continue after an interruption or retry a failed download, run the script again with the same username and output folder, then sign in again. The script reuses existing video files and updates the catalog as it goes.

## License

[MIT License](LICENSE)

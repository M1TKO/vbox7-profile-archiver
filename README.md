# Vbox7 Profile Archiver

Back up your own Vbox7 uploads to your computer. The archiver scans your private, friends-only, and public upload lists in that order and saves downloadable videos alongside a JSON catalog of their details and comments.

## Requirements

- Python 3.9 or newer. Python 3.9 is kept for compatibility. Use a maintained Python release for a new installation.
- Google Chrome installed locally using the standard installer or Linux package. [Google Chrome installation instructions](https://support.google.com/chrome/answer/95346)
- A desktop session that can open Chrome for manual sign-in. Check that your Windows, macOS, or Linux version is supported. [Playwright system requirements](https://playwright.dev/python/docs/intro#system-requirements)
- Access to the Vbox7 account you want to archive.

## Setup

Clone or download this repository and open a terminal in the folder containing `vbox7_archive.py`.

**Windows (PowerShell)**

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --only-binary=:all: -r requirements.txt
```

**Linux and macOS**

```sh
python3 -m venv .venv
.venv/bin/python -m pip install --only-binary=:all: -r requirements.txt
```

On Debian or Ubuntu, install the `python3-venv` package if creating the virtual environment fails because `ensurepip` is unavailable.

## Run an archive

Replace `YOUR_USERNAME` with your Vbox7 username. Start with one upload to check that sign-in and downloads work.

**Windows (PowerShell)**

```powershell
.\.venv\Scripts\python.exe vbox7_archive.py YOUR_USERNAME --limit 1 --output "$env:USERPROFILE\vbox7-smoke"
```

**Linux and macOS**

```sh
.venv/bin/python vbox7_archive.py YOUR_USERNAME --limit 1 --output "$HOME/vbox7-smoke"
```

Sign in to that account on the Vbox7 page in the Chrome window that opens. Handle any notice shown by the site and keep the window open until the script finishes.

Omit `--limit` to process all discovered uploads. Keep the same `--output` to reuse your smoke-check download, or choose a separate folder for the full archive. Store custom archives outside the repository.

During each direct download, the script reports how much data it has received. When the server provides the file size, it also shows the total size and percentage. If the script uses Chrome's download action, it shows when Chrome starts and how much data was saved at completion.

### Preview downloads

Add `--dry-run` to preview an archive after signing in. For example, with the project virtual environment active:

```sh
python vbox7_archive.py YOUR_USERNAME --dry-run --limit 10
```

The preview reports the number of distinct uploads found, how many a normal run would process with the given limit, and how many would need a download attempt. It checks for reusable videos in the selected `--output` folder and leaves all archive files untouched. With `--retry-placeholders`, saved placeholders count as planned attempts. Source availability is checked during an actual archive. If a listing fails, the preview labels the counts as incomplete and exits with status 1.

### Command-line options

| Option | Behavior |
| --- | --- |
| `--limit N` | Scan all three upload lists and process up to `N` uploads, starting with private uploads. |
| `--dry-run` | Preview upload and planned download counts without writing archive files. |
| `--refresh-viewer` | Regenerate offline viewer data from an existing archive without signing in. |
| `--retry-placeholders` | Check whether an original video is available for each selected upload with a saved placeholder. |
| `--browser chrome` | Optional for compatibility with existing commands. The script always uses Google Chrome. |
| `--output PATH` | Save to a custom folder. The default is the folder containing the script. Use a separate output folder for each account. |
| `--help` | Show usage and options. |

## Archive files

| Path | Contents |
| --- | --- |
| `videos/` | Downloaded videos named by their Vbox7 video IDs, usually as MP4 files. |
| `misc/metadata.json` | A catalog keyed by video ID. The `author` field stores the uploader shown on the video page when available. Archive runs fill missing authors with the archive account username, including in older entries. Entries can also include the title, upload date, visibility, page URL, view and comment counts, comment text, and relative path to the saved video. |
| `misc/viewer-data.js` | A local copy of the catalog for automatic loading in the offline viewer. Like the JSON catalog, it can contain private videos' details and comments. |

When the signed-in player reports a blank source, the archiver saves Vbox7's blank video as `videos/<id>.placeholder.mp4` and saves its animated static overlay once as `videos/vbox7-static.gif`. The catalog keeps the original upload's available metadata and comments, marks the video with `video_is_placeholder`, and links the overlay in `placeholder_static_file`. The run summary counts placeholders separately.

Saved placeholders can be reused on later runs. Add `--retry-placeholders` to check whether the original has become available. The catalog keeps pointing to the saved placeholder until a validated original is saved.

The archive can contain private videos and comments. Git ignores generated contents in the repository's default `videos/` and `misc/` directories. Keep custom archives outside the checkout and generated files out of public releases.

## Browse an archive offline

Open [archive-viewer.html](archive-viewer.html) from your local checkout in Chrome. It automatically opens the archive beside the page when `misc/viewer-data.js` is present. Reload the page after an archive run to see the updated catalog.

Click **Open archive folder** to browse a custom `--output` folder or an older archive. You can also copy the HTML file into a custom output folder to give it the same automatic loading behavior. Select an upload to open its local video, details, and saved comments. The viewer marks placeholders and keeps details available when a video file is missing.

Use **Open on Vbox7** in the upload list or video details to open the original Vbox7 page in a new tab.

The upload list shows still-frame previews from supported local videos as you scroll. Click a column heading to sort by title, upload date, views, or comments. Click the same heading again to reverse the sort, and a third time to restore archive order. The **Sort by** menu also offers available-first and unavailable-first order.

Select **Hide unavailable videos** to show only uploads with a local original. This hides placeholders and uploads without a video file. Automatic loading uses the catalog's saved video paths. Use **Open archive folder** to check availability against the files currently in the folder.

Normal archive runs generate the viewer data at the end. To enable automatic loading for an existing archive or refresh the viewer after an interrupted run, use the following command from the project virtual environment. Add `--output PATH` if needed.

```sh
python vbox7_archive.py YOUR_USERNAME --refresh-viewer
```

The viewer runs entirely in your browser and reads local files. Browsing the archive works offline without a server or a Vbox7 sign-in.

## Check or resume an archive

After a run, check the `Videos saved` and `Entries to review` totals. In `misc/metadata.json`, `scan_complete` reports whether the upload lists were scanned successfully. Check `listing_errors` for scan failures and each upload's `errors` for problems retrieving the video, metadata, or comments.

The scan keeps the first visibility for uploads that appear in more than one list. If Vbox7 redirects a scan to a different list or returns an unrecognized layout, the run reports incomplete discovery and exits with status 1. A partial comment refresh records an error and preserves previously saved comments.

To continue after an interruption or retry a failed download, run the script again with the same username and output folder, then sign in again. The script checks video headers before reusing completed files. For MP4 files, it checks that the top-level boxes are complete and that at least one contains media data. It updates the catalog as entries change. Only one archive run can write to an output folder at a time. The file `misc/.archive.lock` stays on disk so runs can acquire the same OS lock.

## Offline checks

From the project virtual environment, run `python -m unittest discover -s tests -v`. The suite uses synthetic data and runs without a browser or account. GitHub Actions runs it on Windows, macOS, and Linux.

## License

[MIT License](LICENSE)

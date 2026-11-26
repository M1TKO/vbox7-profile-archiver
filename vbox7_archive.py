"""Archive uploads from your own Vbox7 account using a browser sign-in.

Run with --help for options. Requires Playwright, Beautiful Soup and Requests.
"""

import argparse
from contextlib import closing
from datetime import datetime
import json
from pathlib import Path
import re
import sys
from time import monotonic, sleep
from urllib.parse import parse_qs, quote, unquote, urlencode, urljoin, urlsplit

from bs4 import BeautifulSoup
from playwright.sync_api import Error as BrowserError, sync_playwright
import requests

from vbox7_storage import (
    ArchiveError, VIDEO_EXTENSIONS, archive_lock, existing_video, is_placeholder,
    load_catalog, save_json, save_viewer_data, staged_file, valid_static, valid_video, video_header_valid,
)


BASE = "https://www.vbox7.com"
PLACEHOLDER_VIDEO_URL = f"{BASE}/assets/blank.mp4"
PLACEHOLDER_STATIC_URL = ("https://i49.vbox7.com/assets/css/vbox/vboxplayer/"
                          "themes/slickblack/static.gif")
FILTERS = ("private", "friends", "public")
VIDEO_ID = re.compile(r"/play:([0-9a-fA-F]+)")
DOWNLOAD_LABEL = re.compile(r"^\s*(?:изтегли|свали|download)\b", re.IGNORECASE)
EMPTY_LIST = re.compile(
    r"(?:no (?:uploads|videos)(?: found)?|няма (?:качени )?(?:клипове|видеа)|"
    r"няма намерени (?:клипове|видеа))[.!]?", re.IGNORECASE
)
RETRYABLE_HTTP = {500, 502, 503, 504}
DOWNLOAD_DEADLINE = 30 * 60


class IncompleteListing(ArchiveError):
    def __init__(self, message, uploads):
        super().__init__(message)
        self.uploads = uploads


def profile_url(username, visibility):
    return f"{BASE}/user:{quote(username, safe='')}?p=items&filter={visibility}&order=date"


def text_at(node, selector):
    element = node.select_one(selector)
    return element.get_text(" ", strip=True) if element else None


def number(value):
    compact = re.sub(r"[\s,]", "", str(value)) if isinstance(value, (str, int)) else ""
    return int(compact) if len(compact) <= 20 and compact.isdecimal() else None


def upload_date(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.strptime(value, "%d.%m.%Y").date().isoformat()
    except ValueError:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()
        except ValueError:
            return None


def parse_listing(html, *, page=1):
    soup = BeautifulSoup(html, "html.parser")
    feed = soup.select_one(".major-col section.feed")
    if not feed:
        raise ArchiveError("Vbox7 did not return an uploads list.")
    uploads = []
    unrecognized_card = False
    for card in feed.select(".video-cell"):
        for link in card.select("a.thumb-cont[href], h3 a[href]"):
            try:
                match = VIDEO_ID.fullmatch(unquote(urlsplit(link["href"]).path))
            except ValueError:
                continue
            if match:
                break
        else:
            unrecognized_card = True
            continue
        video_id = match[1].lower()
        uploads.append({
            "id": video_id,
            "url": f"{BASE}/play:{video_id}",
            "title": text_at(card, "h3") or link.get("title"),
            "upload_date": upload_date(text_at(card, "svg.time + span")),
            "view_count": number(text_at(card, "svg.views + span")),
        })
    if unrecognized_card:
        raise IncompleteListing("An upload card had no recognizable video link.", uploads)
    empty_message = feed.select_one(".row-mb") or feed
    if not uploads and not EMPTY_LIST.fullmatch(empty_message.get_text(" ", strip=True)):
        raise ArchiveError("The uploads list had no recognized videos or empty-state message.")
    has_next = bool(feed.select_one('.pagination a[rel="next"][href]'))
    if not has_next:
        for link in feed.select(".pagination a[href]"):
            try:
                pages = parse_qs(urlsplit(link["href"]).query).get("page", [])
            except ValueError:
                raise IncompleteListing("Vbox7 returned an invalid pagination link.", uploads) from None
            if any((number(value) or 0) > page for value in pages):
                raise IncompleteListing("Vbox7 advertised more uploads without a recognized next-page control.", uploads)
    return uploads, has_next


def parse_video(html):
    soup = BeautifulSoup(html, "html.parser")
    info = {}
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string or "")
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("@type") == "VideoObject":
            info = data
            break
    counts = info.get("interactionStatistic", [])
    if isinstance(counts, dict):
        counts = [counts]
    elif not isinstance(counts, list):
        counts = []
    statistic = {}
    for entry in counts or []:
        if isinstance(entry, dict):
            action = entry.get("interactionType", "")
            if isinstance(action, dict):
                action = action.get("@type", "")
            if isinstance(action, str):
                statistic[action.rsplit("/", 1)[-1]] = number(entry.get("userInteractionCount"))
    views = statistic.get("WatchAction")
    count = statistic.get("CommentAction")
    title = info.get("name")
    return {
        "title": (title if isinstance(title, str) else None) or text_at(soup, ".video-info h1"),
        "author": text_at(soup, ".video-info .channel-name") or None,
        "upload_date": upload_date(info.get("uploadDate") or text_at(soup, ".video-stat svg.time + span")),
        "view_count": views if views is not None else number(text_at(soup, ".video-stat svg.views + span")),
        "comment_count": count if count is not None else number(text_at(soup, ".comments-btn .num")),
    }


def parse_comments(html):
    comments = []
    for item in BeautifulSoup(html, "html.parser").select(".item-comment"):
        parent = item.find_parent(class_="item-comment")
        wrapper = item.select_one(":scope > .parent-comment") or item
        body = next((node for node in wrapper.select(".comment-text")
                     if node.find_parent(class_="item-comment") is item), None)
        if body is None:
            raise ArchiveError("A comment had no recognizable body.")
        comments.append({
            "id": re.sub(r"^comment[-_]", "", item.get("id", "")) or None,
            "parent_id": re.sub(r"^comment[-_]", "", parent.get("id", "")) or None if parent else None,
            "author": text_at(wrapper, ".comment-author"),
            "date_display": text_at(wrapper, ".comment-date"),
            "text": body.get_text("\n", strip=True),
        })
    return comments


def media_candidates(options):
    source = options.get("src")
    if not isinstance(source, str) or not source or source == "blank":
        return []
    try:
        parts = urlsplit(urljoin(BASE, source))
    except ValueError:
        raise ArchiveError("Vbox7 returned an invalid video source URL.") from None
    host = parts.hostname or ""
    if parts.scheme != "https" or not (host == "vbox7.com" or host.endswith(".vbox7.com")):
        return []
    suffix = Path(parts.path).suffix.lower()
    if suffix == ".mp4":
        return [parts.geturl()]
    if suffix not in {".mpd", ".m3u8"}:
        return []
    stem = parts.path[:-len(suffix)]
    resolutions = options.get("resolutions")
    if resolutions is not None and not isinstance(resolutions, list):
        raise ArchiveError("Vbox7 returned an invalid list of video resolutions.")
    sizes = {number(value) for value in resolutions or []}
    return [
        parts._replace(path=f"{stem}_{height}.mp4").geturl()
        for height in sorted(filter(None, sizes), reverse=True)
    ]


def fetch(api, url, *, params=None, referer=None, as_json=False, include_url=False):
    headers = {"Referer": referer, "X-Requested-With": "XMLHttpRequest"} if referer else {}
    for attempt in range(2):
        try:
            response = api.get(url, params=params, headers=headers, timeout=30_000)
        except BrowserError:
            if attempt:
                raise ArchiveError("A Vbox7 request timed out or the connection failed.") from None
        else:
            if response.status not in RETRYABLE_HTTP or attempt:
                break
            response.dispose()
        sleep(1)
    try:
        if not response.ok:
            raise ArchiveError(f"Vbox7 returned HTTP {response.status}.")
        if as_json:
            try:
                data = response.json()
            except ValueError:
                raise ArchiveError("Vbox7 returned an unexpected response.") from None
            if not isinstance(data, dict) or data.get("success") is not True:
                raise ArchiveError("Vbox7 says this item is unavailable to the current session.")
            return data
        return (response.body(), response.url) if include_url else response.body()
    finally:
        response.dispose()


def matches_page(final, expected):
    return (final.scheme == expected.scheme and final.netloc == expected.netloc
            and unquote(final.path).casefold() == unquote(expected.path).casefold())


def discover(api, username, visibility):
    url = profile_url(username, visibility)
    expected = urlsplit(url)
    seen = set()
    page = 1
    while True:
        page_url = url if page == 1 else f"{url}&page={page}"
        html, final_url = fetch(api, page_url, include_url=True)
        final = urlsplit(final_url)
        if not matches_page(final, expected):
            raise ArchiveError("Vbox7 redirected the upload request away from the account's list.")
        query = parse_qs(final.query, keep_blank_values=True)
        final_filter = query.get("filter")
        if page == 1 and visibility == "public" and final_filter == ["private"]:
            raise ArchiveError("Vbox7 redirected the public list to private. The public list is unverified.")
        if final_filter != [visibility] or query.get("p") != ["items"]:
            raise ArchiveError("Vbox7 returned a different visibility list.")
        final_page = query.get("page", ["1"])
        if final_page != [str(page)]:
            raise ArchiveError("Vbox7 returned a different upload page during pagination.")
        issue = None
        try:
            uploads, has_next = parse_listing(html, page=page)
        except IncompleteListing as error:
            uploads, has_next, issue = error.uploads, False, error
        new = []
        for upload in uploads:
            if upload["id"] not in seen:
                seen.add(upload["id"])
                new.append(upload)
        yield from new
        if issue:
            raise issue
        if not new and (page > 1 or has_next):
            raise ArchiveError("Vbox7 stopped returning new uploads during pagination.")
        if not has_next:
            return
        page += 1


def collect_comments(api, record):
    comments = []
    seen = set()
    page = 1
    total = None
    page_size = None
    while True:
        try:
            data = fetch(api, f"{BASE}/aj/comments", as_json=True, referer=record["url"],
                         params={"mdkey": record["id"], "page": page, "order": "date"})
            paging = data.get("pagination")
            if not isinstance(paging, dict):
                raise ArchiveError("Vbox7 returned unrecognized comment pagination.")
            reported = number(paging.get("total"))
            per_page = number(paging.get("perPage"))
            if reported is None or not per_page or not isinstance(data.get("html"), str):
                raise ArchiveError("Vbox7 returned an unrecognized comment list.")
            if total is not None and (reported != total or per_page != page_size):
                raise ArchiveError("Vbox7's comment pagination changed during collection.")
            total, page_size = reported, per_page
            batch = parse_comments(data["html"])
            previous_count = len(comments)
            for comment in batch:
                comment_id = comment["id"]
                if comment_id and comment_id in seen:
                    continue
                comments.append(comment)
                if comment_id:
                    seen.add(comment_id)
            if total and len(comments) == previous_count:
                raise ArchiveError("Vbox7 stopped returning new comments before the last page.")
            if page * per_page >= total:
                root_count = sum(comment["parent_id"] is None for comment in comments)
                if total != len(comments) and (not total or total != root_count):
                    raise ArchiveError("The parsed comments did not match Vbox7's reported total.")
                return comments, total, None
            page += 1
        except ArchiveError as error:
            return comments, total, str(error)


def show_download_progress(size, total=None, *, complete=False):
    status = "Download complete" if complete else "Downloading"
    scale = max(size, total or 0)
    if scale >= 1024 ** 2:
        divisor, unit = 1024 ** 2, "MiB"
    elif scale >= 1024:
        divisor, unit = 1024, "KiB"
    else:
        divisor, unit = 1, "B"
    if total:
        percentage = min(size * 100 // total, 100)
        progress = f"{size / divisor:.1f} / {total / divisor:.1f} {unit} ({percentage}%)"
    else:
        progress = f"{size / divisor:.1f} {unit}"
    print(f"  {status}: {progress}", flush=True)


def stream_mp4(http, url, referer, destination):
    deadline = monotonic() + DOWNLOAD_DEADLINE
    for attempt in range(2):
        if monotonic() >= deadline:
            raise ArchiveError("The direct video transfer exceeded its time limit.")
        try:
            with http.get(url, headers={"Referer": referer, "Accept-Encoding": "identity"},
                          stream=True, timeout=(15, 45)) as response:
                if response.status_code != 200:
                    status = response.status_code
                    if status not in RETRYABLE_HTTP or attempt:
                        raise ArchiveError(f"Direct video download returned HTTP {status}.")
                else:
                    save_mp4_response(response, destination, deadline)
                    return
        except (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError):
            if attempt or monotonic() >= deadline:
                raise ArchiveError("The direct video transfer timed out or the connection failed.") from None
        except requests.RequestException:
            raise ArchiveError("The direct video request failed.") from None
        sleep(1)


def save_mp4_response(response, destination, deadline):
    expected = number(response.headers.get("Content-Length"))
    size = 0
    last_update = monotonic() - 2
    prefix = b""
    with staged_file(destination) as partial:
        with partial.open("wb") as output:
            for chunk in response.iter_content(chunk_size=256 * 1024):
                now = monotonic()
                if now >= deadline:
                    raise ArchiveError("The direct video transfer exceeded its time limit.")
                if chunk:
                    if size == 0:
                        chunk = prefix + chunk
                        if len(chunk) < 8:
                            prefix = chunk
                            continue
                        if not video_header_valid(chunk, ".mp4"):
                            raise ArchiveError("The direct download did not contain a recognized MP4 header.")
                    output.write(chunk)
                    size += len(chunk)
                    if now - last_update >= 2:
                        show_download_progress(size, expected)
                        last_update = now
        if not size:
            raise ArchiveError("The direct download did not contain a recognized MP4 header.")
        if expected is not None and size != expected:
            raise ArchiveError("The direct video transfer had an unexpected file size.")
        if not valid_video(partial, ".mp4"):
            raise ArchiveError("The direct MP4 download did not contain readable media data.")
        partial.replace(destination)
    show_download_progress(size, expected, complete=True)


def browser_download(page, record, destination):
    """Try a download action in the site's video options when available."""
    try:
        response = page.goto(record["url"], wait_until="domcontentloaded", timeout=20_000)
        if not matches_page(urlsplit(page.url), urlsplit(record["url"])):
            raise ArchiveError("Chrome was redirected away from the requested video.")
        if response is not None and not response.ok:
            raise ArchiveError(f"Chrome's video page returned HTTP {response.status}.")
        menu = page.locator("#item-options")
        action = menu.get_by_text(DOWNLOAD_LABEL).first
        if not action.count():
            return None
        if not action.is_visible():
            page.locator('a.drop-trgr[data-id="item-options"]').first.click(timeout=3_000)
        with page.expect_download(timeout=8_000) as event:
            action.click(timeout=3_000)
        download = event.value
        suffix = Path(download.suggested_filename).suffix.lower()
        if suffix not in VIDEO_EXTENSIONS:
            download.cancel()
            raise ArchiveError("Chrome offered a file with an unsupported video extension.")
        print("  Downloading with Chrome...", flush=True)
        if download.failure():
            raise ArchiveError("Chrome could not complete the video transfer.")
        output = destination.with_suffix(suffix)
        with staged_file(output) as partial:
            download.save_as(partial)
            if not valid_video(partial, suffix):
                raise ArchiveError("Chrome's download did not contain a recognized video header.")
            size = partial.stat().st_size
            partial.replace(output)
        show_download_progress(size, complete=True)
        return output
    except BrowserError:
        raise ArchiveError("Chrome could not open the video page or use its download control.") from None


def save_placeholder_static(api, video_dir):
    # The player overlays this GIF on its blank MP4 to display the static effect.
    path = video_dir / "vbox7-static.gif"
    if valid_static(path):
        return path
    data = fetch(api, PLACEHOLDER_STATIC_URL)
    if not data.startswith((b"GIF87a", b"GIF89a")):
        raise ArchiveError("Vbox7 did not return the static placeholder GIF.")
    with staged_file(path) as partial:
        partial.write_bytes(data)
        partial.replace(path)
    return path


def needs_download(existing, retry_placeholders):
    return existing is None or (retry_placeholders and is_placeholder(existing))


def download_video(api, page, http, record, video_dir, existing, errors):
    destination = video_dir / f"{record['id']}.mp4"
    blank = False
    failures = []
    try:
        payload = fetch(api, f"{BASE}/aj/player/item/options", as_json=True, referer=record["url"],
                        params={"vid": record["id"]})
        options = payload.get("options")
        if not isinstance(options, dict):
            raise ArchiveError("Vbox7 returned unrecognized player options.")
        title = options.get("title")
        if isinstance(title, str) and title and not record.get("title"):
            record["title"] = title
        blank = options.get("src") == "blank"
        if blank:
            placeholder = destination.with_suffix(".placeholder.mp4")
            print("  Original unavailable. Saving the Vbox7 placeholder.", flush=True)
            if not valid_video(placeholder):
                stream_mp4(http, PLACEHOLDER_VIDEO_URL, record["url"], placeholder)
            return placeholder, True
        candidates = media_candidates(options)
        if not candidates:
            failures.append("Vbox7 did not return a supported direct video source.")
        for source in candidates:
            try:
                stream_mp4(http, source, record["url"], destination)
                return destination, False
            except ArchiveError as error:
                failures.append(str(error))
    except ArchiveError as error:
        failures.append(str(error))
    if not blank:
        try:
            file = browser_download(page, record, destination)
            if file is not None:
                return file, False
            failures.append("Chrome did not find a download control.")
        except ArchiveError as error:
            failures.append(str(error))
    errors["download"] = " ".join(dict.fromkeys(failures))
    return existing, blank


def archive_video(api, page, http, record, video_dir, *, retry_placeholders=False):
    errors = {}
    saved_comments = record.get("comments")
    saved_comment_count = record.get("comment_count")
    has_saved_comments = bool(saved_comments) or (saved_comments == [] and saved_comment_count == 0)
    try:
        html, final_url = fetch(api, record["url"], include_url=True)
        if not matches_page(urlsplit(final_url), urlsplit(record["url"])):
            raise ArchiveError("Vbox7 redirected the metadata request away from the requested video.")
        details = parse_video(html)
        record.update({key: value for key, value in details.items() if value is not None})
    except ArchiveError as error:
        errors["video_page"] = str(error)

    file = existing_video(record, video_dir)
    blank = False
    if needs_download(file, retry_placeholders):
        file, blank = download_video(api, page, http, record, video_dir, file, errors)
    record["video_file"] = f"videos/{file.name}" if file is not None else None
    record.pop("video_is_placeholder", None)
    if is_placeholder(file):
        record["video_is_placeholder"] = True

    record.pop("placeholder_static_file", None)
    if is_placeholder(file):
        try:
            static = video_dir / "vbox7-static.gif"
            if not valid_static(static):
                if not blank:
                    payload = fetch(api, f"{BASE}/aj/player/item/options", as_json=True, referer=record["url"],
                                    params={"vid": record["id"]})
                    options = payload.get("options")
                    if not isinstance(options, dict) or options.get("src") != "blank":
                        raise ArchiveError("The signed-in player did not confirm a blank source for the overlay.")
                static = save_placeholder_static(api, video_dir)
            record["placeholder_static_file"] = f"videos/{static.name}"
        except ArchiveError as error:
            errors["placeholder_static"] = str(error)

    comments, total, error = collect_comments(api, record)
    if not error or not has_saved_comments:
        record["comments"] = comments
        if total is not None:
            record["comment_count"] = total
    if error:
        errors["comments"] = error
        if has_saved_comments:
            record["comment_count"] = saved_comment_count
    missing = [key for key in ("title", "upload_date", "view_count", "comment_count") if record.get(key) is None]
    if missing:
        errors["metadata"] = "Vbox7 did not return " + ", ".join(missing) + "."
    record["errors"] = errors


def wait_for_sign_in(context, username):
    private_url = profile_url(username, "private")
    expected = urlsplit(private_url)
    deadline = monotonic() + 600
    warned_about_account = False
    while monotonic() < deadline:
        if not context.pages:
            break
        try:
            html, final_url = fetch(context.request, private_url, include_url=True)
            final = urlsplit(final_url)
            owner = None
            query = parse_qs(final.query)
            if matches_page(final, expected) and query.get("p") == ["items"] and query.get("filter") == ["private"]:
                for script in BeautifulSoup(html, "html.parser").find_all("script", src=False):
                    owner = re.search(r"""\bglobalLoginUser\s*=\s*(["'])([^"']*)\1""", script.string or "")
                    if owner:
                        break
            if owner and owner[2]:
                if owner[2].casefold() == username.casefold():
                    return
                if not warned_about_account:
                    print(f"The browser is signed into another account. Switch to {username} to continue.",
                          flush=True)
                    warned_about_account = True
        except ArchiveError:
            pass
        sleep(3)
    raise ArchiveError("Sign-in was interrupted or the browser did not show the requested account.")


def run(args):
    video_dir = args.output / "videos"
    index = args.output / "misc" / "metadata.json"
    if args.dry_run:
        return run_archive(args, video_dir, index)
    refresh_viewer = getattr(args, "refresh_viewer", False)
    if refresh_viewer and not index.is_file():
        raise ArchiveError(f"No archive catalog was found at {index}.")
    with archive_lock(index.parent):
        if refresh_viewer:
            viewer_data = index.with_name("viewer-data.js")
            save_viewer_data(viewer_data, load_catalog(index, args.username))
            print(f"Viewer data refreshed: {viewer_data}")
            return 0
        video_dir.mkdir(parents=True, exist_ok=True)
        return run_archive(args, video_dir, index)


def run_archive(args, video_dir, index):
    catalog = load_catalog(index, args.username)

    with sync_playwright() as playwright, requests.Session() as http:
        try:
            browser = playwright.chromium.launch(channel="chrome", headless=False)
        except BrowserError:
            raise ArchiveError(
                "Could not open Google Chrome. Make sure it is installed and opens from your desktop."
            ) from None
        with closing(browser), closing(browser.new_context(accept_downloads=True)) as context:
            login = context.new_page()
            print(f"Sign in as {args.username} in the browser window. The upload scan starts after sign-in.",
                  flush=True)
            target = urlsplit(profile_url(args.username, "private"))
            login.goto(f"{BASE}/login?" + urlencode({"backto": f"{target.path}?{target.query}"}),
                       wait_until="domcontentloaded", timeout=45_000)
            wait_for_sign_in(context, args.username)
            print("Sign-in detected. Scanning uploads.", flush=True)
            return process_archive(args, context, http, catalog, video_dir, index)


def fill_missing_authors(catalog):
    for record in catalog["uploads"].values():
        author = record.get("author")
        if not isinstance(author, str) or not author.strip():
            record["author"] = catalog["username"]


def process_archive(args, context, http, catalog, video_dir, index):
    records = catalog["uploads"]
    work = {}
    listing_errors = {}
    for visibility in FILTERS:
        count = 0
        try:
            for upload in discover(context.request, args.username, visibility):
                video_id = upload["id"]
                count += 1
                if video_id in work:
                    if work[video_id]["visibility"] != visibility:
                        print(f"  {video_id} also appears in {visibility}. Keeping its first visibility.", flush=True)
                    continue
                record = records.setdefault(video_id, {"comments": [], "comment_count": None})
                record.update({key: value for key, value in upload.items() if value is not None})
                previous_visibility = record.get("visibility")
                if not isinstance(previous_visibility, str) or previous_visibility not in listing_errors:
                    record["visibility"] = visibility
                work[video_id] = record
        except ArchiveError as error:
            listing_errors[visibility] = str(error)
            print(f"{visibility} listing: {error}", file=sys.stderr)
        print(f"{visibility}: {count} upload(s)", flush=True)

    selected = list(work.values())[:args.limit]
    if args.dry_run:
        downloads = sum(needs_download(existing_video(record, video_dir), args.retry_placeholders)
                        for record in selected)
        print(f"Dry run. Uploads found: {len(work)}. Uploads selected: {len(selected)}. "
              f"New downloads planned: {downloads}.")
        if listing_errors:
            print("The upload scan was incomplete. Counts reflect the uploads found so far.")
        return int(bool(listing_errors))

    fill_missing_authors(catalog)
    catalog.update({"listing_errors": listing_errors, "scan_complete": not listing_errors,
                    "last_run_limit": args.limit})
    save_json(index, catalog)
    flagged = 0
    if selected:
        page = context.new_page()
        for offset, record in enumerate(selected, start=1):
            print(f"[{offset}/{len(selected)}] {record['id']} {record.get('title') or ''}", flush=True)
            previous = record.copy()
            archive_video(context.request, page, http, record, video_dir,
                          retry_placeholders=args.retry_placeholders)
            if previous != record:
                save_json(index, catalog)
            if record["errors"]:
                flagged += 1
                print(f"  Review: {', '.join(record['errors'])}", flush=True)
    placeholders = sum(bool(r.get("video_is_placeholder")) for r in selected)
    videos = sum(bool(r["video_file"]) for r in selected) - placeholders
    save_viewer_data(index.with_name("viewer-data.js"), catalog)
    print(f"Videos saved: {videos}/{len(selected)}. Placeholders saved: {placeholders}. "
          f"Entries to review: {flagged}. Index: {index}", flush=True)
    return int(bool(flagged or listing_errors))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("username", help="your Vbox7 username")
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parent,
                        help="destination containing videos/ and misc/")
    parser.add_argument("--browser", choices=("chrome",),
                        help="optional for compatibility. The script always uses Google Chrome.")
    parser.add_argument("--limit", type=int, help="process at most this many uploads, private first")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true",
                      help="show upload and planned download counts without writing archive files")
    mode.add_argument("--refresh-viewer", action="store_true",
                      help="refresh offline viewer data from an existing archive without signing in")
    parser.add_argument("--retry-placeholders", action="store_true",
                        help="check whether saved placeholders can be replaced with original videos")
    args = parser.parse_args()
    if not args.username.strip() or (args.limit is not None and args.limit < 1):
        parser.error("provide a username and a positive --limit when using it")
    args.username = args.username.strip()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    try:
        return run(args)
    except ArchiveError as error:
        print(f"Archive stopped: {error}", file=sys.stderr)
    except BrowserError:
        print("Archive stopped: Playwright could not start or the browser session failed. "
              "Check your connection to Vbox7 and that the browser window is still open.", file=sys.stderr)
    except (OSError, ValueError, KeyError) as error:
        print(f"Archive stopped: could not read or write the local archive ({type(error).__name__}).",
              file=sys.stderr)
    except KeyboardInterrupt:
        print("Archive interrupted. Run it again to continue.", file=sys.stderr)
        return 130
    return 1


if __name__ == "__main__":
    sys.exit(main())

"""Archive uploads from your own Vbox7 account using a browser sign-in.

Run with --help for options. Requires Playwright, Beautiful Soup and Requests.
"""

import argparse
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


BASE = "https://www.vbox7.com"
FILTERS = ("private", "friends", "public")
VIDEO_ID = re.compile(r"/play:([0-9a-fA-F]+)")
DOWNLOAD_LABEL = re.compile(r"^\s*(?:изтегли|свали|download)\b", re.IGNORECASE)
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi", ".flv", ".wmv", ".mpeg", ".mpg", ".3gp"}


class ArchiveError(Exception):
    """An error whose message can be displayed without exposing session data."""


def profile_url(username, visibility):
    return f"{BASE}/user:{quote(username, safe='')}?p=items&filter={visibility}&order=date"


def text_at(node, selector):
    element = node.select_one(selector)
    return element.get_text(" ", strip=True) if element else None


def number(value):
    compact = re.sub(r"[\s,]", "", str(value))
    return int(compact) if compact.isdecimal() else None


def upload_date(value):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%d.%m.%Y").date().isoformat()
    except ValueError:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()
        except ValueError:
            return None


def parse_listing(html):
    soup = BeautifulSoup(html, "html.parser")
    feed = soup.select_one(".major-col section.feed")
    if not feed:
        raise ArchiveError("Vbox7 did not return an uploads list.")
    uploads = []
    for card in feed.select(".video-cell"):
        link = card.select_one("a.thumb-cont[href], h3 a[href]")
        match = VIDEO_ID.fullmatch(unquote(urlsplit(link["href"]).path)) if link else None
        if match:
            uploads.append({
                "id": match[1].lower(),
                "url": f"{BASE}/play:{match[1].lower()}",
                "title": text_at(card, "h3") or link.get("title"),
                "upload_date": upload_date(text_at(card, "svg.time + span")),
                "view_count": number(text_at(card, "svg.views + span")),
            })
    if not uploads and not feed.select_one(".row-mb"):
        raise ArchiveError("The uploads list is empty or its layout has changed.")
    return uploads, bool(feed.select_one('.pagination a[rel="next"][href]'))


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
    return {
        "title": info.get("name") or text_at(soup, ".video-info h1"),
        "author": text_at(soup, ".video-info .channel-name"),
        "upload_date": upload_date(info.get("uploadDate") or text_at(soup, ".video-stat svg.time + span")),
        "view_count": views if views is not None else number(text_at(soup, ".video-stat svg.views + span")),
        "comment_count": count if count is not None else number(text_at(soup, ".comments-btn .num")),
    }


def parse_comments(html):
    comments = []
    for item in BeautifulSoup(html, "html.parser").select(".item-comment"):
        parent = item.find_parent(class_="item-comment")
        wrapper = item.select_one(":scope > .parent-comment") or item
        body = wrapper.select_one(".comment-text")
        if body:
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
    parts = urlsplit(urljoin(BASE, source))
    host = parts.hostname or ""
    if parts.scheme != "https" or not (host == "vbox7.com" or host.endswith(".vbox7.com")):
        return []
    suffix = Path(parts.path).suffix.lower()
    if suffix == ".mp4":
        return [parts.geturl()]
    if suffix not in {".mpd", ".m3u8"}:
        return []
    stem = parts.path[:-len(suffix)]
    sizes = {number(value) for value in options.get("resolutions") or []}
    return [
        parts._replace(path=f"{stem}_{height}.mp4").geturl()
        for height in sorted(filter(None, sizes), reverse=True)
    ]


def fetch(api, url, *, params=None, referer=None, as_json=False, include_url=False):
    headers = {"Referer": referer, "X-Requested-With": "XMLHttpRequest"} if referer else {}
    try:
        response = api.get(url, params=params, headers=headers, timeout=30_000)
    except BrowserError:
        raise ArchiveError("A Vbox7 request timed out or the connection failed.") from None
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


def discover(api, username, visibility):
    url = profile_url(username, visibility)
    expected_path = unquote(urlsplit(url).path).casefold()
    seen = set()
    page = 1
    while True:
        page_url = url if page == 1 else f"{url}&page={page}"
        html, final_url = fetch(api, page_url, include_url=True)
        final = urlsplit(final_url)
        if (final.scheme != "https" or final.netloc != "www.vbox7.com"
                or unquote(final.path).casefold() != expected_path):
            raise ArchiveError("Vbox7 redirected the upload request away from the account's list.")
        final_filter = parse_qs(final.query).get("filter")
        if page == 1 and visibility == "public" and final_filter == ["private"]:
            print("Vbox7 redirects the public list to the private list.", flush=True)
            return
        if final_filter != [visibility]:
            raise ArchiveError("Vbox7 returned a different visibility list.")
        uploads, has_next = parse_listing(html)
        new = []
        for upload in uploads:
            if upload["id"] not in seen:
                seen.add(upload["id"])
                new.append(upload)
        if not new and (page > 1 or has_next):
            raise ArchiveError("Vbox7 stopped returning new uploads during pagination.")
        yield from new
        if not has_next:
            return
        page += 1


def collect_comments(api, record):
    comments = []
    seen = set()
    page = 1
    total = None
    while True:
        try:
            data = fetch(api, f"{BASE}/aj/comments", as_json=True, referer=record["url"],
                         params={"mdkey": record["id"], "page": page, "order": "date"})
            paging = data.get("pagination") or {}
            reported = number(paging.get("total"))
            per_page = number(paging.get("perPage"))
            if reported is None or not per_page or not isinstance(data.get("html"), str):
                raise ArchiveError("Vbox7 returned an unrecognized comment list.")
            total = reported
            batch = parse_comments(data["html"])
            new = [comment for comment in batch if not comment["id"] or comment["id"] not in seen]
            if total and not new:
                raise ArchiveError("Vbox7 stopped returning new comments before the last page.")
            comments.extend(new)
            seen.update(comment["id"] for comment in new if comment["id"])
            if page * per_page >= total:
                return comments, total, None
            page += 1
        except ArchiveError as error:
            return comments, total, str(error)


def stream_mp4(http, url, referer, destination):
    partial = destination.with_suffix(".mp4.part")
    try:
        with http.get(url, headers={"Referer": referer, "Accept-Encoding": "identity"},
                      stream=True, timeout=(15, 45)) as response:
            if response.status_code != 200:
                return False
            chunks = response.iter_content(chunk_size=1024 * 1024)
            first = next(chunks, b"")
            if first[4:8] != b"ftyp":
                return False
            size = len(first)
            with partial.open("wb") as output:
                output.write(first)
                for chunk in chunks:
                    output.write(chunk)
                    size += len(chunk)
            expected = number(response.headers.get("Content-Length"))
            if expected is not None and size != expected:
                return False
        partial.replace(destination)
        return True
    except requests.RequestException:
        return False
    finally:
        partial.unlink(missing_ok=True)


def browser_download(page, record, destination):
    """Try a download action in the site's video options when available."""
    try:
        page.goto(record["url"], wait_until="domcontentloaded", timeout=20_000)
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
        if suffix not in VIDEO_EXTENSIONS or download.failure():
            return None
        output = destination.with_suffix(suffix)
        partial = output.with_suffix(suffix + ".part")
        try:
            download.save_as(partial)
            if partial.stat().st_size:
                partial.replace(output)
                return output
        finally:
            partial.unlink(missing_ok=True)
    except BrowserError:
        pass
    return None


def archive_video(api, page, http, record, video_dir):
    errors = {}
    try:
        details = parse_video(fetch(api, record["url"]))
        record.update({key: value for key, value in details.items() if value is not None})
    except ArchiveError as error:
        errors["video_page"] = str(error)

    destination = video_dir / f"{record['id']}.mp4"
    existing = video_dir / Path(record.get("video_file") or destination.name).name
    if existing.is_file() and existing.stat().st_size:
        record["video_file"] = f"videos/{existing.name}"
    else:
        record["video_file"] = None
        try:
            payload = fetch(api, f"{BASE}/aj/player/item/options", as_json=True, referer=record["url"],
                            params={"vid": record["id"]})
            options = payload.get("options") or {}
            if options.get("title") and not record.get("title"):
                record["title"] = options["title"]
            for source in media_candidates(options):
                if stream_mp4(http, source, record["url"], destination):
                    record["video_file"] = f"videos/{destination.name}"
                    break
        except ArchiveError as error:
            errors["download"] = str(error)
        if record["video_file"] is None:
            file = browser_download(page, record, destination)
            if file:
                record["video_file"] = f"videos/{file.name}"
                errors.pop("download", None)
            else:
                errors.setdefault("download", "No downloadable video was returned. Check its download control in Vbox7.")

    comments, total, error = collect_comments(api, record)
    if not error or not record.get("comments") or len(comments) > len(record["comments"]):
        record["comments"] = comments
    if total is not None:
        record["comment_count"] = total
    if error:
        errors["comments"] = error
    missing = [key for key in ("title", "upload_date", "view_count", "comment_count") if record.get(key) is None]
    if missing:
        errors["metadata"] = "Vbox7 did not return " + ", ".join(missing) + "."
    record["errors"] = errors


def save_json(path, data):
    temporary = path.with_suffix(".json.part")
    with temporary.open("w", encoding="utf-8") as output:
        json.dump(data, output, ensure_ascii=False, indent=2)
        output.write("\n")
    temporary.replace(path)


def wait_for_sign_in(context, username):
    private_url = profile_url(username, "private")
    deadline = monotonic() + 600
    warned_about_account = False
    while monotonic() < deadline:
        if not context.pages:
            break
        try:
            html = fetch(context.request, private_url)
            owner = re.search(rb"""\bglobalLoginUser\s*=\s*(["'])([^"']*)\1""", html)
            if owner and owner[2]:
                if owner[2].decode("utf-8", errors="replace").casefold() == username.casefold():
                    return
                if not warned_about_account:
                    print(f"The browser is signed into another account. Switch to {username} to continue.",
                          flush=True)
                    warned_about_account = True
            # The signed-out profile can show the private tab but never its videos.
            uploads, _ = parse_listing(html)
            if uploads and not owner:
                return
        except ArchiveError:
            pass
        sleep(3)
    raise ArchiveError("Sign-in was interrupted or the browser did not show the requested account.")


def run(args):
    video_dir = args.output / "videos"
    index = args.output / "misc" / "metadata.json"
    video_dir.mkdir(parents=True, exist_ok=True)
    index.parent.mkdir(parents=True, exist_ok=True)
    catalog = json.loads(index.read_text(encoding="utf-8")) if index.is_file() else {
        "username": args.username, "uploads": {},
    }
    if catalog["username"].casefold() != args.username.casefold():
        raise ArchiveError("This output folder belongs to another Vbox7 account. Choose a different --output.")
    records = catalog["uploads"]

    with sync_playwright() as playwright, requests.Session() as http:
        try:
            browser = playwright.chromium.launch(channel="chrome", headless=False)
        except BrowserError:
            raise ArchiveError(
                "Could not open Google Chrome. Make sure it is installed and opens from your desktop."
            ) from None
        context = browser.new_context(accept_downloads=True)
        login = context.new_page()
        print(f"Sign in as {args.username} in the browser window. The archive starts after sign-in.", flush=True)
        target = urlsplit(profile_url(args.username, "private"))
        login.goto(f"{BASE}/login?" + urlencode({"backto": f"{target.path}?{target.query}"}),
                   wait_until="domcontentloaded", timeout=45_000)
        wait_for_sign_in(context, args.username)
        print("Sign-in detected. Scanning uploads.", flush=True)

        work = {}
        listing_errors = {}
        for visibility in FILTERS:
            count = 0
            try:
                for upload in discover(context.request, args.username, visibility):
                    video_id = upload["id"]
                    record = records.setdefault(video_id, {"comments": [], "comment_count": None})
                    record.update({key: value for key, value in upload.items() if value is not None})
                    record["author"] = record.get("author") or catalog["username"]
                    record["visibility"] = visibility
                    work[video_id] = record
                    count += 1
            except ArchiveError as error:
                listing_errors[visibility] = str(error)
                print(f"{visibility} listing: {error}", file=sys.stderr)
            print(f"{visibility}: {count} upload(s)", flush=True)

        catalog.update({"listing_errors": listing_errors, "scan_complete": not listing_errors,
                        "last_run_limit": args.limit})
        save_json(index, catalog)
        if not work:
            raise ArchiveError("No uploads were found. Verify the uploads list in the signed-in browser.")
        selected = list(work.values())[:args.limit]
        page = context.new_page()
        flagged = 0
        for offset, record in enumerate(selected, start=1):
            print(f"[{offset}/{len(selected)}] {record['id']} {record.get('title') or ''}", flush=True)
            archive_video(context.request, page, http, record, video_dir)
            save_json(index, catalog)
            if record["errors"]:
                flagged += 1
                print(f"  Review: {', '.join(record['errors'])}", flush=True)
        print(f"Videos saved: {sum(bool(r['video_file']) for r in selected)}/{len(selected)}. "
              f"Entries to review: {flagged}. Index: {index}", flush=True)
        context.close()
        browser.close()
        return int(bool(flagged or listing_errors))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("username", help="your Vbox7 username")
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parent,
                        help="destination containing videos/ and misc/")
    parser.add_argument("--browser", choices=("chrome",),
                        help="optional for compatibility. The script always uses Google Chrome.")
    parser.add_argument("--limit", type=int, help="process at most this many uploads, private first")
    args = parser.parse_args()
    if not args.username.strip() or (args.limit is not None and args.limit < 1):
        parser.error("provide a username and a positive --limit when using it")
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

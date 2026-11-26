import contextlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import requests

import vbox7_archive as archive


MP4_HEADER = b"\x00\x00\x00\x10ftypisom\x00\x00\x00\x00"
VIDEO = MP4_HEADER + b"\x00\x00\x00\x09mdat\x01"
RECORD = {
    "id": "a", "url": archive.BASE + "/play:a", "title": "Synthetic",
    "upload_date": "2020-01-01", "view_count": 0,
}
VIDEO_PAGE = (b"<html></html>", RECORD["url"])


def listing(content):
    return f'<div class="major-col"><section class="feed"><div class="row-mb">{content}</div></section></div>'


def card(path):
    return f'<div class="video-cell"><h3><a href="{path}">Synthetic</a></h3></div>'


def comment(comment_id):
    return f'<div class="item-comment" id="comment-{comment_id}"><p class="comment-text">Synthetic</p></div>'


def comments(total, html):
    return {"pagination": {"total": total, "perPage": 10}, "html": html}


class SiteTests(unittest.TestCase):
    def test_unparseable_upload_card_is_reported_as_incomplete(self):
        for content in (card("/changed"), card("/play:a") + card("/changed")):
            with self.subTest(content=content), self.assertRaises(archive.ArchiveError):
                archive.parse_listing(listing(content))

    def test_empty_state_requires_explicit_evidence(self):
        with self.assertRaises(archive.ArchiveError):
            archive.parse_listing(listing(""))
        self.assertEqual(archive.parse_listing(listing("<p>No uploads found.</p>")), ([], False))

    def test_public_redirect_is_incomplete(self):
        with patch.object(archive, "fetch", return_value=(b"", archive.profile_url("owner", "private"))):
            with self.assertRaises(archive.ArchiveError):
                list(archive.discover(None, "owner", "public"))

    def test_first_page_redirect_to_later_page_is_incomplete(self):
        url = archive.profile_url("owner", "private")
        with patch.object(archive, "fetch", return_value=(listing(card("/play:a")), url + "&page=2")):
            with self.assertRaises(archive.ArchiveError):
                list(archive.discover(None, "owner", "private"))

    def test_pagination_without_next_control_is_incomplete(self):
        pagination = '<div class="pagination"><a href="?page=2">2</a></div>'
        with self.assertRaises(archive.ArchiveError):
            archive.parse_listing(listing(card("/play:a") + pagination))

    def test_discovery_preserves_valid_cards_on_a_partial_page(self):
        html = listing(card("/play:a") + card("/changed"))
        url = archive.profile_url("owner", "private")
        with patch.object(archive, "fetch", return_value=(html, url)):
            found = archive.discover(None, "owner", "private")
            self.assertEqual(next(found)["id"], "a")
            with self.assertRaises(archive.ArchiveError):
                next(found)

    def test_comment_ids_deduplicated_and_short_page_flagged(self):
        with patch.object(archive, "fetch", return_value=comments(3, comment("1") * 2)):
            entries, total, error = archive.collect_comments(None, RECORD)
        self.assertEqual([row["id"] for row in entries], ["1"])
        self.assertEqual(total, 3)
        self.assertTrue(error)

    def test_unrecognized_reply_makes_refresh_incomplete_even_when_roots_match(self):
        thread = ('<div class="item-comment" id="comment-1"><p class="comment-text">Synthetic</p>'
                  + comment("2") + '<div class="item-comment" id="comment-3"><p>Changed body</p></div></div>')
        with patch.object(archive, "fetch", return_value=comments(1, thread)):
            self.assertTrue(archive.collect_comments(None, RECORD)[2])

    def test_malformed_nested_values_are_scoped(self):
        with patch.object(archive, "fetch", return_value={"pagination": [1], "html": ""}):
            self.assertTrue(archive.collect_comments(None, RECORD)[2])
        malformed = '<script type="application/ld+json">{"@type":"VideoObject","interactionStatistic":1,"uploadDate":1}</script>'
        self.assertIsNone(archive.parse_video(malformed)["upload_date"])
        with self.assertRaises(archive.ArchiveError):
            archive.media_candidates({"src": "/synthetic.mpd", "resolutions": 1})

    def test_sign_in_requires_owner_evidence(self):
        context = SimpleNamespace(pages=[object()], request=None)
        url = archive.profile_url("owner", "private")
        html = listing(card("/play:a")).encode()
        cases = (
            (html, url),
            (b'<script>var globalLoginUser = "owner";</script>', archive.BASE + "/login"),
            (b'<script>var globalLoginUser = "someone_else";</script>', url),
            (b'<p>globalLoginUser = "owner"</p>', url),
        )
        for body, final in cases:
            with self.subTest(final=final):
                with patch.object(archive, "fetch", return_value=(body, final)), patch.object(
                    archive, "monotonic", side_effect=[0, 1, 601]
                ), patch.object(archive, "sleep"), contextlib.redirect_stdout(io.StringIO()), self.assertRaises(
                    archive.ArchiveError
                ):
                    archive.wait_for_sign_in(context, "owner")

    def test_sign_in_accepts_matching_identity_on_private_profile(self):
        context = SimpleNamespace(pages=[object()], request=None)
        url = archive.profile_url("owner", "private")
        with patch.object(archive, "fetch", return_value=(b'<script>var globalLoginUser = "OWNER";</script>', url)):
            archive.wait_for_sign_in(context, "owner")


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.output = Path(self.directory.name)
        self.videos = self.output / "videos"
        self.videos.mkdir()

    def test_catalog_saves_page_author_and_fills_missing_authors(self):
        for video_id in ("a", "b"):
            (self.videos / f"{video_id}.mp4").write_bytes(VIDEO)
        index = self.output / "misc" / "metadata.json"
        catalog = {"username": "Owner", "uploads": {
            "a": dict(RECORD, author="Saved uploader"),
            "c": dict(RECORD, id="c", url=archive.BASE + "/play:c"),
        }}
        args = SimpleNamespace(username="owner", limit=2, dry_run=False, retry_placeholders=False)
        context = SimpleNamespace(request=None, new_page=lambda: None)
        second = dict(RECORD, id="b", url=archive.BASE + "/play:b")
        page = '<div class="video-info"><a class="channel-name">{}</a></div>'
        with archive.archive_lock(index.parent), patch.object(
            archive, "discover", side_effect=[iter([dict(RECORD), second]), iter([]), iter([])]
        ), patch.object(archive, "fetch", side_effect=[
            (page.format(" "), RECORD["url"]), comments(0, ""),
            (page.format("Actual uploader"), second["url"]), comments(0, ""),
        ]), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(archive.process_archive(args, context, None, catalog, self.videos, index), 0)

        uploads = json.loads(index.read_text(encoding="utf-8"))["uploads"]
        self.assertEqual({key: row.get("author") for key, row in uploads.items()},
                         {"a": "Saved uploader", "b": "Actual uploader", "c": "Owner"})

    def test_partial_refresh_preserves_previous_comment_snapshot(self):
        (self.videos / "a.mp4").write_bytes(VIDEO)
        saved = [{"id": str(i), "text": "Synthetic"} for i in range(3)]
        record = dict(RECORD, comments=saved, comment_count=3)
        with patch.object(archive, "fetch", side_effect=[VIDEO_PAGE, comments(3, comment("1"))]):
            archive.archive_video(None, None, None, record, self.videos)
        self.assertEqual(record["comments"], saved)
        self.assertIn("comments", record["errors"])

    def test_partial_refresh_preserves_previous_empty_comment_snapshot(self):
        (self.videos / "a.mp4").write_bytes(VIDEO)
        record = dict(RECORD, comments=[], comment_count=0, errors={})
        with patch.object(archive, "fetch", side_effect=[VIDEO_PAGE, comments(3, comment("1"))]):
            archive.archive_video(None, None, None, record, self.videos)
        self.assertEqual(record["comments"], [])
        self.assertEqual(record["comment_count"], 0)
        self.assertIn("comments", record["errors"])

    def test_recovers_orphan_browser_download(self):
        path = self.videos / "a.avi"
        path.write_bytes(b"RIFF\x10\x00\x00\x00AVI ")
        self.assertEqual(archive.existing_video(RECORD, self.videos), path)

    def test_stale_catalog_path_falls_back_to_canonical_mp4(self):
        path = self.videos / "a.mp4"
        path.write_bytes(VIDEO)
        self.assertEqual(archive.existing_video(dict(RECORD, video_file="videos/a.mov"), self.videos), path)

    def test_existing_html_is_not_reused_as_video(self):
        (self.videos / "a.mp4").write_bytes(b"<html>Synthetic error</html>")
        self.assertIsNone(archive.existing_video(RECORD, self.videos))

    def test_mp4_with_only_a_header_is_not_reused(self):
        (self.videos / "a.mp4").write_bytes(MP4_HEADER)
        self.assertIsNone(archive.existing_video(RECORD, self.videos))

    def test_browser_download_rejects_a_redirect_to_another_video(self):
        page = MagicMock()
        page.url = archive.BASE + "/play:b"
        with self.assertRaisesRegex(archive.ArchiveError, "redirected away"):
            archive.browser_download(page, RECORD, self.videos / "a.mp4")
        self.assertEqual(list(self.videos.iterdir()), [])

    def test_browser_download_rejects_html(self):
        page = MagicMock()
        page.url = RECORD["url"]
        download = page.expect_download.return_value.__enter__.return_value.value
        download.suggested_filename = "synthetic.mp4"
        download.failure.return_value = None
        download.save_as.side_effect = lambda path: path.write_bytes(b"<html>Error</html>")
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(
            archive.ArchiveError, "recognized video header"
        ):
            archive.browser_download(page, RECORD, self.videos / "a.mp4")
        self.assertEqual(list(self.videos.iterdir()), [])

    def test_metadata_for_a_different_video_is_ignored(self):
        (self.videos / "a.mp4").write_bytes(VIDEO)
        record = dict(RECORD)
        redirected = b'<article class="video-info"><h1>Other video</h1></article>'
        with patch.object(archive, "fetch", side_effect=[
            (redirected, archive.BASE + "/play:b"), comments(0, ""),
        ]):
            archive.archive_video(None, None, None, record, self.videos)
        self.assertEqual(record["title"], "Synthetic")
        self.assertIn("video_page", record["errors"])

    def test_placeholder_retry_recovers_original(self):
        (self.videos / "a.placeholder.mp4").write_bytes(VIDEO)
        record = dict(RECORD, video_file="videos/a.placeholder.mp4", video_is_placeholder=True)

        def save(_http, _source, _referer, path, **_kwargs):
            path.write_bytes(VIDEO)
            return True

        with patch.object(archive, "fetch", side_effect=[
            VIDEO_PAGE, {"options": {"src": "/a.mp4"}}, comments(0, ""),
        ]), patch.object(archive, "stream_mp4", side_effect=save):
            archive.archive_video(None, None, None, record, self.videos, retry_placeholders=True)
        self.assertEqual(record["video_file"], "videos/a.mp4")
        self.assertFalse(record.get("video_is_placeholder"))

    def test_placeholder_retry_preserves_previous_media_if_original_fails(self):
        (self.videos / "a.placeholder.mp4").write_bytes(VIDEO)
        gif = self.videos / "vbox7-static.gif"
        gif.write_bytes(b"GIF89a")
        record = dict(RECORD, video_file="videos/a.placeholder.mp4", video_is_placeholder=True)
        with patch.object(archive, "fetch", side_effect=[
            VIDEO_PAGE, {"options": {"src": "/a.mp4"}}, comments(0, ""),
        ]), patch.object(
            archive, "stream_mp4", side_effect=archive.ArchiveError("Direct video download returned HTTP 404.")
        ), patch.object(archive, "browser_download", return_value=None):
            archive.archive_video(None, None, None, record, self.videos, retry_placeholders=True)
        self.assertTrue(record["video_is_placeholder"])
        self.assertEqual(record["video_file"], "videos/a.placeholder.mp4")
        self.assertEqual(record["placeholder_static_file"], "videos/vbox7-static.gif")
        self.assertIn("HTTP 404", record["errors"]["download"])

    def test_download_does_not_create_placeholder_for_malformed_options(self):
        record = dict(RECORD)
        with patch.object(archive, "fetch", side_effect=[
            VIDEO_PAGE, {"options": ["blank"]}, comments(0, ""),
        ]), patch.object(archive, "browser_download", return_value=None):
            archive.archive_video(None, None, None, record, self.videos)
        self.assertFalse(record.get("video_is_placeholder"))
        self.assertIsNone(record["video_file"])
        self.assertIn("unrecognized player options", record["errors"]["download"])
        self.assertEqual(list(self.videos.iterdir()), [])

    def test_blank_player_saves_marked_placeholder_and_overlay(self):
        record = dict(RECORD)

        def save(_http, _source, _referer, path):
            path.write_bytes(VIDEO)

        with patch.object(archive, "fetch", side_effect=[
            VIDEO_PAGE, {"options": {"src": "blank"}}, b"GIF89a", comments(0, ""),
        ]), patch.object(archive, "stream_mp4", side_effect=save), contextlib.redirect_stdout(io.StringIO()):
            archive.archive_video(None, None, None, record, self.videos)
        self.assertTrue(record["video_is_placeholder"])
        self.assertEqual(record["video_file"], "videos/a.placeholder.mp4")
        self.assertEqual(record["placeholder_static_file"], "videos/vbox7-static.gif")
        self.assertEqual(record["title"], "Synthetic")
        self.assertEqual(record["errors"], {})

    def test_blank_player_saves_marked_placeholder_and_overlay(self):
        record = dict(RECORD)

        def save(_http, _source, _referer, path):
            path.write_bytes(VIDEO)

        with patch.object(archive, "fetch", side_effect=[
            VIDEO_PAGE, {"options": {"src": "blank"}}, b"GIF89a", comments(0, ""),
        ]), patch.object(archive, "stream_mp4", side_effect=save), contextlib.redirect_stdout(io.StringIO()):
            archive.archive_video(None, None, None, record, self.videos)
        self.assertTrue(record["video_is_placeholder"])
        self.assertEqual(record["video_file"], "videos/a.placeholder.mp4")
        self.assertEqual(record["placeholder_static_file"], "videos/vbox7-static.gif")
        self.assertEqual(record["title"], "Synthetic")
        self.assertEqual(record["errors"], {})

    def test_blank_player_saves_marked_placeholder_and_overlay(self):
        record = dict(RECORD)

        def save(_http, _source, _referer, path):
            path.write_bytes(VIDEO)

        with patch.object(archive, "fetch", side_effect=[
            VIDEO_PAGE, {"options": {"src": "blank"}}, b"GIF89a", comments(0, ""),
        ]), patch.object(archive, "stream_mp4", side_effect=save), contextlib.redirect_stdout(io.StringIO()):
            archive.archive_video(None, None, None, record, self.videos)
        self.assertTrue(record["video_is_placeholder"])
        self.assertEqual(record["video_file"], "videos/a.placeholder.mp4")
        self.assertEqual(record["placeholder_static_file"], "videos/vbox7-static.gif")
        self.assertEqual(record["title"], "Synthetic")
        self.assertEqual(record["errors"], {})

    def test_dry_run_includes_explicit_placeholder_retry_and_writes_nothing(self):
        (self.videos / "a.placeholder.mp4").write_bytes(VIDEO)
        index = self.output / "misc" / "metadata.json"
        index.parent.mkdir()
        original = json.dumps({"username": "owner", "uploads": {
            "a": dict(RECORD, video_file="videos/a.placeholder.mp4", video_is_placeholder=True),
        }})
        index.write_text(original, encoding="utf-8")
        args = SimpleNamespace(output=self.output, username="owner", limit=1, dry_run=True, retry_placeholders=True)
        with patch.object(archive, "sync_playwright", return_value=MagicMock()), patch.object(
            archive, "wait_for_sign_in"
        ), patch.object(
            archive, "discover", side_effect=lambda *args: iter([dict(RECORD)] if args[2] == "private" else [])
        ), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(archive.run(args), 0)
        self.assertIn("New downloads planned: 1.", out.getvalue())
        self.assertEqual(index.read_text(encoding="utf-8"), original)
        self.assertEqual(list(index.parent.iterdir()), [index])

    def test_dry_run_reports_incomplete_listing_without_creating_output(self):
        output = self.output / "uncreated"
        args = SimpleNamespace(output=output, username="owner", limit=1, dry_run=True, retry_placeholders=False)
        with patch.object(archive, "sync_playwright", return_value=MagicMock()), patch.object(
            archive, "wait_for_sign_in"
        ), patch.object(
            archive, "fetch", side_effect=lambda _api, url, **_: (listing(card("/changed")), url)
        ), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(archive.run(args), 1)
        self.assertFalse(output.exists())

    def test_private_visibility_wins_duplicate_and_limit_applies_after_discovery(self):
        filters = []

        def discover(_api, _owner, visibility):
            filters.append(visibility)
            return iter([dict(RECORD, title=visibility)]) if visibility != "friends" else iter([])

        args = SimpleNamespace(output=self.output, username="owner", limit=1, dry_run=False, retry_placeholders=False)
        with patch.object(archive, "sync_playwright", return_value=MagicMock()), patch.object(
            archive, "wait_for_sign_in"
        ), patch.object(archive, "discover", side_effect=discover), patch.object(
            archive, "archive_video", side_effect=lambda *args, **kw: args[3].update(video_file=None, errors={})
        ), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(archive.run(args), 0)
        data = json.loads((self.output / "misc" / "metadata.json").read_text(encoding="utf-8"))
        self.assertEqual(filters, ["private", "friends", "public"])
        self.assertEqual(data["uploads"]["a"]["visibility"], "private")
        self.assertEqual(data["uploads"]["a"]["title"], "private")

    def test_failed_private_scan_keeps_previously_saved_private_visibility(self):
        index = self.output / "misc" / "metadata.json"
        index.parent.mkdir()
        index.write_text(json.dumps({"username": "owner", "uploads": {
            "a": dict(RECORD, visibility="private"),
        }}), encoding="utf-8")

        def discover(_api, _owner, visibility):
            if visibility == "private":
                raise archive.ArchiveError("Incomplete")
            return iter([dict(RECORD)] if visibility == "public" else [])

        args = SimpleNamespace(output=self.output, username="owner", limit=1, dry_run=False, retry_placeholders=False)
        with patch.object(archive, "sync_playwright", return_value=MagicMock()), patch.object(
            archive, "wait_for_sign_in"
        ), patch.object(archive, "discover", side_effect=discover), patch.object(
            archive, "archive_video", side_effect=lambda *args, **kw: args[3].update(video_file=None, errors={})
        ), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(archive.run(args), 1)
        data = json.loads(index.read_text(encoding="utf-8"))
        self.assertEqual(data["uploads"]["a"]["visibility"], "private")

    def test_catalog_update_failure_preserves_prior_file_and_cleans_part(self):
        path = self.output / "metadata.json"
        path.write_text('{"first": true}', encoding="utf-8")
        with patch.object(archive.json, "dump", side_effect=OSError), self.assertRaises(OSError):
            archive.save_json(path, {"second": True})
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"first": True})
        self.assertEqual(sorted(p.name for p in self.output.iterdir()), ["metadata.json", "videos"])

    def test_second_writer_cannot_acquire_archive_lock(self):
        with archive.archive_lock(self.output):
            with self.assertRaises(archive.ArchiveError), archive.archive_lock(self.output):
                self.fail("second writer acquired the lock")
        with archive.archive_lock(self.output):
            pass

    def test_catalog_rejects_wrong_owner_and_malformed_entries(self):
        index = self.output / "metadata.json"
        for catalog in ({"username": "someone_else", "uploads": {}}, {"username": "owner", "uploads": {"a": []}}):
            with self.subTest(catalog=catalog):
                index.write_text(json.dumps(catalog), encoding="utf-8")
                with self.assertRaises(archive.ArchiveError):
                    archive.load_catalog(index, "owner")

    def test_direct_download_retries_transient_status_and_validates_size(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.status_code = 200
        response.headers = {"Content-Length": str(len(VIDEO))}
        response.iter_content.return_value = iter([VIDEO])
        http = MagicMock(spec=requests.Session)
        http.get.side_effect = [requests.ConnectionError(), response]
        target = self.videos / "a.mp4"
        with patch.object(archive, "sleep"), contextlib.redirect_stdout(io.StringIO()):
            archive.stream_mp4(http, archive.BASE + "/synthetic.mp4", RECORD["url"], target)
        self.assertEqual(target.read_bytes(), VIDEO)

    def test_direct_download_stops_reading_an_unrecognized_header(self):
        def chunks():
            yield b"<html>Error"
            self.fail("download continued after reading an unrecognized header")

        response = MagicMock()
        response.__enter__.return_value = response
        response.status_code = 200
        response.headers = {}
        response.iter_content.return_value = chunks()
        http = MagicMock(spec=requests.Session)
        http.get.return_value = response
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(archive.ArchiveError):
            archive.stream_mp4(http, archive.BASE + "/synthetic.mp4", RECORD["url"], self.videos / "a.mp4")
        self.assertEqual(list(self.videos.iterdir()), [])

    def test_direct_download_reports_completion_after_validated_final_rename(self):
        target = self.videos / "a.mp4"
        response = SimpleNamespace(
            headers={"Content-Length": str(len(VIDEO))},
            iter_content=lambda **_kwargs: iter([VIDEO[:4], VIDEO[4:]]),
        )
        updates = []

        def progress(size, total=None, *, complete=False):
            updates.append((size, total, complete, target.exists()))

        with patch.object(archive, "show_download_progress", side_effect=progress):
            archive.save_mp4_response(response, target, archive.monotonic() + 30)
        self.assertEqual(updates, [(len(VIDEO), len(VIDEO), False, False), (len(VIDEO), len(VIDEO), True, True)])

    def test_direct_size_mismatch_preserves_the_previous_file(self):
        target = self.videos / "a.mp4"
        target.write_bytes(VIDEO)
        response = SimpleNamespace(
            headers={"Content-Length": str(len(VIDEO) + 1)},
            iter_content=lambda **_kwargs: iter([VIDEO[:-1] + b"\x02"]),
        )
        with contextlib.redirect_stdout(io.StringIO()) as log, self.assertRaises(archive.ArchiveError):
            archive.save_mp4_response(response, target, archive.monotonic() + 30)
        self.assertEqual(target.read_bytes(), VIDEO)
        self.assertNotIn("Download complete", log.getvalue())
        self.assertEqual(list(self.videos.iterdir()), [target])

    def test_direct_download_reports_completion_after_validated_final_rename(self):
        target = self.videos / "a.mp4"
        response = SimpleNamespace(
            headers={"Content-Length": str(len(VIDEO))},
            iter_content=lambda **_kwargs: iter([VIDEO[:4], VIDEO[4:]]),
        )
        updates = []

        def progress(size, total=None, *, complete=False):
            updates.append((size, total, complete, target.exists()))

        with patch.object(archive, "show_download_progress", side_effect=progress):
            archive.save_mp4_response(response, target, archive.monotonic() + 30)
        self.assertEqual(updates, [(len(VIDEO), len(VIDEO), False, False), (len(VIDEO), len(VIDEO), True, True)])

    def test_direct_size_mismatch_preserves_the_previous_file(self):
        target = self.videos / "a.mp4"
        target.write_bytes(VIDEO)
        response = SimpleNamespace(
            headers={"Content-Length": str(len(VIDEO) + 1)},
            iter_content=lambda **_kwargs: iter([VIDEO[:-1] + b"\x02"]),
        )
        with contextlib.redirect_stdout(io.StringIO()) as log, self.assertRaises(archive.ArchiveError):
            archive.save_mp4_response(response, target, archive.monotonic() + 30)
        self.assertEqual(target.read_bytes(), VIDEO)
        self.assertNotIn("Download complete", log.getvalue())
        self.assertEqual(list(self.videos.iterdir()), [target])

    def test_direct_download_reports_completion_after_validated_final_rename(self):
        target = self.videos / "a.mp4"
        response = SimpleNamespace(
            headers={"Content-Length": str(len(VIDEO))},
            iter_content=lambda **_kwargs: iter([VIDEO[:4], VIDEO[4:]]),
        )
        updates = []

        def progress(size, total=None, *, complete=False):
            updates.append((size, total, complete, target.exists()))

        with patch.object(archive, "show_download_progress", side_effect=progress):
            archive.save_mp4_response(response, target, archive.monotonic() + 30)
        self.assertEqual(updates, [(len(VIDEO), len(VIDEO), False, False), (len(VIDEO), len(VIDEO), True, True)])

    def test_direct_size_mismatch_preserves_the_previous_file(self):
        target = self.videos / "a.mp4"
        target.write_bytes(VIDEO)
        response = SimpleNamespace(
            headers={"Content-Length": str(len(VIDEO) + 1)},
            iter_content=lambda **_kwargs: iter([VIDEO[:-1] + b"\x02"]),
        )
        with contextlib.redirect_stdout(io.StringIO()) as log, self.assertRaises(archive.ArchiveError):
            archive.save_mp4_response(response, target, archive.monotonic() + 30)
        self.assertEqual(target.read_bytes(), VIDEO)
        self.assertNotIn("Download complete", log.getvalue())
        self.assertEqual(list(self.videos.iterdir()), [target])

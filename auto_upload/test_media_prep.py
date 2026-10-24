import unittest
import sys
import types
from pathlib import Path
from unittest import mock

try:
    import requests  # noqa: F401
except ImportError:
    sys.modules["requests"] = types.SimpleNamespace()

import media_prep


class MusicRotationTests(unittest.TestCase):
    def test_fresh_media_url_preserves_query_and_adds_refresh_token(self):
        fresh = media_prep._fresh_media_url(
            "https://media.example/video.mp4?download=1", token="fixed"
        )
        parts = media_prep.urlsplit(fresh)
        query = dict(media_prep.parse_qsl(parts.query))
        self.assertEqual(parts.path, "/video.mp4")
        self.assertEqual(query["download"], "1")
        self.assertEqual(query["_cd_refresh"], "fixed")

    def test_request_get_retries_truncated_downloads(self):
        truncated = mock.Mock(
            content=b"partial",
            headers={"Content-Type": "video/mp4", "Content-Length": "20"},
        )
        truncated.raise_for_status.return_value = None
        complete = mock.Mock(
            content=b"complete-video-bytes",
            headers={"Content-Type": "video/mp4", "Content-Length": "20"},
        )
        complete.raise_for_status.return_value = None
        with mock.patch("media_prep.requests.get", side_effect=[truncated, complete]), \
             mock.patch("media_prep.time.sleep"):
            resp = media_prep._request_get("https://media.example/video.mp4")
        self.assertEqual(resp.content, b"complete-video-bytes")

    def test_prepare_video_bypasses_cache_and_audits_fresh_source(self):
        response = mock.Mock(
            content=b"updated-video-with-audio",
            headers={"Content-Type": "video/mp4"},
        )
        response.raise_for_status.return_value = None
        with mock.patch("media_prep.requests.get", return_value=response) as get, \
             mock.patch("media_prep.audio_state", return_value="audible"), \
             mock.patch.dict(media_prep.os.environ, {"AUTO_ADD_AUDIO": "true"}, clear=True):
            name, content, content_type = media_prep.prepare_video(
                "https://media.example/video.mp4?download=1"
            )
        requested_url = get.call_args.args[0]
        headers = get.call_args.kwargs["headers"]
        self.assertIn("_cd_refresh=", requested_url)
        self.assertIn("download=1", requested_url)
        self.assertIn("no-cache", headers["Cache-Control"])
        self.assertEqual(name, "video.mp4")
        self.assertEqual(content, b"updated-video-with-audio")
        self.assertEqual(content_type, "video/mp4")

    def test_bundled_library_contains_pinned_real_tracks(self):
        tracks = media_prep.BUNDLED_CC0_MUSIC_URLS
        self.assertEqual(len(tracks), 58)
        self.assertTrue(any("2ce8458293fe4eeb91414a19d6d7ecd1562a5949" in track for track in tracks))
        self.assertEqual(len(media_prep.DURU_CC0_MUSIC_URLS), 17)
        self.assertTrue(all("1f63fef279098a082f2f77b3f74940b2784d37b7" in track for track in media_prep.DURU_CC0_MUSIC_URLS))
        self.assertEqual(len(tracks), len(set(tracks)))
        self.assertTrue(all(" " not in track for track in tracks))

    def test_different_videos_rotate_across_library(self):
        keys = [f"https://media.example/product-{i}.mp4" for i in range(30)]
        selected = {media_prep._stable_choice(media_prep.BUNDLED_CC0_MUSIC_URLS, key) for key in keys}
        self.assertGreaterEqual(len(selected), 18)

    def test_same_video_is_stable(self):
        key = "https://media.example/product-6373.mp4"
        first = media_prep._stable_choice(media_prep.BUNDLED_CC0_MUSIC_URLS, key)
        second = media_prep._stable_choice(media_prep.BUNDLED_CC0_MUSIC_URLS, key)
        self.assertEqual(first, second)

    def test_same_video_rotates_across_destination_accounts(self):
        media_url = "https://media.example/shared-product.mp4"
        keys = [f"instagram|account-{i}|{media_url}" for i in range(30)]
        selected = {
            media_prep._stable_choice(media_prep._music_url_library(), key)
            for key in keys
        }
        self.assertGreaterEqual(len(selected), 18)

    def test_single_configured_url_does_not_collapse_library(self):
        with mock.patch.dict(
            media_prep.os.environ,
            {"BACKGROUND_MUSIC_URLS": "https://example.com/one-track.mp3"},
            clear=True,
        ):
            library = media_prep._music_url_library()
        self.assertEqual(len(library), len(media_prep.BUNDLED_CC0_MUSIC_URLS) + 1)
        self.assertIn("https://example.com/one-track.mp3", library)

    def test_legacy_single_url_cannot_force_one_track(self):
        with mock.patch.dict(
            media_prep.os.environ,
            {"BACKGROUND_MUSIC_URL": "https://example.com/one-track.mp3"},
            clear=True,
        ):
            self.assertEqual(media_prep._configured_music_urls(), [])

    def test_reel_fit_preserves_complete_video_with_blurred_edge_fill(self):
        completed = mock.Mock(returncode=0)
        with mock.patch("media_prep.shutil.which", return_value="/usr/bin/ffmpeg"), \
             mock.patch("media_prep.audio_state", return_value="missing"), \
             mock.patch("media_prep._probe_dimensions", return_value=(1080, 1920)), \
             mock.patch("media_prep.subprocess.run", return_value=completed) as run:
            self.assertTrue(media_prep._to_9x16_fill("input.mp4", "output.mp4"))
        commands = [" ".join(call.args[0]) for call in run.call_args_list]
        command = next(cmd for cmd in commands if "-filter_complex" in cmd)
        self.assertIn("force_original_aspect_ratio=increase", command)
        self.assertIn("force_original_aspect_ratio=decrease", command)
        self.assertIn("crop=1080:1920", command)
        self.assertIn("scale=1080:1920:force_original_aspect_ratio=decrease", command)
        self.assertIn("boxblur=24:8", command)
        self.assertIn("overlay=(W-w)/2:(H-h)/2", command)
        self.assertIn("setdar=9/16", command)
        self.assertIn("-map [v] -map 0:a:0?", command)
        self.assertIn("-c:a aac -b:a 192k -ar 48000 -ac 2", command)
        self.assertNotIn("-shortest", command)
        self.assertNotIn("pad=", command)
        self.assertNotIn("-c:a copy", command)

    def test_instagram_reel_fit_uses_safe_foreground_margin(self):
        completed = mock.Mock(returncode=0)
        with mock.patch("media_prep.shutil.which", return_value="/usr/bin/ffmpeg"), \
             mock.patch("media_prep.audio_state", return_value="audible"), \
             mock.patch("media_prep._probe_dimensions", return_value=(1080, 1920)), \
             mock.patch("media_prep.subprocess.run", return_value=completed) as run:
            self.assertTrue(media_prep._to_9x16_fill("input.mp4", "output.mp4", platform="instagram"))
        command = " ".join(run.call_args.args[0])
        self.assertIn("scale=928:1498:force_original_aspect_ratio=decrease", command)
        self.assertIn("crop=1080:1920", command)
        self.assertIn("-c:a aac", command)

    def test_reel_fit_encodes_audible_original_audio_as_aac(self):
        completed = mock.Mock(returncode=0)
        with mock.patch("media_prep.shutil.which", return_value="/usr/bin/ffmpeg"), \
             mock.patch("media_prep.audio_state", return_value="audible"), \
             mock.patch("media_prep._probe_dimensions", return_value=(1080, 1920)), \
             mock.patch("media_prep.subprocess.run", return_value=completed) as run:
            self.assertTrue(media_prep._to_9x16_fill("input.mp4", "output.mp4"))
        command = " ".join(run.call_args.args[0])
        self.assertIn("-c:a aac", command)
        self.assertNotIn("-c:a copy", command)

    def test_reel_fit_rejects_dropped_original_audio(self):
        with mock.patch("media_prep.shutil.which", return_value="/usr/bin/tool"), \
             mock.patch("media_prep._probe_dimensions", return_value=(1080, 1920)), \
             mock.patch("media_prep.audio_state", side_effect=["audible", "missing"]), \
             mock.patch(
                 "media_prep.subprocess.run",
                 return_value=mock.Mock(returncode=0, stdout="", stderr=b""),
             ):
            with self.assertRaisesRegex(RuntimeError, "dropped the original audio"):
                media_prep._to_9x16_fill("input.mp4", "output.mp4")

    def test_quiet_original_audio_is_not_classified_as_silent(self):
        completed = mock.Mock(stdout="audio", stderr="max_volume: -55.0 dB")
        with mock.patch("media_prep.shutil.which", side_effect=lambda name: f"/usr/bin/{name}"), \
             mock.patch("media_prep.subprocess.run", return_value=completed):
            self.assertEqual(media_prep.audio_state("quiet-original.mp4"), "audible")

    def test_prepare_video_keeps_original_landscape_pixels(self):
        response = mock.Mock(
            content=b"landscape-original-bytes",
            headers={"Content-Type": "video/mp4"},
        )
        response.raise_for_status.return_value = None
        with mock.patch("media_prep.requests.get", return_value=response), \
             mock.patch("media_prep.audio_state", return_value="audible"), \
             mock.patch("media_prep._probe_dimensions", return_value=(1920, 1080)), \
             mock.patch("media_prep.video_layout", return_value="landscape"), \
             mock.patch("media_prep._to_9x16_fill") as convert, \
             mock.patch("media_prep._ensure_playable_audio", return_value=False), \
             mock.patch.dict(media_prep.os.environ, {"AUTO_ADD_AUDIO": "true"}, clear=True):
            name, content, content_type = media_prep.prepare_video(
                "https://media.example/wide.mp4", fill_9x16=True
            )
        convert.assert_not_called()
        self.assertEqual(content, b"landscape-original-bytes")
        self.assertEqual(name, "wide.mp4")
        self.assertEqual(content_type, "video/mp4")

    def test_prepare_video_fits_vertical_model_video_to_facebook_reel(self):
        response = mock.Mock(
            content=b"vertical-original-bytes",
            headers={"Content-Type": "video/mp4"},
        )
        response.raise_for_status.return_value = None

        def fake_fill(src, dest, platform=""):
            Path(dest).write_bytes(b"facebook-fitted")
            return True

        with mock.patch("media_prep.requests.get", return_value=response), \
             mock.patch("media_prep.audio_state", return_value="audible"), \
             mock.patch("media_prep._probe_dimensions", return_value=(720, 1280)), \
             mock.patch("media_prep.video_layout", return_value="vertical"), \
             mock.patch("media_prep._to_9x16_fill", side_effect=fake_fill) as convert, \
             mock.patch("media_prep._ensure_playable_audio", return_value=False), \
             mock.patch.dict(media_prep.os.environ, {"AUTO_ADD_AUDIO": "true"}, clear=True):
            _, content, _ = media_prep.prepare_video(
                "https://media.example/model.mp4",
                fill_9x16=True,
                platform="facebook",
            )
        convert.assert_called_once()
        self.assertEqual(convert.call_args.kwargs.get("platform"), "facebook")
        self.assertEqual(content, b"facebook-fitted")

    def test_prepare_video_fits_instagram_reels_with_safe_margin(self):
        response = mock.Mock(
            content=b"already-1080x1920",
            headers={"Content-Type": "video/mp4"},
        )
        response.raise_for_status.return_value = None

        def fake_fill(src, dest, platform=""):
            Path(dest).write_bytes(b"instagram-fitted")
            return True

        with mock.patch("media_prep.requests.get", return_value=response), \
             mock.patch("media_prep.audio_state", return_value="audible"), \
             mock.patch("media_prep._probe_dimensions", return_value=(1080, 1920)), \
             mock.patch("media_prep.video_layout", return_value="vertical"), \
             mock.patch("media_prep._to_9x16_fill", side_effect=fake_fill) as convert, \
             mock.patch("media_prep._ensure_playable_audio", return_value=False), \
             mock.patch.dict(media_prep.os.environ, {"AUTO_ADD_AUDIO": "true"}, clear=True):
            _, content, _ = media_prep.prepare_video(
                "https://media.example/model.mp4",
                fill_9x16=True,
                platform="instagram",
            )
        convert.assert_called_once()
        self.assertEqual(convert.call_args.kwargs.get("platform"), "instagram")
        self.assertEqual(content, b"instagram-fitted")

    def test_prepare_video_remuxes_original_sound_to_aac(self):
        response = mock.Mock(
            content=b"landscape-original-bytes",
            headers={"Content-Type": "video/mp4"},
        )
        response.raise_for_status.return_value = None

        def fake_remux(src, dest):
            Path(dest).write_bytes(b"aac-original-sound")
            return True

        with mock.patch("media_prep.requests.get", return_value=response), \
             mock.patch("media_prep.audio_state", return_value="audible"), \
             mock.patch("media_prep._probe_dimensions", return_value=(1920, 1080)), \
             mock.patch("media_prep.video_layout", return_value="landscape"), \
             mock.patch("media_prep._to_9x16_fill") as convert, \
             mock.patch("media_prep._ensure_playable_audio", side_effect=fake_remux) as remux, \
             mock.patch.dict(media_prep.os.environ, {"AUTO_ADD_AUDIO": "true"}, clear=True):
            _, content, _ = media_prep.prepare_video(
                "https://media.example/wide.mp4", fill_9x16=True, platform="facebook"
            )
        convert.assert_not_called()
        remux.assert_called_once()
        self.assertEqual(content, b"aac-original-sound")

    def test_display_rotation_uses_side_data(self):
        stream = {
            "width": 1920,
            "height": 1080,
            "tags": {},
            "side_data_list": [{"rotation": -90}],
        }
        self.assertEqual(media_prep._stream_rotation(stream), 90)


if __name__ == "__main__":
    unittest.main()

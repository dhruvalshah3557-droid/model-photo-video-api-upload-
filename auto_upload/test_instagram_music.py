import json
import sys
import types
import unittest
from unittest import mock

try:
    import requests  # noqa: F401
except ImportError:
    sys.modules["requests"] = types.SimpleNamespace(get=lambda *args, **kwargs: None)

from instagram_uploader import (
    InstagramRateLimitError,
    InstagramUploader,
    _COVER_PROBE_CACHE,
    _SOFT_AUDIO_QUERIES,
)


class _Response:
    status_code = 200

    def json(self):
        return {
            "data": [
                {"audio_id": f"track-{i}", "title": f"Track {i}"}
                for i in range(8)
            ]
        }


class _ContainerResponse:
    status_code = 200

    def json(self):
        return {"id": "container-1"}


class _RateLimitResponse:
    status_code = 400
    ok = False

    def json(self):
        return {"error": {
            "code": 4,
            "error_subcode": 2207051,
            "message": "Application request limit reached",
        }}


class _UploadResponse:
    status_code = 200
    ok = True

    def json(self):
        return {"success": True}


class InstagramMusicRotationTests(unittest.TestCase):
    def setUp(self):
        InstagramUploader._LAST_AUDIO_BY_ACCOUNT.clear()
        InstagramUploader._RATE_LIMIT_UNTIL = 0.0
        _COVER_PROBE_CACHE.clear()

    def test_catalog_results_rotate_instead_of_always_first(self):
        uploader = InstagramUploader.__new__(InstagramUploader)
        uploader.ig_user_id = "123"
        uploader.access_token = "token"
        uploader.page_name = "Colour Diam"
        keys = [f"https://media.example/reel-{i}.mp4" for i in range(30)]
        env = {
            "IG_AUDIO_SEARCH_QUERIES":
                "elegant instrumental,luxury piano,cinematic fashion,soft ambient"
        }
        with mock.patch.dict("os.environ", env, clear=True), \
             mock.patch("instagram_uploader.requests.get", return_value=_Response(), create=True):
            configurations = [
                json.loads(uploader._trending_audio_configuration(key))
                for key in keys
            ]
        self.assertGreaterEqual(len({c["audio_id"] for c in configurations}), 6)

    def test_twenty_five_soft_catalog_moods_are_available(self):
        self.assertEqual(len(_SOFT_AUDIO_QUERIES), 25)
        self.assertEqual(len(_SOFT_AUDIO_QUERIES), len(set(_SOFT_AUDIO_QUERIES)))

    def test_china_uses_chinese_or_soft_catalog_searches(self):
        uploader = InstagramUploader.__new__(InstagramUploader)
        uploader.ig_user_id = "china-account"
        uploader.access_token = "token"
        uploader.page_name = "Colour Diam China"
        with mock.patch.dict("os.environ", {}, clear=True), \
             mock.patch("instagram_uploader.requests.get", return_value=_Response(), create=True) as get:
            uploader._trending_audio_configuration("china-job")
        query = get.call_args.kwargs["params"]["search_query"]
        self.assertIn(query, {
            "China trending", "Chinese pop", "Chinese instrumental",
            *_SOFT_AUDIO_QUERIES,
        })

    def test_vietnam_uses_regional_searches(self):
        uploader = InstagramUploader.__new__(InstagramUploader)
        uploader.ig_user_id = "vietnam-account"
        uploader.access_token = "token"
        uploader.page_name = "Colour Diam Vietnam"
        with mock.patch.dict("os.environ", {}, clear=True), \
             mock.patch("instagram_uploader.requests.get", return_value=_Response(), create=True) as get:
            uploader._trending_audio_configuration("job-1")
        self.assertIn(get.call_args.kwargs["params"]["search_query"], {
            "Vietnam trending", "Vietnamese pop", "Vietnam luxury instrumental",
        })

    def test_hong_kong_short_name_uses_regional_searches(self):
        uploader = InstagramUploader.__new__(InstagramUploader)
        uploader.ig_user_id = "hong-kong-account"
        uploader.access_token = "token"
        uploader.page_name = "Colour Diam HK"
        with mock.patch.dict("os.environ", {}, clear=True), \
             mock.patch("instagram_uploader.requests.get", return_value=_Response(), create=True) as get:
            uploader._trending_audio_configuration("job-1")
        self.assertIn(get.call_args.kwargs["params"]["search_query"], {
            "Hong Kong trending", "Cantopop", "Hong Kong luxury instrumental",
        })

    def test_same_track_is_not_repeated_consecutively(self):
        uploader = InstagramUploader.__new__(InstagramUploader)
        uploader.ig_user_id = "123"
        uploader.access_token = "token"
        uploader.page_name = "Colour Diam Vietnam"
        with mock.patch.dict("os.environ", {}, clear=True), \
             mock.patch("instagram_uploader.requests.get", return_value=_Response(), create=True), \
             mock.patch("instagram_uploader.hashlib.sha256") as digest:
            digest.return_value.digest.return_value = bytes(32)
            first = json.loads(uploader._trending_audio_configuration("job-1"))["audio_id"]
            second = json.loads(uploader._trending_audio_configuration("job-2"))["audio_id"]
        self.assertNotEqual(first, second)

    def test_reel_uses_product_image_as_cover(self):
        uploader = InstagramUploader.__new__(InstagramUploader)
        uploader.ig_user_id = "123"
        uploader.access_token = "token"
        uploader.page_name = "Colour Diam Vietnam"
        uploader._ensure_not_rate_limited = lambda: None
        cover = "https://media.example/8732/center.jpg"
        with mock.patch.dict("os.environ", {"IG_AUTO_TRENDING_AUDIO": "false"}, clear=True), \
             mock.patch("instagram_uploader._cover_is_publicly_fetchable", return_value=True), \
             mock.patch("instagram_uploader.requests.post", return_value=_ContainerResponse()) as post:
            container = uploader._create_media_container(
                "https://media.example/8732/video.mp4",
                "caption",
                is_video=True,
                cover_url=cover,
            )
        self.assertEqual(container, "container-1")
        params = post.call_args.kwargs["data"]
        self.assertEqual(params["media_type"], "REELS")
        self.assertEqual(params["cover_url"], cover)
        self.assertNotIn("thumb_offset", params)

    def test_unfetchable_cover_uses_thumb_offset(self):
        uploader = InstagramUploader.__new__(InstagramUploader)
        uploader.ig_user_id = "123"
        uploader.access_token = "token"
        uploader.page_name = "Colour Diam Russia"
        uploader._ensure_not_rate_limited = lambda: None
        with mock.patch.dict("os.environ", {"IG_AUTO_TRENDING_AUDIO": "false"}, clear=True), \
             mock.patch("instagram_uploader._cover_is_publicly_fetchable", return_value=False), \
             mock.patch("instagram_uploader.requests.post", return_value=_ContainerResponse()) as post:
            uploader._create_media_container(
                "https://media.example/2556/vid.mp4",
                "caption",
                is_video=True,
                cover_url="https://www.colourdiam.com/Product/Jewellery/2556/white45/center.jpg",
            )
        params = post.call_args.kwargs["data"]
        self.assertEqual(params["thumb_offset"], 1000)
        self.assertNotIn("cover_url", params)

    def test_reel_without_image_avoids_black_first_frame(self):
        uploader = InstagramUploader.__new__(InstagramUploader)
        uploader.ig_user_id = "123"
        uploader.access_token = "token"
        uploader.page_name = "Colour Diam"
        uploader._ensure_not_rate_limited = lambda: None
        with mock.patch.dict("os.environ", {"IG_AUTO_TRENDING_AUDIO": "false"}, clear=True), \
             mock.patch("instagram_uploader.requests.post", return_value=_ContainerResponse()) as post:
            uploader._create_media_container(
                "https://media.example/video.mp4", "caption", is_video=True
            )
        self.assertEqual(post.call_args.kwargs["data"]["thumb_offset"], 1000)

    def test_force_video_handles_extensionless_model_video_url(self):
        uploader = InstagramUploader.__new__(InstagramUploader)
        uploader.ig_user_id = "123"
        uploader.access_token = "token"
        uploader.page_name = "Colour Diam"
        with mock.patch.object(uploader, "_create_resumable_reel", return_value="container") as create, \
             mock.patch.object(uploader, "_publish_container", return_value={"id": "post"}), \
             mock.patch("instagram_uploader.time.sleep"):
            uploader.upload("https://cdn.example/media?id=45", "caption", force_video=True)
        self.assertEqual(create.call_args.args[0], "https://cdn.example/media?id=45")

    def test_resumable_reel_preserves_attempt_on_rate_limit(self):
        uploader = InstagramUploader.__new__(InstagramUploader)
        uploader.ig_user_id = "123"
        uploader.access_token = "token"
        uploader.page_name = "Colour Diam"
        with mock.patch.dict("os.environ", {"IG_RATE_LIMIT_COOLDOWN_SECONDS": "3600"}), \
             mock.patch("instagram_uploader.prepare_video", return_value=("reel.mp4", b"video", "video/mp4")), \
             mock.patch("instagram_uploader.video_layout_from_bytes", return_value="vertical"), \
             mock.patch("instagram_uploader.requests.post", return_value=_RateLimitResponse()):
            with self.assertRaisesRegex(InstagramRateLimitError, "2207051"):
                uploader._create_resumable_reel("https://example.com/reel.mp4", "caption")
        self.assertGreater(InstagramUploader._RATE_LIMIT_UNTIL, 0)

    def test_resumable_reel_is_normalized_to_full_screen_9x16(self):
        uploader = InstagramUploader.__new__(InstagramUploader)
        uploader.ig_user_id = "123"
        uploader.access_token = "token"
        uploader.page_name = "Colour Diam Dubai"
        with mock.patch(
            "instagram_uploader.prepare_video",
            return_value=("reel.mp4", b"video", "video/mp4"),
        ) as prepare, mock.patch(
            "instagram_uploader.video_layout_from_bytes", return_value="vertical",
        ), mock.patch(
            "instagram_uploader.requests.post",
            side_effect=[_ContainerResponse(), _UploadResponse()],
        ) as post:
            uploader._create_resumable_reel(
                "https://example.com/reel.mp4", "caption"
            )

        self.assertTrue(prepare.call_args.kwargs["fill_9x16"])
        self.assertEqual(prepare.call_args.kwargs["platform"], "instagram")
        self.assertEqual(post.call_args_list[0].kwargs["data"]["media_type"], "REELS")
        self.assertEqual(
            prepare.call_args.kwargs["selection_key"],
            "instagram|123|https://example.com/reel.mp4",
        )

    def test_resumable_reel_skips_404_cover(self):
        uploader = InstagramUploader.__new__(InstagramUploader)
        uploader.ig_user_id = "123"
        uploader.access_token = "token"
        uploader.page_name = "Colour Diam Japan"
        with mock.patch(
            "instagram_uploader.prepare_video",
            return_value=("reel.mp4", b"video", "video/mp4"),
        ), mock.patch(
            "instagram_uploader.video_layout_from_bytes", return_value="vertical",
        ), mock.patch(
            "instagram_uploader._cover_is_publicly_fetchable", return_value=False
        ), mock.patch(
            "instagram_uploader.requests.post",
            side_effect=[_ContainerResponse(), _UploadResponse()],
        ) as post:
            uploader._create_resumable_reel(
                "https://example.com/reel.mp4",
                "caption",
                cover_url="https://www.colourdiam.com/Product/Jewellery/1135/white45/center.jpg",
            )
        params = post.call_args_list[0].kwargs["data"]
        self.assertEqual(params["thumb_offset"], 1000)
        self.assertNotIn("cover_url", params)

    def test_resumable_reel_retries_without_cover_after_meta_reject(self):
        class _CoverFailResponse:
            status_code = 400
            ok = False

            def json(self):
                return {"error": {
                    "message": "Only photo or video can be accepted as media type.",
                    "code": 9004,
                    "error_subcode": 2207052,
                }}

        uploader = InstagramUploader.__new__(InstagramUploader)
        uploader.ig_user_id = "123"
        uploader.access_token = "token"
        uploader.page_name = "Colour Diam Spain"
        with mock.patch(
            "instagram_uploader.prepare_video",
            return_value=("reel.mp4", b"video", "video/mp4"),
        ), mock.patch(
            "instagram_uploader.video_layout_from_bytes", return_value="vertical",
        ), mock.patch(
            "instagram_uploader._cover_is_publicly_fetchable", return_value=True
        ), mock.patch(
            "instagram_uploader.requests.post",
            side_effect=[_CoverFailResponse(), _ContainerResponse(), _UploadResponse()],
        ) as post:
            container = uploader._create_resumable_reel(
                "https://example.com/reel.mp4",
                "caption",
                cover_url="https://media.example/center.jpg",
            )
        self.assertEqual(container, "container-1")
        self.assertEqual(len(post.call_args_list), 3)
        retry_params = post.call_args_list[1].kwargs["data"]
        self.assertNotIn("cover_url", retry_params)
        self.assertEqual(retry_params["thumb_offset"], 1000)

    def test_unfetchable_image_falls_back_to_byte_upload(self):
        class _FetchFailResponse:
            status_code = 400
            ok = False

            def json(self):
                return {"error": {
                    "message": "Only photo or video can be accepted as media type.",
                    "code": 9004,
                    "error_subcode": 2207052,
                    "error_user_title": "Media download has failed. The media URI doesn't meet our requirements.",
                    "error_user_msg": "The media could not be fetched from this URI: https://www.colourdiam.com/Product/Diamond/4662/still.jpg",
                }}

        uploader = InstagramUploader.__new__(InstagramUploader)
        uploader.ig_user_id = "123"
        uploader.access_token = "token"
        uploader.page_name = "Colour Diam Myanmar"
        uploader._ensure_not_rate_limited = lambda: None
        with mock.patch.object(
            uploader, "_create_resumable_image", return_value="byte-container"
        ) as byte_upload, mock.patch(
            "instagram_uploader.requests.post", return_value=_FetchFailResponse()
        ):
            container = uploader._create_media_container(
                "https://www.colourdiam.com/Product/Diamond/4662/still.jpg",
                "caption",
            )
        self.assertEqual(container, "byte-container")
        byte_upload.assert_called_once()

    def test_missing_image_url_parameter_falls_back_to_byte_upload(self):
        class _MissingUrlResponse:
            status_code = 400
            ok = False

            def json(self):
                return {"error": {
                    "message": "(#100) The parameter image_url is required",
                    "code": 100,
                }}

        uploader = InstagramUploader.__new__(InstagramUploader)
        uploader.ig_user_id = "123"
        uploader.access_token = "token"
        uploader.page_name = "Colour Diam Vietnam"
        uploader._ensure_not_rate_limited = lambda: None
        with mock.patch.object(
            uploader, "_create_resumable_image", return_value="byte-container"
        ) as byte_upload, mock.patch(
            "instagram_uploader.requests.post", return_value=_MissingUrlResponse()
        ):
            container = uploader._create_media_container(
                "https://media.example/model.jpg",
                "caption",
            )
        self.assertEqual(container, "byte-container")
        byte_upload.assert_called_once()

    def test_landscape_resumable_video_uses_reel_and_shares_to_feed(self):
        uploader = InstagramUploader.__new__(InstagramUploader)
        uploader.ig_user_id = "123"
        uploader.access_token = "token"
        uploader.page_name = "Colour Diam Dubai"
        with mock.patch(
            "instagram_uploader.prepare_video",
            return_value=("wide.mp4", b"video", "video/mp4"),
        ), mock.patch(
            "instagram_uploader.video_layout_from_bytes", return_value="landscape",
        ), mock.patch(
            "instagram_uploader.requests.post",
            side_effect=[_ContainerResponse(), _UploadResponse()],
        ) as post:
            uploader._create_resumable_reel(
                "https://example.com/wide.mp4", "caption"
            )
        self.assertEqual(post.call_args_list[0].kwargs["data"]["media_type"], "REELS")
        self.assertEqual(post.call_args_list[0].kwargs["data"]["share_to_feed"], "true")
        self.assertNotIn("cover_url", post.call_args_list[0].kwargs["data"])

    def test_carousel_with_one_usable_child_publishes_as_single_post(self):
        uploader = InstagramUploader.__new__(InstagramUploader)
        uploader.ig_user_id = "123"
        uploader.access_token = "token"
        uploader.page_name = "Colour Diam Italy"
        uploader._ensure_not_rate_limited = lambda: None
        with mock.patch.object(
            uploader, "_create_media_container",
            side_effect=["child-1", Exception("unusable")],
        ), mock.patch.object(
            uploader, "upload", return_value={"id": "single-post"}
        ) as upload, mock.patch(
            "instagram_uploader.time.sleep"
        ):
            result = uploader.upload_carousel(
                [
                    "https://media.example/a.jpg",
                    "https://media.example/b.jpg",
                ],
                "caption",
            )
        self.assertEqual(result, {"id": "single-post"})
        upload.assert_called_once_with(
            "https://media.example/a.jpg", "caption", ""
        )


if __name__ == "__main__":
    unittest.main()

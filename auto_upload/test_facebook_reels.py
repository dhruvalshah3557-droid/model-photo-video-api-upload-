import sys
import types
import unittest
from unittest import mock

try:
    import requests  # noqa: F401
except ImportError:
    sys.modules["requests"] = types.SimpleNamespace()

from facebook_uploader import FacebookUploader


class _Response:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.ok = status_code < 400
        self.text = ""

    def json(self):
        return self._payload


class FacebookReelsTests(unittest.TestCase):
    def test_video_uses_native_reels_start_upload_finish_flow(self):
        uploader = FacebookUploader.__new__(FacebookUploader)
        uploader.page_id = "page-123"
        uploader.access_token = "page-token"
        uploader.page_name = "Colour Diam Test"

        responses = [
            _Response({"video_id": "reel-456", "upload_url": "https://rupload.test/reel"}),
            _Response({"success": True}),
            _Response({"success": True}),
        ]
        with mock.patch(
            "facebook_uploader.prepare_video",
            return_value=("source.mp4", b"video-with-audio", "video/mp4"),
        ) as prepare, mock.patch(
            "facebook_uploader.video_layout_from_bytes", return_value="vertical",
        ), mock.patch(
            "facebook_uploader.requests.post", side_effect=responses
        ) as post:
            result = uploader.upload_video(
                "https://media.test/source.mp4", "Caption", "product-1"
            )

        self.assertEqual(result["id"], "reel-456")
        self.assertEqual(prepare.call_args.kwargs.get("platform"), "facebook")
        self.assertTrue(prepare.call_args.kwargs.get("fill_9x16"))
        self.assertEqual(post.call_count, 3)

        start = post.call_args_list[0]
        self.assertTrue(start.args[0].endswith("/page-123/video_reels"))
        self.assertEqual(start.kwargs["data"]["upload_phase"], "start")

        upload = post.call_args_list[1]
        self.assertEqual(upload.args[0], "https://rupload.test/reel")
        self.assertEqual(upload.kwargs["headers"]["file_size"], str(len(b"video-with-audio")))
        self.assertEqual(upload.kwargs["data"], b"video-with-audio")

        finish = post.call_args_list[2]
        self.assertTrue(finish.args[0].endswith("/page-123/video_reels"))
        self.assertEqual(finish.kwargs["data"]["upload_phase"], "finish")
        self.assertEqual(finish.kwargs["data"]["video_state"], "PUBLISHED")
        self.assertEqual(finish.kwargs["data"]["video_id"], "reel-456")
        self.assertNotIn("product_tags", finish.kwargs["data"])

    def test_landscape_video_keeps_original_size_on_page_videos(self):
        uploader = FacebookUploader.__new__(FacebookUploader)
        uploader.page_id = "page-123"
        uploader.access_token = "page-token"
        uploader.page_name = "Colour Diam Test"

        with mock.patch(
            "facebook_uploader.prepare_video",
            return_value=("source.mp4", b"landscape-video", "video/mp4"),
        ), mock.patch(
            "facebook_uploader.video_layout_from_bytes", return_value="landscape",
        ), mock.patch(
            "facebook_uploader.requests.post",
            return_value=_Response({"id": "video-789"}),
        ) as post:
            result = uploader.upload_video(
                "https://media.test/wide.mp4", "Caption", "product-1"
            )

        self.assertEqual(result["id"], "video-789")
        self.assertEqual(post.call_count, 1)
        self.assertTrue(post.call_args.args[0].endswith("/page-123/videos"))
        self.assertEqual(
            post.call_args.kwargs["files"]["source"][1], b"landscape-video"
        )


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
import os
import sys
import types
import unittest
import uuid
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

def _install(name, attrs=None):
    mod = types.ModuleType(name)
    for k, v in (attrs or {}).items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    parent = sys.modules.get(".".join(name.split(".")[:-1]))
    if parent is not None:
        setattr(parent, name.split(".")[-1], mod)
    return mod


gspread = _install("gspread", {})
gspread.exceptions = types.SimpleNamespace(APIError=Exception)
_install("oauth2client")
_install("oauth2client.service_account", {"ServiceAccountCredentials": lambda *a, **k: None})
_install("dotenv", {"load_dotenv": lambda *a, **k: None})
_install("requests", {
    "get": lambda *a, **k: None,
    "post": lambda *a, **k: None,
    "RequestException": Exception,
    "Timeout": type("Timeout", (Exception,), {}),
    "ConnectionError": type("ConnectionError", (Exception,), {}),
})
_install("google_auth_oauthlib")
_install("google_auth_oauthlib.flow", {"InstalledAppFlow": lambda *a, **k: None})
_install("google.auth")
_install("google.auth.transport")
_install("google.auth.transport.requests", {"Request": lambda *a, **k: None})
_install("google.oauth2")
_install("google.oauth2.credentials", {"Credentials": lambda *a, **k: None})
_install("googleapiclient")
_install("googleapiclient.discovery", {"build": lambda *a, **k: None})
_install("googleapiclient.http", {"MediaIoBaseUpload": lambda *a, **k: None})

from line_uploader import LineUploader
from sheets_reader import SheetsReader
import optimized_runner


class LineUploaderTests(unittest.TestCase):
    def test_upload_sends_caption_then_image(self):
        uploader = LineUploader("token", "Colour Diam LINE")
        payload = {"sentMessages": [{"id": "m1"}]}
        with patch("line_uploader.requests.post") as post:
            post.return_value = MagicMock(status_code=200, json=lambda: payload, text="")
            result = uploader.upload(
                "https://example.com/center.jpg",
                caption="Hello LINE",
                is_video=False,
            )
        self.assertEqual(result["id"], "m1")
        messages = post.call_args.kwargs["json"]["messages"]
        self.assertEqual(messages[0], {"type": "text", "text": "Hello LINE"})
        self.assertEqual(messages[1]["type"], "image")
        self.assertEqual(messages[1]["originalContentUrl"], "https://example.com/center.jpg")
        retry_key = post.call_args.kwargs["headers"]["X-Line-Retry-Key"]
        self.assertEqual(str(uuid.UUID(retry_key)), retry_key)

    def test_empty_http_200_body_is_success(self):
        uploader = LineUploader("token", "Colour Diam LINE")
        with patch("line_uploader.requests.post") as post:
            post.return_value = MagicMock(
                status_code=200,
                json=lambda: {},
                text="",
                headers={"x-line-request-id": "line-request-123"},
            )
            result = uploader.upload(
                "https://example.com/center.jpg",
                caption="Accepted broadcast",
                is_video=False,
            )
        self.assertEqual(result["id"], "line-request-123")

    def test_carousel_rejects_non_https(self):
        uploader = LineUploader("token")
        with self.assertRaises(Exception) as ctx:
            uploader.upload_carousel(["http://insecure.example/a.jpg"])
        self.assertIn("HTTPS", str(ctx.exception))

    def test_video_requires_https_preview_image(self):
        uploader = LineUploader("token")
        with self.assertRaises(Exception) as ctx:
            uploader.upload(
                "https://example.com/clip.mp4",
                caption="v",
                is_video=True,
                thumbnail_url="https://example.com/clip.mp4",
            )
        self.assertIn("preview", str(ctx.exception).lower())

    def test_video_with_jpeg_preview(self):
        uploader = LineUploader("token")
        payload = {"sentMessages": [{"id": "v1"}]}
        with patch("line_uploader.requests.post") as post:
            post.return_value = MagicMock(status_code=200, json=lambda: payload, text="")
            result = uploader.upload(
                "https://example.com/clip.mp4",
                caption="Video",
                is_video=True,
                thumbnail_url="https://example.com/thumb.jpg",
            )
        self.assertEqual(result["id"], "v1")
        messages = post.call_args.kwargs["json"]["messages"]
        self.assertEqual(messages[1]["type"], "video")
        self.assertEqual(messages[1]["previewImageUrl"], "https://example.com/thumb.jpg")


class EnableLineHelperTests(unittest.TestCase):
    def test_col_letter_matches_sheets_a1(self):
        self.assertEqual(SheetsReader._col_letter(1), "A")
        self.assertEqual(SheetsReader._col_letter(9), "I")
        self.assertEqual(SheetsReader._col_letter(20), "T")
        self.assertEqual(SheetsReader._col_letter(26), "Z")
        self.assertEqual(SheetsReader._col_letter(27), "AA")


class LineProductionSlotTests(unittest.TestCase):
    def test_platform_limits_exclude_line_while_quota_exhausted(self):
        slots = optimized_runner._platform_limits(50)
        self.assertEqual(slots["line"], 0)
        self.assertEqual(sum(slots.values()), 50)

    def test_platform_limits_skip_line_when_budget_is_tiny(self):
        slots = optimized_runner._platform_limits(1)
        self.assertEqual(slots["line"], 0)

    def test_healthy_candidates_skip_line_while_quota_exhausted(self):
        job = {
            "job_id": "100-LINE-CD-carousel",
            "account_id": "LINE-CD",
            "platform": "line",
            "sku": "100",
            "row": 3,
            "attempts": 0,
            "notes": "",
        }
        sheets = types.SimpleNamespace(update_job=lambda *a, **k: None)
        accounts = {
            "LINE-CD": {"enabled": True, "platform": "line", "timezone": "Asia/Bangkok"},
        }
        sources = {"100": {"sku": "100"}}
        with patch("optimized_runner.resolve_media_fixed", return_value=["https://example.com/a.jpg"]), \
             patch("optimized_runner._dns_resolves", return_value=True), \
             patch("optimized_runner.main._classify_media_url", return_value="image"), \
             patch("optimized_runner._local_slot_due", return_value=True), \
             patch("optimized_runner._video_validation_reason", return_value=""):
            selected = optimized_runner._healthy_candidates(
                [job], accounts, sources, sheets, limit=50,
            )
        self.assertEqual(selected, [])


if __name__ == "__main__":
    unittest.main()

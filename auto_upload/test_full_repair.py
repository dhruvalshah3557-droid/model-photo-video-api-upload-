#!/usr/bin/env python3
import os
import sys
import types
import unittest
from unittest.mock import patch
from datetime import datetime, timezone

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


if "dotenv" not in sys.modules:
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

import optimized_runner
from job_generator import is_model_media


class FullRepairTests(unittest.TestCase):
    def test_model_photo_is_resolved_as_independent_upload(self):
        source = {
            "model_images": [
                "https://colourdiam.com/Product/Model%20Photo%20Video/298/1.jpeg",
                "https://colourdiam.com/Product/Model%20Photo%20Video/298/2.jpeg",
            ]
        }
        media = optimized_runner.resolve_media_fixed(
            {"platform": "facebook", "media_selection": "model_photo:1"},
            source,
        )
        self.assertEqual(
            media[0],
            "https://colourdiam.com/Product/Jewellery/Model%20images/298/2.jpeg",
        )
        self.assertIn(
            "https://colourdiam.com/Product/Model%20Photo%20Video/298/2.jpeg",
            media,
        )

    def test_model_media_is_preferred_over_product_carousel(self):
        jobs = [
            {"job_id": "1-FB-CD-carousel", "sku": "1", "account_id": "FB-CD", "platform": "facebook", "format": "carousel", "media_selection": "carousel", "row": 30, "attempts": 0, "notes": ""},
            {"job_id": "2-FB-CD-model_photo-0", "sku": "2", "account_id": "FB-CD", "platform": "facebook", "format": "carousel", "media_selection": "model_photo:0", "row": 20, "attempts": 0, "notes": ""},
        ]
        accounts = {"FB-CD": {"enabled": True, "platform": "facebook", "timezone": "Asia/Bangkok"}}
        sources = {
            "1": {"main_image": "https://media.example/product.jpg", "side_images": []},
            "2": {"model_images": ["https://media.example/model.jpg"]},
        }
        sheets = types.SimpleNamespace(update_job=lambda *args: None)
        with patch("optimized_runner._platform_limits", return_value={"facebook": 1, "instagram": 0, "youtube": 0, "line": 0}), \
             patch("optimized_runner._local_slot_due", return_value=True), \
             patch("optimized_runner._rotation_rank", return_value=0), \
             patch("optimized_runner._is_clean_source", return_value=(True, "")), \
             patch("optimized_runner._media_preflight_reason", return_value=""):
            selected = optimized_runner._healthy_candidates(
                jobs, accounts, sources, sheets, limit=1
            )
        self.assertEqual(selected[0]["media_selection"], "model_photo:0")

    def test_model_video_outranks_model_photo_and_product_media(self):
        jobs = [
            {"job_id": "1-FB-CD-carousel", "sku": "1", "account_id": "FB-CD", "platform": "facebook", "format": "carousel", "media_selection": "carousel", "row": 40, "attempts": 0, "notes": ""},
            {"job_id": "1-FB-CD-product_video", "sku": "1", "account_id": "FB-CD", "platform": "facebook", "format": "video", "media_selection": "product_video", "row": 30, "attempts": 0, "notes": ""},
            {"job_id": "1-FB-CD-model_photo-0", "sku": "1", "account_id": "FB-CD", "platform": "facebook", "format": "carousel", "media_selection": "model_photo:0", "row": 20, "attempts": 0, "notes": ""},
            {"job_id": "1-FB-CD-model_video-0", "sku": "1", "account_id": "FB-CD", "platform": "facebook", "format": "video", "media_selection": "model_video:0", "row": 10, "attempts": 1, "notes": ""},
        ]
        accounts = {"FB-CD": {"enabled": True, "platform": "facebook", "timezone": "Asia/Bangkok"}}
        sources = {
            "1": {
                "main_image": "https://media.example/product.jpg",
                "side_images": [],
                "video_url": "https://media.example/product.mp4",
                "model_images": ["https://media.example/model.jpg"],
                "model_videos": ["https://media.example/model.mp4"],
            },
        }
        sheets = types.SimpleNamespace(update_job=lambda *args: None)
        with patch("optimized_runner._platform_limits", return_value={"facebook": 1, "instagram": 0, "youtube": 0, "line": 0}), \
             patch("optimized_runner._local_slot_due", return_value=True), \
             patch("optimized_runner._rotation_rank", return_value=0), \
             patch("optimized_runner._is_clean_source", return_value=(True, "")), \
             patch("optimized_runner._media_preflight_reason", return_value=""):
            selected = optimized_runner._healthy_candidates(
                jobs, accounts, sources, sheets, limit=1
            )
        self.assertEqual(selected[0]["media_selection"], "model_video:0")

    def test_instagram_model_photo_outranks_product_carousel(self):
        jobs = [
            {"job_id": "9-IG-CD-carousel", "sku": "9", "account_id": "IG-CD", "platform": "instagram", "format": "carousel", "media_selection": "carousel", "row": 30, "attempts": 0, "notes": ""},
            {"job_id": "9-IG-CD-product_video", "sku": "9", "account_id": "IG-CD", "platform": "instagram", "format": "video", "media_selection": "product_video", "row": 20, "attempts": 0, "notes": ""},
            {"job_id": "9-IG-CD-model_photo-0", "sku": "9", "account_id": "IG-CD", "platform": "instagram", "format": "carousel", "media_selection": "model_photo:0", "row": 10, "attempts": 0, "notes": ""},
        ]
        accounts = {
            "IG-CD": {
                "enabled": True,
                "platform": "instagram",
                "platform_account_id": "17841400000000000",
                "timezone": "Asia/Bangkok",
            }
        }
        sources = {
            "9": {
                "main_image": "https://media.example/product.jpg",
                "side_images": [],
                "video_url": "https://media.example/product.mp4",
                "model_images": ["https://media.example/model.jpg"],
            },
        }
        sheets = types.SimpleNamespace(update_job=lambda *args: None)
        with patch("optimized_runner._platform_limits", return_value={"facebook": 0, "instagram": 1, "youtube": 0, "line": 0}), \
             patch("optimized_runner._local_slot_due", return_value=True), \
             patch("optimized_runner._rotation_rank", return_value=0), \
             patch("optimized_runner._is_clean_source", return_value=(True, "")), \
             patch("optimized_runner._media_preflight_reason", return_value=""):
            selected = optimized_runner._healthy_candidates(
                jobs, accounts, sources, sheets, limit=1
            )
        self.assertEqual(selected[0]["media_selection"], "model_photo:0")

    def test_linked_facebook_and_instagram_market_prefer_same_sku(self):
        jobs = [
            {"job_id": "101-FB-MMR-model_photo-0", "sku": "101", "account_id": "FB-MMR", "platform": "facebook", "format": "carousel", "media_selection": "model_photo:0", "row": 10, "attempts": 0, "notes": ""},
            {"job_id": "202-IG-MMR-model_photo-0", "sku": "202", "account_id": "IG-MMR", "platform": "instagram", "format": "carousel", "media_selection": "model_photo:0", "row": 30, "attempts": 0, "notes": ""},
            {"job_id": "101-IG-MMR-model_photo-0", "sku": "101", "account_id": "IG-MMR", "platform": "instagram", "format": "carousel", "media_selection": "model_photo:0", "row": 20, "attempts": 0, "notes": ""},
        ]
        accounts = {
            "FB-MMR": {"enabled": True, "platform": "facebook", "timezone": "Asia/Yangon"},
            "IG-MMR": {"enabled": True, "platform": "instagram", "platform_account_id": "17841430974311329", "timezone": "Asia/Yangon"},
        }
        sources = {
            "101": {"model_images": ["https://media.example/101.jpg"]},
            "202": {"model_images": ["https://media.example/202.jpg"]},
        }
        sheets = types.SimpleNamespace(update_job=lambda *args: None)
        with patch("optimized_runner._platform_limits", return_value={"facebook": 1, "instagram": 1, "youtube": 0, "line": 0}), \
             patch("optimized_runner._local_slot_due", return_value=True), \
             patch("optimized_runner._rotation_rank", return_value=0), \
             patch("optimized_runner._is_clean_source", return_value=(True, "")), \
             patch("optimized_runner._media_preflight_reason", return_value=""):
            selected = optimized_runner._healthy_candidates(
                jobs, accounts, sources, sheets, limit=2
            )

        self.assertEqual(
            [(job["account_id"], job["sku"]) for job in selected],
            [("FB-MMR", "101"), ("IG-MMR", "101")],
        )

    def test_same_product_different_format_is_blocked_per_account(self):
        records = [{
            "job_id": "298-IG-KUWAIT-carousel",
            "sku": "298",
            "account_id": "IG-KUWAIT",
            "platform": "instagram",
            "status": "uploaded",
            "notes": "",
        }]
        queue_ws = types.SimpleNamespace(get_all_records=lambda head: records)
        queue_sheets = types.SimpleNamespace(queue_ws=queue_ws, queue_header_row=1)
        reserved, _ = optimized_runner._queue_state(queue_sheets)

        duplicate_video = {
            "job_id": "298-IG-KUWAIT-product_video",
            "sku": "298",
            "account_id": "IG-KUWAIT",
            "platform": "instagram",
            "format": "video",
            "media_selection": "product_video",
            "row": 9,
            "attempts": 0,
            "notes": "",
        }
        updates = []
        sheets = types.SimpleNamespace(
            update_job=lambda job, values: updates.append((job, values))
        )
        accounts = {
            "IG-KUWAIT": {
                "enabled": True,
                "platform": "instagram",
                "platform_account_id": "17841436113237015",
                "timezone": "Asia/Kuwait",
            }
        }
        with patch("optimized_runner._local_slot_due", return_value=True), \
             patch("optimized_runner._rotation_rank", return_value=0):
            selected = optimized_runner._healthy_candidates(
                [duplicate_video], accounts, {}, sheets, limit=1,
                reserved_fingerprints=reserved,
            )

        self.assertEqual(selected, [])

    def test_sheets_numeric_sku_still_blocks_same_product_on_page(self):
        records = [{
            "job_id": "1135-FB-CD-carousel",
            "sku": 1135.0,
            "account_id": "FB-CD",
            "platform": "facebook",
            "status": "uploaded",
            "notes": "",
        }]
        queue_ws = types.SimpleNamespace(get_all_records=lambda head: records)
        queue_sheets = types.SimpleNamespace(queue_ws=queue_ws, queue_header_row=1)
        reserved, _ = optimized_runner._queue_state(queue_sheets)

        duplicate_video = {
            "job_id": "1135-FB-CD-product_video",
            "sku": "1135",
            "account_id": "FB-CD",
            "platform": "facebook",
            "format": "video",
            "media_selection": "product_video",
            "row": 9,
            "attempts": 0,
            "notes": "",
        }
        updates = []
        sheets = types.SimpleNamespace(
            update_job=lambda job, values: updates.append((job, values))
        )
        accounts = {
            "FB-CD": {
                "enabled": True,
                "platform": "facebook",
                "timezone": "Asia/Bangkok",
            }
        }
        with patch("optimized_runner._local_slot_due", return_value=True), \
             patch("optimized_runner._rotation_rank", return_value=0):
            selected = optimized_runner._healthy_candidates(
                [duplicate_video], accounts, {}, sheets, limit=1,
                reserved_fingerprints=reserved,
            )

        self.assertEqual(selected, [])
        self.assertEqual(updates, [])

    def test_numeric_sku_still_finds_source_and_pairs_instagram(self):
        jobs = [
            {
                "job_id": "1135-FB-MMR-carousel",
                "sku": 1135.0,
                "account_id": "FB-MMR",
                "platform": "facebook",
                "format": "carousel",
                "media_selection": "model_photo:0",
                "row": 10,
                "attempts": 0,
                "notes": "",
            },
            {
                "job_id": "202-IG-MMR-carousel",
                "sku": "202",
                "account_id": "IG-MMR",
                "platform": "instagram",
                "format": "carousel",
                "media_selection": "model_photo:0",
                "row": 30,
                "attempts": 0,
                "notes": "",
            },
            {
                "job_id": "1135-IG-MMR-carousel",
                "sku": "1135",
                "account_id": "IG-MMR",
                "platform": "instagram",
                "format": "carousel",
                "media_selection": "model_photo:0",
                "row": 20,
                "attempts": 0,
                "notes": "",
            },
        ]
        accounts = {
            "FB-MMR": {"enabled": True, "platform": "facebook", "timezone": "Asia/Yangon"},
            "IG-MMR": {
                "enabled": True,
                "platform": "instagram",
                "platform_account_id": "17841430974311329",
                "timezone": "Asia/Yangon",
            },
        }
        sources = {
            "1135": {"model_images": ["https://media.example/1135.jpg"]},
            "202": {"model_images": ["https://media.example/202.jpg"]},
        }
        sheets = types.SimpleNamespace(update_job=lambda *args: None)
        with patch("optimized_runner._platform_limits", return_value={"facebook": 1, "instagram": 1, "youtube": 0, "line": 0}), \
             patch("optimized_runner._local_slot_due", return_value=True), \
             patch("optimized_runner._rotation_rank", return_value=0), \
             patch("optimized_runner._is_clean_source", return_value=(True, "")), \
             patch("optimized_runner._media_preflight_reason", return_value=""):
            selected = optimized_runner._healthy_candidates(
                jobs, accounts, sources, sheets, limit=2
            )

        self.assertEqual(
            [(job["account_id"], optimized_runner._job_sku(job)) for job in selected],
            [("FB-MMR", "1135"), ("IG-MMR", "1135")],
        )

    def test_caption_preflight_replaces_mismatched_copy_instead_of_skipping(self):
        jobs = [
            {"job_id": "1263-FB-TURKEY-carousel", "sku": "1263", "account_id": "FB-TURKEY",
             "platform": "facebook", "format": "carousel", "media_selection": "model_photo:0",
             "row": 30, "attempts": 0, "notes": ""},
            {"job_id": "1913-FB-TURKEY-carousel", "sku": "1913", "account_id": "FB-TURKEY",
             "platform": "facebook", "format": "carousel", "media_selection": "model_photo:0",
             "row": 10, "attempts": 0, "notes": ""},
        ]
        accounts = {
            "FB-TURKEY": {
                "enabled": True,
                "platform": "facebook",
                "timezone": "Europe/Istanbul",
                "primary_language": "vi-VN",
            }
        }
        sources = {
            "1263": {
                "sku": "1263",
                "lang_captions": {"vi": "A 0.05 ct Light Bluish Gray Round diamond."},
                "product_name": "0.11ct Fancy Deep Pink Marquise GIA Natural Diamond",
                "details": "Sku - 1263\nWeight - 0.11\nShape - Marquise",
                "product_link": "https://colourdiam.com/Product/Diamond/1263/",
                "model_images": ["https://media.example/1263.jpg"],
            },
            "1913": {
                "sku": "1913",
                "lang_captions": {"vi": "Mo ta tieng Viet"},
                "product_name": "Ring",
                "model_images": ["https://media.example/1913.jpg"],
            },
        }
        updates = []
        sheets = types.SimpleNamespace(
            update_job=lambda job, values: updates.append((job, values))
        )
        with patch("optimized_runner._platform_limits", return_value={"facebook": 1, "instagram": 0, "youtube": 0, "line": 0}), \
             patch("optimized_runner._local_slot_due", return_value=True), \
             patch("optimized_runner._rotation_rank", return_value=0), \
             patch("optimized_runner._is_clean_source", return_value=(True, "")), \
             patch("optimized_runner._media_preflight_reason", return_value=""):
            selected = optimized_runner._healthy_candidates(
                jobs, accounts, sources, sheets, limit=1
            )
        self.assertEqual(selected[0]["sku"], "1263")
        self.assertEqual(updates, [])

    def test_instagram_carousel_does_not_embed_product_video(self):
        source = {
            "main_image": "https://media.example/center.jpg",
            "certificate_media_url": "https://media.example/certificate.jpg",
            "side_images": ["https://media.example/side.jpg"],
            "video_url": "https://media.example/product.mp4",
        }
        job = {
            "platform": "instagram",
            "media_selection": "carousel",
        }
        media = optimized_runner.resolve_media_fixed(job, source)
        self.assertEqual(media, [
            "https://media.example/center.jpg",
            "https://media.example/certificate.jpg",
            "https://media.example/side.jpg",
        ])
        self.assertNotIn(source["video_url"], media)

    def test_regional_instagram_does_not_match_generic_colour_diam_page(self):
        uploader = optimized_runner.main.InstagramUploader
        uploader._IG_ID_CACHE.clear()

        def response(data):
            return types.SimpleNamespace(
                json=lambda: {"data": data},
                raise_for_status=lambda: None,
            )

        generic = [{
            "id": "PAGE-CD",
            "name": "Colour Diam",
            "instagram_business_account": {
                "id": "IG-CD",
                "username": "colourdiamonds",
            },
        }]
        with patch("instagram_uploader.requests.get", return_value=response(generic)):
            with self.assertRaises(optimized_runner.main.IGAccountNotLinkedError):
                uploader._resolve_ig_user_id("", "token", "Colour Diam Sweden")

        linked = [{
            "id": "PAGE-SWEDEN",
            "name": "Regional Page",
            "instagram_business_account": {
                "id": "IG-SWEDEN-ID",
                "username": "colourdiamsweden",
            },
        }]
        with patch("instagram_uploader.requests.get", return_value=response(linked)):
            self.assertEqual(
                uploader._resolve_ig_user_id("", "token", "Colour Diam Sweden"),
                "IG-SWEDEN-ID",
            )

    def test_blank_active_instagram_does_not_consume_valid_account_slot(self):
        jobs = [
            {
                "job_id": "1-IG-BLANK-model_photo-0",
                "account_id": "IG-BLANK",
                "platform": "instagram",
                "sku": "1",
                "row": 2,
                "attempts": 0,
                "notes": "",
                "media_selection": "model_photo:0",
            },
            {
                "job_id": "1-IG-VALID-model_photo-0",
                "account_id": "IG-VALID",
                "platform": "instagram",
                "sku": "1",
                "row": 3,
                "attempts": 0,
                "notes": "",
                "media_selection": "model_photo:0",
            },
        ]
        accounts = {
            "IG-BLANK": {
                "enabled": True,
                "platform": "instagram",
                "platform_account_id": "",
                "account_name": "Colour Diam Sweden",
                "credential_property_key": "META_TOKEN_IG_SWEDEN",
                "timezone": "Europe/Stockholm",
            },
            "IG-VALID": {
                "enabled": True,
                "platform": "instagram",
                "platform_account_id": "17841400000000000",
                "account_name": "Colour Diam Valid",
                "credential_property_key": "META_TOKEN_IG_VALID",
                "timezone": "UTC",
            },
        }
        sources = {"1": {"sku": "1", "model_images": ["https://media.example/center.jpg"]}}
        sheets = types.SimpleNamespace(update_job=lambda *args, **kwargs: None)

        with patch("optimized_runner.Config.get_token", return_value=""), \
             patch("optimized_runner.resolve_media_fixed", return_value=["https://media.example/center.jpg"]), \
             patch("optimized_runner._dns_resolves", return_value=True), \
             patch("optimized_runner.main._classify_media_url", return_value="image"):
            selected = optimized_runner._healthy_candidates(
                jobs,
                accounts,
                sources,
                sheets,
                limit=20,
                recent_upload_activity={},
            )

        self.assertEqual(
            [job["account_id"] for job in selected],
            ["IG-VALID"],
        )


    def test_deep_backlog_scan_keeps_fresh_jobs_and_reaches_oldest(self):
        jobs = [{"job_id": str(i), "media_selection": "model_photo:0"} for i in range(1000)]
        sampled = optimized_runner._account_scan_jobs(jobs, limit=300)
        self.assertEqual(len(sampled), 300)
        self.assertEqual([j["job_id"] for j in sampled[:100]], [str(i) for i in range(100)])
        self.assertEqual(sampled[-1]["job_id"], "999")
        self.assertGreater(len({j["job_id"] for j in sampled}), 290)

    def test_extensionless_video_is_forced_through_ffprobe_preflight(self):
        url = "https://media.example/download?id=broken-video"
        with patch("optimized_runner._dns_resolves", return_value=True), \
             patch("optimized_runner.main._classify_media_url", return_value="unknown"), \
             patch("optimized_runner._video_validation_reason",
                   return_value="audio-only source") as validate:
            reason = optimized_runner._media_preflight_reason(
                [url], force_video=True
            )

        self.assertIn("audio-only source", reason)
        validate.assert_called_once_with(url, force=True)

    def test_recent_instagram_rate_limit_pauses_instagram_only(self):
        now = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)
        jobs = [{
            "platform": "instagram",
            "error_message": "META_RATE_LIMIT code=4 subcode=2207051",
            "last_attempt_at": "2026-09-09T11:55:00",
        }]
        active, wait = optimized_runner._instagram_rate_limit_active(jobs, now)
        self.assertTrue(active)
        self.assertGreater(wait, 0)

    def test_instagram_rate_limit_reads_notes_when_error_message_missing(self):
        now = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)
        jobs = [{
            "platform": "instagram",
            "error_message": "",
            "notes": "Meta application limit; cooldown active, retry preserved META_RATE_LIMIT",
            "last_attempt_at": "2026-09-09T11:55:00",
        }]
        active, wait = optimized_runner._instagram_rate_limit_active(jobs, now)
        self.assertTrue(active)
        self.assertGreater(wait, 0)

    def test_caption_blockers_are_revived_for_retry(self):
        updates = []
        records = [
            {
                "status": "needs_review",
                "account_id": "IG-SWEDEN",
                "error_message": "Missing required sv regional caption; refusing English fallback",
                "notes": "Auto-cleaned: regional caption preflight failed",
            },
            {
                "status": "failed",
                "account_id": "FB-CD",
                "error_message": "oauth",
                "notes": "",
            },
        ]
        sheets = types.SimpleNamespace(
            queue_header_row=1,
            queue_ws=types.SimpleNamespace(get_all_records=lambda head=1: records),
            update_job=lambda job, payload: updates.append((job, payload)),
        )
        accounts = {
            "IG-SWEDEN": {"enabled": True, "platform": "instagram"},
            "FB-CD": {"enabled": True, "platform": "facebook"},
        }
        revived = optimized_runner._revive_caption_blockers(sheets, accounts)
        self.assertEqual(revived, 1)
        self.assertEqual(updates[0][1]["status"], "pending")
        self.assertIn("native regional caption fallback", updates[0][1]["notes"])

    def test_known_unusable_pending_jobs_are_skipped_so_healthy_work_is_found(self):
        jobs = []
        for row in range(1, 40):
            jobs.append({
                "job_id": "bad-%s-YT-CD-product_video" % row,
                "account_id": "YT-CD",
                "platform": "youtube",
                "media_selection": "model_video:0",
                "sku": "bad-%s" % row,
                "row": row,
                "attempts": 0,
                "notes": "",
                "error_message": "Media preflight failed: all media URLs are unavailable, dead, DNS-invalid, or not media",
            })
        jobs.append({
            "job_id": "good-YT-CD-product_video",
            "account_id": "YT-CD",
            "platform": "youtube",
            "media_selection": "model_video:0",
            "sku": "good",
            "row": 40,
            "attempts": 0,
            "notes": "",
            "error_message": "",
        })
        accounts = {
            "YT-CD": {
                "enabled": True,
                "platform": "youtube",
                "platform_account_id": "UCTWbcY-YtvAx2QUZKXt230A",
                "timezone": "Asia/Bangkok",
            }
        }
        sources = {
            "good": {"sku": "good", "main_image": "https://media.example/good.jpg"},
        }
        sheets = types.SimpleNamespace(update_job=lambda *args, **kwargs: None)
        with patch("optimized_runner._local_slot_due", return_value=True), \
             patch("optimized_runner._rotation_rank", return_value=0), \
             patch("optimized_runner._is_clean_source", return_value=(True, "")), \
             patch("optimized_runner.resolve_media_fixed", return_value=["https://media.example/good.mp4"]), \
             patch("optimized_runner._dns_resolves", return_value=True), \
             patch("optimized_runner.main._classify_media_url", return_value="video"), \
             patch("optimized_runner._video_validation_reason", return_value=""):
            selected = optimized_runner._healthy_candidates(
                jobs, accounts, sources, sheets, limit=1,
            )
        self.assertEqual([job["sku"] for job in selected], ["good"])

    def test_youtube_video_jobs_survive_product_image_integrity_mismatch(self):
        job = {
            "job_id": "4403-YT-CD-product_video",
            "account_id": "YT-CD",
            "platform": "youtube",
            "sku": "4403",
            "media_selection": "model_video:0",
            "format": "video",
            "row": 1,
            "attempts": 0,
            "notes": "",
        }
        accounts = {
            "YT-CD": {
                "enabled": True,
                "platform": "youtube",
                "platform_account_id": "UCTWbcY-YtvAx2QUZKXt230A",
                "timezone": "Asia/Bangkok",
            }
        }
        sources = {
            "4403": {
                "sku": "4403",
                "integrity_error": (
                    "main image belongs to another SKU "
                    "(https://www.colourdiam.com/Product/Jewellery/4377/white45/center.jpg)"
                ),
                "video_url": "https://colourdiam.com/Product/Jewellery/4403/white45/vid.mp4",
            }
        }
        sheets = types.SimpleNamespace(update_job=lambda *args, **kwargs: None)
        with patch("optimized_runner._local_slot_due", return_value=True), \
             patch("optimized_runner._rotation_rank", return_value=0), \
             patch(
                 "optimized_runner.resolve_media_fixed",
                 return_value=["https://colourdiam.com/Product/Jewellery/4403/white45/vid.mp4"],
             ), \
             patch("optimized_runner._dns_resolves", return_value=True), \
             patch("optimized_runner.main._classify_media_url", return_value="video"), \
             patch("optimized_runner._video_validation_reason", return_value=""):
            selected = optimized_runner._healthy_candidates(
                [job], accounts, sources, sheets, limit=1,
            )
        self.assertEqual([item["sku"] for item in selected], ["4403"])

    def test_model_media_is_not_blocked_by_product_account_lock(self):
        records = [{
            "job_id": "298-IG-KUWAIT-carousel",
            "sku": "298",
            "account_id": "IG-KUWAIT",
            "platform": "instagram",
            "status": "uploaded",
            "notes": "",
        }]
        queue_ws = types.SimpleNamespace(get_all_records=lambda head: records)
        reserved, _ = optimized_runner._queue_state(
            types.SimpleNamespace(queue_ws=queue_ws, queue_header_row=1)
        )
        model_video = {
            "job_id": "298-IG-KUWAIT-model_video-0",
            "sku": "298",
            "account_id": "IG-KUWAIT",
            "platform": "instagram",
            "format": "video",
            "media_selection": "model_video:0",
            "row": 9,
            "attempts": 0,
            "notes": "",
        }
        sources = {
            "298": {
                "sku": "298",
                "model_videos": ["https://media.example/298-model.mp4"],
                "main_image": "https://media.example/298.jpg",
            }
        }
        accounts = {
            "IG-KUWAIT": {
                "enabled": True,
                "platform": "instagram",
                "platform_account_id": "17841436113237015",
                "timezone": "Asia/Kuwait",
            }
        }
        sheets = types.SimpleNamespace(update_job=lambda *a, **k: None)
        with patch("optimized_runner._local_slot_due", return_value=True), \
             patch("optimized_runner._rotation_rank", return_value=0), \
             patch("optimized_runner._is_clean_source", return_value=(True, "")), \
             patch("optimized_runner._media_preflight_reason", return_value=""):
            selected = optimized_runner._healthy_candidates(
                [model_video], accounts, sources, sheets, limit=1,
                reserved_fingerprints=reserved,
            )
        self.assertEqual(selected[0]["media_selection"], "model_video:0")

    def test_instagram_pairing_does_not_outrank_model_media(self):
        jobs = [
            {"job_id": "101-FB-MMR-model_photo-0", "sku": "101", "account_id": "FB-MMR",
             "platform": "facebook", "format": "carousel", "media_selection": "model_photo:0",
             "row": 10, "attempts": 0, "notes": ""},
            {"job_id": "101-IG-MMR-carousel", "sku": "101", "account_id": "IG-MMR",
             "platform": "instagram", "format": "carousel", "media_selection": "carousel",
             "row": 20, "attempts": 0, "notes": ""},
            {"job_id": "202-IG-MMR-model_video-0", "sku": "202", "account_id": "IG-MMR",
             "platform": "instagram", "format": "video", "media_selection": "model_video:0",
             "row": 5, "attempts": 0, "notes": ""},
        ]
        accounts = {
            "FB-MMR": {"enabled": True, "platform": "facebook", "timezone": "Asia/Yangon"},
            "IG-MMR": {
                "enabled": True, "platform": "instagram",
                "platform_account_id": "17841430974311329", "timezone": "Asia/Yangon",
            },
        }
        sources = {
            "101": {"model_images": ["https://media.example/101.jpg"]},
            "202": {"model_videos": ["https://media.example/202-model.mp4"]},
        }
        sheets = types.SimpleNamespace(update_job=lambda *args: None)
        with patch("optimized_runner._platform_limits", return_value={"facebook": 1, "instagram": 1, "youtube": 0, "line": 0}), \
             patch("optimized_runner._local_slot_due", return_value=True), \
             patch("optimized_runner._rotation_rank", return_value=0), \
             patch("optimized_runner._is_clean_source", return_value=(True, "")), \
             patch("optimized_runner._media_preflight_reason", return_value=""):
            selected = optimized_runner._healthy_candidates(
                jobs, accounts, sources, sheets, limit=2
            )
        self.assertEqual(
            [(job["account_id"], job["media_selection"]) for job in selected],
            [("FB-MMR", "model_photo:0"), ("IG-MMR", "model_video:0")],
        )

    def test_account_scan_keeps_model_media_outside_fresh_window(self):
        jobs = [
            {"job_id": "carousel-%s" % i, "media_selection": "carousel"}
            for i in range(400)
        ]
        jobs.append({
            "job_id": "model-late",
            "media_selection": "model_photo:0",
        })
        sampled = optimized_runner._account_scan_jobs(jobs, limit=300)
        self.assertIn("model-late", [job["job_id"] for job in sampled])

    def test_leftover_slots_stay_on_model_photo_video(self):
        jobs = [
            {"job_id": "1-FB-CD-carousel", "sku": "1", "account_id": "FB-CD", "platform": "facebook", "format": "carousel", "media_selection": "carousel", "row": 40, "attempts": 0, "notes": ""},
            {"job_id": "1-FB-CD-model_photo-0", "sku": "1", "account_id": "FB-CD", "platform": "facebook", "format": "carousel", "media_selection": "model_photo:0", "row": 20, "attempts": 0, "notes": ""},
            {"job_id": "1-FB-CD-model_video-0", "sku": "1", "account_id": "FB-CD", "platform": "facebook", "format": "video", "media_selection": "model_video:0", "row": 10, "attempts": 0, "notes": ""},
            {"job_id": "1-FB-CD-model_video-1", "sku": "1", "account_id": "FB-CD", "platform": "facebook", "format": "video", "media_selection": "model_video:1", "row": 11, "attempts": 0, "notes": ""},
        ]
        accounts = {"FB-CD": {"enabled": True, "platform": "facebook", "timezone": "Asia/Bangkok"}}
        sources = {
            "1": {
                "main_image": "https://media.example/product.jpg",
                "side_images": [],
                "model_images": ["https://media.example/model.jpg"],
                "model_videos": ["https://media.example/model0.mp4", "https://media.example/model1.mp4"],
            },
        }
        sheets = types.SimpleNamespace(update_job=lambda *args: None)
        with patch("optimized_runner._platform_limits", return_value={"facebook": 3, "instagram": 0, "youtube": 0, "line": 0}), \
             patch("optimized_runner._local_slot_due", return_value=True), \
             patch("optimized_runner._rotation_rank", return_value=0), \
             patch("optimized_runner._is_clean_source", return_value=(True, "")), \
             patch("optimized_runner._media_preflight_reason", return_value=""):
            selected = optimized_runner._healthy_candidates(
                jobs, accounts, sources, sheets, limit=3
            )
        self.assertEqual(
            {job["media_selection"] for job in selected},
            {"model_video:0", "model_video:1", "model_photo:0"},
        )
        self.assertTrue(all(is_model_media(job) for job in selected))

    def test_unused_youtube_slots_fill_with_model_media_not_product_carousels(self):
        jobs = [
            {"job_id": "1-FB-CD-carousel", "sku": "1", "account_id": "FB-CD", "platform": "facebook", "format": "carousel", "media_selection": "carousel", "row": 40, "attempts": 0, "notes": ""},
            {"job_id": "1-FB-CD-model_photo-0", "sku": "1", "account_id": "FB-CD", "platform": "facebook", "format": "carousel", "media_selection": "model_photo:0", "row": 20, "attempts": 0, "notes": ""},
            {"job_id": "1-FB-CD-model_video-0", "sku": "1", "account_id": "FB-CD", "platform": "facebook", "format": "video", "media_selection": "model_video:0", "row": 10, "attempts": 0, "notes": ""},
            {"job_id": "1-FB-CD-model_video-1", "sku": "1", "account_id": "FB-CD", "platform": "facebook", "format": "video", "media_selection": "model_video:1", "row": 11, "attempts": 0, "notes": ""},
            {
                "job_id": "yt-dead",
                "sku": "9",
                "account_id": "YT-CD",
                "platform": "youtube",
                "format": "video",
                "media_selection": "model_video:0",
                "row": 1,
                "attempts": 1,
                "notes": "invalid_grant",
                "error_message": "invalid_grant",
                "last_attempt_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
            },
        ]
        accounts = {
            "FB-CD": {"enabled": True, "platform": "facebook", "timezone": "Asia/Bangkok"},
            "YT-CD": {"enabled": True, "platform": "youtube", "timezone": "Asia/Bangkok", "platform_account_id": "UCtest"},
        }
        sources = {
            "1": {
                "main_image": "https://media.example/product.jpg",
                "side_images": [],
                "model_images": ["https://media.example/model.jpg"],
                "model_videos": ["https://media.example/model0.mp4", "https://media.example/model1.mp4"],
            },
            "9": {"model_videos": ["https://media.example/yt.mp4"]},
        }
        sheets = types.SimpleNamespace(update_job=lambda *args: None)
        with patch("optimized_runner._platform_limits", return_value={"facebook": 1, "instagram": 0, "youtube": 2, "line": 0}), \
             patch("optimized_runner._local_slot_due", return_value=True), \
             patch("optimized_runner._rotation_rank", return_value=0), \
             patch("optimized_runner._is_clean_source", return_value=(True, "")), \
             patch("optimized_runner._media_preflight_reason", return_value=""), \
             patch("optimized_runner.main.build_caption", return_value="ok"), \
             patch("optimized_runner._youtube_auth_dead_accounts", return_value={"YT-CD"}):
            selected = optimized_runner._healthy_candidates(
                jobs, accounts, sources, sheets, limit=3
            )
        self.assertEqual(len(selected), 3)
        self.assertTrue(all(is_model_media(job) for job in selected))
        self.assertEqual({job["account_id"] for job in selected}, {"FB-CD"})
        self.assertNotIn("carousel", {job["media_selection"] for job in selected})

    def test_account_scan_ignores_product_jobs(self):
        jobs = [
            {"job_id": "model-%s" % i, "media_selection": "model_video:0"}
            for i in range(400)
        ]
        jobs.append({
            "job_id": "product-healthy",
            "media_selection": "carousel",
        })
        sampled = optimized_runner._account_scan_jobs(jobs, limit=300)
        self.assertNotIn("product-healthy", [job["job_id"] for job in sampled])
        self.assertLessEqual(len(sampled), 300)
        self.assertTrue(all(
            str(job.get("media_selection", "")).startswith("model_")
            for job in sampled
        ))

    def test_account_scan_keeps_model_photos_when_model_videos_fill_window(self):
        jobs = [
            {"job_id": "model-video-%s" % i, "media_selection": "model_video:0"}
            for i in range(400)
        ]
        jobs.append({
            "job_id": "model-photo-late",
            "media_selection": "model_photo:0",
        })
        jobs.append({
            "job_id": "product-carousel",
            "media_selection": "carousel",
        })
        sampled = optimized_runner._account_scan_jobs(jobs, limit=300)
        sampled_ids = [job["job_id"] for job in sampled]
        self.assertIn("model-photo-late", sampled_ids)
        self.assertNotIn("product-carousel", sampled_ids)
        self.assertLessEqual(len(sampled), 300)

    def test_healthy_candidates_select_model_photo_when_model_videos_fail_preflight(self):
        jobs = [
            {
                "job_id": "model-%s-FB-BKK-model_video-0" % i,
                "sku": "bad-%s" % i,
                "account_id": "FB-BKK",
                "platform": "facebook",
                "format": "video",
                "media_selection": "model_video:0",
                "row": i,
                "attempts": 0,
                "notes": "",
            }
            for i in range(1, 301)
        ]
        jobs.append({
            "job_id": "good-FB-BKK-model_photo-0",
            "sku": "good",
            "account_id": "FB-BKK",
            "platform": "facebook",
            "format": "carousel",
            "media_selection": "model_photo:0",
            "row": 998,
            "attempts": 0,
            "notes": "",
        })
        jobs.append({
            "job_id": "good-FB-BKK-carousel",
            "sku": "good",
            "account_id": "FB-BKK",
            "platform": "facebook",
            "format": "carousel",
            "media_selection": "carousel",
            "row": 999,
            "attempts": 0,
            "notes": "",
        })
        accounts = {
            "FB-BKK": {"enabled": True, "platform": "facebook", "timezone": "Asia/Bangkok"},
        }
        sources = {job["sku"]: {"sku": job["sku"]} for job in jobs}
        sheets = types.SimpleNamespace(update_job=lambda *args, **kwargs: None)

        def preflight(media, force_video=False):
            url = str((media or [""])[0])
            if "good" in url:
                return ""
            return "video failed validation"

        with patch("optimized_runner._platform_limits", return_value={"facebook": 1, "instagram": 0, "youtube": 0, "line": 0}), \
             patch("optimized_runner._local_slot_due", return_value=True), \
             patch("optimized_runner._rotation_rank", return_value=0), \
             patch("optimized_runner._is_clean_source", return_value=(True, "")), \
             patch("optimized_runner.resolve_media_fixed", side_effect=lambda job, source: ["https://media.example/%s.mp4" % job["sku"]]), \
             patch("optimized_runner._media_preflight_reason", side_effect=preflight), \
             patch("optimized_runner.main.build_caption", return_value="ok"):
            selected = optimized_runner._healthy_candidates(
                jobs, accounts, sources, sheets, limit=1
            )
        self.assertEqual([job["media_selection"] for job in selected], ["model_photo:0"])

    def test_leftover_slots_prefer_model_photos_after_dead_model_videos(self):
        jobs = [
            {
                "job_id": "bad-%s-FB-CD-model_video-0" % i,
                "sku": "bad-%s" % i,
                "account_id": "FB-CD",
                "platform": "facebook",
                "format": "video",
                "media_selection": "model_video:0",
                "row": i,
                "attempts": 0,
                "notes": "",
            }
            for i in range(1, 12)
        ]
        jobs.extend([
            {
                "job_id": "good-FB-CD-model_photo-0",
                "sku": "good-photo",
                "account_id": "FB-CD",
                "platform": "facebook",
                "format": "carousel",
                "media_selection": "model_photo:0",
                "row": 20,
                "attempts": 0,
                "notes": "",
            },
            {
                "job_id": "good-FB-CD-model_photo-1",
                "sku": "good-photo-2",
                "account_id": "FB-CD",
                "platform": "facebook",
                "format": "carousel",
                "media_selection": "model_photo:1",
                "row": 21,
                "attempts": 0,
                "notes": "",
            },
            {
                "job_id": "good-FB-CD-carousel",
                "sku": "good-product",
                "account_id": "FB-CD",
                "platform": "facebook",
                "format": "carousel",
                "media_selection": "carousel",
                "row": 99,
                "attempts": 0,
                "notes": "",
            },
        ])
        accounts = {"FB-CD": {"enabled": True, "platform": "facebook", "timezone": "Asia/Bangkok"}}
        sources = {job["sku"]: {"sku": job["sku"]} for job in jobs}
        sheets = types.SimpleNamespace(update_job=lambda *args, **kwargs: None)

        def preflight(media, force_video=False):
            url = str((media or [""])[0])
            if "good" in url:
                return ""
            return "video failed validation"

        with patch("optimized_runner._platform_limits", return_value={"facebook": 2, "instagram": 0, "youtube": 0, "line": 0}), \
             patch("optimized_runner._local_slot_due", return_value=True), \
             patch("optimized_runner._rotation_rank", return_value=0), \
             patch("optimized_runner._is_clean_source", return_value=(True, "")), \
             patch("optimized_runner.resolve_media_fixed", side_effect=lambda job, source: ["https://media.example/%s.jpg" % job["sku"]]), \
             patch("optimized_runner._media_preflight_reason", side_effect=preflight), \
             patch("optimized_runner.main.build_caption", return_value="ok"):
            selected = optimized_runner._healthy_candidates(
                jobs, accounts, sources, sheets, limit=2
            )
        self.assertEqual(
            {job["media_selection"] for job in selected},
            {"model_photo:0", "model_photo:1"},
        )
        self.assertTrue(all(is_model_media(job) for job in selected))

    def test_healthy_candidates_select_model_photo_when_model_videos_fail_integrity(self):
        jobs = [
            {
                "job_id": "model-%s-FB-BKK-model_video-0" % i,
                "sku": "bad-%s" % i,
                "account_id": "FB-BKK",
                "platform": "facebook",
                "format": "video",
                "media_selection": "model_video:0",
                "row": i,
                "attempts": 0,
                "notes": "",
            }
            for i in range(1, 12)
        ]
        jobs.append({
            "job_id": "good-FB-BKK-model_photo-0",
            "sku": "good",
            "account_id": "FB-BKK",
            "platform": "facebook",
            "format": "carousel",
            "media_selection": "model_photo:0",
            "row": 998,
            "attempts": 0,
            "notes": "",
        })
        accounts = {
            "FB-BKK": {"enabled": True, "platform": "facebook", "timezone": "Asia/Bangkok"},
        }
        sources = {job["sku"]: {"sku": job["sku"]} for job in jobs}
        sheets = types.SimpleNamespace(update_job=lambda *args, **kwargs: None)

        def clean_source(source, job=None):
            selection = str((job or {}).get("media_selection", "") or "")
            if selection.startswith("model_video:"):
                return False, "Source row integrity mismatch - product link belongs to another SKU"
            return True, ""

        with patch("optimized_runner._platform_limits", return_value={"facebook": 1, "instagram": 0, "youtube": 0, "line": 0}), \
             patch("optimized_runner._local_slot_due", return_value=True), \
             patch("optimized_runner._rotation_rank", return_value=0), \
             patch("optimized_runner._is_clean_source", side_effect=clean_source), \
             patch("optimized_runner.resolve_media_fixed", side_effect=lambda job, source: ["https://media.example/%s.jpg" % job["sku"]]), \
             patch("optimized_runner._media_preflight_reason", return_value=""), \
             patch("optimized_runner.main.build_caption", return_value="ok"):
            selected = optimized_runner._healthy_candidates(
                jobs, accounts, sources, sheets, limit=1
            )
        self.assertEqual([job["media_selection"] for job in selected], ["model_photo:0"])

    def test_healthy_candidates_do_not_select_product_when_model_backlog_fails_preflight(self):
        jobs = [
            {
                "job_id": "model-%s-FB-BKK-model_video-0" % i,
                "sku": "bad-%s" % i,
                "account_id": "FB-BKK",
                "platform": "facebook",
                "format": "video",
                "media_selection": "model_video:0",
                "row": i,
                "attempts": 0,
                "notes": "",
            }
            for i in range(1, 301)
        ]
        jobs.append({
            "job_id": "good-FB-BKK-carousel",
            "sku": "good",
            "account_id": "FB-BKK",
            "platform": "facebook",
            "format": "carousel",
            "media_selection": "carousel",
            "row": 999,
            "attempts": 0,
            "notes": "",
        })
        accounts = {
            "FB-BKK": {"enabled": True, "platform": "facebook", "timezone": "Asia/Bangkok"},
        }
        sources = {job["sku"]: {"sku": job["sku"]} for job in jobs}
        sheets = types.SimpleNamespace(update_job=lambda *args, **kwargs: None)

        def preflight(media, force_video=False):
            url = str((media or [""])[0])
            if "good" in url:
                return ""
            return "video failed validation"

        with patch("optimized_runner._platform_limits", return_value={"facebook": 1, "instagram": 0, "youtube": 0, "line": 0}), \
             patch("optimized_runner._local_slot_due", return_value=True), \
             patch("optimized_runner._rotation_rank", return_value=0), \
             patch("optimized_runner._is_clean_source", return_value=(True, "")), \
             patch("optimized_runner.resolve_media_fixed", side_effect=lambda job, source: ["https://media.example/%s.mp4" % job["sku"]]), \
             patch("optimized_runner._media_preflight_reason", side_effect=preflight), \
             patch("optimized_runner.main.build_caption", return_value="ok"):
            selected = optimized_runner._healthy_candidates(
                jobs, accounts, sources, sheets, limit=1
            )
        self.assertEqual(selected, [])

    def test_skipped_model_jobs_are_revived_after_product_duplicate_lock(self):
        updates = []
        records = [{
            "status": "skipped",
            "account_id": "FB-TURKEY",
            "media_selection": "model_video:0",
            "notes": "Duplicate product already uploaded to this account in another media format",
        }]
        sheets = types.SimpleNamespace(
            queue_header_row=1,
            queue_ws=types.SimpleNamespace(get_all_records=lambda head=1: records),
            update_job=lambda job, payload: updates.append((job, payload)),
        )
        accounts = {"FB-TURKEY": {"enabled": True, "platform": "facebook"}}
        revived = optimized_runner._revive_skipped_model_jobs(sheets, accounts)
        self.assertEqual(revived, 1)
        self.assertEqual(updates[0][1]["status"], "pending")
        self.assertIn("model media", updates[0][1]["notes"].lower())

    def test_model_videos_stay_on_model_photo_video_folder(self):
        urls = optimized_runner._rewrite_media_urls(
            [
                "https://colourdiam.com/Product/Jewellery/Model%20images/298/video.mp4",
                "https://colourdiam.com/Product/Model%20Photo%20Video/298/1.jpeg",
            ],
        )
        self.assertEqual(
            urls[0],
            "https://colourdiam.com/Product/Model%20Photo%20Video/298/video.mp4",
        )
        self.assertEqual(
            urls[1],
            "https://colourdiam.com/Product/Model%20Photo%20Video/298/1.jpeg",
        )
        self.assertNotIn("Jewellery/Model%20images", urls[0])

    def test_model_media_preflight_jobs_are_revived_for_live_fallback(self):
        updates = []
        records = [{
            "status": "needs_review",
            "account_id": "FB-JPN",
            "media_selection": "model_video:0",
            "error_message": "Media preflight failed: all media URLs are unavailable, dead, DNS-invalid, or not media",
            "notes": "Auto-cleaned: media failed production preflight",
        }]
        sheets = types.SimpleNamespace(
            queue_header_row=1,
            queue_ws=types.SimpleNamespace(get_all_records=lambda head=1: records),
            update_job=lambda job, payload: updates.append((job, payload)),
        )
        accounts = {"FB-JPN": {"enabled": True, "platform": "facebook"}}
        revived = optimized_runner._revive_model_media_preflight_jobs(sheets, accounts)
        self.assertEqual(revived, 1)
        self.assertEqual(updates[0][1]["status"], "pending")
        self.assertIn("model photo video", updates[0][1]["notes"].lower())

    def test_model_photo_video_http_fallback_uses_jewellery_model_images(self):
        from sheets_reader import SheetsReader

        canonical = "https://colourdiam.com/Product/Model%20Photo%20Video/298/video.mp4"
        self.assertEqual(
            SheetsReader._model_media_http_fallback(canonical, "298"),
            "https://colourdiam.com/Product/Jewellery/Model%20images/298/video.mp4",
        )
        urls = optimized_runner._rewrite_media_urls(
            [canonical],
            include_fallbacks=True,
        )
        self.assertEqual(urls[0], "https://colourdiam.com/Product/Jewellery/Model%20images/298/video.mp4")
        self.assertIn(canonical, urls)
        media = optimized_runner.resolve_media_fixed(
            {"platform": "facebook", "media_selection": "model_video:0", "sku": "414"},
            {"sku": "414", "model_videos": [canonical.replace("/298/", "/414/")]},
        )
        self.assertEqual(
            media[0],
            "https://colourdiam.com/Product/Jewellery/Model%20images/414/video.mp4",
        )

    def test_facebook_first_pass_is_one_job_per_account(self):
        jobs = []
        accounts = {}
        sources = {}
        for aid in ("FB-A", "FB-B", "FB-C"):
            accounts[aid] = {"enabled": True, "platform": "facebook", "timezone": "Asia/Bangkok"}
            sources["1"] = {
                "model_videos": ["https://media.example/model0.mp4", "https://media.example/model1.mp4"],
                "model_images": ["https://media.example/model.jpg"],
            }
            jobs.extend([
                {"job_id": "1-%s-model_video-0" % aid, "sku": "1", "account_id": aid, "platform": "facebook", "format": "video", "media_selection": "model_video:0", "row": 10, "attempts": 0, "notes": ""},
                {"job_id": "1-%s-model_video-1" % aid, "sku": "1", "account_id": aid, "platform": "facebook", "format": "video", "media_selection": "model_video:1", "row": 11, "attempts": 0, "notes": ""},
                {"job_id": "1-%s-model_photo-0" % aid, "sku": "1", "account_id": aid, "platform": "facebook", "format": "carousel", "media_selection": "model_photo:0", "row": 20, "attempts": 0, "notes": ""},
            ])
        sheets = types.SimpleNamespace(update_job=lambda *args: None)
        with patch("optimized_runner._platform_limits", return_value={"facebook": 3, "instagram": 0, "youtube": 0, "line": 0}), \
             patch("optimized_runner._local_slot_due", return_value=True), \
             patch("optimized_runner._rotation_rank", return_value=0), \
             patch("optimized_runner._is_clean_source", return_value=(True, "")), \
             patch("optimized_runner._media_preflight_reason", return_value=""):
            selected = optimized_runner._healthy_candidates(
                jobs, accounts, sources, sheets, limit=3
            )
        self.assertEqual(len(selected), 3)
        self.assertEqual({job["account_id"] for job in selected}, {"FB-A", "FB-B", "FB-C"})
        self.assertTrue(all(is_model_media(job) for job in selected))

    def test_still_jpg_preflight_also_tries_center_image(self):
        urls = optimized_runner._rewrite_media_urls(
            ["https://www.colourdiam.com/Product/Diamond/9542/still.jpg"],
            include_fallbacks=True,
        )
        self.assertEqual(urls[0], "https://www.colourdiam.com/Product/Diamond/9542/still.jpg")
        self.assertIn(
            "https://www.colourdiam.com/Product/Diamond/9542/center.jpg",
            urls,
        )

    def test_pre_publish_timeout_is_not_ambiguous_delivery(self):
        exc = Exception(
            "Media validation failed (https://colourdiam.com/Product/Jewellery/461/white45/video.mp4): ffprobe timed out"
        )
        self.assertTrue(optimized_runner._is_pre_publish_error(exc))
        self.assertFalse(optimized_runner._is_ambiguous_delivery_error(exc))
        self.assertFalse(
            optimized_runner._is_ambiguous_delivery_error(
                Exception("Connection broken: IncompleteRead(16384 bytes read)")
            )
        )
        self.assertTrue(
            optimized_runner._is_ambiguous_delivery_error(
                optimized_runner.main.DeliveryUncertainError("pending")
            )
        )

    def test_pre_publish_holds_are_revived_for_retry(self):
        updates = []
        records = [{
            "status": "hold",
            "account_id": "FB-FRANCE",
            "error_message": "Media validation failed (https://colourdiam.com/Product/Jewellery/461/white45/video.mp4): ffprobe timed out",
            "notes": "IDEMPOTENCY_LOCK:1 | DELIVERY_UNCERTAIN:ffprobe timed out",
        }]
        sheets = types.SimpleNamespace(
            queue_header_row=1,
            queue_ws=types.SimpleNamespace(get_all_records=lambda head=1: records),
            update_job=lambda job, payload: updates.append((job, payload)),
        )
        accounts = {"FB-FRANCE": {"enabled": True, "platform": "facebook"}}
        revived = optimized_runner._revive_pre_publish_holds(sheets, accounts)
        self.assertEqual(revived, 1)
        self.assertEqual(updates[0][1]["status"], "pending")
        self.assertIn("pre-publish", updates[0][1]["notes"])

    def test_transient_ffprobe_timeout_does_not_park_video(self):
        optimized_runner._VIDEO_VALIDATION_CACHE.clear()
        with patch("optimized_runner.main.validate_media_url", return_value="ffprobe timed out"):
            self.assertEqual(
                optimized_runner._video_validation_reason("https://media.example/video.mp4"),
                "",
            )
        self.assertNotIn("https://media.example/video.mp4", optimized_runner._VIDEO_VALIDATION_CACHE)

    def test_unfetchable_media_jobs_are_revived_for_byte_upload(self):
        updates = []
        records = [{
            "status": "needs_review",
            "account_id": "IG-MMR",
            "error_message": "Only photo or video can be accepted as media type.",
            "notes": "The media could not be fetched from this URI: https://www.colourdiam.com/Product/Diamond/9542/still.jpg",
        }]
        sheets = types.SimpleNamespace(
            queue_header_row=1,
            queue_ws=types.SimpleNamespace(get_all_records=lambda head=1: records),
            update_job=lambda job, payload: updates.append((job, payload)),
        )
        accounts = {"IG-MMR": {"enabled": True, "platform": "instagram"}}
        revived = optimized_runner._revive_unfetchable_media_jobs(sheets, accounts)
        self.assertEqual(revived, 1)
        self.assertEqual(updates[0][1]["status"], "pending")
        self.assertIn("byte-upload", updates[0][1]["notes"])

    def test_recent_youtube_oauth_failure_does_not_block_other_channel(self):
        now = datetime(2026, 9, 29, 14, 0, tzinfo=timezone.utc)
        jobs = [
            {
                "job_id": "2281-YT-JIYA-model_video-0",
                "account_id": "YT-JIYA",
                "platform": "youtube",
                "sku": "2281",
                "media_selection": "model_video:0",
                "format": "video",
                "row": 1,
                "attempts": 1,
                "notes": "",
                "error_message": "invalid_grant: Bad Request",
                "last_attempt_at": "2026-09-29T13:55:25Z",
            },
            {
                "job_id": "2281-YT-JIYA-model_video-1",
                "account_id": "YT-JIYA",
                "platform": "youtube",
                "sku": "2281",
                "media_selection": "model_video:1",
                "format": "video",
                "row": 2,
                "attempts": 0,
                "notes": "",
                "error_message": "",
            },
            {
                "job_id": "2056-YT-CD-model_video-0",
                "account_id": "YT-CD",
                "platform": "youtube",
                "sku": "2056",
                "media_selection": "model_video:0",
                "format": "video",
                "row": 3,
                "attempts": 0,
                "notes": "",
                "error_message": "",
            },
        ]
        accounts = {
            "YT-JIYA": {
                "enabled": True,
                "platform": "youtube",
                "platform_account_id": "UCDFzWfIRvLu1LXq3mQrkj6Q",
                "timezone": "Asia/Bangkok",
            },
            "YT-CD": {
                "enabled": True,
                "platform": "youtube",
                "platform_account_id": "UCTWbcY-YtvAx2QUZKXt230A",
                "timezone": "Asia/Bangkok",
            },
        }
        sources = {
            "2281": {"sku": "2281", "model_videos": ["https://media.example/jiya.mp4"]},
            "2056": {"sku": "2056", "model_videos": ["https://media.example/cd.mp4"]},
        }
        sheets = types.SimpleNamespace(update_job=lambda *args, **kwargs: None)
        with patch("optimized_runner._youtube_auth_dead_accounts", return_value={"YT-JIYA"}), \
             patch("optimized_runner._platform_limits", return_value={"facebook": 0, "instagram": 0, "youtube": 1, "line": 0}), \
             patch("optimized_runner._local_slot_due", return_value=True), \
             patch("optimized_runner._rotation_rank", side_effect=lambda aid, *a, **k: 0 if aid == "YT-JIYA" else 1), \
             patch("optimized_runner._is_clean_source", return_value=(True, "")), \
             patch("optimized_runner.resolve_media_fixed", side_effect=lambda job, source: source.get("model_videos") or []), \
             patch("optimized_runner._dns_resolves", return_value=True), \
             patch("optimized_runner.main._classify_media_url", return_value="video"), \
             patch("optimized_runner._video_validation_reason", return_value=""):
            selected = optimized_runner._healthy_candidates(
                jobs, accounts, sources, sheets, limit=1,
            )
        self.assertEqual([job["account_id"] for job in selected], ["YT-CD"])
        self.assertEqual(
            optimized_runner._youtube_auth_dead_accounts(jobs, now=now),
            {"YT-JIYA"},
        )

    def test_stale_youtube_oauth_error_does_not_quarantine_channel(self):
        now = datetime(2026, 9, 29, 14, 0, tzinfo=timezone.utc)
        jobs = [{
            "account_id": "YT-CD",
            "platform": "youtube",
            "error_message": "invalid_grant: Bad Request",
            "last_attempt_at": "2026-09-22T23:35:38Z",
        }]
        self.assertEqual(
            optimized_runner._youtube_auth_dead_accounts(jobs, now=now),
            set(),
        )

    def test_youtube_token_cache_is_isolated_per_client(self):
        from youtube_uploader import token_cache_path
        colour = token_cache_path("colour-client", "colour-refresh")
        jiya = token_cache_path("shared-client", "jiya-refresh")
        self.assertNotEqual(colour, jiya)
        self.assertTrue(str(colour).endswith(".pickle"))
        self.assertIn("youtube_token_", colour.name)


if __name__ == "__main__":
    unittest.main()

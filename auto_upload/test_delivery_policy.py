#!/usr/bin/env python3
import os
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

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

import delivery_policy
import optimized_runner


def _primary(account_id, platform, enabled=True, platform_account_id="123"):
    return {
        "account_id": account_id,
        "platform": platform,
        "enabled": enabled,
        "platform_account_id": platform_account_id,
        "timezone": "UTC",
    }


class DeliveryPolicyTests(unittest.TestCase):
    def test_floor_is_eight_posts_with_half_hour_gap(self):
        self.assertEqual(delivery_policy.MINIMUM_POSTS_24H, 8)
        self.assertEqual(delivery_policy.MINIMUM_GAP_HOURS, 0.5)
        self.assertEqual(len(optimized_runner.LOCAL_POSTING_SLOTS), 11)
        self.assertTrue(delivery_policy.LINE_QUOTA_EXHAUSTED)

    def test_slot_eligible_skips_line_placeholders_and_disabled(self):
        self.assertTrue(delivery_policy.slot_eligible(_primary("FB-CD", "facebook")))
        self.assertTrue(delivery_policy.slot_eligible(_primary("IG-CD", "instagram")))
        self.assertTrue(delivery_policy.slot_eligible(_primary("YT-CD", "youtube")))
        self.assertFalse(delivery_policy.slot_eligible(_primary(
            "IG-SWEDEN", "instagram", platform_account_id="",
        )))
        self.assertFalse(delivery_policy.slot_eligible({
            "account_id": "LINE-CD",
            "platform": "line",
            "enabled": True,
            "platform_account_id": "",
        }))
        self.assertFalse(delivery_policy.slot_eligible(_primary(
            "YT-JIYA", "youtube", enabled=False,
        )))

    def test_parse_queue_time_accepts_gviz_and_iso(self):
        iso = delivery_policy.parse_queue_time("2026-08-30T08:00:00Z")
        self.assertEqual(iso, datetime(2026, 8, 30, 8, 0, tzinfo=timezone.utc))
        gviz = delivery_policy.parse_queue_time("Date(2026,7,30,8,0,0)")
        self.assertEqual(gviz, datetime(2026, 8, 30, 8, 0, tzinfo=timezone.utc))

    def test_due_accounts_respect_half_hour_spacing(self):
        now = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)
        accounts = {
            "IG-SPAIN": _primary("IG-SPAIN", "instagram"),
            "IG-ITALY": _primary("IG-ITALY", "instagram"),
            "FB-CD": _primary("FB-CD", "facebook"),
            "IG-BLANK": _primary("IG-BLANK", "instagram", platform_account_id=""),
            "LINE-CD": {
                "account_id": "LINE-CD",
                "platform": "line",
                "enabled": True,
                "platform_account_id": "",
            },
        }
        activity = {
            "IG-SPAIN": {"count": 2, "last": now - timedelta(hours=6)},
            "IG-ITALY": {"count": 2, "last": now - timedelta(minutes=20)},
            "FB-CD": {"count": 8, "last": now - timedelta(hours=6)},
        }
        due = delivery_policy.due_deficit_accounts(accounts, activity, now)
        self.assertEqual([item["account_id"] for item in due], ["IG-SPAIN"])
        self.assertEqual(due[0]["deficit"], 6)

        just_inside = {"IG-SPAIN": {"count": 1, "last": now - timedelta(minutes=30) + timedelta(seconds=1)}}
        self.assertEqual(delivery_policy.due_deficit_accounts(
            {"IG-SPAIN": accounts["IG-SPAIN"]}, just_inside, now,
        ), [])
        exactly_gap = {"IG-SPAIN": {"count": 1, "last": now - timedelta(minutes=30)}}
        self.assertEqual(
            [item["account_id"] for item in delivery_policy.due_deficit_accounts(
                {"IG-SPAIN": accounts["IG-SPAIN"]}, exactly_gap, now,
            )],
            ["IG-SPAIN"],
        )

    def test_rolling_activity_counts_only_successes_in_24h(self):
        now = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)
        accounts = {"FB-CD": _primary("FB-CD", "facebook")}
        records = [
            {"account_id": "FB-CD", "status": "uploaded", "last_attempt_at": "2026-08-30 08:00:00"},
            {"account_id": "FB-CD", "status": "uploaded", "last_attempt_at": "2026-08-29 13:00:00"},
            {"account_id": "FB-CD", "status": "failed", "last_attempt_at": "2026-08-30 10:00:00"},
            {"account_id": "FB-CD", "status": "uploaded", "last_attempt_at": "2026-08-29 11:59:59"},
            {"account_id": "LINE-CD", "status": "uploaded", "last_attempt_at": "2026-08-30 08:00:00"},
        ]
        activity = delivery_policy.rolling_activity(records, accounts, now)
        self.assertEqual(activity["FB-CD"]["count"], 2)
        self.assertNotIn("LINE-CD", activity)

    def test_platform_limits_cover_all_ready_accounts_and_skip_line(self):
        accounts = {}
        for idx in range(21):
            aid = "FB-%02d" % idx
            accounts[aid] = _primary(aid, "facebook")
        for idx in range(17):
            aid = "IG-%02d" % idx
            accounts[aid] = _primary(aid, "instagram")
        accounts["IG-SWEDEN"] = _primary("IG-SWEDEN", "instagram", platform_account_id="")
        accounts["YT-CD"] = _primary("YT-CD", "youtube")
        accounts["LINE-CD"] = {
            "account_id": "LINE-CD",
            "platform": "line",
            "enabled": True,
            "platform_account_id": "",
        }
        slots = optimized_runner._platform_limits(50, accounts)
        self.assertEqual(slots["facebook"], 21)
        self.assertEqual(slots["instagram"], 17)
        self.assertEqual(slots["tiktok"], 0)
        self.assertEqual(slots["line"], 0)
        self.assertEqual(slots["youtube"], 12)
        self.assertEqual(sum(slots.values()), 50)

    def test_finished_facebook_accounts_do_not_starve_instagram(self):
        now = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)
        accounts = {}
        activity = {}
        for idx in range(21):
            aid = "FB-%02d" % idx
            accounts[aid] = _primary(aid, "facebook")
            activity[aid] = {"count": 8, "last": now - timedelta(hours=1), "success_times": []}
        for idx in range(17):
            aid = "IG-%02d" % idx
            accounts[aid] = _primary(aid, "instagram")
            activity[aid] = {"count": 1, "last": now - timedelta(hours=6), "success_times": []}
        accounts["YT-CD"] = _primary("YT-CD", "youtube")
        activity["YT-CD"] = {"count": 0, "last": None, "success_times": []}
        slots = optimized_runner._platform_limits(50, accounts, activity)
        self.assertEqual(slots["facebook"], 0)
        self.assertEqual(slots["instagram"], 17)
        self.assertEqual(slots["youtube"], 33)
        self.assertEqual(sum(slots.values()), 50)

    def test_instagram_cooldown_moves_unused_slots_to_youtube(self):
        accounts = {
            "FB-A": _primary("FB-A", "facebook"),
            "IG-A": _primary("IG-A", "instagram"),
            "IG-B": _primary("IG-B", "instagram"),
            "YT-CD": _primary("YT-CD", "youtube"),
        }
        jobs = [{
            "job_id": "100-IG-A-video",
            "account_id": "IG-A",
            "platform": "instagram",
            "sku": "100",
            "media_selection": "model_video:0",
            "row": 3,
            "attempts": 1,
            "notes": "application request limit reached",
            "error_message": "application limit",
            "last_attempt_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
        }]
        sources = {"100": {"sku": "100"}}
        sheets = type("Sheets", (), {"update_job": staticmethod(lambda *a, **k: None)})()
        activity = {
            "FB-A": {"count": 8, "last": None, "success_times": []},
            "IG-A": {"count": 1, "last": None, "success_times": []},
            "IG-B": {"count": 1, "last": None, "success_times": []},
            "YT-CD": {"count": 0, "last": None, "success_times": []},
        }
        for account_id, platform in (("FB-A", "facebook"), ("IG-A", "instagram"), ("IG-B", "instagram"), ("YT-CD", "youtube")):
            jobs.append({
                "job_id": "200-%s-video" % account_id,
                "account_id": account_id,
                "platform": platform,
                "sku": "200",
                "media_selection": "model_video:0",
                "row": 4,
                "attempts": 0,
                "notes": "",
            })
        sources["200"] = {"sku": "200"}
        with patch("optimized_runner.resolve_media_fixed", return_value=["https://example.com/a.mp4"]), \
             patch("optimized_runner._dns_resolves", return_value=True), \
             patch("optimized_runner.main._classify_media_url", return_value="video"), \
             patch("optimized_runner._local_slot_due", return_value=False), \
             patch("optimized_runner._video_validation_reason", return_value=""):
            selected = optimized_runner._healthy_candidates(
                jobs, accounts, sources, sheets, limit=50,
                recent_upload_activity=activity,
            )
        selected_ids = [job["account_id"] for job in selected]
        self.assertNotIn("IG-A", selected_ids)
        self.assertNotIn("IG-B", selected_ids)
        self.assertIn("YT-CD", selected_ids)

    def test_instagram_per_run_cap_is_configurable(self):
        accounts = {
            "IG-A": _primary("IG-A", "instagram"),
            "IG-B": _primary("IG-B", "instagram"),
            "FB-A": _primary("FB-A", "facebook"),
        }
        with patch.dict(os.environ, {"IG_MAX_JOBS_PER_RUN": "2"}):
            slots = optimized_runner._platform_limits(50, accounts)
        self.assertEqual(slots["instagram"], 2)

    def test_platform_limits_fallback_excludes_line_at_production_cap(self):
        slots = optimized_runner._platform_limits(50)
        self.assertEqual(slots["line"], 0)
        self.assertEqual(slots["tiktok"], 0)
        self.assertEqual(sum(slots.values()), 50)
        self.assertGreaterEqual(slots["facebook"], 1)
        self.assertGreaterEqual(slots["instagram"], 1)
        self.assertGreaterEqual(slots["youtube"], 1)
        self.assertEqual(set(slots), {"facebook", "instagram", "youtube", "tiktok", "line"})

    def test_healthy_candidates_apply_global_instagram_budget(self):
        jobs = []
        accounts = {}
        sources = {"100": {"sku": "100"}}
        ready_ids = ["FB-A", "FB-B", "IG-A", "IG-B", "YT-CD"]
        for account_id in ready_ids:
            platform = "facebook" if account_id.startswith("FB") else (
                "youtube" if account_id.startswith("YT") else "instagram"
            )
            accounts[account_id] = _primary(account_id, platform)
            jobs.append({
                "job_id": "100-%s-carousel" % account_id,
                "account_id": account_id,
                "platform": platform,
                "sku": "100",
                "media_selection": "model_photo:0",
                "row": 3,
                "attempts": 0,
                "notes": "",
            })
        accounts["LINE-CD"] = {
            "account_id": "LINE-CD",
            "platform": "line",
            "enabled": True,
            "platform_account_id": "",
        }
        jobs.append({
            "job_id": "100-LINE-CD-carousel",
            "account_id": "LINE-CD",
            "platform": "line",
            "sku": "100",
            "media_selection": "model_photo:0",
            "row": 9,
            "attempts": 0,
            "notes": "",
        })
        sheets = type("Sheets", (), {"update_job": staticmethod(lambda *a, **k: None)})()
        activity = {
            aid: {"count": 0, "last": None, "success_times": []}
            for aid in ready_ids
        }
        with patch("optimized_runner.resolve_media_fixed", return_value=["https://example.com/a.jpg"]), \
             patch("optimized_runner._dns_resolves", return_value=True), \
             patch("optimized_runner.main._classify_media_url", return_value="image"), \
             patch("optimized_runner._local_slot_due", return_value=False), \
             patch("optimized_runner._video_validation_reason", return_value=""):
            selected = optimized_runner._healthy_candidates(
                jobs, accounts, sources, sheets, limit=50,
                recent_upload_activity=activity,
            )
        selected_ids = [job["account_id"] for job in selected]
        self.assertEqual(len([aid for aid in selected_ids if aid.startswith("IG-")]), 2)
        self.assertIn("FB-A", selected_ids)
        self.assertIn("FB-B", selected_ids)
        self.assertIn("YT-CD", selected_ids)
        self.assertNotIn("LINE-CD", selected_ids)

    def test_instagram_prefers_model_video_over_model_photo(self):
        account_id = "IG-CD"
        accounts = {account_id: _primary(account_id, "instagram")}
        sources = {"carousel": {"sku": "carousel"}, "video": {"sku": "video"}}
        jobs = [
            {"job_id": "carousel-IG-CD-carousel", "account_id": account_id,
             "platform": "instagram", "format": "carousel", "sku": "carousel",
             "media_selection": "model_photo:0",
             "row": 20, "attempts": 0, "notes": ""},
            {"job_id": "video-IG-CD-product_video", "account_id": account_id,
             "platform": "instagram", "format": "video", "sku": "video",
             "media_selection": "model_video:0",
             "row": 10, "attempts": 0, "notes": ""},
        ]
        sheets = type("Sheets", (), {"update_job": staticmethod(lambda *a, **k: None)})()
        activity = {account_id: {"count": 0, "last": None, "success_times": []}}
        with patch("optimized_runner.resolve_media_fixed", return_value=["https://example.com/a.mp4"]), \
             patch("optimized_runner._dns_resolves", return_value=True), \
             patch("optimized_runner.main._classify_media_url", return_value="video"), \
             patch("optimized_runner._local_slot_due", return_value=False), \
             patch("optimized_runner._video_validation_reason", return_value=""):
            selected = optimized_runner._healthy_candidates(
                jobs, accounts, sources, sheets, limit=50,
                recent_upload_activity=activity,
            )
        self.assertEqual(selected[0]["job_id"], "video-IG-CD-product_video")

    def test_healthy_candidates_do_not_starve_new_youtube_jobs(self):
        account_id = "YT-CD"
        accounts = {account_id: _primary(account_id, "youtube")}
        jobs = []
        sources = {}
        for row in range(1, optimized_runner.PER_ACCOUNT_SCAN_LIMIT + 2):
            sku = "good" if row == optimized_runner.PER_ACCOUNT_SCAN_LIMIT + 1 else "bad-%s" % row
            sources[sku] = {"sku": sku}
            jobs.append({
                "job_id": "%s-%s-product_video" % (sku, account_id),
                "account_id": account_id,
                "platform": "youtube",
                "sku": sku,
                "media_selection": "model_video:0",
                "row": row,
                "attempts": 0,
                "notes": "",
            })

        sheets = type("Sheets", (), {"update_job": staticmethod(lambda *a, **k: None)})()
        activity = {account_id: {"count": 0, "last": None, "success_times": []}}

        def clean_source(source, job=None):
            return (source["sku"] == "good", "invalid test source")

        with patch("optimized_runner._is_clean_source", side_effect=clean_source), \
             patch("optimized_runner.resolve_media_fixed", return_value=["https://example.com/a.mp4"]), \
             patch("optimized_runner._dns_resolves", return_value=True), \
             patch("optimized_runner.main._classify_media_url", return_value="valid"), \
             patch("optimized_runner._local_slot_due", return_value=False), \
             patch("optimized_runner._video_validation_reason", return_value=""):
            selected = optimized_runner._healthy_candidates(
                jobs, accounts, sources, sheets, limit=50,
                recent_upload_activity=activity,
            )

        self.assertEqual([job["sku"] for job in selected], ["good"])

    def test_youtube_receives_leftover_slots_and_multiple_jobs_per_account(self):
        accounts = {
            "FB-A": _primary("FB-A", "facebook"),
            "IG-A": _primary("IG-A", "instagram"),
            "YT-CD": _primary("YT-CD", "youtube"),
        }
        slots = optimized_runner._platform_limits(50, accounts)
        self.assertEqual(slots["facebook"], 1)
        self.assertEqual(slots["instagram"], 1)
        self.assertEqual(slots["youtube"], 48)

        jobs = []
        sources = {}
        for idx in range(1, 6):
            sku = "yt-%s" % idx
            sources[sku] = {"sku": sku}
            jobs.append({
                "job_id": "%s-YT-CD-model_video-1" % sku,
                "media_selection": "model_video:1",
                "format": "video",
                "account_id": "YT-CD",
                "platform": "youtube",
                "sku": sku,
                "row": idx,
                "attempts": 0,
                "notes": "",
            })
        jobs.append({
            "job_id": "100-FB-A-carousel",
            "account_id": "FB-A",
            "platform": "facebook",
            "sku": "100",
            "media_selection": "model_photo:0",
            "row": 10,
            "attempts": 0,
            "notes": "",
        })
        sources["100"] = {"sku": "100"}
        sheets = type("Sheets", (), {"update_job": staticmethod(lambda *a, **k: None)})()
        activity = {
            "FB-A": {"count": 0, "last": None, "success_times": []},
            "YT-CD": {"count": 0, "last": None, "success_times": []},
        }

        def resolve(job, source):
            return ["https://example.com/%s.mp4" % job.get("sku")]

        with patch("optimized_runner.resolve_media_fixed", side_effect=resolve), \
             patch("optimized_runner._dns_resolves", return_value=True), \
             patch("optimized_runner.main._classify_media_url", return_value="video"), \
             patch("optimized_runner._local_slot_due", return_value=False), \
             patch("optimized_runner._video_validation_reason", return_value=""):
            selected = optimized_runner._healthy_candidates(
                jobs, accounts, sources, sheets, limit=50,
                recent_upload_activity=activity,
            )
        youtube_jobs = [job for job in selected if job["account_id"] == "YT-CD"]
        self.assertEqual(len(youtube_jobs), 5)
        self.assertEqual(
            [job["sku"] for job in youtube_jobs],
            ["yt-5", "yt-4", "yt-3", "yt-2", "yt-1"],
        )


if __name__ == "__main__":
    unittest.main()

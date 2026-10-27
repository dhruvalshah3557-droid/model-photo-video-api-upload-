#!/usr/bin/env python3
"""Mock-based pipeline tests (no real credentials required).

Verifies the two reliability guarantees:
  1. `python main.py --generate` is idempotent: running it twice appends no
     duplicate job rows (unique key = SKU + account_id + platform + format +
     media_type). Only missing jobs are added.
  2. One failing job never stops the other jobs in the same run, results are
     written back to the Publishing Queue, and a second run does not re-post
     already successful jobs.

Run: python3 test_pipeline.py
"""
import os
import sys
import types
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _install(name, attrs):
    """Register a fake (possibly dotted) module in sys.modules."""
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    parent = sys.modules.get(".".join(name.split(".")[:-1]))
    if parent is not None:
        setattr(parent, name.split(".")[-1], mod)
    return mod


class _FakeAPIError(Exception):
    def __init__(self, status_code=429):
        self.response = types.SimpleNamespace(status_code=status_code)
        super().__init__("rate limited")


gspread = _install("gspread", {})
gspread.exceptions = types.SimpleNamespace(APIError=_FakeAPIError)

_install("oauth2client", {})
_install("oauth2client.service_account", {
    "ServiceAccountCredentials": lambda *a, **k: None,
})

_install("dotenv", {"load_dotenv": lambda *a, **k: None})


class _FakeResponse:
    status_code = 200
    headers = {"Content-Type": "image/jpeg"}

    def close(self):
        pass


def _fake_requests_get(*a, **k):
    return _FakeResponse()


_install("requests", {
    "get": _fake_requests_get,
    "post": lambda *a, **k: None,
    "RequestException": Exception,
})

_install("google_auth_oauthlib", {})
_install("google_auth_oauthlib.flow", {"InstalledAppFlow": lambda *a, **k: None})
_install("google.auth", {})
_install("google.auth.transport", {})
_install("google.auth.transport.requests", {"Request": lambda *a, **k: None})
_install("google.oauth2", {})
_install("google.oauth2.credentials", {"Credentials": lambda *a, **k: None})
_install("googleapiclient", {})
_install("googleapiclient.discovery", {"build": lambda *a, **k: None})
_install("googleapiclient.http", {"MediaIoBaseUpload": lambda *a, **k: None})

from config import Config  # noqa: E402
from sheets_reader import SheetsReader  # noqa: E402
import main  # noqa: E402

QUEUE_COLS = SheetsReader.QUEUE_COLS
SKIP_STATUSES = {
    Config.JOB_STATUS_UPLOADED,
    Config.JOB_STATUS_FAILED,
    Config.JOB_STATUS_SKIPPED,
    Config.JOB_STATUS_NEEDS_REVIEW,
}


class _FakeLogWorksheet:
    """Minimal stand-in for the Publishing Log worksheet used by
    insert_logs_newest_first: converts inserted value rows back into
    LOG_COLS-keyed dicts appended to the owning FakeSheets.log_entries."""

    def __init__(self, fake_sheets):
        self._sheets = fake_sheets

    def insert_rows(self, rows, row=None, value_input_option=None):
        for values in rows:
            entry = {}
            for i, col in enumerate(FakeSheets.LOG_COLS):
                entry[col] = values[i] if i < len(values) else ""
            self._sheets.log_entries.append(entry)


class FakeSheets:
    """In-memory stand-in for SheetsReader used by main.run_generate/process_pending."""

    LOG_COLS = SheetsReader.LOG_COLS

    def __init__(self, source_rows, accounts, queue_rows):
        self.guide_error = ""
        self.source_rows = dict(source_rows)
        self.accounts = list(accounts)
        self.queue_rows = list(queue_rows)
        self.append_jobs_calls = 0
        self.appended_jobs = []
        self.updated = []
        self.log_entries = []
        self.log_header_row = 2
        self.log_ws = _FakeLogWorksheet(self)

    def get_upload_guide(self):
        return []

    def guide_safety_rules(self, guide_rows):
        return []

    def get_accounts(self):
        return list(self.accounts)

    def get_source_rows(self):
        return dict(self.source_rows)

    def get_pending_jobs(self):
        jobs = []
        for i, rec in enumerate(self.queue_rows, start=2):
            status = str(rec.get("status", "")).strip().lower()
            if status in SKIP_STATUSES or not str(rec.get("job_id", "")).strip():
                continue
            media_selection = str(rec.get("media_selection", "")).strip()
            jobs.append({
                "row": i,
                "job_id": str(rec.get("job_id", "")).strip(),
                "sku": str(rec.get("sku", "")).strip(),
                "account_id": str(rec.get("account_id", "")).strip(),
                "media_selection": media_selection,
                "platform": str(rec.get("platform", "")).strip().lower(),
                "format": str(rec.get("format", "")).strip().lower(),
                "language": str(rec.get("language", "")).strip(),
                "timezone": str(rec.get("timezone", "")).strip(),
                "stock_id_tag": str(rec.get("stock_id_tag", "")).strip(),
                "tagging_status": str(rec.get("tagging_status", "")).strip() or "Pending",
                "tag_stock_id_used": str(rec.get("tag_stock_id_used", "")).strip(),
                "caption_final": str(rec.get("caption_final", "")).strip(),
                "attempts": int(rec.get("attempts") or 0),
                "last_attempt_at": str(rec.get("last_attempt_at", "") or "").strip(),
                "error_message": str(rec.get("error_message", "") or "").strip(),
                "notes": str(rec.get("notes", "")).strip(),
            })
        return jobs

    def get_existing_job_keys(self):
        keys = set()
        for rec in self.queue_rows:
            sku = str(rec.get("sku", "")).strip()
            if not sku:
                continue
            keys.add((sku, str(rec.get("account_id", "")).strip(),
                      str(rec.get("platform", "")).strip().lower(),
                      str(rec.get("format", "")).strip().lower(),
                      str(rec.get("media_selection", "")).strip()))
        return keys

    def get_existing_job_ids(self):
        return {
            str(rec.get("job_id", "") or "").strip()
            for rec in self.queue_rows
            if str(rec.get("job_id", "") or "").strip()
        }

    def append_jobs(self, jobs):
        self.append_jobs_calls += 1
        for job in jobs:
            self.appended_jobs.append(job)
            self.queue_rows.append({col: job.get(col, "") for col in QUEUE_COLS})

    def update_job(self, job, updates):
        self.updated.append((job, dict(updates)))
        rec = self.queue_rows[job["row"] - 2]
        for k, v in updates.items():
            rec[k] = v

    def log_entry(self, job, result, error_message="", api_error_code="", notes=""):
        return {
            "job_id": job.get("job_id", ""),
            "attempt_time": "2026-01-01T00:00:00",
            "result": result,
            "platform_post_id": job.get("platform_post_id", ""),
            "published_url": job.get("published_url", ""),
            "api_error_code": api_error_code,
            "error_message": error_message,
            "next_retry_at": "",
            "raw_response_reference": "",
            "notes": notes,
        }

    def append_logs(self, entries):
        self.log_entries.extend(entries)


def _account(account_id="FB-ISR", platform="facebook", enabled=True):
    return {
        "row": 2,
        "account_id": account_id,
        "platform": platform,
        "account_name": account_id,
        "platform_account_id": "123456789",
        "username_or_channel": "",
        "primary_language": "en",
        "fallback_language": "",
        "timezone": "UTC",
        "enabled": enabled,
        "allowed_formats": "",
        "caption_style": "",
        "hashtag_set": "",
        "min_gap_minutes": "",
        "product_tagging": False,
        "catalog_or_store_id": "",
        "credential_property_key": "META_TOKEN_FB_ISR",
        "approval_required": False,
        "notes": "",
    }


def _source(sku="100"):
    return {
        "row": 2,
        "sku": sku,
        "sr_no": "1",
        "lab": "",
        "certificate_id": "",
        "source_status": "",
        "product_link": "",
        "product_name": "Diamond Ring",
        "images": [f"http://example.com/{sku}_center.jpg", f"http://example.com/{sku}_side1.jpg"],
        "main_image": f"http://example.com/{sku}_center.jpg",
        "side_images": [f"http://example.com/{sku}_side1.jpg"],
        "video_url": f"http://example.com/{sku}.mp4",
        "multiple_video": [],
        "model_images": [],
        "model_videos": [f"http://example.com/{sku}_model1.mp4"],
        "facebook_caption": "Caption",
        "instagram_caption": "Caption",
        "youtube_shorts_caption": "Caption",
        "youtube_product_id": "",
        "hashtags": "#diamond",
        "lang_captions": {},
        "lang_hashtags": {},
    }


def test_generate_queues_model_media_only():
    accounts = [_account()]
    source = _source("100")
    source["model_images"] = ["http://example.com/100_model.jpg"]
    sources = {"100": source}
    fake = FakeSheets(sources, accounts, [])
    with patch.object(Config, "MAX_GENERATE_JOBS", 10):
        main.run_generate(fake)
    selections = [job["media_selection"] for job in fake.appended_jobs]
    assert set(selections) == {"model_video:0", "model_photo:0"}, selections
    print("OK test_generate_queues_model_media_only")


def test_round_robin_prefers_model_media_within_platform():
    jobs = [
        {"job_id": "fb-carousel", "platform": "facebook", "media_selection": "carousel", "attempts": 0, "row": 1},
        {"job_id": "fb-model", "platform": "facebook", "media_selection": "model_photo:0", "attempts": 0, "row": 2},
        {"job_id": "ig-product", "platform": "instagram", "media_selection": "product_video", "attempts": 0, "row": 3},
        {"job_id": "ig-model", "platform": "instagram", "media_selection": "model_video:0", "attempts": 0, "row": 4},
    ]
    selected = main._round_robin_jobs(jobs, 2)
    picked = [job["job_id"] for job in selected]
    assert picked == ["fb-model", "ig-model"], picked
    print("OK test_round_robin_prefers_model_media_within_platform")


def test_generate_is_idempotent():
    accounts = [_account()]
    sources = {"100": _source("100")}
    fake = FakeSheets(sources, accounts, [])
    assert fake.get_existing_job_keys() == set()

    with patch.object(Config, "MAX_GENERATE_JOBS", 3):
        main.run_generate(fake)
        first_count = len(fake.appended_jobs)
        assert first_count == 1, f"expected 1 model video job, got {first_count}"
        assert fake.appended_jobs[0]["media_selection"] == "model_video:0"

        # Second identical run must append NOTHING new.
        main.run_generate(fake)
        assert len(fake.appended_jobs) == first_count, "duplicate jobs generated on second run"

        # A brand-new SKU appears -> only its missing jobs are added.
        fake.source_rows["200"] = _source("200")
        main.run_generate(fake)
        assert len(fake.appended_jobs) == first_count + 1

    # No duplicate unique keys ever.
    keys = [main.job_unique_key(j) for j in fake.appended_jobs]
    assert len(keys) == len(set(keys)), "duplicate unique job keys produced"
    print("OK test_generate_is_idempotent")


def test_generate_uses_stable_job_id_when_mutable_key_drifted():
    accounts = [_account()]
    sources = {"100": _source("100")}
    existing = [{
        "job_id": "100-FB-CD-carousel", "sku": "100", "account_id": "FB-CD",
        "platform": "facebook", "format": "carousel",
        "media_selection": "legacy-carousel-name", "status": "uploaded",
    }]
    fake = FakeSheets(sources, accounts, existing)
    with patch.object(Config, "MAX_GENERATE_JOBS", 10):
        main.run_generate(fake)
    assert "100-FB-CD-carousel" not in {
        job["job_id"] for job in fake.appended_jobs
    }, fake.appended_jobs
    print("OK test_generate_uses_stable_job_id_when_mutable_key_drifted")


def test_generate_skips_unlinked_instagram_placeholders():
    accounts = [
        _account("IG-CD", "instagram"),
        _account("IG-DENMARK", "instagram"),
        _account("FB-CD", "facebook"),
    ]
    accounts[1]["platform_account_id"] = ""
    sources = {"100": _source("100")}
    fake = FakeSheets(sources, accounts, [])
    with patch.object(Config, "MAX_GENERATE_JOBS", 40):
        main.run_generate(fake)
    generated_accounts = {job["account_id"] for job in fake.appended_jobs}
    assert "IG-CD" in generated_accounts, generated_accounts
    assert "FB-CD" in generated_accounts, generated_accounts
    assert "IG-DENMARK" not in generated_accounts, generated_accounts
    print("OK test_generate_skips_unlinked_instagram_placeholders")


def test_generation_cap_is_fair_across_accounts():
    accounts = [
        _account("IG-OLD", "instagram"),
        _account("IG-SPAIN", "instagram"),
        _account("IG-ITALY", "instagram"),
    ]
    sources = {str(i): _source(str(i)) for i in range(100, 106)}
    fake = FakeSheets(sources, accounts, [])
    with patch.object(Config, "MAX_GENERATE_JOBS", 3), patch("main.time.time", return_value=0):
        main.run_generate(fake)
    generated_accounts = {job["account_id"] for job in fake.appended_jobs}
    assert generated_accounts == {"IG-OLD", "IG-SPAIN", "IG-ITALY"}, generated_accounts
    print("OK test_generation_cap_is_fair_across_accounts")


def test_generation_cap_stays_on_model_media():
    accounts = [
        _account("FB-A", "facebook"),
        _account("FB-B", "facebook"),
    ]
    source_a = _source("100")
    source_b = _source("200")
    source_b["model_images"] = ["http://example.com/200_model.jpg"]
    fake = FakeSheets({"100": source_a, "200": source_b}, accounts, [])
    with patch.object(Config, "MAX_GENERATE_JOBS", 3), patch("main.time.time", return_value=0):
        main.run_generate(fake)
    selections = [
        (job["account_id"], job["media_selection"])
        for job in fake.appended_jobs
    ]
    assert len(selections) == 3, selections
    picked = {sel for _, sel in selections}
    assert all(sel.startswith("model_") for sel in picked), selections
    assert "carousel" not in picked
    assert "product_video" not in picked
    print("OK test_generation_cap_stays_on_model_media")


def test_one_failure_does_not_stop_others():
    accounts = [_account("FB-A"), _account("FB-B")]
    sources = {"100": _source("100")}
    jobs = [
        {"job_id": "100-FB-A-model_photo-0", "sku": "100", "account_id": "FB-A",
         "media_selection": "model_photo:0", "platform": "facebook", "format": "carousel",
         "status": "pending", "attempts": 0},
        {"job_id": "100-FB-B-model_photo-0", "sku": "100", "account_id": "FB-B",
         "media_selection": "model_photo:0", "platform": "facebook", "format": "carousel",
         "status": "pending", "attempts": 0},
    ]
    fake = FakeSheets(sources, accounts, jobs)

    def fake_publish(job, source, account):
        if job["account_id"] == "FB-A":
            raise Exception("(#190) Invalid OAuth 2.0 Access Token")
        return "111111111", "https://www.facebook.com/123456789/posts/111111111"

    with patch.object(Config, "MAX_JOB_ATTEMPTS", 1), \
         patch.object(Config, "MAX_JOBS_PER_RUN", 40), \
         patch("main.publish_job", side_effect=fake_publish), \
         patch("main.time.sleep"):
        main.process_pending(fake)

    status_by_job = {job_id: updates["status"] for job_id, updates in
                     ((u[0]["job_id"], u[1]) for u in fake.updated)}
    assert status_by_job["100-FB-A-model_photo-0"] == Config.JOB_STATUS_FAILED
    assert status_by_job["100-FB-B-model_photo-0"] == Config.JOB_STATUS_UPLOADED

    fb_b_updates = next(u[1] for u in fake.updated if u[0]["job_id"] == "100-FB-B-model_photo-0")
    assert fb_b_updates.get("platform_post_id") == "111111111"
    assert fb_b_updates.get("published_url").startswith("https://www.facebook.com/")

    assert sorted(e["result"] for e in fake.log_entries) == ["failed", "success"]
    print("OK test_one_failure_does_not_stop_others")


def test_second_run_does_not_repost_uploaded():
    accounts = [_account("FB-A")]
    sources = {"100": _source("100")}
    jobs = [
        {"job_id": "100-FB-A-model_photo-0", "sku": "100", "account_id": "FB-A",
         "media_selection": "model_photo:0", "platform": "facebook", "format": "carousel",
         "status": "pending", "attempts": 0},
    ]
    fake = FakeSheets(sources, accounts, jobs)
    calls = {"n": 0}

    def fake_publish(job, source, account):
        calls["n"] += 1
        return "111111111", "https://www.facebook.com/123456789/posts/111111111"

    with patch("main.publish_job", side_effect=fake_publish), patch("main.time.sleep"):
        main.process_pending(fake)
    assert calls["n"] == 1
    assert fake.queue_rows[0]["status"] == Config.JOB_STATUS_UPLOADED

    # Second run: the job is already "uploaded" -> not picked up, not re-posted.
    with patch("main.publish_job", side_effect=fake_publish), patch("main.time.sleep"):
        main.process_pending(fake)
    assert calls["n"] == 1, "successful job was re-posted on a second run"
    print("OK test_second_run_does_not_repost_uploaded")


def test_generate_new_platforms():
    accounts = [
        _account("LINE-CD", "line"),
        _account("WECHAT-CD", "wechat"),
        _account("PINTEREST-CD", "pinterest"),
    ]
    sources = {"100": _source("100")}
    fake = FakeSheets(sources, accounts, [])

    with patch.object(Config, "MAX_GENERATE_JOBS", 40):
        main.run_generate(fake)
    jobs = fake.appended_jobs
    assert len(jobs) == 3, f"expected 3 model video jobs, got {len(jobs)}"
    platforms = {j["platform"] for j in jobs}
    assert platforms == {"line", "wechat", "pinterest"}
    assert {j["media_selection"] for j in jobs} == {"model_video:0"}

    # Idempotent on second run.
    with patch.object(Config, "MAX_GENERATE_JOBS", 40):
        main.run_generate(fake)
    assert len(fake.appended_jobs) == 3, "duplicate jobs generated for new platforms"
    print("OK test_generate_new_platforms")


def test_publish_new_platforms_routing():
    accounts = [
        _account("LINE-CD", "line"),
        _account("WECHAT-CD", "wechat"),
        _account("PINTEREST-CD", "pinterest"),
    ]
    source = _source("100")
    source["model_images"] = ["http://example.com/100_model.jpg"]
    sources = {"100": source}
    jobs = [
        {"job_id": "100-LINE-CD-model_photo-0", "sku": "100", "account_id": "LINE-CD",
         "media_selection": "model_photo:0", "platform": "line", "format": "carousel",
         "status": "pending", "attempts": 0},
        {"job_id": "100-WECHAT-CD-model_photo-0", "sku": "100", "account_id": "WECHAT-CD",
         "media_selection": "model_photo:0", "platform": "wechat", "format": "carousel",
         "status": "pending", "attempts": 0},
        {"job_id": "100-PINTEREST-CD-model_video-0", "sku": "100", "account_id": "PINTEREST-CD",
         "media_selection": "model_video:0", "platform": "pinterest", "format": "video",
         "status": "pending", "attempts": 0},
    ]
    fake = FakeSheets(sources, accounts, jobs)

    class FakeLine:
        def __init__(self, *a, **k):
            self.called = False
        def upload_carousel(self, *a, **k):
            return {"id": "111", "url": "https://line.example/111"}

    class FakeWeChat:
        def __init__(self, *a, **k):
            self.called = False
        def upload_carousel(self, *a, **k):
            return {"id": "222", "url": "https://mp.weixin.qq.com/"}

    class FakePinterest:
        def __init__(self, *a, **k):
            self.called = False
        def upload(self, *a, **k):
            return {"id": "333", "url": "https://www.pinterest.com/pin/333/"}

    with patch.object(Config, "MAX_JOB_ATTEMPTS", 1), \
         patch.object(Config, "MAX_JOBS_PER_RUN", 40), \
         patch.object(Config, "MEDIA_VALIDATION", False), \
         patch("main.time.sleep"), \
         patch("main.LineUploader", FakeLine), \
         patch("main.WeChatUploader", FakeWeChat), \
         patch("main.PinterestUploader", FakePinterest), \
         patch.dict(os.environ, {
             "LINE_CHANNEL_ACCESS_TOKEN": "line-token",
             "WECHAT_APPID": "wx-appid",
             "WECHAT_APPSECRET": "wx-secret",
             "PINTEREST_ACCESS_TOKEN": "pin-token",
         }):
        main.process_pending(fake)

    statuses = {u[0]["job_id"]: u[1]["status"] for u in fake.updated}
    for job_id in ("100-LINE-CD-model_photo-0", "100-WECHAT-CD-model_photo-0", "100-PINTEREST-CD-model_video-0"):
        assert statuses[job_id] == Config.JOB_STATUS_UPLOADED, job_id
    assert fake.updated[0][1]["published_url"].startswith("https://")
    print("OK test_publish_new_platforms_routing")


def test_round_robin_jobs_mix_platforms():
    jobs = [
        {"job_id": "a", "platform": "facebook"},
        {"job_id": "b", "platform": "facebook"},
        {"job_id": "c", "platform": "instagram"},
        {"job_id": "d", "platform": "youtube"},
        {"job_id": "e", "platform": "instagram"},
        {"job_id": "f", "platform": "facebook"},
    ]
    selected = main._round_robin_jobs(jobs, 5)
    picked = [j["job_id"] for j in selected]
    assert picked == ["a", "c", "d", "b", "e"], picked
    counts = {p: 0 for p in ("facebook", "instagram", "youtube")}
    for j in selected:
        counts[j["platform"]] += 1
    assert counts == {"facebook": 2, "instagram": 2, "youtube": 1}, counts
    assert main._round_robin_jobs([], 5) == []
    print("OK test_round_robin_jobs_mix_platforms")


def test_resolve_ig_user_id_handles_linked_and_unlinked_pages():
    from instagram_uploader import IGAccountNotLinkedError

    main.InstagramUploader._IG_ID_CACHE.clear()

    def _resp(data):
        return types.SimpleNamespace(json=lambda: data, raise_for_status=lambda: None)

    def fake_get(url, params, timeout=15):
        assert url.endswith("/me/accounts")
        return _resp({"data": [
            {
                "id": "LINKED_PAGE",
                "name": "Colour Diam Linked",
                "instagram_business_account": {"id": "IG_ACCT", "username": "colourdiamlinked"},
            },
            {"id": "UNLINKED_PAGE", "name": "Unlinked Page"},
        ]})

    with patch("instagram_uploader.requests.get", side_effect=fake_get):
        # A configured Instagram Business ID is authoritative.
        assert main.InstagramUploader._resolve_ig_user_id("IG_ACCT", "tok") == "IG_ACCT"
        # A blank ID can be resolved from the linked Facebook page name.
        assert (
            main.InstagramUploader._resolve_ig_user_id("", "tok", "Colour Diam Linked")
            == "IG_ACCT"
        )
        try:
            main.InstagramUploader._resolve_ig_user_id("", "tok", "Unlinked Page")
            assert False, "expected IGAccountNotLinkedError"
        except IGAccountNotLinkedError:
            pass
    print("OK test_resolve_ig_user_id_handles_linked_and_unlinked_pages")

def test_filter_valid_media_drops_dead_links():
    good = "https://colourdiam.com/Product/Jewellery/2518/white45/2518_1.jpg"
    video = "https://colourdiam.com/Product/Jewellery/3832/white45/video.mp4"
    dead = "https://www.colourdiam.com/Product/Jewellery/7988/white45/center.jpg"

    def fake_classify(url):
        if url == dead:
            return "invalid"
        if url == video:
            return "video"
        return "image"

    with patch("main._classify_media_url", side_effect=fake_classify):
        assert main._filter_valid_media([good, video]) == [good, video]
        assert main._filter_valid_media([dead, good, video]) == [good, video]
        assert main._filter_valid_media([dead]) == []
    print("OK test_filter_valid_media_drops_dead_links")


def test_optimized_preflight_skips_corrupt_video_and_uses_next_job():
    import optimized_runner

    bad = {
        "job_id": "bad", "account_id": "FB-CD", "platform": "facebook",
        "sku": "1", "row": 5, "attempts": 0, "notes": "",
        "media_selection": "model_video:0",
    }
    good = {
        "job_id": "good", "account_id": "FB-CD", "platform": "facebook",
        "sku": "2", "row": 4, "attempts": 0, "notes": "",
        "media_selection": "model_photo:0",
    }
    updates = []
    sheets = types.SimpleNamespace(update_job=lambda job, values: updates.append((job, values)))
    accounts = {"FB-CD": {"enabled": True, "platform": "facebook"}}
    sources = {"1": {"sku": "1"}, "2": {"sku": "2"}}

    def media_for(job, source):
        suffix = "broken.mp4" if job["job_id"] == "bad" else "healthy.jpg"
        return [f"https://media.example/{suffix}"]

    optimized_runner._VIDEO_VALIDATION_CACHE.clear()
    with patch("optimized_runner.resolve_media_fixed", side_effect=media_for), \
         patch("optimized_runner._dns_resolves", return_value=True), \
         patch("optimized_runner.main._classify_media_url", side_effect=lambda u: "video" if u.endswith(".mp4") else "image"), \
         patch("optimized_runner.main.validate_media_url", return_value="ffprobe rejected the file"), \
         patch("optimized_runner._rotation_rank", return_value=0):
        selected = optimized_runner._healthy_candidates(
            [bad, good], accounts, sources, sheets, limit=1
        )

    assert [job["job_id"] for job in selected] == ["good"], selected
    assert updates[0][0]["job_id"] == "bad"
    assert updates[0][1]["status"] == Config.JOB_STATUS_NEEDS_REVIEW
    assert "ffprobe rejected" in updates[0][1]["error_message"]
    print("OK test_optimized_preflight_skips_corrupt_video_and_uses_next_job")


def test_video_preflight_result_is_cached_across_accounts():
    import optimized_runner

    url = "https://media.example/shared.mp4"
    optimized_runner._VIDEO_VALIDATION_CACHE.clear()
    with patch("optimized_runner.main.validate_media_url", return_value="broken container") as validate:
        assert optimized_runner._video_validation_reason(url) == "broken container"
        assert optimized_runner._video_validation_reason(url) == "broken container"
    assert validate.call_count == 1
    print("OK test_video_preflight_result_is_cached_across_accounts")


def test_all_primary_accounts_prioritize_24h_deficit():
    import optimized_runner

    now = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)
    due = {"IG-SPAIN": {"count": 2, "last": now - timedelta(hours=6)}}
    recent = {"IG-SPAIN": {"count": 2, "last": now - timedelta(minutes=30) + timedelta(seconds=1)}}
    complete = {"IG-SPAIN": {"count": 8, "last": now - timedelta(hours=6)}}
    assert optimized_runner._minimum_delivery_priority("IG-SPAIN", due, now) == (0, 2)
    assert optimized_runner._minimum_delivery_priority("IG-SPAIN", recent, now) == (1, 2)
    assert optimized_runner._minimum_delivery_priority("IG-SPAIN", complete, now) == (1, 8)
    assert optimized_runner._minimum_delivery_priority("FB-CD", {}, now) == (0, 0)
    print("OK test_all_primary_accounts_prioritize_24h_deficit")


def test_queue_state_counts_only_successes_in_rolling_24h():
    import optimized_runner

    now = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)
    records = [
        {"account_id": "IG-DUBAI", "status": "uploaded", "last_attempt_at": "2026-08-30 08:00:00"},
        {"account_id": "IG-DUBAI", "status": "uploaded", "last_attempt_at": "2026-08-29 13:00:00"},
        {"account_id": "IG-DUBAI", "status": "failed", "last_attempt_at": "2026-08-30 10:00:00"},
        {"account_id": "IG-DUBAI", "status": "uploaded", "last_attempt_at": "2026-08-29 11:59:59"},
    ]
    queue_ws = types.SimpleNamespace(get_all_records=lambda head: records)
    sheets = types.SimpleNamespace(queue_ws=queue_ws, queue_header_row=1)
    accounts = {
        "IG-DUBAI": {
            "enabled": True,
            "platform": "instagram",
            "platform_account_id": "17841438237093119",
        }
    }
    _, activity = optimized_runner._queue_state(sheets, now, accounts)
    assert activity["IG-DUBAI"]["count"] == 2, activity
    assert activity["IG-DUBAI"]["last"] == datetime(2026, 8, 30, 8, 0, tzinfo=timezone.utc)
    print("OK test_queue_state_counts_only_successes_in_rolling_24h")


def test_stable_job_id_blocks_duplicate_when_media_changed():
    import optimized_runner

    job_id = "3442-FB-ISR-carousel"
    records = [{"job_id": job_id, "status": "uploaded", "notes": ""}]
    queue_ws = types.SimpleNamespace(get_all_records=lambda head: records)
    queue_sheets = types.SimpleNamespace(queue_ws=queue_ws, queue_header_row=1)
    reserved, _ = optimized_runner._queue_state(queue_sheets)

    duplicate = {
        "job_id": job_id, "account_id": "FB-ISR", "platform": "facebook",
        "format": "carousel", "sku": "3442", "row": 12,
        "media_selection": "model_photo:0",
        "attempts": 0, "notes": "",
    }
    updates = []
    sheets = types.SimpleNamespace(
        update_job=lambda job, values: updates.append((job, values))
    )
    accounts = {
        "FB-ISR": {
            "enabled": True, "platform": "facebook",
            "platform_account_id": "123", "timezone": "UTC",
        }
    }
    with patch("optimized_runner._local_slot_due", return_value=True), \
         patch("optimized_runner._rotation_rank", return_value=0):
        selected = optimized_runner._healthy_candidates(
            [duplicate], accounts, {}, sheets, limit=1,
            reserved_fingerprints=reserved,
        )

    assert selected == []
    assert updates[0][1]["status"] == Config.JOB_STATUS_SKIPPED
    assert "stable job ID" in updates[0][1]["notes"]
    print("OK test_stable_job_id_blocks_duplicate_when_media_changed")


def test_account_local_posting_slots_are_timezone_aware_and_idempotent():
    import optimized_runner

    account = {"timezone": "Asia/Bangkok"}
    now = datetime(2026, 8, 30, 1, 5, tzinfo=timezone.utc)  # 08:05 Bangkok
    assert optimized_runner._local_slot_due("IG-CD", account, {}, now)

    completed = {
        "IG-CD": {"success_times": [datetime(2026, 8, 30, 1, 2, tzinfo=timezone.utc)]}
    }
    assert not optimized_runner._local_slot_due("IG-CD", account, completed, now)
    outside_window = datetime(2026, 8, 30, 2, 0, tzinfo=timezone.utc)  # 09:00 Bangkok
    assert not optimized_runner._local_slot_due("IG-CD", account, {}, outside_window)
    print("OK test_account_local_posting_slots_are_timezone_aware_and_idempotent")


def test_source_import_duplicate_headers_do_not_abort_uploads():
    from sheets_reader import SheetsReader

    reader = SheetsReader.__new__(SheetsReader)
    reader.SOURCE_HEADER_ROW = 1
    reader.source_ws = types.SimpleNamespace(get_all_values=lambda: [
        ["STK", "image1 link", "image1 link", "PRODUCT LINK"],
        ["100", "https://media.example/100/center.jpg", "duplicate", "https://example.com/100"],
    ])
    sources = reader.get_source_rows()
    assert "100" in sources, sources
    assert sources["100"]["main_image"] == "https://media.example/100/center.jpg"
    print("OK test_source_import_duplicate_headers_do_not_abort_uploads")


def test_supplemental_model_media_merges_by_sku_without_new_products():
    from sheets_reader import SheetsReader

    reader = SheetsReader.__new__(SheetsReader)
    reader.model_media_ws = types.SimpleNamespace(get_all_values=lambda: [
        ["sku", "multiple model photo link", "multiple model video link"],
        [
            "298",
            "https://cdn.example/298/model-1.jpg\nhttps://cdn.example/298/model-2.jpg",
            "https://cdn.example/298/model.mp4",
        ],
        ["not-in-primary", "https://cdn.example/other.jpg", ""],
    ])
    sources = {
        "298": {
            "model_images": ["https://cdn.example/298/model-1.jpg"],
            "model_videos": [],
        }
    }

    reader._merge_model_media_rows(sources)

    assert sources["298"]["model_images"] == [
        "https://cdn.example/298/model-1.jpg",
        "https://cdn.example/298/model-2.jpg",
    ]
    assert sources["298"]["model_videos"] == ["https://cdn.example/298/model.mp4"]
    assert "not-in-primary" not in sources
    print("OK test_supplemental_model_media_merges_by_sku_without_new_products")


def test_model_media_ftp_schema_merges_by_stock_id():
    from sheets_reader import SheetsReader

    reader = SheetsReader.__new__(SheetsReader)
    reader.model_media_ws = types.SimpleNamespace(get_all_values=lambda: [
        ["Stock ID", "Model Photo Links", "Model Video Links", "FTP Folder"],
        [
            "416",
            "https://colourdiam.com/Product/Model%20Photo%20Video/416/center.jpeg\n"
            "https://colourdiam.com/Product/Model%20Photo%20Video/416/2.jpeg",
            "https://colourdiam.com/Product/Model%20Photo%20Video/416/video.mp4",
            "416",
        ],
    ])
    sources = {"416": {"model_images": [], "model_videos": []}}

    reader._merge_model_media_rows(sources)

    assert len(sources["416"]["model_images"]) == 2
    assert sources["416"]["model_videos"] == [
        "https://colourdiam.com/Product/Model%20Photo%20Video/416/video.mp4"
    ]
    print("OK test_model_media_ftp_schema_merges_by_stock_id")


def test_source_import_builds_product_name_from_details():
    from sheets_reader import SheetsReader

    reader = SheetsReader.__new__(SheetsReader)
    reader.SOURCE_HEADER_ROW = 1
    reader.source_ws = types.SimpleNamespace(get_all_values=lambda: [
        ["STK", "DETAILS", "CODE", "image1 link", "PRODUCT LINK"],
        [
            "309",
            "Sku - 309\nWeight - 0.16\nColour - Fancy Deep Pink\nShape - Marquise\nClarity - SI2",
            "Fancy Deep Pink",
            "https://media.example/309/center.jpg",
            "https://www.colourdiam.com/diamonddetails/Diamonds/Product/309",
        ],
        [
            "1263",
            "0.30 – Fancy Green VS\n18K Ring : 5.183 gram",
            "",
            "https://media.example/1263/center.jpg",
            "https://colourdiam.com/productdetail/1263",
        ],
    ])
    sources = reader.get_source_rows()
    assert sources["309"]["product_name"] == "0.16 Ct Fancy Deep Pink Marquise Diamond SI2"
    assert sources["1263"]["product_name"] == "0.30 – Fancy Green VS"
    print("OK test_source_import_builds_product_name_from_details")


def test_compound_sku_product_links_are_not_false_mismatches():
    from sheets_reader import SheetsReader

    reader = SheetsReader.__new__(SheetsReader)
    reader.SOURCE_HEADER_ROW = 1
    reader.source_ws = types.SimpleNamespace(get_all_values=lambda: [
        ["STK", "image1 link", "PRODUCT LINK"],
        [
            "665_142",
            "https://www.colourdiam.com/Product/Jewellery/665_142/white45/center.jpg",
            "https://colourdiam.com/productdetail/Menu/665/142",
        ],
        [
            "3443_3444",
            "https://www.colourdiam.com/Product/Jewellery/3442/white45/center.jpg",
            "https://colourdiam.com/productdetail/Menu/3443/3444",
        ],
    ])
    sources = reader.get_source_rows()
    assert sources["665_142"]["integrity_error"] == ""
    assert "another SKU" in sources["3443_3444"]["integrity_error"]
    print("OK test_compound_sku_product_links_are_not_false_mismatches")


def test_glued_model_photo_and_video_links_use_model_photo_video_origin():
    from sheets_reader import SheetsReader

    assert SheetsReader._split_urls(
        "https://colourdiam.com/Product/Model%20Photo%20Video/415/1.jpeg"
        "https://colourdiam.com/Product/Model%20Photo%20Video/415/2.jpeg"
    ) == [
        "https://colourdiam.com/Product/Model%20Photo%20Video/415/1.jpeg",
        "https://colourdiam.com/Product/Model%20Photo%20Video/415/2.jpeg",
    ]
    assert SheetsReader._rewrite_model_media_url(
        "https://images.colourdiam.com/products/STK298-1.jpg", "298", "image",
        filename="1.jpeg",
    ) == "https://colourdiam.com/Product/Model%20Photo%20Video/298/1.jpeg"
    assert SheetsReader._rewrite_model_media_url(
        "https://images.colourdiam.com/videos/STK298/model-video.mp4",
        "298",
        "video",
        filename="video.mp4",
    ) == "https://colourdiam.com/Product/Model%20Photo%20Video/298/video.mp4"
    assert SheetsReader._rewrite_model_media_url(
        "https://colourdiam.com/Product/Model%20Photo%20Video/414/video.mp4",
        "414",
        "video",
    ) == "https://colourdiam.com/Product/Model%20Photo%20Video/414/video.mp4"
    # Preserve exact FTP folder aliases and filenames; never rebuild canonical URLs.
    alias = "https://colourdiam.com/Product/Model%20Photo%20Video/1333_6674/model%20video.mp4"
    assert SheetsReader._rewrite_model_media_url(
        alias, "1333_6674_6673", "video", filename="video.mp4"
    ) == alias
    assert SheetsReader._rewrite_model_media_url(
        "https://cdn.shopify.com/s/files/1/0651/4135/1614/files/1996_6379_1.mp4",
        "1996_6379",
        "video",
    ) == "https://cdn.shopify.com/s/files/1/0651/4135/1614/files/1996_6379_1.mp4"

    reader = SheetsReader.__new__(SheetsReader)
    reader.SOURCE_HEADER_ROW = 1
    reader.source_ws = types.SimpleNamespace(get_all_values=lambda: [
        [
            "STK", "image1 link",
            "model image link 1", "model image link 2", "model image link 3",
            "multiple model photo link", "model video link 1",
        ],
        [
            "298",
            "https://colourdiam.com/Product/Jewellery/298/white45/center.jpg",
            "https://images.colourdiam.com/products/STK298-1.jpg",
            "https://images.colourdiam.com/products/STK298-2.jpg",
            "https://images.colourdiam.com/products/STK298-3.jpg",
            "https://colourdiam.com/Product/Model%20Photo%20Video/298/1.jpeg"
            "https://colourdiam.com/Product/Model%20Photo%20Video/298/center.jpeg",
            "https://images.colourdiam.com/videos/STK298/model-video.mp4",
        ],
    ])
    sources = reader.get_source_rows()
    assert sources["298"]["model_images"] == [
        "https://colourdiam.com/Product/Model%20Photo%20Video/298/1.jpeg",
        "https://colourdiam.com/Product/Model%20Photo%20Video/298/2.jpeg",
        "https://colourdiam.com/Product/Model%20Photo%20Video/298/3.jpeg",
        "https://colourdiam.com/Product/Model%20Photo%20Video/298/center.jpeg",
    ]
    assert sources["298"]["model_videos"] == [
        "https://colourdiam.com/Product/Model%20Photo%20Video/298/video.mp4",
    ]
    print("OK test_glued_model_photo_and_video_links_use_model_photo_video_origin")


def test_dead_model_photo_video_http_urls_fall_back_to_jewellery_folder():
    from sheets_reader import SheetsReader

    fallback = SheetsReader._model_media_http_fallback(
        "https://colourdiam.com/Product/Model%20Photo%20Video/414/1.jpeg",
        "414",
    )
    assert fallback == "https://colourdiam.com/Product/Jewellery/Model%20images/414/1.jpeg"
    assert SheetsReader._model_media_http_fallback(
        "https://cdn.shopify.com/s/files/1/0651/4135/1614/files/1996_6379_1.mp4",
        "1996_6379",
    ) == ""
    print("OK test_dead_model_photo_video_http_urls_fall_back_to_jewellery_folder")


def test_dead_cdn_hosts_are_rewritten_onto_live_origin():
    import optimized_runner

    rewritten = optimized_runner._rewrite_media_url(
        "https://images.colourdiam.com/videos/STK298/model-video.mp4"
    )
    assert rewritten == "https://www.colourdiam.com/videos/STK298/model-video.mp4"
    urls = optimized_runner._rewrite_media_urls(
        ["https://videos.colourdiam.com/stk461-1.mp4"],
        include_fallbacks=True,
    )
    assert urls[0] == "https://www.colourdiam.com/stk461-1.mp4"
    assert "https://colourdiam.com/stk461-1.mp4" in urls
    print("OK test_dead_cdn_hosts_are_rewritten_onto_live_origin")


def test_youtube_jobs_are_queued_when_only_product_images_mismatch():
    from job_generator import generate_jobs, _is_clean_source

    source = _source("4403")
    source["integrity_error"] = (
        "main image belongs to another SKU "
        "(https://www.colourdiam.com/Product/Jewellery/4377/white45/center.jpg)"
    )
    source["main_image"] = "https://www.colourdiam.com/Product/Jewellery/4377/white45/center.jpg"
    source["images"] = [source["main_image"]]
    source["video_url"] = "https://colourdiam.com/Product/Jewellery/4403/white45/vid.mp4"
    source["model_videos"] = [
        "https://colourdiam.com/Product/Jewellery/4403/model.mp4",
        "https://colourdiam.com/Product/Jewellery/4377/model.mp4",
    ]
    accounts = [
        _account("FB-CD", "facebook"),
        _account("YT-CD", "youtube"),
    ]
    jobs = generate_jobs({"4403": source}, accounts)
    selections = {
        (job["account_id"], job["media_selection"])
        for job in jobs
        if job["status"] != "needs_review"
    }
    assert ("YT-CD", "product_video") not in selections, jobs
    assert ("YT-CD", "model_video:0") in selections, jobs
    assert ("YT-CD", "model_video:1") not in selections, jobs
    assert ("FB-CD", "model_video:0") in selections, jobs
    assert ("FB-CD", "model_video:1") not in selections, jobs
    assert ("FB-CD", "carousel") not in selections, jobs
    assert ("FB-CD", "product_video") not in selections, jobs
    assert _is_clean_source(source)[0] is False
    assert _is_clean_source(source, {"media_selection": "model_video:0", "platform": "facebook"})[0] is True
    assert _is_clean_source(source, {"media_selection": "model_video:1", "platform": "facebook"})[0] is False
    print("OK test_youtube_jobs_are_queued_when_only_product_images_mismatch")


def test_dns_and_integrity_preflight_failures_are_retried():
    import optimized_runner

    job = {
        "error_message": "Media preflight failed: all media URLs are unavailable, dead, DNS-invalid, or not media",
        "notes": "Auto-cleaned: media failed production preflight",
    }
    assert not optimized_runner._known_unusable_pending(job)
    auth_job = {
        "error_message": "invalid_grant",
        "notes": "",
    }
    assert optimized_runner._known_unusable_pending(auth_job)
    print("OK test_dns_and_integrity_preflight_failures_are_retried")


def test_ffprobe_timeout_is_not_broken_media():
    timeout = "Media validation failed (https://colourdiam.com/x.mp4): ffprobe timed out"
    truncated = "('Connection broken: IncompleteRead(16384 bytes read)', IncompleteRead(16384 bytes read))"
    assert not main._is_broken_media_error(timeout)
    assert not main._is_broken_media_error(truncated)
    assert main._is_broken_media_error("Media validation failed: 404 not found")
    print("OK test_ffprobe_timeout_is_not_broken_media")


if __name__ == "__main__":
    test_generate_queues_model_media_only()
    test_round_robin_prefers_model_media_within_platform()
    test_generate_is_idempotent()
    test_generate_uses_stable_job_id_when_mutable_key_drifted()
    test_generate_skips_unlinked_instagram_placeholders()
    test_generation_cap_is_fair_across_accounts()
    test_generation_cap_stays_on_model_media()
    test_one_failure_does_not_stop_others()
    test_second_run_does_not_repost_uploaded()
    test_generate_new_platforms()
    test_publish_new_platforms_routing()
    test_round_robin_jobs_mix_platforms()
    test_resolve_ig_user_id_handles_linked_and_unlinked_pages()
    test_filter_valid_media_drops_dead_links()
    test_optimized_preflight_skips_corrupt_video_and_uses_next_job()
    test_video_preflight_result_is_cached_across_accounts()
    test_all_primary_accounts_prioritize_24h_deficit()
    test_queue_state_counts_only_successes_in_rolling_24h()
    test_stable_job_id_blocks_duplicate_when_media_changed()
    test_account_local_posting_slots_are_timezone_aware_and_idempotent()
    test_source_import_duplicate_headers_do_not_abort_uploads()
    test_supplemental_model_media_merges_by_sku_without_new_products()
    test_model_media_ftp_schema_merges_by_stock_id()
    test_source_import_builds_product_name_from_details()
    test_compound_sku_product_links_are_not_false_mismatches()
    test_glued_model_photo_and_video_links_use_model_photo_video_origin()
    test_dead_model_photo_video_http_urls_fall_back_to_jewellery_folder()
    test_dead_cdn_hosts_are_rewritten_onto_live_origin()
    test_youtube_jobs_are_queued_when_only_product_images_mismatch()
    test_dns_and_integrity_preflight_failures_are_retried()
    test_ffprobe_timeout_is_not_broken_media()
    print("All pipeline tests passed.")

import os

import requests
from dotenv import load_dotenv

load_dotenv()

NEW_WORKBOOK_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "1jjC4oaWsyqLzG6vT5EwJkVAgJCXGpz_7fWr6wb7OU3o/edit"
)
MODEL_MEDIA_WORKBOOK_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "1kAD1ASXaaqrBmNHDVMYgj_cfW8pFJPEiRCY8ENutAvQ/edit"
)


def _env(key, default):
    """Return env value, falling back to default when unset or empty."""
    value = os.getenv(key)
    return value.strip() if value is not None and value.strip() else default


class Config:
    GOOGLE_SHEET_CREDENTIALS = _env("GOOGLE_SHEET_CREDENTIALS", "credentials/service_account.json")
    GOOGLE_SHEET_URL = _env("GOOGLE_SHEET_URL", NEW_WORKBOOK_URL)
    GOOGLE_SHEET_NAME = _env("GOOGLE_SHEET_NAME", "")
    MODEL_MEDIA_SHEET_URL = _env("MODEL_MEDIA_SHEET_URL", MODEL_MEDIA_WORKBOOK_URL)
    MODEL_MEDIA_SOURCE_SHEET = _env("MODEL_MEDIA_SOURCE_SHEET", "Model Media FTP")

    SOURCE_IMPORT_SHEET = _env("SOURCE_IMPORT_SHEET", "Source Import")
    ACCOUNTS_SHEET = _env("ACCOUNTS_SHEET", "Accounts")
    QUEUE_SHEET = _env("QUEUE_SHEET", "Publishing Queue")
    LOG_SHEET = _env("LOG_SHEET", "Publishing Log")
    UPLOAD_GUIDE_SHEET = _env("UPLOAD_GUIDE_SHEET", "UPLOAD GUIDE")

    FB_ACCESS_TOKEN = os.getenv("FB_ACCESS_TOKEN") or os.getenv("FACEBOOKTOKEN") or os.getenv("FACEBOOKDEBUGTOKEN")

    GOOGLE_CLOUD_PROJECT = _env("GOOGLE_CLOUD_PROJECT", "")
    USE_SECRET_MANAGER = _env("USE_SECRET_MANAGER", "false").lower() == "true"

    MAX_JOB_ATTEMPTS = int(_env("MAX_JOB_ATTEMPTS", "3"))
    # Validate media (HTTP status, content-type, magic bytes, ffprobe for
    # videos) before calling a platform API. Broken/truncated files are then
    # marked needs_review instead of burning retry attempts. Set to false in
    # unit tests or when the media host is unreachable from CI.
    MEDIA_VALIDATION = _env("MEDIA_VALIDATION", "true").lower() in ("true", "1", "yes")
    # Keep each scheduled run intentionally small. The workflow runs every
    # 2 minutes and uploads one job at a time to stay well under Google Sheets
    # and platform API quota while still draining the queue continuously.
    MAX_JOBS_PER_RUN = int(_env("MAX_JOBS_PER_RUN", "1"))
    # Generate only a small number of new queue rows per run as well. This
    # prevents queue generation itself from causing a Google Sheets write spike.
    MAX_GENERATE_JOBS = int(_env("MAX_GENERATE_JOBS", "1"))
    JOB_STATUS_FAILED = "failed"
    JOB_STATUS_UPLOADED = "uploaded"
    JOB_STATUS_SKIPPED = "skipped"
    JOB_STATUS_NEEDS_REVIEW = "needs_review"

    @classmethod
    def get_token(cls, credential_property_key=""):
        """Resolve an account token by its credential property key.

        Falls back to the shared FB_ACCESS_TOKEN so accounts that share the
        same user-level token still work without per-account secrets.
        """
        if credential_property_key:
            token = os.getenv(credential_property_key)
            if token:
                return token.strip()
        return cls.FB_ACCESS_TOKEN

    @classmethod
    def get_pages(cls):
        if not cls.FB_ACCESS_TOKEN:
            return []
        resp = requests.get(
            "https://graph.facebook.com/v26.0/me/accounts",
            params={"access_token": cls.FB_ACCESS_TOKEN},
            timeout=15,
        )
        data = resp.json()
        pages = []
        for page in data.get("data", []):
            page_id = page["id"]
            page_token = page["access_token"]
            page_name = page.get("name", "")
            ig_resp = requests.get(
                f"https://graph.facebook.com/v26.0/{page_id}",
                params={
                    "fields": "instagram_business_account",
                    "access_token": cls.FB_ACCESS_TOKEN,
                },
                timeout=15,
            )
            ig_data = ig_resp.json()
            ig_user_id = ig_data.get("instagram_business_account", {}).get("id", "")
            pages.append({
                "name": page_name,
                "page_id": page_id,
                "page_token": page_token,
                "ig_user_id": ig_user_id,
            })
        return pages

# Upload Status Report

Updated: 2026-08-21 (workflow run 32488953691)

> Status updated 2026-08-22: per the workbook Accounts tab, IG-BKK/TREND/LTD/CD, IG-INDO, IG-RUS, IG-KOR, YT-CD, YT-JIYA, and FB-INDO are now `enabled = Yes`. The "Remaining work" bullets below (IG accounts `enabled = No`, YT-CD `enabled = No`) are outdated.

## Last run (Auto Upload workflow, manual dispatch) — SUCCESS

- Status: completed/success
- 40 jobs processed (MAX_JOBS_PER_RUN=40), 40 uploaded, 0 failed
- First successful run: carousel + product video + model videos posted across ISR/JPN/KOR/RUS (sample URLs above in run log)
- Fixes applied:
  1. `FACEBOOKTOKEN` secret updated with a valid token (was: expired/invalid, code 190).
  2. `facebook_uploader.py`: auto-resolves the page-scoped token from the user token (`GET /{page_id}?fields=access_token`, cached per page). Fixes `(#200) Unpublished posts must be posted to a page as the page itself` and `(#100) No permission to publish the video`.
  3. Workflow now reads YouTube client secret from the `YOUTUBEJSON` secret (was referencing nonexistent `YOUTUBE_CLIENT_SECRET_JSON`).

## Remaining work

- Queue: ~22,515 Facebook jobs still pending; 40 are processed per run (scheduled cron every 2h will drain it).
- Media URLs: some Source Import URLs return 404 (e.g. `colourdiam.com/Product/Jewellery/298/white45/center.jpg`), which fails posts with `Missing or invalid image file` (code 324). Verify/fix source links if such errors appear.
- Instagram: 4 accounts (IG-BKK/TREND/LTD/CD) still `enabled = No`, no IG business account IDs resolved (`GET /{page_id}?fields=instagram_business_account` returned none). No IG jobs generated.
- YouTube: YT-CD still `enabled = No`, no channel ID. `YOUTUBEJSON` is now wired to the client-secret step; OAuth auth code + token still needed (`YOUTUBE_AUTH_CODE` secret does not exist yet).

## Account token map (from GitHub Secrets)

- `FACEBOOKTOKEN` — valid, updated 2026-08-21
- `FACEBOOKDEBUGTOKEN` — fallback (unchanged)
- `APIKEYJSON`, `GOOGLE_SHEET_CREDENTIALS_JSON` — Sheets access
- `YOUTUBEJSON`, `YOUTUBEKEY` — YouTube (client secret / API key)

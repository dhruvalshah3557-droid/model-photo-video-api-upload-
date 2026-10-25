# AGENTS.md

Project facts and conventions for the `auto_upload` project. These are persistent — no need to re-explain.

## Hard rule: always save to GitHub

- ALWAYS commit and push changes to this repo after making them: `https://github.com/dhruvalshah3557-droid/model-photo-video-api-upload-` (`origin/main`). Do not leave work only in the local working tree or only in local commits. If the remote moved (concurrent agent), rebase onto `origin/main` and push.
- Remote must stay `origin` → `https://github.com/dhruvalshah3557-droid/model-photo-video-api-upload-`. Do not push project updates to other remotes.
- This is the user's explicit standing instruction.

## Project

- Social-media auto uploader (`auto_upload/`) that publishes **model photo and model video only** to Facebook, Instagram, YouTube, Pinterest, LINE, WeChat, TikTok, X, LinkedIn and Twitch. Product carousels and product Reels are never generated or uploaded.
- Code: `auto_upload/main.py`, `sheets_reader.py`, `job_generator.py`, `facebook_uploader.py`, `instagram_uploader.py`, `youtube_uploader.py`, `caption_generator.py`, `product_scraper.py`, `config.py`, `media_prep.py`, `pinterest_uploader.py`, `line_uploader.py`, `wechat_uploader.py`, `tiktok_uploader.py`, `x_uploader.py`, `linkedin_uploader.py`, `twitch_uploader.py`, `shopee_uploader.py`, `lazada_uploader.py`.

## Workbook structure (authoritative)

The uploader reads/writes ONE Google Sheets workbook:
- URL: `https://docs.google.com/spreadsheets/d/1jjC4oaWsyqLzG6vT5EwJkVAgJCXGpz_7fWr6wb7OU3o/edit`
- Sheet ID: `1jjC4oaWsyqLzG6vT5EwJkVAgJCXGpz_7fWr6wb7OU3o`

Tabs (only these):
- `UPLOAD GUIDE` — workflow rules (carousel order, tagging, safety). Read before changing uploader logic.
- `Sheet1` — HUMAN PREVIEW ONLY. The uploader must NOT use Sheet1 as its data source.
- `Source Import` — primary product/media/content source. One row per SKU. Headers in row 1.
- `Accounts` — destination account settings (per-account tokens, language, formats, product_tagging). Headers in row 1.
- `Publishing Queue` — upload jobs (read + status updates). Headers in row 1.
- `Publishing Log` — upload result history (write-only). Headers in row 1.

Flow: Source Import → Accounts → Publishing Queue → Publishing Log.

## Credentials / secrets

- All real credentials are stored in **GitHub Secrets** (repo: `dhruvalshah3557-droid/api-upload-facebook-instagram-youtube-`), NOT in the repo or local env.
- GitHub Secrets are write-only and cannot be read back via API. To access the workbook locally, either:
  - Share the sheet to anyone with the link (public read) and fetch via `https://docs.google.com/spreadsheets/d/<ID>/gviz/tq?tqx=out:json&sheet=<Tab>`, or
  - Paste the service account JSON locally into `auto_upload/credentials/service_account.json` (gitignored).
- GitHub Actions workflow: `.github/workflows/auto-upload-production.yml` — scheduled on 10-minute boundaries + manual dispatch + push on `.github/production-trigger.txt`. It materializes credentials from secrets, runs Meta account synchronization, then `python optimized_runner.py`. Concurrency group `auto-upload-production` uses `cancel-in-progress: false`. Production sets `MAX_JOBS_PER_RUN: "30"` and `MAX_GENERATE_JOBS: "80"`; the runner batches/throttles Sheets work, selects accounts fairly, and allocates one slot per due publish-ready Facebook and Instagram account. Leftover slots stay on model photo/video from the Model Photo Video folder instead of product carousels. Instagram is capped at `IG_MAX_JOBS_PER_RUN` (production 8). Facebook pages that already hit the 24-hour floor do not keep unused slots; that budget goes to Instagram first, then YouTube (`YT_MAX_JOBS_PER_RUN` production 12), which can upload multiple model videos from the same channel in one run. YouTube descriptions append multi-regional captions and hashtags (Thai, Japanese, Korean, Chinese, Arabic, Hebrew, Russian, Vietnamese, Indonesian, Spanish, French, Italian, German, Turkish, Hindi) plus matching snippet tags for search reach. Unused YouTube/TikTok slots (dead OAuth or no healthy job) stay on Facebook/Instagram model photo/video instead of product carousels. LINE is excluded while its monthly Messaging API quota is exhausted.
- Per-account tokens: `Accounts.credential_property_key` names an env var (e.g. `META_TOKEN_FB_ISR`). `Config.get_token()` reads it, falling back to the shared `FB_ACCESS_TOKEN`. The workflow maps each `META_TOKEN_*` to a GitHub secret. Meta Graph API calls use **v26.0** (`facebook_uploader.py`, `instagram_uploader.py`, `meta_account_sync.py`, `config.py`, `dump_pages.py`, `get_fb_token.py`, and `verify-token.yml`).
- The workbook is publicly readable: column maps can be re-verified with the gviz endpoint above without credentials.
- To run the pipeline from a local/agent environment, trigger the GitHub Actions workflow (secrets live only there). The environment's git credential helper can provide a GitHub token: `printf "protocol=https\nhost=github.com\n\n" | git credential fill` → `password`. Use it to `POST /repos/<owner>/<repo>/actions/workflows/auto-upload.yml/dispatches` with `{"ref":"master"}` (HTTP 204 = accepted). Never print the token; reuse it in a shell var and unset after. Credentials are ALWAYS in GitHub Secrets, never local env or the repo.

## Source Import columns (1-based)

- `B` = STK/SKU (the product tag value; never use SR NO). STK arrives as a number (e.g. `298.0`); normalize with `_normalize_sku`.
- `K:R` = product images `image1 link`..`image8 link`; the file whose name contains `center` is the MAIN image (fallback: first nonblank).
- `S` = `video link` (product video → its own Reel/video job; never embedded in carousels).
- `T` = `multiple side image link` (side images; `center.*` URLs are removed).
- `V/W/X` + `Y` = model images (`model image link 1/2/3`, `multiple model photo link`). Glued `https://https://` cells are split. Canonical Model Photo Video URLs are kept, then 404s fall back to live `https://colourdiam.com/Product/Jewellery/Model%20images/{SKU}/{file}`. Optional `FTP_HOST`/`FTP_USER`/`FTP_PASSWORD` listing maps that same public origin. Legacy dead-host links still rewrite onto Model Photo Video and must pass media validation before publishing.
- `Z/AA/AB` + `AC` = model videos (`model video link 1/2/3`, `multiple model video link`; AC deduped against Z:AB). Same split and canonical-source handling as model photos. Model Media Lookup imports Main Data Model Media FTP; never rewrite those canonical URLs back to the legacy Jewellery/Model images folder. Live `AA`/`AB`/`AC` headers may be `#REF!`/blank; missing named headers still read those columns.
- `AI` = `INSTAGRAM CAPTION`, `AJ` = `FACEBOOK CAPTION`, `BD` = `YouTube Shorts Caption`, `AR` = `HASHTAGS`.
- Per-language: `<Lang> Description` / `<Lang> Hashtag` columns for Burmese, Thai, Filipino, Chinese, Russian, Japanese, Korean, Hebrew (`israli` spelling in the live headers), Arabic, Spanish, Indonesian, Italian, French, German, Danish, Polish (`polish hahstag` spelling), Turkish, Swedish, Lebanese (`lebenesse description` / `lebenesse hashtag`) and Czech (`vestslavic description` / `vestslavic hashtag`). Vietnamese accepts both `Vietnamese Description` / `Vietnamese Hashtag` and the live `vietnam description` / `vietnam hashtag` headers. Greece (`el-GR`) has no Source Import column; `build_caption` generates native Greek copy. Live `greek description` / `greek hashtag` headers are Turkey aliases, not Greece. Empty regional cells (Sweden, Germany, Poland, Denmark, France, Italy, Spain, Vietnam, Indonesia, Hebrew, Arabic, Lebanon, Czech, Turkey, Greece and the rest) still get native fallback copy so those pages are not starved or marked `needs_review`.
- `G` = `LAB` (value `NON CERTIFIED` blocks auto-publish — review only), `Status` = generated-content status (`Error: ...` / `429` rows are skipped as incomplete).

## Accounts sheet

Columns include `account_id`, `platform`, `account_name`, `platform_account_id`, `primary_language`, `timezone`, `enabled`, `allowed_formats`, `min_gap_minutes`, `product_tagging`, `catalog_or_store_id`, `credential_property_key`, `approval_required`.

- Only `enabled = Yes` accounts are used. Enabled Facebook/Instagram/YouTube destinations remain as on the live Accounts tab. LINE-CD is temporarily disabled while the Messaging API monthly broadcast quota is exhausted and must not consume Facebook/Instagram/YouTube delivery capacity. Disabled until tokens/IDs are filled in: WECHAT-CD, SHOPEE-CD, LAZADA-CD, TWITCH-CD, PINTEREST-CD, THREADS-CD. YT-CD, YT-JIYA and YT-TREND (`@trendingjewels`, Colour Diam Google `colourdiamondss@gmail.com`) are enabled YouTube destinations. Instagram placeholders with blank platform IDs (Germany, Denmark, France, China) intentionally remain enabled. Meta sync fills a matching placeholder instead of appending a duplicate once the linked professional account becomes visible. Until then, production probes them but skips them without consuming an Instagram slot or generation budget, so valid pages continue posting. Sweden, Poland and Turkey now have live Instagram IDs and must receive jobs. Greece is a discovered market: page/IG names containing `greece` map to `FB-GREECE` / `IG-GREECE`, language `el-GR`, timezone `Europe/Athens`, and credential keys `META_TOKEN_FB_GREECE` / `META_TOKEN_IG_GREECE`. There is no Source Import Greek column — captions use native Greek fallback generation; the live `greek description` / `greek hashtag` headers belong to Turkey. Lebanon and Czech are the next discovered markets: `lebanon`/`lebanese`/`lebenesse` map to `FB-LEBANON` / `IG-LEBANON`, language `lb-LB`, timezone `Asia/Beirut`, credentials `META_TOKEN_FB_LEBANON` / `META_TOKEN_IG_LEBANON`; `czech`/`czechia`/`vestslavic` map to `FB-CZECH` / `IG-CZECH`, language `cs-CZ`, timezone `Europe/Prague`, credentials `META_TOKEN_FB_CZECH` / `META_TOKEN_IG_CZECH`. Source Import uses the live misspelled `lebenesse` and `vestslavic` headers for those markets. They are not yet on the live Accounts tab or the current 24-page Meta token; sync will add them (enabled=No until reviewed) once the pages appear on `me/accounts`.
- Credential keys map to GitHub secrets: `META_TOKEN_FB_ISR`..`META_TOKEN_FB_NFCD`, `META_TOKEN_FB_INDO`, `META_TOKEN_FB_GREECE`, `META_TOKEN_FB_LEBANON`, `META_TOKEN_FB_CZECH`, `META_TOKEN_IG_BKK/TREND/LTD/CD/INDO/RUS/KOR/JPN/MMR/PH`, `META_TOKEN_IG_GREECE`, `META_TOKEN_IG_LEBANON`, `META_TOKEN_IG_CZECH`, plus `YOUTUBE_OAUTH_REFRESH_TOKEN`, `YOUTUBE_OAUTH_REFRESH_TOKEN_JIYA`, `YOUTUBE_OAUTH_REFRESH_TOKEN_COLOURDIAMONDSS`, `YOUTUBE_OAUTH_REFRESH_TOKEN_TRENDINGJEWELS`, `YOUTUBE_CLIENT_ID_COLOURDIAMONDSS` and `YOUTUBE_CLIENT_SECRET_COLOURDIAMONDSS`. YT-CD is `@colourdiamondss`. YT-TREND is `@trendingjewels` (`UCWSGpmJfBIzcA9O9kFWaRNw`) on the same Colour Diam Google client. A YouTube channel whose latest OAuth error is `invalid_grant`/`invalid_client` is skipped for 12 hours so it cannot burn the only YouTube slot. LINE uses `LINE_CHANNEL_ACCESS_TOKEN` (per-account override `META_TOKEN_LINE_CD`). WeChat uses `WECHAT_APPID`/`WECHAT_APPSECRET` (per-account `META_TOKEN_WECHAT_CD=APPID:SECRET`). Pinterest uses `PINTEREST_ACCESS_TOKEN` (per-account `META_TOKEN_PINTEREST_CD`). Twitch uses `TWITCH_STREAM_KEY` (per-account `META_TOKEN_TWITCH_CD`), plus optional `TWITCH_CLIENT_ID`/`TWITCH_CLIENT_SECRET`/`TWITCH_BROADCASTER_ID`/`TWITCH_ACCESS_TOKEN`. Shopee uses `SHOPEE_PARTNER_ID`/`SHOPEE_PARTNER_KEY`/`SHOPEE_ACCESS_TOKEN`/`SHOPEE_SHOP_ID` (per-account override `META_TOKEN_SHOPEE_CD`). Lazada uses `LAZADA_REGION`/`LAZADA_APP_KEY`/`LAZADA_APP_SECRET`/`LAZADA_ACCESS_TOKEN` (per-account override `META_TOKEN_LAZADA_CD`).
- `product_tagging = Yes` means every FB/IG post is tagged with the row's `STK`/SKU (Publishing Queue `stock_id_tag` / `tag_stock_id_used`). A rejected tag → `tagging_status = Failed` + exact API error in `error_message`.

## Publishing Queue

Columns: `job_id, sku, account_id, media_selection, platform, format, language, scheduled_at, timezone, stock_id_tag, status, attempts, last_attempt_at, platform_post_id, published_url, error_message, notes, tagging_status, tag_stock_id_used, caption_final`.

- `media_selection` values used by this repo: `model_video:<n>`, `model_photo:<n>`. Existing product `carousel` / `product_video` queue rows are ignored.
- `status` lifecycle: empty/pending → `uploaded` | `failed` (after `MAX_JOB_ATTEMPTS`) | `skipped` | `needs_review`.
- `python main.py --generate` fills the queue idempotently from clean Source Import rows (one job per model video, one job per model photo, per enabled account). Missing jobs are grouped by account and selected in rotating 10-minute account windows, one job/account/pass. Production `optimized_runner.py` always runs fair generation, waits 65 seconds for the Sheets quota window, then processes pending model photo/video jobs. `MAX_GENERATE_JOBS` / `MAX_JOBS_PER_RUN` cap generation and uploads per run.

## Publishing Log

Columns: `job_id, attempt_time, result, platform_post_id, published_url, api_error_code, error_message, next_retry_at, raw_response_reference, notes`. Appended after every attempt.

## Upload behavior

- `main.py` reads pending jobs from Publishing Queue, resolves media from Source Import by SKU, builds the caption per account language, tags with the same SKU, uploads, then writes status back to the queue and an entry to the log.
- Caption precedence (`build_caption`): primary_language translation + matching hashtags → native-language generated copy when the Source Import cell is empty or describes a different stone (wrong carat/shape) → platform caption + hashtags (English accounts only, same mismatch check) → auto-generated from Source Import DETAILS (no cross-language mixing). A missing or mismatched regional Source cell never refuses the job and never falls back to English.
- Hashtag formatting: regional and fallback hashtag cells are normalized before publishing. Comma/semicolon-separated values and bare words receive `#`, duplicates are removed, and output is space-separated so tags remain clickable. A regional account never inherits the general English hashtag field; an empty localized hashtag cell uses a native-language fallback for that market.
- Carousel media order: MAIN center image → side images only. The product video is NEVER in a carousel — it is always its own separate Reel/video job (avoids duplicate posts).
- Automatic audio handling (`media_prep.py`): original usable audio is preserved. Silent/muted videos receive real instrumental music via ffmpeg. Selection priority is `BACKGROUND_MUSIC_URLS` (comma/newline-separated approved URLs), `BACKGROUND_MUSIC_PATH` (audio file or directory), then 58 CC0 tracks pinned to immutable commits of `effacestudios/Royalty-Free-Music-Pack` and `uncle-sheepsky/duru-ai-cc0-bgm`. A SHA-256 hash of the source video URL rotates both track and starting section consistently; the retired single-track `BACKGROUND_MUSIC_URL` is intentionally ignored. Music is loudness-normalized and faded at both ends.
- Each model video is its OWN Reel/video job. Each model photo is its own image/carousel job. Upload, generation, and production selection only use model video then model photo. Product Reels and product carousels are never queued or published. Instagram pairing with the Facebook SKU also cannot outrank model media.
- Instagram images Meta cannot fetch by public URL (`still.jpg` and similar 9004/2207052 failures) are retried by uploading the original file bytes instead of `image_url`. Parked `needs_review` rows with those errors are revived automatically.
- Media with video extension (`.mp4`/`.mov`/`.avi`/`.mkv`/`.webm`) → video/Reel; otherwise photo/carousel.
- Instagram single videos: published as REELS with share_to_feed=true for every aspect ratio; VIDEO is only used for carousel children. The prepared file retains its soundtrack and square/landscape dimensions; vertical sources use the existing uncropped 9:16 padding. The original file bytes are uploaded via multipart (`prepare_video`), not a remote URL, to preserve resolution. Instagram carousels use the mixed children API (video children use `media_type=VIDEO`, not REELS).
- Instagram catalog music: silent Reels first try Meta's authorized `/ig_audio` catalog with market-specific trending, local-pop and luxury-instrumental searches derived from the account region plus 25 gentle mood/instrument searches. `IG_AUDIO_SEARCH_QUERIES` may add approved searches; a SHA-256 hash selects across queries and results, and the same account cannot receive the same catalog track twice consecutively within a production worker. If the catalog is unavailable, the Reel uses the bundled rotating CC0 library via `prepare_video()`.
- Instagram Reel covers: use the product's main `center` image via `cover_url`. If no valid public image is available, use a 1000 ms thumbnail offset so Meta does not select a black frame at time zero. Both normal URL containers and processed/resumable music uploads follow this rule.
- Minimum market delivery: `delivery_policy.py` / `optimized_runner.py` count confirmed `uploaded` queue rows in a rolling 24-hour UTC window and require eight successful posts for every enabled publish-ready Facebook, Instagram and YouTube account. Deficit posts use a 30-minute minimum spacing so the eight posts are distributed instead of dumped together. Instagram is capped at `IG_MAX_JOBS_PER_RUN=8` in production and cools down for 3600s after a Meta application-limit error so shared API quota is not burst. Duplicate-media locks and normal media validation still apply. LINE is excluded from this floor while its monthly quota is exhausted.
- Facebook carousels: children created via `/{page}/photos` with `published=false`, then published via `/{page}/feed` with `attached_media` + `message`.
- Facebook and Instagram Reels are both normalized to a full 1080x1920 canvas without cropping, using the same blurred edge-fill. A failed/unknown layout probe is treated as vertical so a tiny original cannot publish as a Page video. Landscape/square videos stay original size as feed/Page posts. Existing audible tracks—including quiet original audio—are re-encoded to AAC so Meta plays them; music is added only when audio is missing or genuinely silent.
- LINE (`line_uploader.py`): LINE-CD is temporarily disabled while the Messaging API monthly broadcast quota is exhausted. Broadcasts via Messaging API to all followers. Caption text is sent first, then media. No native carousel — a carousel is sent as caption + up to 5 image messages per broadcast request. Media URLs must be publicly reachable HTTPS URLs. Videos need a JPEG/PNG preview (product main image). Production does not reserve a LINE slot until quota resets. Secrets: `LINE_CHANNEL_ACCESS_TOKEN` and/or `META_TOKEN_LINE_CD`. Use `python enable_line_account.py` (workflow Enable LINE Account) to set Accounts `LINE-CD.enabled = Yes` without rewriting other rows.
- WeChat (`wechat_uploader.py`): uploads permanent materials then mass-sends via `message/mass/sendall`. Carousels become draft articles (图文) via `draft/add` + `freepublish/submit` (needs a verified account). Video uses `prepare_video()` before upload.
- Pinterest (`pinterest_uploader.py`): API v5 pins on the account board (`platform_account_id` = board ID, else first board). Videos use `video_url` media source; carousels use `multiple_image_urls` (max 5 images). Production base `api.pinterest.com/v5` by default (override `PINTEREST_API_BASE`).
- Twitch (`twitch_uploader.py`): Twitch has no video file upload API, so each product video is broadcast to the RTMP ingest (`rtmp://live.twitch.tv/app/<stream key>`) as a short live stream via ffmpeg, which becomes a VOD. Video-only (carousel/image jobs are rejected). `TWITCH_STREAM_KEY` (or per-account `META_TOKEN_TWITCH_CD`); `TWITCH_CLIENT_ID`/`TWITCH_CLIENT_SECRET`/`TWITCH_BROADCASTER_ID` enable best-effort VOD URL lookup via Helix, `TWITCH_ACCESS_TOKEN` (scope `channel:manage:broadcast`) optionally sets the stream title.
- Shopee (`shopee_uploader.py`) / Lazada (`lazada_uploader.py`): marketplaces have no generic "media post" API, so these platforms are media-hosting only — carousel/image jobs are hosted onto the shop (Shopee `product/upload_img`; Lazada `image/migrate`), video jobs are rejected. Thailand region (Shopee `partner.shopeemobile.com`; Lazada `api.lazada.co.th`). Require approved Open Platform apps + Seller Center shop auth; the v2 signing must be validated against real credentials once approved. SHOPEE-CD / LAZADA-CD are provisioned but disabled until then.
- Run modes: `python main.py` (once), `python main.py --generate` (populate queue), `python main.py --cycle` (generate missing rows + upload pending jobs; used by CI), `python main.py --loop` (poll every 300s; only for App Engine, not CI), `python main.py --direct` (direct upload via env).
- `auto_upload/dump_pages.py` + `.github/workflows/dump-pages.yml` dump all FB page names/IG ids via `me/accounts` (manual dispatch → `pages` artifact).

## 24/7 GitHub agents

- **Watchdog** (`.github/workflows/watchdog.yml` + `auto_upload/watchdog.py`): runs every 30 min. Monitors Auto Upload runs and the publishing queue (public gviz, no extra secrets). After 3+ consecutive failures it escalates to a GitHub issue labeled `auto-fix` with the error signature and run IDs; posts a queue health report each cycle. It also calculates per-account 24-hour delivery deficits for publish-ready Facebook, Instagram and YouTube accounts and dispatches Auto Upload Production when a deficit account is due after the 30-minute spacing period.
- **Issue Fixer** (`.github/workflows/issue-fixer.yml` + `auto_upload/issue_fixer.py`): watches issues labeled `auto-fix` (on open/label + daily 04:00 sweep + manual). Uses `USER_LLM_API_KEY` secret (plus optional `USER_LLM_BASE_URL`/`USER_LLM_MODEL`/`ISSUE_FIXER_MAX_ISSUES`) to generate a diff, validates via `git apply` + `py_compile`, opens a PR on a feature branch, labels `auto-pr`, and marks unfixable issues `needs-human`. Disabled (green no-op) until `USER_LLM_API_KEY` is added to GitHub Secrets.

## Local dev

- Preview page: `auto_upload/preview.html` — interactive simulation of the pipeline (serve via `python3 -m http.server`).
- `.env` goes in `auto_upload/` (gitignored), keys per `auto_upload/.env.example`.

## TikTok via Zernio

- TIKTOK-CD uses Zernio account ID `6aae50288d284ffb211a67e9` (@colourdiamondsbkk), with `ZERNIO_API_KEY` stored only in GitHub Secrets.
- Supports product/model videos and photo carousels; caption comes from the existing same-SKU caption pipeline. Public own-brand posts, comments/duets/stitches off.
- Included in production selection and the existing eight-post rolling daily delivery target. Unconfirmed Zernio responses remain on hold to prevent duplicate submissions.

## Steady upload batches (2026-10-03)

Production uses up to 30 uploads per batch and 80 generated jobs. Instagram is capped at 8 and YouTube at 12. Wait 45 seconds between uploads and retain a short rest within the serialized workflow. Generation and leftover slots stay on model videos/photos from the Model Photo Video folder for branding; product carousels wait until no model jobs remain. API calls still consume quota; batching does not eliminate usage. Already-running workflows retain their original settings.


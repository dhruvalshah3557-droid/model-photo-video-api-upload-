#!/usr/bin/env python3
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

EXPECTED_CHANNEL_ID = "UCTWbcY-YtvAx2QUZKXt230A"
CHANNEL_HANDLE = "@colourdiamondss"
SECRET_NAME = "YOUTUBE_OAUTH_REFRESH_TOKEN_COLOURDIAMONDSS"
REDIRECT_URI = "http://localhost"
SCOPES = (
    "https://www.googleapis.com/auth/youtube.upload "
    "https://www.googleapis.com/auth/youtube.readonly"
)
TOKEN_URL = "https://oauth2.googleapis.com/token"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
CHANNEL_URL = "https://www.googleapis.com/youtube/v3/channels?part=id,snippet&mine=true"


def _env(name):
    return (os.getenv(name) or "").strip()


def _login_url(client_id):
    params = {
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPES,
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
    }
    return AUTH_URL + "?" + urllib.parse.urlencode(params)


def _extract_code(raw):
    raw = (raw or "").strip()
    if "code=" in raw:
        query = urllib.parse.urlsplit(raw).query
        values = urllib.parse.parse_qs(query).get("code") or []
        if values:
            return values[0]
    return raw


def _post(url, data):
    req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode())
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode()
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return {"error": "http_error", "error_description": body}


def _get_json(url, access_token):
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + access_token})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def _summary(text):
    path = _env("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(text)


def _mask(value):
    if value:
        print("::add-mask::" + value)


def main():
    client_id = _env("YOUTUBE_CLIENT_ID_COLOURDIAMONDSS")
    client_secret = _env("YOUTUBE_CLIENT_SECRET_COLOURDIAMONDSS")
    if not client_id or not client_secret:
        print("ERROR: YOUTUBE_CLIENT_ID_COLOURDIAMONDSS or YOUTUBE_CLIENT_SECRET_COLOURDIAMONDSS is missing.")
        return 1

    login_url = _login_url(client_id)
    redirect_url = _env("REDIRECT_URL")
    if not redirect_url:
        print("LOGIN_URL=" + login_url)
        _summary(
            "## Next step\n\n"
            "1. Open this Google login link while signed into "
            + CHANNEL_HANDLE
            + " (colourdiamondss@gmail.com):\n\n"
            + login_url
            + "\n\n"
            "2. Click Allow.\n"
            "3. The browser will fail to open localhost. Copy the full address bar.\n"
            "4. Re-run this workflow and paste that address into redirect_url.\n"
        )
        return 0

    _mask(redirect_url)
    payload = _post(TOKEN_URL, {
        "client_id": client_id,
        "client_secret": client_secret,
        "code": _extract_code(redirect_url),
        "grant_type": "authorization_code",
        "redirect_uri": REDIRECT_URI,
    })
    refresh_token = (payload.get("refresh_token") or "").strip()
    access_token = (payload.get("access_token") or "").strip()
    if not refresh_token or not access_token:
        err = payload.get("error") or "unknown"
        desc = payload.get("error_description") or "no description"
        print("ERROR: Google rejected the login code.")
        print(str(err) + ": " + str(desc))
        print("The code expires in a few minutes. Get a fresh login link first.")
        return 1

    _mask(refresh_token)
    _mask(access_token)
    channel = _get_json(CHANNEL_URL, access_token)
    items = channel.get("items") or []
    channel_id = ((items[0].get("id") if items else "") or "").strip()
    channel_title = (((items[0].get("snippet") or {}).get("title") if items else "") or "").strip()
    if channel_id != EXPECTED_CHANNEL_ID:
        print(
            "ERROR: Wrong YouTube channel: "
            + (channel_title or "unknown")
            + " ("
            + (channel_id or "unknown")
            + "). Expected "
            + EXPECTED_CHANNEL_ID
            + " ("
            + CHANNEL_HANDLE
            + ")."
        )
        return 1

    print("OK: authenticated as " + channel_title + " (" + channel_id + ").")
    _summary(
        "## Token ready for "
        + CHANNEL_HANDLE
        + "\n\n"
        "GitHub secret name:\n\n"
        + SECRET_NAME
        + "\n\n"
        "Paste only this value:\n\n```\n"
        + refresh_token
        + "\n```\n\nThen delete this workflow run.\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Obtain a long-lived Facebook Graph API user access token.

The project stores a shared FB_ACCESS_TOKEN (fallback) plus per-account
META_TOKEN_FB_* secrets. This script helps create that token:

  1. Exchange a short-lived user token for a 60-day long-lived token.
  2. Optionally resolve page tokens for every page the user manages.
  3. Verify the final token against the Graph API debug_token endpoint.

Get a short-lived token first from the Graph API Explorer:
  https://developers.facebook.com/tools/explorer/
Select your app, add permissions (pages_show_list, pages_manage_posts,
pages_read_engagement, read_insights, instagram_basic,
instagram_content_publish, instagram_manage_insights, business_management), click "Generate Access Token", and authorize.

Usage examples:
  python3 get_fb_token.py --app-id 123 --app-secret abc --short-lived-token EAA...
  python3 get_fb_token.py --app-id 123 --app-secret abc --short-lived-token EAA... --pages
  python3 get_fb_token.py --token EAA... --verify
"""
import argparse
import sys

import requests

GRAPH_URL = "https://graph.facebook.com/v26.0"
LONG_TOKEN_MAX_AGE_SECONDS = 60 * 24 * 60 * 60


def _request(path, params):
    resp = requests.get(f"{GRAPH_URL}/{path}", params=params, timeout=30)
    data = resp.json()
    error = data.get("error")
    if error:
        sys.exit(
            "Graph API error (%s, code %s): %s"
            % (error.get("type", "?"), error.get("code", "?"), error.get("message", "?"))
        )
    return data


def exchange_token(app_id, app_secret, short_lived_token):
    print("Exchanging short-lived token for a long-lived token...")
    data = _request(
        "oauth/access_token",
        {
            "grant_type": "fb_exchange_token",
            "client_id": app_id,
            "client_secret": app_secret,
            "fb_exchange_token": short_lived_token,
        },
    )
    token = data.get("access_token")
    if not token:
        sys.exit("No access_token returned in the exchange response.")
    print("Exchange OK.")
    return token


def verify_token(token):
    data = _request(
        "debug_token",
        {"input_token": token, "access_token": token},
    )
    info = data.get("data") or {}
    if info.get("is_valid") is not True:
        sys.exit("Token is INVALID (is_valid=false).")
    expires = info.get("expires_at") or 0
    if expires and expires <= 0:
        expires = 0
    if expires:
        print("Token is valid until: %d" % expires)
    else:
        print("Token is valid (no expiry).")
    print("Token owner (app-scoped profile id): %s" % (info.get("profile_id") or info.get("user_id") or "unknown"))
    print("Token app id: %s" % (info.get("app_id") or "unknown"))
    print("Scopes: %s" % (", ".join(info.get("scopes") or [])))
    return info


def list_pages(token):
    print("Fetching managed pages...")
    data = _request("me/accounts", {"access_token": token})
    pages = data.get("data", [])
    if not pages:
        print("No pages found for this token.")
        return
    print("Managed pages:")
    for page in pages:
        print("  - %s (id %s)" % (page.get("name", "?"), page.get("id", "?")))


def print_secrets(token, include_fallback):
    print("\n" + "=" * 60)
    print("PASTE THESE INTO GITHUB SECRETS (exact KEY=VALUE pairs)")
    print("=" * 60)
    if include_fallback:
        print(f"FB_ACCESS_TOKEN={token}")
    print(f"FACEBOOKTOKEN={token}")
    print(f"META_TOKEN_FB_ISR={token}")
    print(f"META_TOKEN_FB_JPN={token}")
    print(f"META_TOKEN_FB_KOR={token}")
    print(f"META_TOKEN_FB_RUS={token}")
    print(f"META_TOKEN_FB_PH={token}")
    print(f"META_TOKEN_FB_JIYA={token}")
    print(f"META_TOKEN_FB_MMR={token}")
    print(f"META_TOKEN_FB_GLOBAL={token}")
    print(f"META_TOKEN_FB_BKK={token}")
    print(f"META_TOKEN_FB_TREND={token}")
    print(f"META_TOKEN_FB_LTD={token}")
    print(f"META_TOKEN_FB_CD={token}")
    print(f"META_TOKEN_FB_NFCD={token}")
    print(f"META_TOKEN_FB_INDO={token}")
    print(f"META_TOKEN_FB_GREECE={token}")
    print(f"META_TOKEN_FB_LEBANON={token}")
    print(f"META_TOKEN_FB_CZECH={token}")
    print("=" * 60)
    print(
        "Set each KEY=VALUE line as a GitHub Secret (Settings > Secrets and "
        "variables > Actions > New repository secret). Tokens are long-lived "
        "(60 days); re-run this script and update the secrets before expiry."
    )


def main():
    parser = argparse.ArgumentParser(description="Create a long-lived Facebook Graph API token.")
    parser.add_argument("--app-id", help="Facebook app ID")
    parser.add_argument("--app-secret", help="Facebook app secret")
    parser.add_argument("--short-lived-token", help="Short-lived user token from the Graph API Explorer")
    parser.add_argument(
        "--token",
        help="Existing token to verify (or a freshly exchanged long-lived token).",
    )
    parser.add_argument(
        "--pages",
        action="store_true",
        help="Also list the pages managed by the resulting token.",
    )
    parser.add_argument(
        "--no-fallback",
        action="store_true",
        help="Skip the FB_ACCESS_TOKEN fallback secret (per-account META_TOKEN_FB_* only).",
    )
    args = parser.parse_args()

    token = args.token
    if token:
        if args.pages:
            list_pages(token)
        verify_token(token)
        print_secrets(token, not args.no_fallback)
        return

    if not (args.app_id and args.app_secret and args.short_lived_token):
        parser.error("provide --app-id, --app-secret and --short-lived-token (or --token)")

    token = exchange_token(args.app_id, args.app_secret, args.short_lived_token)
    if args.pages:
        list_pages(token)
    verify_token(token)
    print_secrets(token, not args.no_fallback)


if __name__ == "__main__":
    main()

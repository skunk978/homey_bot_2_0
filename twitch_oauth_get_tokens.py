"""
One-shot Twitch user OAuth helper.

Reads twitch.client_id and twitch.client_secret from config.yaml in this directory.

Two modes:

1) Authorization code (default) — needs an https:// redirect registered on your app
   (often ngrok). Run: .\\venviron\\Scripts\\python.exe twitch_oauth_get_tokens.py

2) Device code — no redirect URI; user opens Twitch activate in a browser.
   Run: .\\venviron\\Scripts\\python.exe twitch_oauth_get_tokens.py --device
   Docs: https://dev.twitch.tv/docs/authentication/getting-tokens-oauth/#device-code-grant-flow
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import yaml

CONFIG_PATH = Path(__file__).resolve().parent / "config.yaml"

# Space-separated Twitch scopes (URL-encoded when built). Add what your bot needs.
DEFAULT_SCOPES = "user:read:chat"


def _http_form(url: str, fields: dict[str, str]) -> dict:
    body = urllib.parse.urlencode(fields).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _http_form_maybe_error(url: str, fields: dict[str, str]) -> tuple[int, dict]:
    body = urllib.parse.urlencode(fields).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read().decode("utf-8"))
        except json.JSONDecodeError:
            payload = {"status": e.code, "message": e.reason}
        return e.code, payload


def run_device_flow(client_id: str, client_secret: str, scopes: str) -> int:
    """Twitch device code grant — no OAuth redirect URL required."""
    try:
        start = _http_form(
            "https://id.twitch.tv/oauth2/device",
            {"client_id": client_id, "scopes": scopes},
        )
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", errors="replace")
        print(f"Device flow start failed: HTTP {e.code}\n{err}", file=sys.stderr)
        return 1

    device_code = start.get("device_code", "")
    if not device_code:
        print(f"Unexpected /device response: {start}", file=sys.stderr)
        return 1

    interval = max(3, int(start.get("interval", 5)))
    expires_in = int(start.get("expires_in", 1800))
    verification_uri = start.get("verification_uri", "https://www.twitch.tv/activate")
    user_code = start.get("user_code", "")

    print()
    print("Device code flow (no redirect URI needed).")
    print()
    print("1) On this PC or any device, open:")
    print(f"    {verification_uri}")
    if user_code:
        print()
        print(f"2) If Twitch asks for a code, use: {user_code}")
    print()
    print("3) Log in as your BOT Twitch account and approve the scopes.")
    print(f"    (Waiting up to ~{expires_in // 60} minutes — this window will poll automatically.)")
    print()

    deadline = time.monotonic() + expires_in
    fields: dict[str, str] = {
        "client_id": client_id,
        "scopes": scopes,
        "device_code": device_code,
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
    }
    if client_secret:
        fields["client_secret"] = client_secret

    while time.monotonic() < deadline:
        status, data = _http_form_maybe_error("https://id.twitch.tv/oauth2/token", fields)
        if status == 200:
            access = data.get("access_token", "")
            refresh = data.get("refresh_token", "")
            if not access or not refresh:
                print(f"Unexpected token response: {data}", file=sys.stderr)
                return 1
            print("Authorized. Put these in config.yaml:")
            print()
            print(f'  bot_token: "oauth:{access}"')
            print(f'  refresh_token: "{refresh}"')
            print()
            print("Then save config.yaml and run the bot.")
            return 0

        msg = str(data.get("message", ""))
        if msg == "authorization_pending":
            time.sleep(interval)
            continue
        if msg == "slow_down":
            interval += 5
            time.sleep(interval)
            continue

        print(f"Token poll failed: HTTP {status}\n{data}", file=sys.stderr)
        return 1

    print("Timed out waiting for you to approve on twitch.tv/activate.", file=sys.stderr)
    return 1


def run_authorization_code_flow(client_id: str, client_secret: str) -> int:
    print()
    print(
        "Twitch requires an HTTPS redirect for your app. Use a URL registered on the app, e.g.\n"
        "  https://<your-ngrok-host>.ngrok-free.app/callback\n"
        "after `ngrok http 17563` (or any free port — the tunnel only needs to exist when you authorize).\n"
    )
    redirect = input(
        "Redirect URI (must start with https://, match Twitch console exactly): "
    ).strip()
    if not redirect:
        print("Redirect URI is required.", file=sys.stderr)
        return 1
    if not redirect.startswith("https://"):
        print(
            "This redirect does not start with https:// — Twitch rejected http:// before. "
            "Use an https:// URL (ngrok / Cloudflare Tunnel / your domain).",
            file=sys.stderr,
        )
        return 1
    scopes = input(f"Scopes [{DEFAULT_SCOPES}]: ").strip() or DEFAULT_SCOPES

    q = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect,
            "scope": scopes,
        }
    )
    auth_url = f"https://id.twitch.tv/oauth2/authorize?{q}"
    print()
    print("1) Open this URL in a browser (log in as the bot Twitch user):")
    print()
    print(auth_url)
    print()
    print(
        "Before opening the URL: Twitch Developer Console → your app → "
        "OAuth Redirect URLs → add this EXACT string (scheme, host, port, path):"
    )
    print(f"    {redirect}")
    print()
    raw = input("2) Paste the full redirect URL from the address bar, or paste only the code: ").strip()
    if not raw:
        print("No input.", file=sys.stderr)
        return 1

    code = ""
    if raw.startswith("http://") or raw.startswith("https://"):
        parsed = urllib.parse.urlparse(raw)
        params = urllib.parse.parse_qs(parsed.query)
        errs = params.get("error") or []
        if errs:
            desc = (params.get("error_description") or [""])[0]
            desc = urllib.parse.unquote_plus(desc)
            print("\nTwitch returned an error in the redirect (no code to exchange).", file=sys.stderr)
            print(f"  error: {errs[0]}", file=sys.stderr)
            print(f"  error_description: {desc}", file=sys.stderr)
            if errs[0] == "redirect_mismatch":
                print(
                    "\nFix: In https://dev.twitch.tv/console/apps open this Client ID's application, "
                    "edit OAuth Redirect URLs, and add the SAME redirect_uri you entered above "
                    f"(copy/paste: {redirect}). Save, wait a few seconds, run this script again.",
                    file=sys.stderr,
                )
                if redirect.startswith("http://"):
                    print(
                        "\nNote: Your app may require https:// redirects — use an HTTPS URL "
                        "(e.g. ngrok) and register that exact https://... URL on the app.",
                        file=sys.stderr,
                    )
            return 1
        codes = params.get("code") or []
        code = codes[0] if codes else ""
    elif "code=" in raw:
        parsed = urllib.parse.urlparse(raw)
        params = urllib.parse.parse_qs(parsed.query)
        codes = params.get("code") or []
        code = codes[0] if codes else ""
    else:
        code = raw.strip()

    if not code:
        print("Could not find authorization code in that input.", file=sys.stderr)
        return 1

    body = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect,
        }
    ).encode("utf-8")

    req = urllib.request.Request(
        "https://id.twitch.tv/oauth2/token",
        data=body,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", errors="replace")
        print(f"Token exchange failed: HTTP {e.code}\n{err}", file=sys.stderr)
        return 1

    access = data.get("access_token", "")
    refresh = data.get("refresh_token", "")
    if not access or not refresh:
        print(f"Unexpected response: {data}", file=sys.stderr)
        return 1

    print()
    print("3) Put these in config.yaml (same OAuth response — keep them together):")
    print()
    print(f'  bot_token: "oauth:{access}"')
    print(f'  refresh_token: "{refresh}"')
    print()
    print("Then save config.yaml and run the bot.")
    return 0


def main() -> int:
    if not CONFIG_PATH.is_file():
        print(f"Missing {CONFIG_PATH}", file=sys.stderr)
        return 1

    with CONFIG_PATH.open(encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    tw = cfg.get("twitch") or {}
    client_id = str(tw.get("client_id", "")).strip()
    client_secret = str(tw.get("client_secret", "")).strip()
    if not client_id:
        print("config.yaml must have twitch.client_id.", file=sys.stderr)
        return 1

    if len(sys.argv) > 1 and sys.argv[1] in ("--device", "-d"):
        extra = [a for a in sys.argv[2:] if a != "--"]
        scopes = " ".join(extra).strip() if extra else DEFAULT_SCOPES
        if not client_secret:
            print(
                "Device flow: add twitch.client_secret to config.yaml for this confidential app, "
                "or see Twitch docs for public clients.",
                file=sys.stderr,
            )
            return 1
        print(f"Scopes: {scopes}")
        return run_device_flow(client_id, client_secret, scopes)

    if not client_secret:
        print("config.yaml must have twitch.client_secret for the authorization-code flow.", file=sys.stderr)
        return 1

    return run_authorization_code_flow(client_id, client_secret)


if __name__ == "__main__":
    raise SystemExit(main())

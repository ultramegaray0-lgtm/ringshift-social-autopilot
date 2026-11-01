"""One-time helper: authorize your TikTok app for `video.upload` and get the refresh token for GitHub secrets.

Run it on YOUR computer (standard library only, nothing to install):

    python scripts/tiktok_auth.py --client-key <key> --redirect-uri https://<your-registered-redirect>/

1. It prints a TikTok authorization link. Open it, log in to the TikTok account the videos are for, and allow access.
2. TikTok sends your browser to the redirect URI with `?code=...` in the address bar. The page itself may show an
   error or 404; that is fine. Copy the WHOLE address and paste it here.
3. It asks for the client secret (typed hidden), exchanges the code with TikTok and prints the refresh token ONCE,
   with what to do with it. Nothing is written to disk.

Paste the refresh token into the GitHub secret TIKTOK_REFRESH_TOKEN and close the terminal. Never put it in a file,
an issue or a chat.
"""

import argparse
import getpass
import json
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request

AUTHORIZE_URL = "https://www.tiktok.com/v2/auth/authorize/"
TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
SCOPES = ("user.info.basic", "video.upload")


class AuthError(RuntimeError):
    pass


def authorize_link(client_key, redirect_uri, state):
    query = urllib.parse.urlencode({"client_key": client_key, "scope": ",".join(SCOPES), "response_type": "code",
                                    "redirect_uri": redirect_uri, "state": state})
    return f"{AUTHORIZE_URL}?{query}"


def code_from_redirect(pasted, expected_state):
    """The authorization code from the address TikTok redirected to (or a bare code)."""
    pasted = pasted.strip()
    if "://" not in pasted and "code=" not in pasted:
        if not pasted:
            raise AuthError("nothing was pasted")
        return pasted
    query = urllib.parse.parse_qs(urllib.parse.urlparse(pasted).query or pasted.split("?", 1)[-1])
    if query.get("error"):
        raise AuthError(f"TikTok refused: {query['error'][0]} {query.get('error_description', [''])[0]}")
    if query.get("state", [expected_state])[0] != expected_state:
        raise AuthError("the state in the address does not match this session (an old or foreign link); start again")
    code = (query.get("code") or [""])[0]
    if not code:
        raise AuthError("no ?code= in the pasted address")
    scopes = set((query.get("scopes") or [""])[0].split(","))
    if query.get("scopes") and "video.upload" not in scopes:
        raise AuthError("video.upload was not granted: allow it on the TikTok screen (and enable it for the app)")
    return code


def exchange(client_key, client_secret, code, redirect_uri):
    body = urllib.parse.urlencode({"client_key": client_key, "client_secret": client_secret, "code": code,
                                   "grant_type": "authorization_code", "redirect_uri": redirect_uri}).encode()
    request = urllib.request.Request(TOKEN_URL, data=body, method="POST", headers={
        "Content-Type": "application/x-www-form-urlencoded", "Cache-Control": "no-cache"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise AuthError(f"TikTok returned HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:300]}") from exc
    if payload.get("error"):
        raise AuthError(f"TikTok error {payload['error']}: {payload.get('error_description', '')}")
    if "video.upload" not in set((payload.get("scope") or "").split(",")):
        raise AuthError("the token has no video.upload scope: add it to the app (Content Posting API) and authorize again")
    if not payload.get("refresh_token"):
        raise AuthError("TikTok returned no refresh token")
    return payload


def main(argv=None):
    parser = argparse.ArgumentParser(description="Get a TikTok refresh token with video.upload for GitHub secrets.")
    parser.add_argument("--client-key", required=True)
    parser.add_argument("--redirect-uri", required=True, help="exactly as registered in the TikTok app (https)")
    args = parser.parse_args(argv)
    state = secrets.token_urlsafe(16)
    print("\n1) Open this link, log in to the TikTok account for Ringshift, and allow access:\n")
    print("   " + authorize_link(args.client_key, args.redirect_uri, state))
    try:
        pasted = input("\n2) Paste the whole address your browser was sent to: ")
        code = code_from_redirect(pasted, state)
        client_secret = getpass.getpass("3) TikTok client secret (hidden): ").strip()
        payload = exchange(args.client_key, client_secret, code, args.redirect_uri)
    except (AuthError, EOFError, KeyboardInterrupt) as error:
        print(f"\nerror: {error}", file=sys.stderr)
        return 1
    days = int(payload.get("refresh_expires_in") or 0) // 86400
    print("\nOK. Put this value in the GitHub secret TIKTOK_REFRESH_TOKEN (Settings > Secrets and variables > Actions),")
    print("then close this window. Do not save it anywhere else." + (f" It is valid for about {days} days;" if days else ""))
    print("the workflow keeps it fresh from now on (encrypted, on the tiktok-auth-state branch).\n")
    print(payload["refresh_token"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

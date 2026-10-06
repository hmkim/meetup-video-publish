#!/usr/bin/env python3
"""YouTube OAuth helper for a headless desktop.

Step 1  (prints a URL to open on ANY browser, e.g. the laptop):
    youtube_auth.py url
Step 2  (paste the full redirect URL the browser landed on, http://localhost:8765/?code=...):
    youtube_auth.py finish '<redirect-url>'
Check:
    youtube_auth.py whoami

Files (all under ~/.cache/zoom-video-edit/youtube/, 0600):
    client_secret.json  -- OAuth "Desktop app" client downloaded from Google Cloud Console
    token.json          -- refresh token written by `finish`
    pending_flow.json   -- transient state between `url` and `finish`
"""
import json, os, sys, warnings
warnings.filterwarnings("ignore", category=FutureWarning)

from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

BASE = os.path.expanduser("~/.cache/zoom-video-edit/youtube")
CLIENT = os.path.join(BASE, "client_secret.json")
TOKEN = os.path.join(BASE, "token.json")
PENDING = os.path.join(BASE, "pending_flow.json")
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]
# Loopback redirect; the browser will fail to load it (nothing listens on the laptop),
# which is fine: we only need the URL it lands on.
REDIRECT = os.environ.get("YT_REDIRECT", "http://localhost:8765/")


def _write_private(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)


def cmd_url():
    if not os.path.exists(CLIENT):
        sys.exit(f"missing {CLIENT} (download the Desktop-app OAuth client JSON there, chmod 600)")
    flow = Flow.from_client_secrets_file(CLIENT, scopes=SCOPES, redirect_uri=REDIRECT)
    url, state = flow.authorization_url(access_type="offline", prompt="consent", include_granted_scopes="true")
    _write_private(PENDING, {"state": state, "redirect_uri": REDIRECT, "code_verifier": flow.code_verifier})
    print(url)


def cmd_finish(redirect_url):
    pend = json.load(open(PENDING)) if os.path.exists(PENDING) else {}
    flow = Flow.from_client_secrets_file(CLIENT, scopes=SCOPES, redirect_uri=pend.get("redirect_uri", REDIRECT),
                                         code_verifier=pend.get("code_verifier"))
    if redirect_url.startswith("http://localhost") or redirect_url.startswith("https://"):
        os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
    flow.fetch_token(authorization_response=redirect_url.strip())
    c = flow.credentials
    _write_private(TOKEN, {
        "token": c.token, "refresh_token": c.refresh_token, "token_uri": c.token_uri,
        "client_id": c.client_id, "client_secret": c.client_secret, "scopes": list(c.scopes or SCOPES),
    })
    if os.path.exists(PENDING):
        os.remove(PENDING)
    print("token saved:", TOKEN, "(refresh_token present)" if c.refresh_token else "(NO refresh token!)")
    cmd_whoami()


def load_creds():
    c = Credentials.from_authorized_user_file(TOKEN, SCOPES)
    if not c.valid:
        c.refresh(Request())
    return c


def cmd_whoami():
    from googleapiclient.discovery import build
    yt = build("youtube", "v3", credentials=load_creds(), cache_discovery=False)
    r = yt.channels().list(part="snippet,statistics", mine=True).execute()
    for ch in r.get("items", []):
        print(f"channel: {ch['snippet']['title']}  id={ch['id']}  videos={ch['statistics'].get('videoCount')}")
    if not r.get("items"):
        print("no channel for this Google account (log in with the account that owns the target channel)")


if __name__ == "__main__":
    a = sys.argv[1:]
    if a[:1] == ["url"]:
        cmd_url()
    elif a[:1] == ["finish"] and len(a) == 2:
        cmd_finish(a[1])
    elif a[:1] == ["whoami"]:
        cmd_whoami()
    else:
        sys.exit(__doc__)

"""Device-code OAuth against a personal Microsoft account, with a token cache.

Device code keeps the password entirely inside Microsoft's own sign-in page:
this script only ever sees the short user code and the resulting token.
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

TENANT = "consumers"  # personal Microsoft accounts
AUTH = f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0"
SCOPE = "Notes.ReadWrite offline_access"
sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402

CACHE = config.TOKEN_CACHE
CLIENT_ID_FILE = config.CLIENT_ID_FILE


def client_id():
    cid = os.environ.get("ONENOTE_CLIENT_ID")
    if cid:
        return cid.strip()
    if CLIENT_ID_FILE.exists():
        return CLIENT_ID_FILE.read_text().strip()
    sys.exit(
        "No client id. Put your Azure app's Application (client) ID in "
        f"{CLIENT_ID_FILE} or set ONENOTE_CLIENT_ID."
    )


def _post(url, data):
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    try:
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return json.loads(e.read())


def _device_code_login():
    cid = client_id()
    dc = _post(f"{AUTH}/devicecode", {"client_id": cid, "scope": SCOPE})
    if "user_code" not in dc:
        sys.exit(f"devicecode failed: {json.dumps(dc, indent=2)}")

    print("\n" + "=" * 68)
    print(dc["message"])
    print("=" * 68 + "\n")

    interval = int(dc.get("interval", 5))
    deadline = time.time() + int(dc.get("expires_in", 900))
    while time.time() < deadline:
        time.sleep(interval)
        tok = _post(f"{AUTH}/token", {
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "client_id": cid,
            "device_code": dc["device_code"],
        })
        err = tok.get("error")
        if err == "authorization_pending":
            continue
        if err == "slow_down":
            interval += 5
            continue
        if err:
            sys.exit(f"sign-in failed: {tok.get('error_description', err)}")
        return _store(tok)
    sys.exit("device code expired")


def _store(tok):
    tok["expires_at"] = time.time() + int(tok.get("expires_in", 3600)) - 120
    CACHE.write_text(json.dumps(tok))
    CACHE.chmod(0o600)
    return tok["access_token"]


def _refresh(tok):
    new = _post(f"{AUTH}/token", {
        "grant_type": "refresh_token",
        "client_id": client_id(),
        "refresh_token": tok["refresh_token"],
        "scope": SCOPE,
    })
    if "access_token" not in new:
        return None
    return _store(new)


def token():
    """Return a valid access token, signing in or refreshing as needed."""
    if CACHE.exists():
        tok = json.loads(CACHE.read_text())
        if tok.get("expires_at", 0) > time.time():
            return tok["access_token"]
        if tok.get("refresh_token"):
            fresh = _refresh(tok)
            if fresh:
                return fresh
    return _device_code_login()


if __name__ == "__main__":
    print(token()[:40] + "...")

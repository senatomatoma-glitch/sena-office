import base64
import hashlib
import json
import os
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

JST = timezone(timedelta(hours=9))
SHEET_ID = "1SeFUu817K96pCsE09HBarA3hZvBmfAj7-ctClxrGkrE"
REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX_HTML = os.path.join(REPO_DIR, "index.html")
HASH_FILE = os.path.join(REPO_DIR, ".office_sync_hash")


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def get_google_token(sa_info: dict) -> str:
    now = int(time.time())
    header = b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    claim = b64url(json.dumps({
        "iss": sa_info["client_email"],
        "scope": "https://www.googleapis.com/auth/spreadsheets.readonly",
        "aud": "https://oauth2.googleapis.com/token",
        "exp": now + 3600,
        "iat": now,
    }).encode())
    signing_input = f"{header}.{claim}".encode("ascii")
    priv_key = serialization.load_pem_private_key(sa_info["private_key"].encode("utf-8"), password=None)
    sig = priv_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    jwt_token = f"{header}.{claim}.{b64url(sig)}"

    body = urllib.parse.urlencode({
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion": jwt_token,
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://oauth2.googleapis.com/token",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))["access_token"]


def fetch_range(token: str, a1_range: str) -> list:
    url = (
        f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}/values/"
        + urllib.parse.quote(a1_range, safe="!:")
    )
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=25) as resp:
        return json.loads(resp.read().decode("utf-8")).get("values", [])


def build_payload(q_rows: list, o_rows: list) -> dict:
    h_idx = 0
    for idx, r in enumerate(q_rows):
        if any("상태" in str(c) for c in r) and any("티켓" in str(c) for c in r):
            h_idx = idx
            break

    tickets = []
    for r in q_rows[h_idx + 1:]:
        if len(r) < 6 or not str(r[14] if len(r) > 14 else "").strip():
            continue
        tickets.append({
            "time": r[0] if len(r) > 0 else "",
            "area": r[1] if len(r) > 1 else "",
            "type": r[2] if len(r) > 2 else "",
            "director": r[3] if len(r) > 3 else "",
            "owner": r[4] if len(r) > 4 else "",
            "status": r[5] if len(r) > 5 else "",
            "started": r[6] if len(r) > 6 else "",
            "finished": r[7] if len(r) > 7 else "",
            "verifier": r[8] if len(r) > 8 else "",
            "ver_started": r[9] if len(r) > 9 else "",
            "final_owner": r[10] if len(r) > 10 else "",
            "final_time": r[11] if len(r) > 11 else "",
            "priority": r[12] if len(r) > 12 else "",
            "deadline": r[13] if len(r) > 13 else "",
            "title": r[14] if len(r) > 14 else "",
            "memo": r[15] if len(r) > 15 else "",
            "ticket": r[16] if len(r) > 16 else "",
        })

    opinions = []
    for r in o_rows[1:]:
        if len(r) < 2 or not str(r[1]).strip():
            continue
        opinions.append({
            "date": r[0] if len(r) > 0 else "",
            "topic": r[1] if len(r) > 1 else "",
            "author": r[2] if len(r) > 2 else "",
            "mihyang": r[3] if len(r) > 3 else "",
            "miyu": r[4] if len(r) > 4 else "",
            "aimi": r[5] if len(r) > 5 else "",
            "conclusion": r[6] if len(r) > 6 else "",
        })

    return {
        "updated": datetime.now(JST).strftime("%Y-%m-%d %H:%M JST"),
        "tickets": tickets,
        "opinions": list(reversed(opinions[-12:])),
    }


def encrypt_payload(payload: dict, pin: str) -> dict:
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    salt = os.urandom(16)
    iv = os.urandom(12)
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=100000)
    key = kdf.derive(pin.encode("utf-8"))
    ct = AESGCM(key).encrypt(iv, raw, None)
    return {
        "s": base64.b64encode(salt).decode("ascii"),
        "i": base64.b64encode(iv).decode("ascii"),
        "c": base64.b64encode(ct).decode("ascii"),
    }


def main():
    sa_raw = os.environ.get("GOOGLE_SA_JSON", "").strip()
    pin = os.environ.get("OFFICE_PIN", "").strip()
    if not sa_raw or not pin:
        raise RuntimeError("GOOGLE_SA_JSON or OFFICE_PIN environment variable is missing.")

    sa_info = json.loads(sa_raw)
    token = get_google_token(sa_info)
    q_rows = fetch_range(token, "접수 큐!A5:Q80")
    o_rows = fetch_range(token, "의견 정리!A1:G80")

    payload = build_payload(q_rows, o_rows)
    core_hash = hashlib.sha256(
        json.dumps({"tickets": payload["tickets"], "opinions": payload["opinions"]}, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()

    prev_hash = open(HASH_FILE, encoding="utf-8").read().strip() if os.path.exists(HASH_FILE) else ""
    if core_hash == prev_hash:
        print("NO_CHANGES")
        return

    enc = encrypt_payload(payload, pin)
    html = open(INDEX_HTML, encoding="utf-8").read()
    new_html, n = re.subn(
        r"const ENC = \{.*?\};",
        f"const ENC = {json.dumps(enc)};",
        html,
        count=1,
        flags=re.S,
    )
    if n != 1:
        raise RuntimeError("Failed to locate ENC block in index.html")

    with open(INDEX_HTML, "w", encoding="utf-8") as f:
        f.write(new_html)
    with open(HASH_FILE, "w", encoding="utf-8") as f:
        f.write(core_hash)
    print(f"UPDATED tickets={len(payload['tickets'])} opinions={len(payload['opinions'])}")


if __name__ == "__main__":
    main()

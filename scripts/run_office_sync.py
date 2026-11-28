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
STATE_FILE = os.path.join(REPO_DIR, ".office_sync_state.json")
OFFICE_URL = "https://senatomatoma-glitch.github.io/sena-office/"


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


def build_payload(q_rows: list, o_rows: list, d_rows: list, l_rows: list = None) -> dict:
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

    done_recent = []
    for r in d_rows[1:]:
        if len(r) < 15 or not str(r[14]).strip():
            continue
        done_recent.append({
            "ticket": r[16] if len(r) > 16 else "",
            "area": r[1] if len(r) > 1 else "",
            "owner": r[4] if len(r) > 4 else "",
            "title": r[14] if len(r) > 14 else "",
            "done_time": (r[11] if len(r) > 11 and str(r[11]).strip() else (r[17] if len(r) > 17 else "")),
            "archived_at": r[17] if len(r) > 17 else "",
        })
    # Also include tickets currently in queue with status == '완료'
    for t in tickets:
        if "완료" in (t.get("status") or ""):
            done_recent.append({
                "ticket": t.get("ticket", ""),
                "area": t.get("area", ""),
                "owner": t.get("owner", ""),
                "title": t.get("title", ""),
                "done_time": t.get("final_time") or t.get("finished") or "",
                "archived_at": "",
            })

    ceo_briefings = []
    for o in opinions:
        topic = (o.get("topic") or "")
        if "CEO 브리핑" in topic or topic.startswith("[CEO 브리핑]"):
            ceo_briefings.append({
                "date": o.get("date", ""),
                "title": o.get("topic", ""),
                "author": o.get("author", "") or "아카네",
                "body": o.get("conclusion") or o.get("mihyang") or o.get("miyu") or o.get("aimi") or "",
            })

    lounge = []
    for r in (l_rows or [])[1:]:
        if len(r) < 4 or not str(r[3]).strip():
            continue
        lounge.append({
            "time": r[0] if len(r) > 0 else "",
            "name": r[1] if len(r) > 1 else "",
            "tag": r[2] if len(r) > 2 else "잡담",
            "text": r[3] if len(r) > 3 else "",
        })

    airbnb_monthly = []
    for o in reversed(opinions):
        txt = "\n".join([o.get("topic") or "", o.get("conclusion") or "", o.get("mihyang") or "", o.get("miyu") or "", o.get("aimi") or ""])
        if "[에어비앤비 월별 실적]" in txt or "월별 실적" in txt:
            rows = []
            for line in txt.splitlines():
                m = re.match(r"^\s*(\d+월)\s*[:：]\s*(.+)$", line.strip())
                if m:
                    rows.append({"month": m.group(1), "detail": m.group(2).strip()})
            if rows:
                airbnb_monthly = rows[:3]
                break

    return {
        "updated": datetime.now(JST).strftime("%Y-%m-%d %H:%M JST"),
        "tickets": tickets,
        "opinions": list(reversed(opinions[-12:])),
        "done_recent": list(reversed(done_recent[-15:])),
        "ceo_briefings": list(reversed(ceo_briefings[-7:])),
        "lounge": lounge[-40:],
        "airbnb_monthly": airbnb_monthly,
    }


def send_ntfy(topic: str, title: str, message: str, tags: str = "clipboard", priority: str = "default"):
    if not topic:
        return
    url = f"https://ntfy.sh/{urllib.parse.quote(topic)}"
    headers = {
        "Title": title.encode("utf-8"),
        "Click": OFFICE_URL,
        "Tags": tags,
        "Priority": priority,
        "Content-Type": "text/plain; charset=utf-8",
    }
    req = urllib.request.Request(url, data=message.encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            print("NTFY_SENT", resp.status)
    except Exception as e:
        print("NTFY_ERR", e)


def check_and_notify(payload: dict):
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    cur_done = set()
    for d in payload.get("done_recent") or []:
        k = (d.get("ticket") or d.get("title") or "").strip()
        if k:
            cur_done.add(k)
    cur_dec = set()
    for t in payload.get("tickets") or []:
        if t.get("status") == "결정 필요":
            k = (t.get("ticket") or t.get("title") or "").strip()
            if k:
                cur_dec.add(k)

    prev = {}
    if os.path.exists(STATE_FILE):
        try:
            prev = json.load(open(STATE_FILE, encoding="utf-8"))
        except Exception:
            prev = {}

    if prev:
        prev_done = set(prev.get("done", []))
        prev_dec = set(prev.get("dec", []))
        new_done = [d for d in (payload.get("done_recent") or []) if (d.get("ticket") or d.get("title") or "").strip() in (cur_done - prev_done)]
        new_dec = [t for t in (payload.get("tickets") or []) if t.get("status") == "결정 필요" and (t.get("ticket") or t.get("title") or "").strip() in (cur_dec - prev_dec)]

        lines = []
        if new_done:
            lines.append(f"✅ 새 완료 {len(new_done)}건:")
            for d in new_done[:4]:
                title_short = re.sub(r"^\[[^\]]+\]\s*", "", d.get("title", "")).strip()[:40]
                lines.append(f"• {d.get('ticket','')} ({d.get('owner','')}) {title_short}")
        if new_dec:
            lines.append(f"👑 새 대표승인 {len(new_dec)}건:")
            for t in new_dec[:3]:
                title_short = re.sub(r"^\[[^\]]+\]\s*", "", t.get("title", "")).strip()[:40]
                lines.append(f"• {t.get('ticket','')} {title_short}")

        if lines and topic:
            title_hdr = "세나 비서실 새 알림"
            if new_dec and not new_done:
                title_hdr = f"👑 대표승인 {len(new_dec)}건 도착!"
            elif new_done and not new_dec:
                title_hdr = f"✅ 접수 큐 완료 {len(new_done)}건!"
            send_ntfy(topic, title_hdr, "\n".join(lines), tags="crown" if new_dec else "white_check_mark")

    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"done": sorted(cur_done), "dec": sorted(cur_dec)}, f, ensure_ascii=False)


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
    d_rows = fetch_range(token, "완료 보관!A1:R250")
    try:
        l_rows = fetch_range(token, "💬 라운지!A1:D150")
    except Exception:
        l_rows = []

    payload = build_payload(q_rows, o_rows, d_rows, l_rows)
    core_hash = hashlib.sha256(
        json.dumps(
            {"tickets": payload["tickets"], "opinions": payload["opinions"], "done_recent": payload["done_recent"], "lounge": payload["lounge"]},
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()

    prev_hash = open(HASH_FILE, encoding="utf-8").read().strip() if os.path.exists(HASH_FILE) else ""
    if core_hash == prev_hash:
        print("NO_CHANGES")
        return

    check_and_notify(payload)
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
    print(f"UPDATED tickets={len(payload['tickets'])} done={len(payload['done_recent'])}")


if __name__ == "__main__":
    main()

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
CLEAN_SHEET_ID = "1Il9HtzngOYvzyrxZjNh_YpQbgZnfFFV9-GUnAzJP-Ic"
MINT_SHEET_ID = "15lJkLS9j1SSLfpUUNmgSrEA0iWLIjUMDLnlAXzCV9Ys"
CLEAN_TABS = [
    {"tab": "shiwa", "start": 98, "end": 3100},
    {"tab": "safe", "start": 2, "end": 1018},
    {"tab": "OOKINI", "start": 2, "end": 1002},
]
KOR_DOW = ["월", "화", "수", "목", "금", "토", "일"]
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


def fetch_range(token: str, a1_range: str, sheet_id: str = SHEET_ID) -> list:
    url = (
        f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/"
        + urllib.parse.quote(a1_range, safe="!:")
    )
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=25) as resp:
        return json.loads(resp.read().decode("utf-8")).get("values", [])


def norm_date(value) -> str:
    """'2026. 11. 28' 같은 표기를 'YYYY-MM-DD'로 맞춘다."""
    m = re.match(r"^(\d{4})\D{1,2}(\d{1,2})\D{1,2}(\d{1,2})", str(value or "").strip())
    if not m:
        return None
    try:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).strftime("%Y-%m-%d")
    except ValueError:
        return None


def clean_name(value) -> str:
    s = re.sub(r"\([^)]*\)|（[^）]*）", "", str(value or ""))
    return re.sub(r"\s+", " ", s).strip()


def clean_room(value) -> str:
    """호실명은 괄호 표기(1201(Two room) 등)를 살린다."""
    return re.sub(r"\s+", " ", str(value or "")).strip()


def build_roster(token: str, dates: list) -> dict:
    """MINT 탭: 날짜별 스태프 배정 상태(All assigned / Holiday). 읽기 전용."""
    out = {d: [] for d in dates}
    try:
        rows = fetch_range(token, "MINT!A1:HC30", MINT_SHEET_ID)
    except Exception as e:
        print("MINT_ERR", e)
        return out
    if len(rows) < 4:
        return out
    header = rows[0]
    col_of = {}
    for idx, cell in enumerate(header):
        d = norm_date(cell)
        if d in out:
            col_of[d] = idx
    for d, ci in col_of.items():
        entries = []
        for r in rows[3:]:
            if not (r and str(r[0]).strip()):
                continue
            status = str(r[ci]).strip() if len(r) > ci else ""
            entries.append({"name": clean_name(r[0]), "status": status})
        out[d] = entries
    return out


def build_cleaning(token: str, today: str, tomorrow: str) -> dict:
    """오늘·내일 청소 배정(호실·담당)과 스태프별 방 수. 시트는 읽기만 한다."""
    targets = [today, tomorrow]
    days = []
    for d in targets:
        days.append({
            "date": d,
            "label": "오늘" if d == today else "내일",
            "dow": KOR_DOW[datetime.strptime(d, "%Y-%m-%d").weekday()],
            "total": 0,
            "staff": [],
            "off": [],
            "sources": [],
        })
    seen = set()
    for cfg in CLEAN_TABS:
        tab = cfg["tab"]
        try:
            date_rows = fetch_range(token, f"{tab}!B{cfg['start']}:B{cfg['end']}", CLEAN_SHEET_ID)
        except Exception as e:
            print("CLEAN_DATES_ERR", tab, e)
            try:
                date_rows = fetch_range(token, f"{tab}!B:B", CLEAN_SHEET_ID)[cfg["start"] - 1:]
            except Exception as e2:
                print("CLEAN_DATES_ERR2", tab, e2)
                continue
        hits = [cfg["start"] + i for i, r in enumerate(date_rows) if r and norm_date(r[0]) in targets]
        if not hits:
            continue
        lo, hi = min(hits), max(hits)
        try:
            block = fetch_range(token, f"{tab}!A{lo}:Q{hi}", CLEAN_SHEET_ID)
        except Exception as e:
            print("CLEAN_BLOCK_ERR", tab, e)
            continue
        added = {d: 0 for d in targets}
        for r in block:
            if len(r) < 4:
                continue
            nd = norm_date(r[1])
            if nd not in added:
                continue
            room = clean_room(r[2])
            staff = clean_name(r[3])
            if not room or not staff:
                continue
            key = (nd, room, staff)
            if key in seen:
                continue
            seen.add(key)
            added[nd] += 1
            day = days[targets.index(nd)]
            entry = next((s for s in day["staff"] if s["name"] == staff), None)
            if entry is None:
                entry = {"name": staff, "count": 0, "rooms": [], "status": "", "warn": False}
                day["staff"].append(entry)
            entry["rooms"].append(room)
            entry["count"] = len(entry["rooms"])
        for d in targets:
            if added[d]:
                days[targets.index(d)]["sources"].append({"tab": tab, "rooms": added[d]})

    roster = build_roster(token, targets)
    for day in days:
        entries = roster.get(day["date"]) or []
        status_of = {e["name"]: e["status"] for e in entries}
        day["off"] = [e["name"] for e in entries if "holiday" in (e["status"] or "").lower()]
        for s in day["staff"]:
            s["status"] = status_of.get(s["name"], "")
            s["warn"] = "holiday" in (s["status"] or "").lower()
        day["staff"].sort(key=lambda x: (-x["count"], x["name"]))
        day["total"] = sum(s["count"] for s in day["staff"])

    return {"generated": datetime.now(JST).strftime("%Y-%m-%d %H:%M JST"), "days": days}


def build_payload(q_rows: list, o_rows: list, d_rows: list, l_rows: list = None, cleaning: dict = None) -> dict:
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

    # Compute mood & stress per member from queue workload + lounge posts
    now_str = datetime.now(JST).strftime("%Y-%m-%d %H:%M")
    today_prefix = datetime.now(JST).strftime("%Y-%m-%d")
    mood_stress = {}
    for m_name in ["미향", "미유", "아카네", "아이미"]:
        run_c = sum(1 for t in tickets if t.get("owner") == m_name and t.get("status") == "실행 중")
        ver_c = sum(1 for t in tickets if t.get("verifier") == m_name and t.get("status") == "검증 중")
        over_c = sum(
            1 for t in tickets
            if t.get("owner") == m_name and t.get("status") in ("실행 중", "접수")
            and t.get("deadline") and str(t.get("deadline")).strip() < now_str
        )
        lounge_today = [l for l in lounge if l.get("name") == m_name and str(l.get("time", "")).startswith(today_prefix)]
        stress = 12 + run_c * 4 + ver_c * 3 + over_c * 9 - min(len(lounge_today) * 6, 18)
        mood = 82 - over_c * 6 + min(len(lounge_today) * 5, 15)
        if lounge_today:
            last_txt = lounge_today[-1].get("text", "")
            m_s = re.search(r"스트레스\s*[:：]?\s*(\d{1,3})", last_txt)
            m_m = re.search(r"기분\s*[:：]?\s*(\d{1,3})", last_txt)
            if m_s:
                stress = int(m_s.group(1))
            if m_m:
                mood = int(m_m.group(1))
        stress = max(5, min(98, stress))
        mood = max(10, min(100, mood))
        if stress >= 70:
            emoji, fx = "😫", "💢"
        elif stress >= 45:
            emoji, fx = "😅", "💦"
        elif mood >= 80:
            emoji, fx = "🤩", "✨"
        else:
            emoji, fx = "😊", "☕"
        mood_stress[m_name] = {
            "stress": stress, "mood": mood, "emoji": emoji, "fx": fx,
            "run": run_c, "over": over_c, "lounge_cnt": len(lounge_today),
        }

    return {
        "updated": datetime.now(JST).strftime("%Y-%m-%d %H:%M JST"),
        "tickets": tickets,
        "opinions": list(reversed(opinions[-12:])),
        "done_recent": list(reversed(done_recent[-15:])),
        "done_total": len(done_recent),
        "ceo_briefings": list(reversed(ceo_briefings[-7:])),
        "lounge": lounge[-40:],
        "airbnb_monthly": airbnb_monthly,
        "mood_stress": mood_stress,
        "cleaning": cleaning or {"days": []},
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

    now_jst = datetime.now(JST)
    try:
        cleaning = build_cleaning(token, now_jst.strftime("%Y-%m-%d"), (now_jst + timedelta(days=1)).strftime("%Y-%m-%d"))
    except Exception as e:
        print("CLEANING_ERR", e)
        cleaning = {"error": str(e)[:200], "days": []}

    payload = build_payload(q_rows, o_rows, d_rows, l_rows, cleaning)
    core_hash = hashlib.sha256(
        json.dumps(
            {"tickets": payload["tickets"], "opinions": payload["opinions"], "done_recent": payload["done_recent"], "lounge": payload["lounge"], "cleaning": payload["cleaning"]},
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

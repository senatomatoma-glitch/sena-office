#!/usr/bin/env python3
import os, sys, json, base64, subprocess
from datetime import datetime, timezone, timedelta
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

JST = timezone(timedelta(hours=9))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "work"))
import secret_build_cards as sbc
import secret_fetch as sfc

ENC_PATH = os.path.join(ROOT, "secret_enc.json")
WEEK_CACHE = os.path.join(ROOT, "work", "secret_week_cache.json")
MONTHLY_STATE = os.path.join(ROOT, "work", "secret_monthly.json")
MONTHLY_TTL = timedelta(hours=168)      # 마지막 갱신 후 7일
MONTHLY_REFRESH_HOUR = 4                # 월요일 04:00 JST 이후 이번 주 미갱신이면 갱신


def _read_old_payload(pin):
    try:
        e = json.load(open(ENC_PATH, encoding="utf-8"))
        key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32,
                         salt=base64.b64decode(e["s"]), iterations=100000).derive(pin.encode("utf-8"))
        pt = AESGCM(key).decrypt(base64.b64decode(e["i"]), base64.b64decode(e["c"]), None)
        return json.loads(pt)
    except Exception as ex:
        print(f"[week-cache] old payload unreadable ({type(ex).__name__}) — cold start")
        return None


def restore_week_cache(old):
    """직전 실행의 secret_enc.json 안 `_wc`(주간 스케줄 캐시)를 복원한다.
    워크플로가 secret_enc.json 만 커밋하므로, 5분마다 도는 실행 사이에 7일치 캐시를 이어쓰는 유일한 경로.
    로컬에 이미 더 새 캐시가 있으면 그것을 살리고, 겹치는 항목은 ts 가 새것을 쓴다."""
    wc = (old or {}).get("_wc") or {}
    items = wc.get("items") if isinstance(wc, dict) else None
    if not isinstance(items, dict) or not items:
        return
    cur = {}
    try:
        d = json.load(open(WEEK_CACHE, encoding="utf-8"))
        if isinstance(d.get("items"), dict):
            cur = d["items"]
    except Exception:
        cur = {}
    for k, v in items.items():
        if k not in cur or (v or {}).get("ts", 0) > (cur[k] or {}).get("ts", 0):
            cur[k] = v
    os.makedirs(os.path.dirname(WEEK_CACHE), exist_ok=True)
    json.dump({"v": 2, "items": cur}, open(WEEK_CACHE, "w", encoding="utf-8"), ensure_ascii=False)
    print(f"[week-cache] restored {len(items)} entries from previous secret_enc.json")


def read_week_cache():
    try:
        import time as _t
        d = json.load(open(WEEK_CACHE, encoding="utf-8"))
        items = d.get("items") or {}
        if not isinstance(items, dict):
            return None
        cutoff = _t.time() - 3 * 86400
        items = {k: v for k, v in items.items() if (v or {}).get("ts", 0) >= cutoff}
        return {"v": 2, "items": items}
    except Exception:
        return None


def _ts_jst(s):
    """'_monthly_shops.updated_at' 파싱 — JST 오프셋 포함/미포함 모두."""
    if not isinstance(s, str) or not s.strip():
        return None
    t = s.strip().replace("Z", "+00:00")
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M"):
        try:
            dt = datetime.strptime(t, fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=JST)
        except ValueError:
            continue
    return None


def restore_monthly_state(old):
    """직전 secret_enc.json payload 의 `_monthly_shops` 를 work/secret_monthly.json 으로 복원.
    워크플로가 secret_enc.json 만 커밋하므로 주 1회 교체 결과를 실행 사이에 이어쓰는 유일한 경로."""
    ms = (old or {}).get("_monthly_shops")
    if not isinstance(ms, dict) or not isinstance(ms.get("shops"), list) or not ms["shops"]:
        return None
    cur = None
    try:
        d = json.load(open(MONTHLY_STATE, encoding="utf-8"))
        if isinstance(d, dict) and isinstance(d.get("shops"), list):
            cur = d
    except Exception:
        cur = None
    a, b = _ts_jst((cur or {}).get("updated_at")), _ts_jst(ms.get("updated_at"))
    if cur and a and b and a > b:
        print(f"[monthly] local state newer ({cur.get('updated_at')}) — kept")
        return cur
    os.makedirs(os.path.dirname(MONTHLY_STATE), exist_ok=True)
    json.dump(ms, open(MONTHLY_STATE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"[monthly] restored {len(ms['shops'])} shops from previous secret_enc.json ({ms.get('updated_at')})")
    return ms


def monthly_due(state, now):
    """월간 TOP5 갱신 시점 — ① 없음 ② 마지막 갱신 후 7일 경과 ③ 월요일 04:00 JST 이후 이번 주 미갱신."""
    ts = _ts_jst((state or {}).get("updated_at"))
    if not state or not isinstance(state.get("shops"), list) or not state["shops"] or not ts:
        return True, "no state"
    want_v = int(getattr(sfc, "MONTHLY_STATE_V", 1) or 1)
    try:
        have_v = int(state.get("v") or 1)
    except Exception:
        have_v = 1
    if have_v < want_v:
        return True, f"schema v{have_v} -> v{want_v} (인기 TOP5 + 접객 TOP3)"
    age = now - ts
    if age >= MONTHLY_TTL:
        return True, f"age {age.total_seconds() / 3600:.1f}h >= 168h"
    monday = (now - timedelta(days=now.weekday())).replace(hour=MONTHLY_REFRESH_HOUR, minute=0,
                                                          second=0, microsecond=0)
    if now >= monday and ts < monday and age >= timedelta(hours=24):
        return True, f"monday {monday:%Y-%m-%d %H:%M} JST anchor not passed"
    return False, f"fresh ({age.total_seconds() / 3600:.1f}h since {ts:%Y-%m-%d %H:%M} JST)"


def refresh_monthly(pin_state):
    """secret_fetch 의 수집기를 빌려 4지역 월간 TOP5 를 새로 긁고 상태를 저장한다."""
    state = sfc.refresh_monthly_top5(pin_state)
    if not state or not state.get("shops"):
        print("[monthly] refresh produced nothing — keeping previous state")
        return pin_state
    return state


def main():
    fetch_script = os.path.join(ROOT, "work", "secret_fetch.py")
    live_path = os.path.join(ROOT, "work", "secret_live.json")
    cache_path = os.path.join(ROOT, "work", "secret_cache.json")

    pin = os.environ["SECRET_PIN"]
    old = _read_old_payload(pin)
    restore_week_cache(old)
    monthly_state = restore_monthly_state(old)
    if monthly_state is None:
        try:
            monthly_state = sfc.load_monthly_state()   # payload 못 읽어도 남아있는 로컬 상태는 살린다
        except Exception:
            monthly_state = None
    now_jst = datetime.now(JST)
    due, why = monthly_due(monthly_state, now_jst)
    if due:
        print(f"[monthly] refresh due — {why}")
        monthly_state = refresh_monthly(monthly_state) or monthly_state
    else:
        print(f"[monthly] cached — {why}")

    subprocess.run([sys.executable, fetch_script, "--quick", "--max-age", "5", "--no-images", "--out", live_path], check=True)
    live = json.load(open(live_path, encoding="utf-8"))
    cache = json.load(open(cache_path, encoding="utf-8")) if os.path.exists(cache_path) else {}
    now = datetime.now(JST).replace(tzinfo=None)

    sbc.SHOPS_LIVE = live.get("shops") or {}
    shops = sbc.SHOPS_LIVE
    b = sbc.build(live, cache, now)

    def person_obj(p, roles=None):
        sh_name = p.get("shop") or ""
        sh = shops.get(sh_name, {})
        img = p.get("img_url") or p.get("img") or ""
        if img.startswith("downloads/"):
            img = ""
        st = p.get("status") or sbc.status_of(p, now)
        tag = ", ".join(sorted(roles)) if roles else ("신입" if p.get("new") else "")
        if roles and "가게 순위" in roles:
            tag = sbc.ROLE_LABEL_FN["가게 순위"](p, sh)
        week = p.get("week") or []
        if not any(week):
            week = []
        return {
            "name": p.get("name") or "",
            "age": p.get("age"),
            "cup": p.get("cup") or "",
            "height": p.get("height"),
            "shop": sh_name,
            "area": sh.get("area") or "",
            "status": st,
            "today": sbc.today_text(p) or "",
            "price": sbc.price_text(p) or (f"{sh.get('free_course','')} ¥{sh.get('price_free'):,}" if sh.get("price_free") else ""),
            "phone": sh.get("phone") or "",
            "url": p.get("url") or sh.get("official_url") or "",
            "img": img,
            "new": bool(p.get("new")),
            "tag": tag,
            "week": week,
        }

    def shop_obj(s):
        pf = s.get("price_free")
        fc = s.get("free_course") or "120분"
        return {
            "is_shop": True,
            "name": s.get("name") or s.get("shop") or "",
            "shop": s.get("name") or s.get("shop") or "",
            "area": s.get("area") or "",
            "status": "🆓",
            "today": s.get("hours") or "",
            "price": f"{fc} ¥{pf:,}" if pf else "",
            "phone": s.get("phone") or "",
            "url": s.get("official_url") or s.get("url") or "",
            "img": s.get("logo_url") or "",
            "tag": sbc.home_badge(s.get("name") or "", s) or "",
        }

    def ord_key(p):
        """카드 안 순서: 🟢 → 🟡 → 📅 → ⚪ → ❔ (출근시간·다음 출근일 빠른 순)."""
        return sbc.display_order(p, now)

    c1 = sorted((b.get("card1") or []), key=lambda pr: ord_key(pr[0]))
    c2_people = sorted((b.get("reps2") or []), key=ord_key)
    c3 = sorted((b.get("card3") or []), key=ord_key)
    c4 = sorted((b.get("card4") or []), key=ord_key)
    c5 = sorted((b.get("card5") or []), key=ord_key)
    c6 = sorted((b.get("card6") or []), key=ord_key)
    c7 = sorted((b.get("card7") or []), key=ord_key)
    c8 = sorted((b.get("card8") or []), key=lambda v: ord_key(v["p"]))

    cards = [
        {"id": "card1", "title": "🟢 지금 가능 · 오늘 예정", "items": [person_obj(p, roles) for p, roles in c1]},
        {"id": "card2", "title": "🏪 가게별 랭킹 대표 (공식 랭킹 우선)", "items": [person_obj(p, {'가게 순위'}) for p in c2_people] + [shop_obj(s) for s in (b.get("shop_level") or [])]},
        {"id": "card3", "title": "🌱 신입·체입 최대 컵 TOP 6", "items": [person_obj(p) for p in c3]},
        {"id": "card4", "title": "💎 최대 컵 TOP 6", "items": [person_obj(p) for p in c4]},
        {"id": "card5", "title": "🌸 최연소·최대 컵 TOP 6", "items": [person_obj(p) for p in c5]},
        {"id": "card6", "title": "✨ 최대 컵·연상 우선 TOP 6", "items": [person_obj(p) for p in c6]},
        {"id": "card7", "title": "🔥 H컵 이상 · 155cm 이하 TOP 6", "items": [person_obj(p) for p in c7]},
        {"id": "card8", "title": "📋 전체 인원 리스트", "items": [person_obj(v["p"], v.get("roles")) for v in c8]},
        {"id": "card9", "title": "🆓 가게별 프리 (120분 가격순)", "items": [shop_obj(s) for s in (b.get("shops_sorted") or [])]},
    ]

    all_items = cards[7]["items"]  # card8 is full union
    wc = read_week_cache() or {"v": 2, "items": {}}
    mon_out = monthly_state if isinstance(monthly_state, dict) else None
    if mon_out:
        mon_out = {k: mon_out.get(k) for k in ("v", "updated_at", "basis", "areas", "shops", "notes") if mon_out.get(k) is not None}
    payload_obj = {
        "updated": now.strftime("%Y-%m-%d %H:%M JST"),
        "week_days": live.get("meta", {}).get("week_days") or [],
        "_wc": wc,
        "counts": {
            "green": sum(1 for x in all_items if x["status"] == "🟢"),
            "yellow": sum(1 for x in all_items if x["status"] == "🟡"),
            "total": len(all_items),
        },
        "cards": cards,
        "items": all_items,
    }
    if mon_out:
        payload_obj["_monthly_shops"] = mon_out
    payload = json.dumps(payload_obj, ensure_ascii=False).encode("utf-8")

    salt = os.urandom(16)
    iv = os.urandom(12)
    key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=100000).derive(os.environ["SECRET_PIN"].encode("utf-8"))
    ct = AESGCM(key).encrypt(iv, payload, None)

    out_path = os.path.join(ROOT, "secret_enc.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({
            "s": base64.b64encode(salt).decode("ascii"),
            "i": base64.b64encode(iv).decode("ascii"),
            "c": base64.b64encode(ct).decode("ascii"),
        }, f)
    print(f"Wrote secret_enc.json: 9 cards, {len(all_items)} total people")
    if mon_out:
        act = [s for s in (live.get("shops") or {}).values() if s.get("active")]
        print(f"[monthly] payload `_monthly_shops` {mon_out.get('updated_at')} — "
              f"{len(mon_out.get('shops') or [])} slots, active shops {len(act)}")


if __name__ == "__main__":
    main()

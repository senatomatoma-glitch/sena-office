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

ENC_PATH = os.path.join(ROOT, "secret_enc.json")
WEEK_CACHE = os.path.join(ROOT, "work", "secret_week_cache.json")


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


def restore_week_cache(pin):
    """직전 실행의 secret_enc.json 안 `_wc`(주간 스케줄 캐시)를 복원한다.
    워크플로가 secret_enc.json 만 커밋하므로, 5분마다 도는 실행 사이에 7일치 캐시를 이어쓰는 유일한 경로.
    로컬에 이미 더 새 캐시가 있으면 그것을 살리고, 겹치는 항목은 ts 가 새것을 쓴다."""
    old = _read_old_payload(pin)
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


def main():
    fetch_script = os.path.join(ROOT, "work", "secret_fetch.py")
    live_path = os.path.join(ROOT, "work", "secret_live.json")
    cache_path = os.path.join(ROOT, "work", "secret_cache.json")

    pin = os.environ["SECRET_PIN"]
    restore_week_cache(pin)

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
    payload = json.dumps({
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
    }, ensure_ascii=False).encode("utf-8")

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


if __name__ == "__main__":
    main()

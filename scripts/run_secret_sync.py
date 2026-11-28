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


def main():
    fetch_script = os.path.join(ROOT, "work", "secret_fetch.py")
    live_path = os.path.join(ROOT, "work", "secret_live.json")
    cache_path = os.path.join(ROOT, "work", "secret_cache.json")

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
        {"id": "card2", "title": "🏪 가게별 랭킹 1위", "items": [person_obj(p) for p in c2_people] + [shop_obj(s) for s in (b.get("shop_level") or [])]},
        {"id": "card3", "title": "🌱 신입·체입 최대 컵 TOP 6", "items": [person_obj(p) for p in c3]},
        {"id": "card4", "title": "💎 최대 컵 TOP 6", "items": [person_obj(p) for p in c4]},
        {"id": "card5", "title": "🌸 최연소·최대 컵 TOP 6", "items": [person_obj(p) for p in c5]},
        {"id": "card6", "title": "✨ 최대 컵·연상 우선 TOP 6", "items": [person_obj(p) for p in c6]},
        {"id": "card7", "title": "🔥 H컵 이상 · 155cm 이하 TOP 6", "items": [person_obj(p) for p in c7]},
        {"id": "card8", "title": "📋 전체 인원 리스트", "items": [person_obj(v["p"], v.get("roles")) for v in c8]},
        {"id": "card9", "title": "🆓 가게별 프리 (120분 가격순)", "items": [shop_obj(s) for s in (b.get("shops_sorted") or [])]},
    ]

    all_items = cards[7]["items"]  # card8 is full union
    payload = json.dumps({
        "updated": now.strftime("%Y-%m-%d %H:%M JST"),
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

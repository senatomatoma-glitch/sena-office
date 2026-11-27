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

    # Collect core picks + all 🟢/🟡 E+ people
    seen = set()
    items = []

    def add_person(p, tag=""):
        key = (p.get("shop") or "", sbc.norm_name(p.get("name") or ""))
        if key in seen:
            return
        seen.add(key)
        st = sbc.status_of(p, now)
        sh = shops.get(p.get("shop") or "", {})
        img = p.get("img_url") or p.get("img") or ""
        if img.startswith("downloads/"):
            img = ""
        items.append({
            "name": p.get("name") or "",
            "age": p.get("age"),
            "cup": p.get("cup") or "",
            "height": p.get("height"),
            "shop": p.get("shop") or "",
            "area": sh.get("area") or "",
            "status": st,
            "today": sbc.today_text(p) or "",
            "price": sbc.price_text(p) or (f"{sh.get('free_course','')} ¥{sh.get('price_free'):,}" if sh.get("price_free") else ""),
            "phone": sh.get("phone") or "",
            "url": p.get("url") or sh.get("official_url") or "",
            "img": img,
            "new": bool(p.get("new")),
            "tag": tag,
        })

    # 1. Core picks from build()
    for p, roles in (b.get("core_picks") or []):
        add_person(p, ", ".join(roles))

    # 2. All active 🟢 / 🟡 people with E+ cup (or any 🟢)
    pool = [p for p in (live.get("people") or []) if sbc.status_of(p, now) in ("🟢", "🟡") and sbc.cup_rank(p) >= 5]
    pool.sort(key=lambda p: (0 if sbc.status_of(p, now) == "🟢" else 1, -sbc.cup_rank(p), p.get("age") or 99))
    for p in pool[:140]:
        add_person(p, "추천" if p.get("new") else "")

    payload = json.dumps({
        "updated": now.strftime("%Y-%m-%d %H:%M JST"),
        "counts": {
            "green": sum(1 for x in items if x["status"] == "🟢"),
            "yellow": sum(1 for x in items if x["status"] == "🟡"),
            "total": len(items),
        },
        "items": items,
    }, ensure_ascii=False).encode("utf-8")

    salt = os.urandom(16)
    iv = os.urandom(12)
    key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=100000).derive(b"0710")
    ct = AESGCM(key).encrypt(iv, payload, None)

    out_path = os.path.join(ROOT, "secret_enc.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({
            "s": base64.b64encode(salt).decode("ascii"),
            "i": base64.b64encode(iv).decode("ascii"),
            "c": base64.b64encode(ct).decode("ascii"),
        }, f)
    print(f"Wrote secret_enc.json: {len(items)} items (green={sum(1 for x in items if x['status']=='🟢')})")

if __name__ == "__main__":
    main()

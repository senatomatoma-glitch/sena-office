#!/usr/bin/env python3
"""비밀놀이 자동 카드 빌더 — work/secret-live.json + work/secret_cache.json → card_1..9.txt

- work/secret-list.md 규칙 1~10을 코드로 구현
- 필요한 인물 사진만 downloads/secret-photos/ 로 내려받고 PIL로 검증
- 출력: tmp/secret_cards_auto/{card_1..9.txt, summary.md} + work/secret_build_report.json

사용: python3 work/secret_build_cards.py [--live work/secret_live.json] [--out tmp/secret_cards_auto]
"""
import os, re, sys, json, datetime, subprocess, hashlib
from concurrent.futures import ThreadPoolExecutor
from PIL import Image

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CUP_ORDER = {'A': 1, 'B': 2, 'C': 3, 'D': 4, 'E': 5, 'F': 6, 'G': 7, 'H': 8, 'I': 9, 'J': 10, 'K': 11}
CUP_NAME = {v: k for k, v in CUP_ORDER.items()}
UA = ('Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 '
      '(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1')
PHOTO_DIR = os.path.join(BASE, 'downloads/secret-photos')
WDAY = ['월', '화', '수', '목', '금', '토', '일']

ROLE_ORDER = ['등록 리스트', '가게 순위', '신입 대표', '최대 컵', '최연소', '연상 대표', 'H컵 대표']
ROLE_LABEL_FN = {
    '가게 순위': lambda p, shop: f"{shop.get('rank_label') or '본지명 랭킹'} 1위",
    '신입 대표': lambda p, shop: f"{p['shop']} 신입·체입 최대 컵 대표",
    '최대 컵': lambda p, shop: f"{p['shop']} 최대 컵 대표",
    '최연소': lambda p, shop: f"{p['shop']} 최연소·최대 컵 대표",
    '연상 대표': lambda p, shop: f"{p['shop']} 최대 컵·연상 대표",
    'H컵 대표': lambda p, shop: f"{p['shop']} H컵 이상·155cm 이하 대표",
}

STATUS_RANK = {'🟢': 0, '🟡': 1, '📅': 2, '⚪': 3, '❔': 4}
SHOPS_LIVE = {}


def norm_name(s):
    if not s:
        return ''
    s = s.replace('\u3000', ' ').strip()
    s = re.sub(r'\s*\d+分\s*\d+円.*$', '', s)
    s = re.sub(r'★\s*\d+', '', s)
    s = re.sub(r'[（(]\s*\d+\s*[)）]', '', s)
    s = re.sub(r'【.*?】', '', s)
    s = re.sub(r'^日本橋\s+|^難波\s+|^梅田\s+|^心斎橋\s+', '', s)
    s = s.replace('♡', '').replace(' ', '')
    return s.strip()


def to_min(s):
    m = re.match(r'^(\d{1,2}):(\d{2})$', (s or '').strip())
    if not m:
        return None
    return int(m.group(1)) * 60 + int(m.group(2))


def cup_rank(p):
    return CUP_ORDER.get(p.get('cup') or '', 0)


def photo_ok(p):
    img = p.get('image') or ''
    return bool(img) and os.path.exists(os.path.join(BASE, img)) and os.path.getsize(os.path.join(BASE, img)) > 2000


def photo_maybe(p):
    """선발 단계용: 로컬 사진이 있거나, 내려받을 원격 사진 URL이 있으면 후보로 둔다.
    실제 카드 탑재 전에 다운로드·검증을 거쳐 photo_ok 로 다시 거른다."""
    if p.get('photo_failed'):
        return False
    return photo_ok(p) or bool(p.get('img_url') or p.get('img'))


def status_of(p, now):
    today = p.get('today') or {}
    op, cl = today.get('open'), today.get('close')
    if today.get('soldout'):
        return '⚪'
    if op and cl:
        o, c = to_min(op), to_min(cl)
        while c is not None and o is not None and c <= o:
            c += 1440
        if o is not None and c is not None:
            cur = now.hour * 60 + now.minute
            if cur < o:
                return '🟡'
            if cur < c:
                return '🟢'
            return '⚪'
    nxt = p.get('next_shift') or ''
    if nxt and '미정' not in nxt and '없음' not in nxt and '미등록' not in nxt:
        return '📅'
    return '❔'


def official_ok(p):
    """규칙 9 — 공식 재적 목록이 충분히 완전할 때만 목록 밖 인원을 제외. 등록 리스트는 항상 유지."""
    if p.get('registered'):
        return True
    if p.get('official_authoritative') and p.get('in_official') is False:
        return False
    ratio = p.get('official_ratio')
    if ratio is None:
        return True
    if ratio >= 0.6:
        return p.get('in_official') is not False
    return True


def eligible(p, min_cup=5):
    if not official_ok(p):
        return False
    if not min_cup:
        return True
    if cup_rank(p) < min_cup:
        return False
    if p.get('age') is None or p.get('height') is None:
        return False
    if not p.get('cup'):
        return False
    return True


def sort_key_cup(p):
    return (-cup_rank(p), p.get('age') or 99, p.get('height') or 999)


def sort_key_young(p):
    return (p.get('age') or 99, -cup_rank(p), p.get('height') or 999)


def sort_key_old_shop(p):
    return (-cup_rank(p), -(p.get('age') or 0), p.get('height') or 999)


def sort_key_old(p):
    return (-(p.get('age') or 0), p.get('height') or 999, -cup_rank(p))


def shop_best(pool, keyfn):
    best = {}
    for p in pool:
        k = p['shop']
        if k not in best or keyfn(p) < keyfn(best[k]):
            best[k] = p
    return list(best.values())


def display_order(p, now):
    st = p['status']
    if st in ('🟢', '🟡', '⚪'):
        t = to_min((p.get('today') or {}).get('open')) or 9999
        return (STATUS_RANK[st], t, p['shop'], p['name'])
    if st == '📅':
        m = re.search(r'(\d{1,2})/(\d{1,2})\([^)]*\)\s*(\d{1,2}):(\d{2})', p.get('next_shift') or '')
        if m:
            mm, dd, hh, mi = map(int, m.groups())
            return (2, mm * 1000000 + dd * 10000 + hh * 60 + mi, p['shop'], p['name'])
        return (2, 9999999, p['shop'], p['name'])
    return (4, 9999, p['shop'], p['name'])


def today_text(p):
    st = p['status']
    today = p.get('today') or {}
    op, cl = today.get('open'), today.get('close')
    if st == '🟢':
        return f"지금 가능 (~{cl})" if cl else "지금 가능"
    if st == '🟡':
        return f"오늘 {op}~{cl} 예정" if op else "오늘 출근 예정"
    if st == '⚪':
        return f"오늘 {op}~{cl} 마감" if op else "오늘 마감"
    if st == '📅':
        nxt = p.get('next_shift') or ''
        first = nxt.split(' · ')[0].strip()
        return f"{first} 출근" if first else "다음 출근 예정"
    return "출근 미정"


def price_text(p):
    tot = p.get('price_total')
    if not tot:
        return "금액 확인 필요"
    note = p.get('price_note') or ''
    return f"¥{tot:,}" + (f"({note})" if note else "")


def fmt_role_lines(p, roles, shops):
    out = []
    shop = shops.get(p['shop'], {})
    for r in ROLE_ORDER:
        if r not in roles:
            continue
        if r == '등록 리스트':
            out.append(f"- **등록 리스트** {norm_name(p['name'])} · 등록 리스트")
        else:
            out.append(f"- **{r}** {ROLE_LABEL_FN[r](p, shop)}")
    return out


HOME_DISPATCH_OK = {
    'SUPER HAPPY GIRLS', '俺の家', 'Pinkyring 難波店', 'Pinky ring 日本橋店',
    'SPA シルビア', 'SUKI♡だらけ 難波店', '天使の手のひら 心斎橋店',
}


def home_badge(shop_name, shop=None):
    ok = (shop or {}).get('home_dispatch')
    if ok is None:
        ok = shop_name in HOME_DISPATCH_OK
    return '🏠 자택OK' if ok else '🏨 호텔·룸(자택미확인)'


def home_detail_text(shop_name, shop=None):
    ok = (shop or {}).get('home_dispatch')
    if ok is None:
        ok = shop_name in HOME_DISPATCH_OK
    return '가능 (공식 페이지 자택 출장 명시)' if ok else '호텔·룸 위주 (공식 페이지에 자택 가능 명시 없음)'


def fmt_person(p, shops, roles=None, compact=False):
    roles = roles if roles is not None else p.get('roles_all', [])
    shop = shops.get(p['shop'], {})
    cup_txt = f"{p['cup']}컵" if p.get('cup') else "컵 미공개"
    ht_txt = f"{p['height']}cm" if p.get('height') else "신장 미공개"
    age_txt = f"{p['age']}세" if p.get('age') else "나이 미공개"
    detail = p.get('price_detail') or (shop.get('price') or {}).get('detail') or ''
    feats = p.get('features') or ' · '.join(p.get('tags') or []) or '-'
    url = p.get('url') or (p.get('seed') or {}).get('profile_url') or ''
    if os.path.exists(os.path.join(BASE, 'work/secret_official_urls.json')):
        _omap = json.load(open(os.path.join(BASE, 'work/secret_official_urls.json'), encoding='utf-8'))
        _nm = re.sub(r'^日本橋\s+|^難波\s+|^梅田\s+|^心斎橋\s+', '', re.sub(r'【.*?】|[（(]\s*\d+\s*[)）]|★\s*\d+|\s*\d+分\s*\d+円.*$', '', p.get('name', '').replace('\u3000', ' ').strip())).replace('♡', '').replace(' ', '').replace('\u3000', '').strip()
        _ov = (_omap.get('profiles') or {}).get(f"{p['shop']}|{_nm}") or (_omap.get('lists') or {}).get(p['shop'])
        if _ov:
            url = _ov
    phone = (p.get('seed') or {}).get('phone') or shop.get('phone') or ''
    nxt = p.get('next_shift') or '미정(사이트 미등록)'
    img_path = p.get('image') or ''
    lines = [
        f"## {p['status']} {p['name']} · {age_txt} · {cup_txt} · {ht_txt}",
        f"### {today_text(p)} · {price_text(p)}",
    ]
    if img_path and os.path.exists(os.path.join(BASE, img_path)):
        lines.append(f"![{p['name']}]({img_path})")
    lines += [
        "",
        f"- **가게** {p['shop']}",
        f"- **자택 출장** {home_detail_text(p['shop'], shop)}",
    ]
    if not compact:
        lines += fmt_role_lines(p, roles, shops)
        lines += [
            f"- **오늘 상태** {today_text(p)}",
            f"- **다음 출근** {nxt}",
            f"- **금액 상세** {detail}",
            f"- **특징** {feats}",
            f"- **전화번호** {phone}",
        ]
    else:
        lines += [
            f"- **금액 상세** {detail}",
            f"- **전화번호** {phone}",
        ]
    if url:
        lines.append(f"[공식 프로필]({url})")
    return "\n".join(lines)


ESTAMA_AREA_NAME = {'2704': '難波', '2703': '日本橋', '2702': '心斎橋'}


def estama_rank_text(live, shop):
    sid = str(shop.get('estama') or '')
    for area, rows in (live.get('estama_shop_rank') or {}).items():
        for r in rows:
            if r['shop_id'] == sid:
                return f"에스타마 {ESTAMA_AREA_NAME.get(area, area)} 월간 {r['rank']}위"
    return None


def fmt_shop_entry(shop, shops, rank_text=None, inline=False):
    logo = shop.get('logo') or ''
    pf = shop.get('price_free')
    course = shop.get('free_course') or ''
    tr = shop.get('transport_note') or '미기재'
    pr = shop.get('price') or {}
    hotel_note = ''
    if pr.get('hotel'):
        hotel_note = f" · 호텔 {pr['hotel']:,}엔"
    price_line = f"프리(지명 없음) ¥{pf:,}({course})" if pf else "프리 가격 미확인"
    tail = ' · 세라피스트 미등록' if shop.get('adapter') == 'estama' else ''
    sub2 = ("🏪 " if inline else "") + f"{price_line} · 교통비 {tr}{hotel_note}{tail}"
    lines = [
        f"## 🏪 {shop['name']}",
        f"### {sub2}",
    ]
    if logo and os.path.exists(os.path.join(BASE, logo)):
        lines.append(f"![{shop['name']}]({logo})")
    lines += [
        "",
        f"- **가게** {shop['name']} ({shop.get('area') or ''})",
        f"- **자택 출장** {home_detail_text(shop['name'], shop)}",
    ]
    rt = rank_text or estama_rank_text(SHOPS_LIVE, shop) or shop.get('rank_label')
    if rt:
        lines.append(f"- **가게 순위** {rt}")
    lines += [
        f"- **금액 상세** {shop.get('price_detail') or (shop.get('price') or {}).get('detail') or ''}",
        f"- **전화번호** {shop.get('phone') or ''}",
    ]
    surl = shop.get('official_url') or ''
    if surl:
        lines.append(f"[공식 홈페이지]({surl})")
    return "\n".join(lines)


def card_wrap(title, blocks):
    body = "\n```\n```card\n".join(blocks)
    return f"````card\n{title}\n\n```card\n{body}\n```\n````\n" if blocks else f"````card\n{title}\n````\n"


# ---------------------------------------------------------------- build
def build(live, cache, now):
    shops = live['shops']
    people = []
    for p in live['people']:
        if not p.get('name'):
            continue
        p['status'] = status_of(p, now)
        p['cup_rank'] = cup_rank(p)
        people.append(p)
    # registered seed info merged already; roles from seed
    for p in people:
        seed = p.get('seed') or {}
        p['roles_seed'] = seed.get('roles') or []
    all_people = people

    min_cup_people = [p for p in people if eligible(p, 5)]
    with_photo = [p for p in min_cup_people if photo_maybe(p)]
    any_photo = [p for p in people if eligible(p, 0) and photo_maybe(p)]
    # card 2 — shop ranking 1
    reps2 = []
    shop_level = []
    for skey, shop in shops.items():
        if not shop.get('active'):
            continue
        if skey == 'Pinky ring 日本橋店':
            continue
        cands = [p for p in any_photo if p['shop'] == skey]
        rep = None
        st = live['shop_status'].get(skey, {})
        rank_list = st.get('ms_rank') or st.get('brand_rank') or []
        by_name = {norm_name(c['name']): c for c in cands}
        for rk in rank_list:
            c = by_name.get(norm_name(rk['name']))
            if c and c.get('age'):
                rep = c
                break
        if not rep and shop.get('adapter') in ('estama', 'brand'):
            order = sorted([p for p in live['people'] if p['shop'] == skey],
                           key=lambda x: (x.get('order') if x.get('order') is not None else 9999))
            for c in order:
                if c in cands:
                    rep = c
                    break
        if rep:
            reps2.append(rep)
        elif shop.get('adapter') in ('estama', 'brand'):
            shop_level.append(shop)

    def dedupe_by_name(lst):
        seen, out = set(), []
        for p in lst:
            shop = 'Pinkyring 難波店' if p['shop'] == 'Pinky ring 日本橋店' else p['shop']
            k = (shop, norm_name(p['name']))
            if k in seen:
                continue
            seen.add(k)
            out.append(p)
        return out

    # card 3 — new / 体입 max cup per shop -> top6
    new_pool = [p for p in with_photo if p.get('new') or '신입 대표' in (p.get('roles_seed') or []) or '新人' in (p.get('tags') or [])]
    card3 = sorted(shop_best(dedupe_by_name(new_pool), sort_key_cup), key=sort_key_cup)[:6]
    # card 4 — max cup per shop -> top6 (나이 어린 순 → 키 작은 순)
    card4 = sorted(shop_best(dedupe_by_name(with_photo), sort_key_cup), key=lambda p: (p.get('age') or 99, p.get('height') or 999))[:6]
    # card 5 — youngest per shop -> top6
    card5 = sorted(shop_best(dedupe_by_name(with_photo), sort_key_young), key=sort_key_young)[:6]
    # card 6 — max cup oldest per shop -> top6 by age desc
    card6 = sorted(shop_best(dedupe_by_name(with_photo), sort_key_old_shop), key=sort_key_old)[:6]
    # card 7 — H+ <=155 global -> top6
    h_pool = [p for p in with_photo if cup_rank(p) >= 8 and (p.get('height') or 999) <= 155]
    card7 = sorted(h_pool, key=sort_key_young)[:6]

    card3, card4, card5, card6, card7 = map(dedupe_by_name, (card3, card4, card5, card6, card7))
    survivors = {}
    for card, tag in ((card3, '신입 대표'), (card4, '최대 컵'), (card5, '최연소'), (card6, '연상 대표'), (card7, 'H컵 대표')):
        for p in card:
            key = (p['shop'], norm_name(p['name']))
            survivors.setdefault(key, dict(p=p, roles=set(), cards=[]))
            survivors[key]['roles'].add(tag)
            survivors[key]['cards'].append(tag)

    # card 1 — survivors (3~7) + registered, only 🟢/🟡
    registered = [p for p in all_people if p.get('registered')]
    card1 = []
    seen = set()
    for item in survivors.values():
        p = item['p']
        if p['status'] in ('🟢', '🟡'):
            card1.append((p, set(item['roles']) | ({'등록 리스트'} if p.get('registered') else set())))
            seen.add((p['shop'], norm_name(p['name'])))
    for p in registered:
        key = (p['shop'], norm_name(p['name']))
        if key in seen or not photo_maybe(p):
            continue
        if p['status'] in ('🟢', '🟡'):
            card1.append((p, {'등록 리스트'}))
            seen.add(key)
    card1.sort(key=lambda x: display_order(x[0], now))

    # card 8 — union card2..7 + registered
    union = {}
    for p in reps2:
        union.setdefault((p['shop'], norm_name(p['name'])), dict(p=p, roles=set()))
    for p in card3 + card4 + card5 + card6 + card7:
        union.setdefault((p['shop'], norm_name(p['name'])), dict(p=p, roles=set()))
    for item in survivors.values():
        union[(item['p']['shop'], norm_name(item['p']['name']))]['roles'] |= item['roles']
    for p in registered:
        key = (p['shop'], norm_name(p['name']))
        if not photo_maybe(p):
            continue
        union.setdefault(key, dict(p=p, roles=set()))['roles'].add('등록 리스트')
    for p in reps2:
        union[(p['shop'], norm_name(p['name']))]['roles'].add('가게 순위')
    card8 = [v for v in union.values()]
    card8.sort(key=lambda v: display_order(v['p'], now))

    # card 9 — shops by free price asc
    shop_list = []
    for skey, shop in shops.items():
        if not shop.get('active'):
            continue
        pf = shop.get('price_free')
        if pf:
            shop_list.append((pf, shop))
    shop_list.sort(key=lambda x: x[0])

    return dict(card1=card1, reps2=reps2, card3=card3, card4=card4, card5=card5,
                card6=card6, card7=card7, card8=card8, shop_level=shop_level,
                shops_sorted=[s for _, s in shop_list])


def render(b, live, cache, now):
    global SHOPS_LIVE
    SHOPS_LIVE = live
    shops = live['shops']
    out = {}
    out['card_1'] = card_wrap(
        f"## 🟢 지금 가능 / 오늘 밤 출근 예정 (통합 · {len(b['card1'])}명)",
        [fmt_person(p, shops, roles) for p, roles in b['card1']])
    entries2 = []
    for p in sorted(b['reps2'], key=lambda x: display_order(x, now)):
        entries2.append(fmt_person(p, shops, {'가게 순위'}))
    out['card_2'] = card_wrap(
        f"## 🏪 가게별 랭킹 1위 ({len(b['reps2'])}명)",
        entries2)
    out['card_3'] = card_wrap(
        f"## 🌱 신입·체입 최대 컵 TOP 6 (E컵 이상) · {len(b['card3'])}명",
        [fmt_person(p, shops, {'신입 대표'}) for p in sorted(b['card3'], key=lambda x: display_order(x, now))])
    out['card_4'] = card_wrap(
        f"## 💎 최대 컵 TOP 6 (E컵 이상) · {len(b['card4'])}명",
        [fmt_person(p, shops, {'최대 컵'}) for p in sorted(b['card4'], key=lambda x: display_order(x, now))])
    out['card_5'] = card_wrap(
        f"## 🌸 최연소·최대 컵 TOP 6 (E컵 이상) · {len(b['card5'])}명",
        [fmt_person(p, shops, {'최연소'}) for p in sorted(b['card5'], key=lambda x: display_order(x, now))])
    out['card_6'] = card_wrap(
        f"## ✨ 최대 컵·연상 우선 TOP 6 (E컵 이상) · {len(b['card6'])}명",
        [fmt_person(p, shops, {'연상 대표'}) for p in sorted(b['card6'], key=lambda x: display_order(x, now))])
    out['card_7'] = card_wrap(
        f"## 🔥 H컵 이상 · 155cm 이하 TOP 6 · {len(b['card7'])}명",
        [fmt_person(p, shops, {'H컵 대표'}) for p in sorted(b['card7'], key=lambda x: display_order(x, now))])
    out['card_8'] = card_wrap(
        f"## 📋 전체 인원 리스트 ({len(b['card8'])}명)",
        [fmt_person(v['p'], shops, v['roles']) for v in b['card8']])
    entries9 = [fmt_shop_entry(s, shops) for s in b['shops_sorted']]
    out['card_9'] = card_wrap(
        f"## 🆓 가게별 프리(지명 없음) 120분 가격 오름차순 ({len(b['shops_sorted'])}곳)",
        entries9)
    out['card_1_core'] = card_wrap(
        f"## 🟢 지금 가능 / 오늘 밤 출근 예정 (통합 · {len(b['card1'])}명)",
        [fmt_person(p, shops, roles, compact=True) for p, roles in b['card1']])
    reps2_today = [p for p in sorted(b['reps2'], key=lambda x: display_order(x, now)) if p.get('status') in ('🟢', '🟡')]
    out['card_2_core'] = card_wrap(
        f"## 🏪 가게별 랭킹 1위 · 오늘 출근 ({len(reps2_today)}명)",
        [fmt_person(p, shops, {'가게 순위'}, compact=True) for p in reps2_today])
    return out


def download_selected(b, live):
    """카드에 실제로 올라가는 인물 중 로컬 사진이 없는 사람만 병렬 다운로드."""
    shops = live['shops']
    targets = []
    seen = set()
    for p in [x[0] for x in b['card1']] + b['reps2'] + b['card3'] + b['card4'] + b['card5'] + b['card6'] + b['card7'] + [v['p'] for v in b['card8']]:
        k = (p['shop'], norm_name(p['name']))
        if k in seen or photo_ok(p):
            continue
        src = p.get('img_url') or p.get('img') or ''
        if not src:
            continue
        seen.add(k)
        shop = shops.get(p['shop'], {})
        prefix = 'est' if shop.get('adapter') == 'estama' else 'ms'
        sid = shop.get('estama') if prefix == 'est' else shop.get('ms')
        ext = 'jpg'
        m = re.search(r'\.(jpe?g|png|webp)(?:\?|$)', src)
        if m:
            ext = 'jpg' if m.group(1).startswith('jpe') else m.group(1)
        fn = f"{prefix}-{sid}-{p.get('pid') or hashlib.md5(p['name'].encode()).hexdigest()[:6]}.{ext}"
        targets.append((p, os.path.join(PHOTO_DIR, fn), src))

    def dl(item):
        p, dest, src = item
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        tmp = dest + '.part'
        try:
            r = subprocess.run(['curl', '-sSL', '--compressed', '-m', '30', '-A', UA, '-o', tmp, src],
                               capture_output=True, timeout=40)
            if r.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 2000:
                os.replace(tmp, dest)
                try:
                    Image.open(dest).verify()
                    p['image'] = os.path.relpath(dest, BASE)
                    return True
                except Exception:
                    os.remove(dest)
        except Exception:
            pass
        if os.path.exists(tmp):
            os.remove(tmp)
        p['photo_failed'] = True
        return False

    ok = 0
    if targets:
        with ThreadPoolExecutor(max_workers=8) as ex:
            for r in ex.map(dl, targets):
                ok += 1 if r else 0
    return len(targets), ok


def main():
    args = sys.argv[1:]
    live_path = 'work/secret_live.json'
    out_dir = 'tmp/secret_cards_auto'
    for i, a in enumerate(args):
        if a == '--live':
            live_path = args[i + 1]
        if a == '--out':
            out_dir = args[i + 1]
    live = json.load(open(os.path.join(BASE, live_path), encoding='utf-8'))
    cache = json.load(open(os.path.join(BASE, 'work/secret_cache.json'), encoding='utf-8'))
    now = datetime.datetime.now()
    t0 = datetime.datetime.now()

    b = build(live, cache, now)
    total, got = download_selected(b, live)
    # 사진 다운로드 실패 인원은 다시 선발에서 제외하고 1회 재계산
    b = build(live, cache, now)
    cards = render(b, live, cache, now)
    os.makedirs(os.path.join(BASE, out_dir), exist_ok=True)
    for name, txt in cards.items():
        with open(os.path.join(BASE, out_dir, f'{name}.txt'), 'w', encoding='utf-8') as f:
            f.write(txt)
    # summary
    lines = [f"# 비밀놀이 자동 카드 — {now.strftime('%Y-%m-%d %H:%M')} JST", '',
             f"- 수집: {live['meta'].get('fetched_at')} ({live['meta'].get('elapsed_sec')}s, {live['meta'].get('sources')} sources)",
             f"- 카드 생성: {(datetime.datetime.now() - t0).total_seconds():.1f}s",
             f"- 사진: 신규 다운로드 {got}/{total}", '',
             '| 파일 | 내용 | 인원 |', '|---|---|---|']
    counts = {'card_1': len(b['card1']), 'card_2': len(b['reps2']),
              'card_3': len(b['card3']), 'card_4': len(b['card4']), 'card_5': len(b['card5']),
              'card_6': len(b['card6']), 'card_7': len(b['card7']), 'card_8': len(b['card8']),
              'card_9': len(b['shops_sorted'])}
    for k, v in counts.items():
        lines.append(f"| {k} | {cards[k].splitlines()[1][3:]} | {v} |")
    statuses = {}
    for v in b['card8']:
        statuses[v['p']['status']] = statuses.get(v['p']['status'], 0) + 1
    lines += ['', f"- 상태 분포(card_8): " + ' · '.join(f"{k} {v}" for k, v in sorted(statuses.items(), key=lambda x: STATUS_RANK.get(x[0], 9)))]
    fr_path = os.path.join(BASE, 'work/secret_fetch_report.json')
    if os.path.exists(fr_path):
        fr = json.load(open(fr_path, encoding='utf-8'))
        lines += ['', '## 수집 결과', '',
                  f"- 소스 {fr['ok']}/{fr['ok'] + len(fr['failed'])} 성공 · 소요 {fr['meta'].get('elapsed_sec')}초",
                  f"- 브라우저 fallback 필요: {len(fr['browser_fallback'])}곳"]
        for f_ in fr['failed']:
            lines.append(f"  - FAIL {f_['kind']} {f_.get('shop') or f_.get('area')} ({f_.get('error')})")
    with open(os.path.join(BASE, out_dir, 'summary.md'), 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')

    # report vs reference
    ref_dir = os.path.join(BASE, 'tmp/secret_cards_20261127_06')
    report = dict(built_at=now.isoformat(timespec='seconds'), counts=counts,
                  photos=dict(downloaded=got, attempted=total), output=out_dir)
    if os.path.isdir(ref_dir):
        report['diff'] = compare(cards, ref_dir)
    json.dump(report, open(os.path.join(BASE, 'work/secret_build_report.json'), 'w', encoding='utf-8'),
              ensure_ascii=False, indent=1)
    print(f"[build] {counts}")
    print(f"[build] photos {got}/{total} downloaded")
    if 'diff' in report:
        lines2 = ['', '## 기준 카드(07:30)와의 차이', '', '| 카드 | 기준 | 자동 | 일치 |', '|---|---|---|---|']
        for k, v in report['diff'].items():
            lines2.append(f"| {k} | {v['ref']} | {v['auto']} | {v['common']} |")
        with open(os.path.join(BASE, out_dir, 'summary.md'), 'a', encoding='utf-8') as f:
            f.write('\n'.join(lines2) + '\n')
        for k, v in report['diff'].items():
            print(f"  {k}: ref={v['ref']} auto={v['auto']} common={v['common']}")
            if v.get('missing'):
                print(f"    missing: {v['missing']}")
            if v.get('extra'):
                print(f"    extra: {v['extra']}")


def parse_cards(dirpath):
    out = {}
    for n in range(1, 10):
        p = os.path.join(BASE, dirpath, f'card_{n}.txt')
        if not os.path.exists(p):
            continue
        raw = open(p, encoding='utf-8').read()
        sets = []
        for b in re.findall(r'```card\n(.*?)\n```', raw.replace('````card', '', 1).rsplit('````', 1)[0], re.S):
            head = b.split('\n')[0]
            m = re.match(r'^## (\S+) (.*?) · (\d+)세', head)
            if m:
                shop = re.search(r'- \*\*가게\*\* (.*?)(?: \(|$)', b)
                sets.append((norm_name(m.group(2)), (shop.group(1).strip() if shop else '')))
            elif head.startswith('## 🏪') or head.startswith('## 🆓'):
                sets.append(('SHOP:' + head[5:].strip(), ''))
        out[f'card_{n}'] = sets
    return out


def compare(cards, ref_dir):
    ref = parse_cards(os.path.relpath(ref_dir, BASE)) if os.path.isabs(ref_dir) else parse_cards(os.path.relpath(os.path.join(BASE, ref_dir), BASE))
    auto = {}
    for k, txt in cards.items():
        raw = txt
        sets = []
        for b in re.findall(r'```card\n(.*?)\n```', raw.replace('````card', '', 1).rsplit('````', 1)[0], re.S):
            head = b.split('\n')[0]
            m = re.match(r'^## (\S+) (.*?) · (\d+)세', head)
            if m:
                sets.append(norm_name(m.group(2)))
            elif head.startswith('## 🏪') or head.startswith('## 🆓'):
                sets.append('SHOP:' + head[5:].strip())
        auto[k] = sets
    diff = {}
    for k in ref:
        r = {x if isinstance(x, str) else x[0] for x in ref[k]}
        a = set(auto.get(k, []))
        diff[k] = dict(ref=len(ref[k]), auto=len(auto.get(k, [])), common=len(r & a),
                       missing=sorted(x for x in r if x not in a)[:15],
                       extra=sorted(x for x in a if x not in r)[:15])
    return diff


if __name__ == '__main__':
    main()

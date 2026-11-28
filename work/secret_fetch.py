#!/usr/bin/env python3
"""비밀놀이 초고속 병렬 수집기.

- 2026-11-28 월간 기준: 에스타마 월간 TOP5(梅田·日本橋·難波·心斎橋 出張 + 4지역 통합 出張/접객) 가게 31곳
  (가게 목록은 work/secret_cache.json 이 확정 — 랭킹 수집은 폐지, 캐시의 월간 리스트 사용)
- 등록 11개 가게 + 월간 TOP5 신규 가게(10곳)의 공식/포털 재적·출근·요금 페이지
- 인물 이미지가 로컬에 없을 때만 downloads/secret-photos/ 로 다운로드
- work/secret_live.json (수집 결과) + work/secret_fetch_report.json (성공/실패·소요·fallback)

사용: python3 work/secret_fetch.py [--quick] [--max-age MIN] [--no-images] [--workers 18] [--out work/secret_live.json]
"""
import os, re, sys, json, time, ssl, subprocess, datetime, hashlib, threading, tempfile
import urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed

_HOST_SEM = {}
_ESTAMA_BLOCK = {'fails': 0}   # 연속 혜잡 페이지 → 이번 실행에서는 에스타마 주간 뷰 중단

UA = ('Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 '
      '(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1')
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JINA = 'https://r.jina.ai/'
# 직접 접속이 막히는 호스트 → 리더 프록시 우선
BLOCKED_HOSTS = {'www.genkaya.net', 'genkaya.net', 'hihouden.net', 'sumeragi-spa.com',
                 'sukesukekan.com'}
CACHE_PATH = os.path.join(BASE, 'work/secret_cache.json')
WEEK_CACHE_PATH = os.path.join(BASE, 'work/secret_week_cache.json')
WEEK_DAYS = int(os.environ.get('SECRET_WEEK_DAYS', '7'))   # 오늘 포함 7일(오늘~+6)
WEEK_TTL = int(os.environ.get('SECRET_WEEK_TTL', '1500'))  # 내일 이후 스케줄 캐시(초)
WDAY = ['월', '화', '수', '목', '금', '토', '일']
PHOTO_DIR = os.path.join(BASE, 'downloads/secret-photos')
LOGO_DIR = os.path.join(BASE, 'downloads/secret-logos')

CUP_ORDER = {'A': 1, 'B': 2, 'C': 3, 'D': 4, 'E': 5, 'F': 6, 'G': 7, 'H': 8, 'I': 9, 'J': 10, 'K': 11}
ESTAMA_AREAS = {'2704': '難波', '2703': '日本橋', '2702': '心斎橋'}
PERIODS = {'daily': '', 'weekly': 'weekly/', 'monthly': 'monthly/'}


# ---------------------------------------------------------------- http
def _curl(url, timeout=25):
    t0 = time.time()
    try:
        r = subprocess.run(['curl', '-sSL', '--compressed', '-m', str(timeout), '-A', UA, url],
                           capture_output=True, timeout=timeout + 5)
        txt = r.stdout.decode('utf-8', errors='replace')
        return txt, time.time() - t0, r.returncode
    except Exception:
        return '', time.time() - t0, -1


def http_get(url, timeout=20):
    """직접 → curl → r.jina.ai 순 폴백. dict(method, status, text, bytes, seconds, error)"""
    host = re.sub(r'^https?://', '', url).split('/')[0]
    out = dict(url=url, method=None, status=None, text='', bytes=0, seconds=0.0, error=None)
    t0 = time.time()
    if host in BLOCKED_HOSTS:
        order = ['jina']
    else:
        order = ['urllib', 'curl', 'jina']
    # 호스트별 동시 접속 제한 (에스타마는 병렬 과다 시 900 혼잡 페이지)
    _HOST_SEM.setdefault(host, threading.Semaphore(3 if host == 'estama.jp' else 8))
    for attempt in range(2):
        if host == 'estama.jp':
            time.sleep(0.3 + (hash(threading.get_ident() + attempt) % 5) * 0.12)
        with _HOST_SEM[host]:
            for m in order:
                if m == 'urllib':
                    try:
                        req = urllib.request.Request(url, headers={
                            'User-Agent': UA, 'Accept-Language': 'ja-JP,ja;q=0.9,ko;q=0.8',
                            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'})
                        with urllib.request.urlopen(req, timeout=timeout) as r:
                            data = r.read()
                        txt = data.decode('utf-8', errors='replace')
                        if len(txt) > 3000:
                            out.update(method='urllib', status=200, text=txt, bytes=len(data),
                                       seconds=time.time() - t0)
                            return out
                        out['error'] = 'short body'
                    except Exception as e:
                        out['error'] = f'{type(e).__name__}: {e}'
                elif m == 'curl':
                    txt, sec, rc = _curl(url, timeout)
                    if rc == 0 and len(txt) > 3000:
                        out.update(method='curl', status=200, text=txt, bytes=len(txt.encode('utf-8')),
                                   seconds=time.time() - t0)
                        return out
                    out['error'] = (out.get('error') or '') + f' | curl rc={rc}' + (' Could not resolve' if rc == 6 else '')
                elif m == 'jina':
                    txt, sec, rc = _curl(JINA + url, max(timeout, 45))
                    if rc == 0 and len(txt) > 3000 and 'Markdown Content' in txt:
                        out.update(method='jina', status=200, text=txt, bytes=len(txt.encode('utf-8')),
                                   seconds=time.time() - t0)
                        return out
                    out['error'] = (out.get('error') or '') + f' | jina rc={rc}'
                if m != 'jina' and ('name resolution' in (out.get('error') or '') or 'Could not resolve' in (out.get('error') or '')):
                    continue  # DNS가 죽어도 jina 프록시로는 읽히는 호스트가 있으므로 jina는 계속 시도
        if host == 'estama.jp' and out.get('error'):
            time.sleep(3.0)
    out['seconds'] = time.time() - t0
    out['status'] = 0
    return out


# ---------------------------------------------------------------- utils
def norm_name(s):
    if not s:
        return ''
    s = s.replace('\u3000', ' ').strip()
    s = re.sub(r'\s*\d+分\s*\d+円.*$', '', s)
    s = re.sub(r'★\s*\d+', '', s)
    s = re.sub(r'[（(]\s*\d+\s*[)）]', '', s)
    s = re.sub(r'【.*?】', '', s)
    s = re.sub(r'^日本橋\s+|^難波\s+|^梅田\s+|^心斎橋\s+', '', s)
    s = s.replace('♡', '').replace(' ', '').replace('\u3000', '')
    return s.strip()


def to_min(hhmm):
    m = re.match(r'^(\d{1,2}):(\d{2})$', hhmm or '')
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    return h * 60 + mi


def close_min(open_s, close_s):
    o, c = to_min(open_s), to_min(close_s)
    if o is None or c is None:
        return o, c
    while c <= o:
        c += 1440
    return o, c


# ---------------------------------------------------------------- estama
EXCLUDED_ESTAMA_SHOP_IDS = {'25295', '50297'}  # バンクスパ(남성 제모 살롱), イコールラブ♡(사기 회사, 세나 지시 영구 제외)


def parse_estama_shop_ranking(text):
    i = text.find('main_ranking_list')
    if i < 0:
        return []
    seg = text[i:]
    out = []
    for m in re.finditer(r'ranking_list__badge-(\d+).*?<a href="https://estama\.jp/shop/(\d+)/">\s*(.*?)\s*</a>', seg, re.S):
        rank, sid, name = int(m.group(1)), m.group(2), re.sub(r'<[^>]+>', '', m.group(3)).strip()
        if sid in EXCLUDED_ESTAMA_SHOP_IDS:
            continue
        out.append(dict(rank=rank, shop_id=sid, name=name))
        if len(out) >= 18:
            break
    return out


def parse_estama_therapist_ranking(text, area):
    out = []
    blocks = re.split(r'therapist_rank_no(\d+)', text)
    for i in range(1, len(blocks) - 1, 2):
        rank, b = int(blocks[i]), blocks[i + 1]
        links = re.search(r'<a href="https://estama\.jp/shop/(\d+)/cast/(\d+)/">', b)
        if not links:
            continue
        sid, pid = links.groups()
        nm = re.search(r'<span class="cast_name">\s*<a[^>]*>(.*?)</a>', b, re.S)
        st = re.search(r'right_side_cast_st">(.*?)</div>', b, re.S)
        shopname = re.search(r'<span class="shop_name">\s*(.*?)\s*</span>', b, re.S)
        st = re.sub(r'<[^>]+>', ' ', st.group(1)) if st else ''
        st = re.sub(r'\s+', ' ', st).strip()
        cup = re.search(r'\(([A-K])\)', st)
        ht = re.search(r'T(\d{3})', st)
        age = re.search(r'\((\d{2})\)\s*$', st)
        out.append(dict(rank=rank, shop_id=sid, pid=pid, name=re.sub(r'<[^>]+>', '', nm.group(1)).strip() if nm else '',
                        cup=cup.group(1) if cup else None,
                        height=int(ht.group(1)) if ht else None,
                        age=int(age.group(1)) if age else None,
                        area=area, shop_name=re.sub(r'<[^>]+>', '', shopname.group(1)).strip() if shopname else '',
                        url=f'https://estama.jp/shop/{sid}/cast/{pid}/'))
    return out


def parse_estama_cast(text, shop_key, sid):
    """에스타마 점포 セラピスト一覧 — 재적·나이·컵·키·신인·이미지 URL."""
    out = {}
    _idx = 0
    # 'すべて' 탭(전체 재적)만 사용 — 앞쪽 本日出勤 탭의 순서와 섞지 않는다
    j = text.find('item_therapist_all')
    if j > 0:
        text = text[j:]
    for m in re.finditer(
            r'<a href="https://estama\.jp/shop/%s/cast/(\d+)/#CastProfile"(?: class="([^"]*)")?\s*>.*?'
            r'<img class="therapist__img" src="([^"]+)"[^>]*alt="([^"]*)".*?'
            r'<h4 class="therapist_name tc">\s*(.*?)\s*</h4>.*?'
            r'<p class="details tc">\s*<span>(.*?)</span>' % sid, text, re.S):
        pid, cls, img, alt, h4, det = m.groups()
        cls = cls or ''
        if pid in out:
            continue
        h4 = re.sub(r'<[^>]+>', '', h4)
        nm = re.sub(r'[（(]\s*\d+\s*[)）]', '', h4).strip()
        nm = re.sub(r'[☆★]?\s*体験入店$', '', nm).strip()
        age = re.search(r'[（(](\d+)[)）]', h4)
        cup = re.search(r'B\.\d+\(([A-K])\)', det)
        ht = re.search(r'T\.(\d{2,3})', det)
        rec = dict(pid=pid, order=_idx, name=nm, age=int(age.group(1)) if age else None,
                   cup=cup.group(1) if cup else None,
                   height=int(ht.group(1)) if ht else None,
                   new=('batch_newface' in cls),
                   img=img, url=f'https://estama.jp/shop/{sid}/cast/{pid}/')
        _idx += 1
        out[pid] = rec
    return out


# ---------------------------------------------------------------- estama 월간 TOP5 (주 1회 자동 교체)
# 4지역(梅田 2701·日本橋 2703·難波 2704·心斎橋 2702) 월간 出張 랭킹에서 PR 배너·PR 피드·おすすめ(badge-recommend)를
# 뺀 실제 1~5위만 골라 '월간 슬롯'으로 관리한다. 수동(fixed) 등록 가게는 건드리지 않는다.
MONTHLY_AREAS = (('2701', '梅田'), ('2703', '日本橋'), ('2704', '難波'), ('2702', '心斎橋'))
MONTHLY_RANK_URL = 'https://estama.jp/kansai/ranking/shop/{area}/delivery/monthly/'
MONTHLY_TOP_N = 5                # 주 1회 갱신 판단은 scripts/run_secret_sync.py 가 한다
MONTHLY_STATE_PATH = os.path.join(BASE, 'work/secret_monthly.json')
JST_TZ = datetime.timezone(datetime.timedelta(hours=9))


def parse_estama_monthly_top(text, limit=MONTHLY_TOP_N, scan=10):
    """月間 出張 랭킹 — 실제 순위 배지만. PR 배너(li.list_item_A_wrap 배너)·PR 피드(is_pr_icon)·
    おすすめ 배지(badge-recommend)는 제외. 영구 제외 업체를 건너뛴 뒤 상위 limit 곳을 채운다(예: 心斎橋 1·5위 제외 → 6위까지)."""
    i = text.find('<ul class="main_ranking_list">')
    if i < 0:
        return []
    rows = []
    for part in re.split(r'<li(?=[\s>])', text[i:]):
        if not part.strip() or 'is_pr_icon' in part or 'badge-recommend' in part:
            continue
        b = re.search(r'ranking_list__badge[^>]*>\s*<i>(\d+)</i>', part)
        if not b:
            continue
        rank = int(b.group(1))
        if rank > scan:
            continue
        nm = re.search(r'main_details_shop_name">\s*<a href="https://estama\.jp/shop/(\d+)/">\s*(.*?)\s*</a>',
                       part, re.S)
        if not nm:
            continue
        sid = nm.group(1)
        if sid in EXCLUDED_ESTAMA_SHOP_IDS:
            continue
        name = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', '', nm.group(2))).strip()
        if not name:
            continue
        rows.append(dict(rank=rank, shop_id=sid, name=name))
    rows.sort(key=lambda r: r['rank'])
    out, seen = [], set()
    for r in rows:
        if r['shop_id'] in seen:
            continue
        seen.add(r['shop_id'])
        out.append(r)
        if len(out) >= limit:
            break
    return out


def parse_estama_shop_brief(text):
    """에스타마 샵 페이지(/shop/{sid}/) — 상호·지역·전화·공식 홈페이지·평균 예산·대표 이미지."""
    out = {}
    h = re.search(r'<div class="shop-head">\s*<h2>\s*(.*?)\s*</h2>', text, re.S) \
        or re.search(r'<h1[^>]*>\s*(.*?)\s*</h1>', text, re.S)
    if h:
        out['name'] = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', '', h.group(1))).strip()
    am = re.findall(r'https://estama\.jp/kansai/shoplist/(\d+)/', text)
    if am:
        known = [x for x in am if x in dict(MONTHLY_AREAS)]
        out['area_id'] = (known or am)[0]
        out['area'] = dict(MONTHLY_AREAS).get(out['area_id']) or ''
    tel = re.search(r'tel:([0-9\-]{8,})', text)
    if tel:
        out['phone'] = tel.group(1)
    hp = re.search(r'公式ホームページ\s*</dt>\s*<dd>\s*<a[^>]*href="\s*(https?://[^"]+?)\s*"', text, re.S)
    if hp:
        out['official_url'] = hp.group(1)
    bg = re.search(r'_budget">\s*(\d+)\s*分\s*/\s*([\d,]+)\s*円', text)
    if bg:
        out['budget_min'] = int(bg.group(1))
        out['budget_yen'] = int(bg.group(2).replace(',', ''))
    img = re.search(r'<img src="(https://img\.estama\.jp/shop_data/\d+/shop/[^"]+)"', text)
    if img:
        out['logo_url'] = img.group(1)
    return out


def parse_estama_menu_120(text):
    """料金ページ(/menu/)의 120분 코스 금액 — '120 分</dt><dd>¥18,000' 형 우선, 없으면 텍스트 표기."""
    for pat in (r'120\s*分\s*</dt>\s*<dd>\s*&\s*yen;\s*([\d,]+)',
                r'120\s*分[^<]{0,40}?&\s*yen;\s*([\d,]+)',
                r'120\s*分[^<]{0,40}?¥\s*([\d,]+)'):
        m = re.search(pat, text or '')
        if m:
            try:
                return int(m.group(1).replace(',', ''))
            except Exception:
                pass
    return None


def detect_official_adapter(url):
    """공식 사이트 첫 페이지로 우리 공통 CMS 패턴 판별 — gal(/list/profile?uid=)·gcms(/s/profile?lid=)·brand(/therapist/)."""
    if not url:
        return None
    r = http_get(url)
    if r['status'] != 200 or len(r['text']) < 500:
        return None
    t = r['text']
    host = re.sub(r'^https?://', '', url).split('/')[0]
    base = 'https://' + host
    if re.search(r'/list/profile\?uid=\d+', t):
        return dict(adapter='gal', host=base, cast_url=base + '/list/', schedule_url=base + '/schedule/')
    if re.search(r'/s/profile\?lid=\d+', t):
        return dict(adapter='gcms', host=base, cast_url=base + '/s/girl', schedule_url=base + '/s/schedule/')
    if '/therapist/' in t or '__worktime' in t:
        return dict(adapter='brand', host=base, cast_url=base + '/therapist/', schedule_url=base + '/schedule/')
    return None


def _host_of(url):
    return re.sub(r'^https?://', '', (url or '')).split('/')[0].lower()


def _unique_shop_key(name, shops, sid):
    base = re.sub(r'\s+', ' ', name or '').strip() or f'estama {sid}'
    keys = {s.get('key') for s in shops}
    return base if base not in keys else f'{base} ({sid})'


def _match_by_name(shops, name, area):
    """에스타마 id가 없는 가게(예: 공식/포털만 등록된 수동 가게)를 랭킹 상호로 찾는다 — 같은 지역만."""
    n = norm_name(name)
    best, best_len = None, 0
    for s in shops:
        if area and s.get('area') and s.get('area') != area:
            continue
        for cand in (s.get('key') or '', s.get('name') or ''):
            c = norm_name(cand)
            if len(c) < 4 or c not in n:
                continue
            if len(c) > best_len:
                best, best_len = s, len(c)
    return best


def build_new_monthly_entry(sid, ranking_name, area, cache, notes, log=print):
    """TOP5에 새로 들어온 가게 — 에스타마 샵 페이지에서 공식 URL·전화·요금을 뽑고 CMS 패턴이면 공식 어댑터로 연결."""
    r = http_get(f'https://estama.jp/shop/{sid}/', timeout=30)
    brief = parse_estama_shop_brief(r['text']) if r['status'] == 200 else {}
    name = brief.get('name') or ranking_name
    area = brief.get('area') or area
    official = brief.get('official_url') or ''
    price120 = None
    m = http_get(f'https://estama.jp/shop/{sid}/menu/', timeout=30)
    if m['status'] == 200:
        price120 = parse_estama_menu_120(m['text'])
    base_price = price120 or brief.get('budget_yen')
    shops = cache.get('shops') or []
    key = _unique_shop_key(name, shops, sid)
    entry = dict(
        key=key, name=name, area=area, estama=sid,
        official_url=official or f'https://estama.jp/shop/{sid}/',
        phone=brief.get('phone') or '',
        logo_url=brief.get('logo_url') or '',
        price_free=base_price, free_course='120분',
        transport_note='미확인(자동 편입)',
        adapter='estama', fixed=False, monthly=True, active=True,
        auto_enrolled=True,
        enrolled_at=datetime.datetime.now(JST_TZ).strftime('%Y-%m-%dT%H:%M'),
        source='estama monthly TOP5',
        price=dict(base=base_price or 0, minutes=120, nomi=0, transport=0, hotel=0,
                   hotel_included=False,
                   detail=(f"자동 편입: 에스타마 표기 120분 {base_price:,}엔 (지명·교통 미확인)"
                           if base_price else '자동 편입: 요금 미확인')),
    )
    # 공식 사이트가 이미 다른 가게(같은 브랜드 타 지점 등)에 등록돼 있으면 재적 목록이 겹치므로 estama 어댑터 유지
    hosts = {_host_of(s.get('official_url')): s for s in shops
             if _host_of(s.get('official_url')) not in ('', 'estama.jp')}
    cms = None
    if official and _host_of(official) in hosts:
        dup = hosts[_host_of(official)]
        notes.append(f"{name}({sid}): 공식 {_host_of(official)} 는 '{dup.get('key')}'와 같은 사이트 → estama 어댑터로 편입")
    elif official:
        cms = detect_official_adapter(official)
    if cms:
        entry.update(adapter=cms['adapter'], cast_url=cms['cast_url'], schedule_url=cms['schedule_url'],
                     system_url=cms['host'] + '/system/',
                     official_site=dict(kind=cms['adapter'] + '_cards', host=cms['host'],
                                        cast=cms['cast_url'], list=cms['cast_url']))
        notes.append(f"{name}({sid}): 공식 {cms['host']} → {cms['adapter']} 어댑터 자동 연결")
    log(f"[monthly] +신규 {name}({sid}, {area}) adapter={entry['adapter']} 정가={base_price}")
    return entry


def load_monthly_state():
    try:
        with open(MONTHLY_STATE_PATH, encoding='utf-8') as f:
            d = json.load(f)
        if isinstance(d, dict) and isinstance(d.get('shops'), list):
            return d
    except Exception:
        pass
    return None


def save_monthly_state(state):
    try:
        os.makedirs(os.path.dirname(MONTHLY_STATE_PATH), exist_ok=True)
        with open(MONTHLY_STATE_PATH, 'w', encoding='utf-8') as f:
            json.dump(state, f, ensure_ascii=False, indent=1)
        return True
    except Exception as e:
        print(f'[monthly] state write failed: {e}')
        return False


def refresh_monthly_top5(prev=None, sleep_s=1.2, log=print):
    """4지역 월간 출장 TOP5를 새로 긁어 상태를 만든다. 일부 지역 실패 시 그 지역은 직전 목록 유지,
    전 지역 실패 시 직전 상태를 그대로 돌려준다(가게가 통째로 내려가지 않게)."""
    prev = prev if isinstance(prev, dict) else {}
    prev_areas = prev.get('areas') or {}
    prev_by_sid = {str(x.get('shop_id')): x for x in (prev.get('shops') or [])}
    try:
        cache = load_cache()
    except Exception:
        cache = {'shops': [], 'people': []}
    notes, areas, flat = [], {}, []
    ok = 0
    for area_id, area_name in MONTHLY_AREAS:
        r = http_get(MONTHLY_RANK_URL.format(area=area_id), timeout=30)
        rows = parse_estama_monthly_top(r['text']) if r['status'] == 200 else []
        if rows:
            ok += 1
        else:
            old = (prev_areas.get(area_id) or {}).get('shops') or []
            rows = [dict(rank=x.get('rank'), shop_id=str(x.get('shop_id')), name=x.get('name')) for x in old]
            notes.append(f"{area_name}: 랭킹 수집 실패({r.get('error') or r['status']}) → "
                         + ('직전 목록 유지' if rows else '직전 목록 없음'))
        areas[area_id] = dict(area=area_name, shops=rows, url=MONTHLY_RANK_URL.format(area=area_id))
        log(f"[monthly] {area_name}({area_id}) {len(rows)}곳: " +
            ', '.join(f"{x['rank']}.{x['name']}({x['shop_id']})" for x in rows))
        time.sleep(sleep_s)
    if ok == 0 and (prev.get('shops')):
        log('[monthly] 4지역 모두 실패 — 직전 상태 유지')
        return prev
    by_sid = {}
    for s in cache.get('shops') or []:
        for f in ('estama', 'week_estama', 'estama_cast'):
            v = str(s.get(f) or '')
            if v:
                by_sid.setdefault(v, s)
    by_key = {s.get('key'): s for s in cache.get('shops') or []}
    for area_id, area_name in MONTHLY_AREAS:
        for row in areas[area_id]['shops']:
            sid = str(row['shop_id'])
            item = dict(shop_id=sid, name=row['name'], area=area_name, rank=row['rank'], area_id=area_id)
            shop = by_sid.get(sid) or _match_by_name(cache.get('shops') or [], row['name'], area_name)
            if shop:
                item.update(key=shop['key'], matched='curated')
            else:
                old = prev_by_sid.get(sid) or {}
                entry = old.get('entry')
                if not isinstance(entry, dict) or not entry.get('key'):
                    entry = build_new_monthly_entry(sid, row['name'], area_name, cache, notes, log)
                    if entry and entry.get('key') in by_key:
                        entry.pop('key')
                        entry = None
                    if entry:
                        cache.setdefault('shops', []).append(entry)
                        by_key[entry['key']] = entry
                        by_sid[sid] = entry
                if entry:
                    item.update(key=entry['key'], matched='new', entry=entry)
                else:
                    item.update(matched='unresolved')
                    notes.append(f"{area_name} {row['rank']}위 {row['name']}({sid}): 자동 편입 실패 — 이번 주 제외")
            flat.append(item)
    state = dict(
        v=1,
        updated_at=datetime.datetime.now(JST_TZ).strftime('%Y-%m-%dT%H:%M:%S+09:00'),
        basis='estama monthly delivery TOP5 — 2701/2703/2704/2702, PR(배너·피드·おすすめ) 제외',
        areas=areas, shops=flat, notes=notes,
    )
    save_monthly_state(state)
    log(f"[monthly] updated {state['updated_at']} — {len(flat)}곳 "
        f"(curated {sum(1 for x in flat if x.get('matched') == 'curated')}, "
        f"new {sum(1 for x in flat if x.get('matched') == 'new')})")
    return state


def merge_monthly_shops(cache, state):
    """캐시의 가게 목록에 월간 슬롯을 적용 — fixed(수동)는 항상 유지, 월간 슬롯만 TOP5로 교체."""
    if not isinstance(state, dict) or not state.get('shops'):
        return cache
    shops = [dict(s) for s in (cache.get('shops') or [])]
    by_key = {s.get('key'): s for s in shops}
    by_sid = {}
    for s in shops:
        for f in ('estama', 'week_estama', 'estama_cast'):
            v = str(s.get(f) or '')
            if v:
                by_sid.setdefault(v, s)
    live_keys, live_sids = set(), set()
    for m in state['shops']:
        sid = str(m.get('shop_id') or '')
        s = by_key.get(m.get('key') or '') or by_sid.get(sid) \
            or _match_by_name(shops, m.get('name') or '', m.get('area') or '')
        if s is None:
            entry = m.get('entry')
            if not isinstance(entry, dict) or not entry.get('key'):
                continue
            s = dict(entry)
            s['key'] = _unique_shop_key(s['key'], shops, sid)
            shops.append(s)
            by_key[s['key']] = s
            by_sid[sid] = s
        if not s.get('estama'):
            s['estama'] = sid
        s['active'] = True
        s['monthly'] = True
        s['monthly_rank'] = m.get('rank')
        if not s.get('fixed'):        # 수동 등록 가게의 상호·지역·랭킹 라벨은 그대로 둔다
            if m.get('area'):
                s['area'] = m['area']
            s['rank_label'] = f"에스타마 {m.get('area') or s.get('area')} 월간 {m.get('rank')}위"
        live_keys.add(s['key'])
        live_sids.add(str(s.get('estama') or ''))
    for s in shops:
        if s.get('fixed'):
            s['active'] = True
            continue
        if not (s.get('monthly') or s.get('estama')):
            continue
        if s['key'] in live_keys or str(s.get('estama') or '') in live_sids:
            continue
        if s.get('active'):
            s['active'] = False
            s['dropped_at'] = datetime.datetime.now(JST_TZ).strftime('%Y-%m-%d')
            s['drop_reason'] = 'monthly TOP5 이탈'
    out = dict(cache)
    out['shops'] = shops
    return out


def parse_estama_schedule_today(text):
    """出勤表(오늘) — 이름(나이) + 17:00～29:00 형태의 오늘 출근 시프트."""
    out = {}
    lines = [re.sub(r'<[^>]+>', '', x).strip() for x in re.split(r'<[^>]+>', text)]
    lines = [x for x in lines if x]
    for i, l in enumerate(lines):
        m = re.match(r'^(.+?)[（(](\d+)[)）]$', l)
        if m and i + 3 < len(lines) and re.match(r'^\d{1,2}:\d{2}$', lines[i + 1]) \
                and lines[i + 2] in ('～', '~', '-') and re.match(r'^\d{1,2}:\d{2}$', lines[i + 3]):
            out[norm_name(m.group(1))] = dict(open=lines[i + 1], close=lines[i + 3])
    return out


# ---------------------------------------------------------------- mensesthe
def parse_ms_cast(text, ms_id):
    out = {}
    for m in re.finditer(
            r'<a href="https://ranking-mensesthe\.jp/shop/%s/(\d+)/" class="sokuiku">(.*?)</li>' % ms_id,
            text, re.S):
        pid, chunk = m.group(1), m.group(2)
        img = re.search(r'<img src="([^"]+)"', chunk)
        nm = re.search(r'<p class="name ellipsis">(.*?)</p>', chunk)
        cup = re.search(r'<p class="bust-size">\s*([A-K]|-|)\s*</p>', chunk)
        ageht = re.search(r'<p class="age">\s*(\d+)歳\s*/\s*(\d+)cm', chunk)
        bwh = re.search(r'<p class="bwh">B:(\d+) W:(\d+) H:(\d+)', chunk)
        tags = re.findall(r'<li class="girl-tag[^"]*">(.*?)</li>', chunk)
        state = re.search(r'<div class="girlsState\s+([^"]*)">(.*?)</div>', chunk, re.S)
        st = dict(state='', open='', close='', soldout=False)
        if state:
            cls, sc = state.groups()
            sm = re.search(r'<p class="state">(.*?)</p>', sc)
            st['state'] = re.sub(r'\s+', '', re.sub(r'<[^>]+>', '', sm.group(1))) if sm else ''
            om = re.search(r'<span class="open">(.*?)</span>', sc)
            cm = re.search(r'<span class="close">(.*?)</span>', sc)
            st['open'] = om.group(1).strip() if om else ''
            st['close'] = cm.group(1).strip() if cm else ''
            st['soldout'] = ('完売' in st['state']) or ('受付終了' in st['state']) or ('soldout' in cls)
        name = re.sub(r'<[^>]+>', '', nm.group(1)).strip() if nm else ''
        name = re.sub(r'\s*\d+分\s*\d+円.*$', '', name).strip()
        mm = re.match(r'^【([^【】]{1,16})】', name)
        if mm:
            name = mm.group(1).strip()
        else:
            name = re.sub(r'【.*?】', '', name).strip()
        price60 = re.search(r'(\d+)分(\d+)円', re.sub(r'<[^>]+>', '', nm.group(1)) if nm else '')
        out[pid] = dict(pid=pid, name=name, cup=cup.group(1) if cup and cup.group(1) not in ('', '-') else None,
                        age=int(ageht.group(1)) if ageht else None,
                        height=int(ageht.group(2)) if ageht else None,
                        bust=int(bwh.group(1)) if bwh else None,
                        tags=[re.sub(r'<[^>]+>', '', t).strip() for t in tags],
                        new=any('新人' in re.sub(r'<[^>]+>', '', t) for t in tags),
                        price60=int(price60.group(2)) if price60 else None,
                        img=img.group(1) if img else '',
                        url=f'https://ranking-mensesthe.jp/shop/{ms_id}/{pid}/',
                        today=st)
    return out


def parse_ms_ranking(text, ms_id):
    i = text.find('rankingList')
    if i < 0:
        return []
    seg = text[i:text.find('</ol>', i)]
    out = []
    for m in re.finditer(r'<p class="rank[^"]*">\s*(\d+)\s*</p>.*?'
                         r'href="https://ranking-mensesthe\.jp/shop/%s/(\d+)/".*?'
                         r'alt="([^"]*)"' % ms_id, seg, re.S):
        nm = re.sub(r'\s*\d+分\s*\d+円.*$', '', m.group(3)).strip()
        mm = re.match(r'^【([^【】]{1,16})】', nm)
        if mm:
            nm = mm.group(1).strip()
        out.append(dict(rank=int(m.group(1)), pid=m.group(2), name=nm))
    return out


# ---------------------------------------------------------------- official adapters
def parse_genkaya_girl(text):
    people = {}
    pat = re.compile(r'\[([^\[\]]+?)\s(\d+)分\s?(\d+)円\s*\((\d+)\)\s*T\.(\d+)\s*B\.(\d+)\(([A-K])\)\s*/\s*W\.(\d+)\s*/\s*H\.(\d+)\]'
                     r'\((https://www\.genkaya\.net/s/profile\?lid=(\d+))\)')
    for m in pat.finditer(text):
        name, min_, price, age, ht, bust, cup, w, h, url, lid = m.groups()
        pre = text[max(0, m.start() - 260):m.start()]
        people[norm_name(name)] = dict(name=name.strip(), age=int(age), cup=cup, height=int(ht),
                                       bust=int(bust), price60=int(price), new=(']NEW]' in pre or '\nNEW' in pre),
                                       url=url, pid=lid,
                                       img=f'https://www.genkaya.net/photos/{lid}/raw_{lid}.jpg')
    return people


def parse_genkaya_schedule(text):
    out = {}
    pat = re.compile(r'\[(?:日本橋\s+)?([^\[\]]+?)\s(\d+)分\s?(\d+)円\((\d+)\)\s*T\.(\d+)\s*B\.(\d+)\(([A-K])\)\s*/\s*W\.[\d.]+\s*/\s*H\.[\d.]+\s*'
                     r'(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})(?:\s*【(.*?)】)?\]')
    for m in pat.finditer(text):
        name, _, _, age, ht, bust, cup, op, cl, st = m.groups()
        out[norm_name(name)] = dict(open=op, close=cl, state=st or '', cup=cup, height=int(ht), age=int(age))
    return out


def parse_hihouden_girl(text):
    people = {}
    blocks = re.split(r'\n(?=\[!\[Image)', text)
    for b in blocks:
        nm = re.search(r'\n\s*\n([^\n\[\]()]{1,16})\((\d+)\)\s*\n\s*\n身長', b)
        urlm = re.search(r'\((https://hihouden\.net/profile\?lid=(\d+))\)', b)
        if not nm or not urlm:
            continue
        name, age = nm.groups()
        url, lid = urlm.groups()
        ht = re.search(r'身長(\d{2,3})cm', b)
        people[norm_name(name)] = dict(name=name.strip(), age=int(age),
                                       height=int(ht.group(1)) if ht else None,
                                       cup=None, url=url, pid=lid, new=(']NEW]' in b),
                                       img=f'https://hihouden.net/photos/{lid}/raw_{lid}.jpg')
    return people


def parse_hihouden_schedule(text):
    out = {}
    pat = re.compile(r'\[([^\[\]]+?)\((\d+)\)\s*身長(\d{2,3})cm\s*([^\]]*?)(\d{1,2}:\d{2})\s*[-～]\s*(翌?\d{1,2}:\d{2})')
    for m in pat.finditer(text):
        name, age, ht, _mid, op, cl = m.groups()
        out[norm_name(name)] = dict(open=op, close=cl.replace('翌', ''), state='', height=int(ht), age=int(age))
    return out


GENERIC_SITE_PATTERNS = {
    # adapter: (item regex with named groups, url builder)
}


def parse_lirica(text):
    people = {}
    for m in re.finditer(r'<a href="(/profile\?id=(\d+))">(.*?)</article>', text, re.S):
        href, pid, chunk = m.groups()
        nm = re.search(r'<h3>\s*(.*?)\s*(?:<span class="list_star">.*?</span>)?\s*</h3>', chunk, re.S)
        if not nm:
            continue
        raw = re.sub(r'<[^>]+>', '', nm.group(1)).strip()
        mm = re.match(r'^(.*?)\((\d+)\)$', raw)
        if not mm:
            continue
        name = re.sub(r'^[^\w\u3040-\u9fff]+', '', mm.group(1)).replace('\u3000', ' ').strip()
        people[norm_name(name)] = dict(name=name, age=int(mm.group(2)),
                                      cup=None, height=None, pid=pid,
                                      url=f'https://lirica-osaka.com{href}',
                                      img=f'https://lirica-osaka.com/upload/cast/thumb_{pid}.jpg')
    return people


def parse_belle(text):
    people = {}
    for m in re.finditer(r'<a href="(/profile\?id=(\d+))"(.*?)</a>', text, re.S):
        href, pid, chunk = m.groups()
        nm = re.search(r'([^\s<>(){}\[\]]+?)\((\d+)\)', re.sub(r'<[^>]+>', ' ', chunk))
        if not nm:
            continue
        people[norm_name(nm.group(1))] = dict(name=nm.group(1), age=int(nm.group(2)), cup=None, height=None,
                                              pid=pid, url=f'https://osaka-bellefemme.com{href}')
    return people


def parse_superhappy(text):
    people = {}
    for m in re.finditer(r'<a href="https://super-happy\.net/profile\?lid=(\d+)">(.*?)</figure>', text, re.S):
        lid, b = m.groups()
        nm = re.search(r'<p class="name">\s*(.*?)<span>\((\d+)\)</span>', b, re.S)
        if not nm:
            continue
        name = re.sub(r'<[^>]+>', '', nm.group(1)).strip()
        age = int(nm.group(2))
        sz = re.search(r'<p class="size"><span>(\d{2,3})cm</span>【([A-K])】', b)
        people[norm_name(name)] = dict(name=name, age=age,
                                       cup=sz.group(2) if sz else None,
                                       height=int(sz.group(1)) if sz else None,
                                       new='新人' in b, pid=lid,
                                       url=f'https://super-happy.net/profile?lid={lid}',
                                       img=f'https://super-happy.net/photos/{lid}/main_{lid}.jpg',
                                       today=bool(re.search(r'<span class="todaycat">', b)))
    return people


def parse_superhappy_schedule(text):
    out = {}
    for m in re.finditer(r'<a href="https://super-happy\.net/profile\?lid=(\d+)">(.*?)</figure>', text, re.S):
        lid, b = m.groups()
        nm = re.search(r'<p class="name">\s*(.*?)<span>\((\d+)\)</span>', b, re.S)
        tm = re.search(r'<p class="time">.*?(\d{1,2}:\d{2})\s*[～~]\s*(\d{1,2}:\d{2})', b, re.S)
        if not nm or not tm:
            continue
        out[norm_name(re.sub(r'<[^>]+>', '', nm.group(1)))] = dict(open=tm.group(1), close=tm.group(2), state='', soldout=False)
    return out


def parse_orenoie_schedule(text):
    out = {}
    for m in re.finditer(r'<p class="name">\s*【([^【】]+)】\((\d+)\)</p>(.*?)<span class="time">\s*(\d{1,2}:\d{2})-(\d{1,2}:\d{2})', text, re.S):
        name, age, _b, op, cl = m.groups()
        out[norm_name(name)] = dict(open=op, close=cl, state='', soldout=False)
    return out


def parse_femmefatale(text):
    people = {}
    for m in re.finditer(r'<li class="room[^"]*">(.*?)</li>', text, re.S):
        b = m.group(1)
        nm = re.search(r'<h3>\s*([^<>()]+?)\s*\((\d+)\)\s*</h3>', b)
        if not nm:
            continue
        name = nm.group(1).replace('\u3000', ' ').strip()
        ht = re.search(r'<p>T\.?(\d{2,3})', b)
        tm = re.search(r'<h4>\s*(\d{1,2}:\d{2})\s*[～~-]\s*(\d{1,2}:\d{2})', b)
        people[norm_name(name)] = dict(name=name, age=int(nm.group(2)), cup=None,
                                       height=int(ht.group(1)) if ht else None,
                                       today=dict(open=tm.group(1), close=tm.group(2)) if tm else None)
    return people


def parse_sumeragi(text):
    people = {}
    for m in re.finditer(
            r'<a class="c-therapist-cards__link[^"]*" href="(/therapist/(\d+)/)">(.*?)c-therapist-cards__style">T(\d{2,3})',
            text, re.S):
        href, pid, b, ht = m.groups()
        nm = re.search(r'c-therapist-cards__name">\s*([^<>]+?)\s*<span class="c-therapist-cards__age">\((\d+)\)', b, re.S)
        if not nm:
            continue
        name = nm.group(1).replace('\u3000', ' ').strip()
        wt = re.search(r'c-therapist-cards__worktime[^>]*>\s*(?:<i[^>]*></i>)?\s*(\d{1,2}:\d{2})\s*[～~]\s*(\d{1,2}:\d{2})', b)
        people[norm_name(name)] = dict(name=name, age=int(nm.group(2)), cup=None, height=int(ht), pid=pid,
                                       url=f'https://sumeragispa.com{href}',
                                       today=dict(open=wt.group(1), close=wt.group(2)) if wt else None)
    return people


def parse_kimiiro(text):
    people = {}
    for pm in re.finditer(r'<div class="c-panel-inner">(.*?)(?=<div class="\s*c-panel-inner">|\Z)', text, re.S):
        b = pm.group(1)
        nm = re.search(r'c-panel__name[^"]*">\s*([^<>]+?)\s*\((\d+)\)', b)
        if not nm:
            continue
        name = nm.group(1).strip()
        sz = re.search(r'c-panel__size">(.*?)</p>', b, re.S)
        cm = re.search(r'身長(\d{2,3})cm\s*/\s*([A-K])\s*カップ', sz.group(1) if sz else '')
        lid = re.search(r'profile\?lid=(\d+)', b)
        wt = re.search(r'c-panel__time[^>]*>.*?<span>\s*(\d{1,2}:\d{2})\s*[〜～~]\s*(\d{1,2}:\d{2})', b, re.S)
        rec = dict(name=name, age=int(nm.group(2)),
                   cup=cm.group(2) if cm else None,
                   height=int(cm.group(1)) if cm else None,
                   pid=lid.group(1) if lid else None,
                   url=f'https://kimiiro-dress-spa.com/profile?lid={lid.group(1)}' if lid else None,
                   today=dict(open=wt.group(1), close=wt.group(2)) if wt else None)
        prev = people.get(norm_name(name))
        if prev:
            for k2, v2 in prev.items():
                if not rec.get(k2) and v2:
                    rec[k2] = v2
        people[norm_name(name)] = rec
    return people


def parse_zenith(text):
    people = {}
    for m in re.finditer(r'<a href="(/list/profile\?uid=(\d+))">(.*?)</a>', text, re.S):
        href, uid, b = m.groups()
        nm = re.search(r'<h3>\s*([^<>()]+?)\s*\((\d+)\)\s*</h3>', b)
        if not nm:
            continue
        name = nm.group(1).replace('\u3000', ' ').strip()
        ht = re.search(r'<p>T\.?(\d{2,3})\s*</p>', b)
        tt = re.search(r'<span class="iconToday">\s*本日\s*(\d{1,2}:\d{2})-(\d{1,2}:\d{2})', b)
        people[norm_name(name)] = dict(name=name, age=int(nm.group(2)), cup=None,
                                       height=int(ht.group(1)) if ht else None, pid=uid,
                                       url=f'https://zenithosaka.com{href}',
                                       today=dict(open=tt.group(1), close=tt.group(2)) if tt else None)
    return people


def parse_orenoie(text):
    people = {}
    for m in re.finditer(r'【([^【】]{1,16})】\((\d+)\)', text):
        name, age = m.groups()
        people[norm_name(name)] = dict(name=name, age=int(age), cup=None, height=None)
    return people


def parse_cream(text):
    people = {}
    plain = re.sub(r'<script.*?</script>', '', text, flags=re.S)
    lines = [re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', x)).strip() for x in re.split(r'<[^>]+>', plain)]
    lines = [x for x in lines if x]
    for i, l in enumerate(lines):
        m = re.match(r'^([^\s\d]{1,16})さん$', l)
        if not m or i + 2 >= len(lines):
            continue
        am = re.match(r'^(\d+)歳$', lines[i + 1])
        hm = re.match(r'^T(\d{2,3})cm$', lines[i + 2])
        if not (am and hm):
            continue
        people[norm_name(m.group(1))] = dict(name=m.group(1), age=int(am.group(1)), cup=None, height=int(hm.group(1)))
    return people


def _brand_items(text):
    """브랜드 사이트(에스타마 계열 CMS) — /therapist/·/schedule/ 공통 카드 블록 분해."""
    ms = list(re.finditer(r'<a class="[^"]*" href="/therapist/(\d+)/">', text))
    out = []
    for i, m in enumerate(ms):
        end = ms[i + 1].start() if i + 1 < len(ms) else len(text)
        out.append((m.group(1), text[m.start():min(end, m.start() + 4000)]))
    return out


def _brand_name(chunk):
    nm = re.search(r'(?:<img[^>]*title|<span[^>]*title)="([^"]+)"', chunk)
    if nm:
        return nm.group(1).strip(), None
    m = re.search(r'__name">(.*?)</(?:div|span|p)>', chunk, re.S)
    if not m:
        return None, None
    raw = re.sub(r'<[^>]+>', ' ', m.group(1))
    raw = re.sub(r'\s+', ' ', raw).strip()
    am = re.search(r'\((\d+)\)\s*$', raw)
    age = int(am.group(1)) if am else None
    name = re.sub(r'\(\d+\)\s*$', '', raw).strip()
    return (name or None), age


def parse_brand_therapist(text, host):
    """브랜드 공식 /therapist/ 재적 목록 — 이름·나이·컵·키·신인·이미지·프로필 URL."""
    people = {}
    idx = 0
    for pid, chunk in _brand_items(text):
        name, age_alt = _brand_name(chunk)
        if not name:
            continue
        if age_alt is None:
            am = re.search(r'__age">\s*\((\d+)\)', chunk)
            if am:
                age_alt = int(am.group(1))
            else:
                am2 = re.search(re.escape(name) + r'\s*\((\d+)\)', chunk)
                if am2:
                    age_alt = int(am2.group(1))
        style = re.search(r'class="[^"]*__style">\s*([^<]+)<', chunk)
        st = style.group(1) if style else ''
        cup = re.search(r'[BT]\.?\s?\d{2,3}\s?.*?\(([A-K])\)', st) or re.search(r'\(([A-K])\)', st)
        ht = re.search(r'T\.?\s?(\d{2,3})', st) or re.search(r'T\.?\s?(\d{2,3})', chunk)
        img = re.search(r'<img[^>]*src="(https://img\.estama\.jp[^"]+)"', chunk)
        key = norm_name(name)
        rec = dict(
            name=name, age=age_alt,
            cup=cup.group(1) if cup else None,
            height=int(ht.group(1)) if ht else None,
            new=('新人' in chunk or 'NEW FACE' in chunk or 'new-face-label' in chunk),
            order=idx, img=img.group(1) if img else None,
            url=f'https://{host}/therapist/{pid}/')
        prev = people.get(key)
        if prev:
            for k2, v2 in prev.items():
                if not rec.get(k2) and v2:
                    rec[k2] = v2
        people[key] = rec
        idx += 1
    return people


def parse_brand_schedule(text):
    """브랜드 공식 /schedule/ — 오늘 출근 시간·상태."""
    out = {}
    for _pid, chunk in _brand_items(text):
        name, _ = _brand_name(chunk)
        wt = re.search(r'__worktime[^>]*>\s*(?:<i[^>]*></i>)?\s*(\d{1,2}:\d{2})\s*[～~-]\s*(\d{1,2}:\d{2})',
                       chunk, re.S)
        if not name or not wt:
            continue
        at = re.search(r'__attend[^>]*>(.*?)</span>', chunk, re.S)
        state = re.sub(r'<[^>]+>', '', at.group(1)).strip() if at else ''
        out[norm_name(name)] = dict(open=wt.group(1), close=wt.group(2), state=state,
                                    soldout=('完売' in state or '受付終了' in state))
    return out


def parse_brand_ranking(text):
    """브랜드 공식 /ranking/ — 카드 순서가 곧 순위."""
    out = []
    for pid, chunk in _brand_items(text):
        name, _ = _brand_name(chunk)
        if name:
            out.append(dict(rank=len(out) + 1, pid=pid, name=name))
    return out


def _split_by_anchor(text, pattern, span=6000):
    """앵커(href) 위치마다 잘라 카드 블록들을 돌려준다 — 중첩 <a>(후기 링크) 때문에 정규식 한 번으로는 못 잡는 CMS용."""
    marks = list(re.finditer(pattern, text))
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else m.start() + span
        yield m, text[m.start():min(end, m.start() + span)]


def parse_gal(text, host):
    """gal CMS(/list/·/list/profile?uid=N) — 이름(나이)·T./컵·사진·프로필·신인·오늘 출근.
    대상: 恋する放課後ヒロイン(koihiro.com), SUKI♡だらけ(sukidarake.com)."""
    people = {}
    idx = 0
    for _m, chunk in _split_by_anchor(text, r'<a href="/list/profile\?uid=(\d+)"'):
        pid = _m.group(1)
        if 'dataHeightFixed' not in chunk or 'g_name' in chunk:
            continue
        nm = re.search(r'<h3>\s*(.*?)\s*</h3>', chunk, re.S)
        if not nm:
            continue
        raw = re.sub(r'<[^>]+>', '', nm.group(1)).strip()
        am = re.search(r'\((\d{2})\)\s*$', raw)
        age = int(am.group(1)) if am else None
        name = re.sub(r'\(\d{2}\)\s*$', '', raw).replace('\u3000', ' ').strip()
        if not name:
            continue
        sz = re.search(r'dataHeightFixed">(.*?)</p>', chunk, re.S)
        st = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', '', sz.group(1))).strip() if sz else ''
        ht = re.search(r'T\.?\s?(\d{2,3})', st)
        cup = re.search(r'\(([A-K])\)', st)
        img = re.search(r'<img[^>]*data-original="([^"]+)"', chunk) or re.search(r'<img[^>]*src="(/wcms/[^"]+)"', chunk)
        imgurl = None
        if img:
            u = img.group(1)
            imgurl = u if u.startswith('http') else f'https://{host}{u}'
        tt = re.search(r'iconToday">\s*本日\s*(\d{1,2}:\d{2})\s*[-～]\s*(\d{1,2}:\d{2})', chunk)
        rec = dict(name=name, age=age, cup=cup.group(1) if cup else None,
                   height=int(ht.group(1)) if ht else None,
                   new=bool(re.search(r'iconNew|新入生|新人|NEW FACE', chunk)),
                   img=imgurl, url=f'https://{host}/list/profile?uid={pid}', pid=pid, order=idx)
        if tt:
            rec['today'] = dict(open=tt.group(1), close=tt.group(2))
        key = norm_name(name)
        prev = people.get(key)
        if prev:
            for k2, v2 in prev.items():
                if not rec.get(k2) and v2 is not None:
                    rec[k2] = v2
        people[key] = rec
        idx += 1
    return people


def parse_gal_schedule(text):
    """gal CMS /schedule/ — 카드의 <h4>HH:MM～HH:MM</h4> 가 오늘 출근시간."""
    out = {}
    for _m, chunk in _split_by_anchor(text, r'<a href="/list/profile\?uid=(\d+)"'):
        if 'dataHeightFixed' not in chunk:
            continue
        nm = re.search(r'<h3>\s*(.*?)\s*</h3>', chunk, re.S)
        tm = re.search(r'<h4>\s*(\d{1,2}:\d{2})\s*[～~-]\s*(\d{1,2}:\d{2})\s*</h4>', chunk)
        if not nm or not tm:
            continue
        name = re.sub(r'\(\d{2}\)\s*$', '', re.sub(r'<[^>]+>', '', nm.group(1)).strip()).strip()
        out[norm_name(name)] = dict(open=tm.group(1), close=tm.group(2), state='出勤', soldout=False)
    return out


def parse_gcms(text, host):
    """genkaya 계열 CMS(/s/girl·/s/schedule) — c-panel 카드. 대상: Aroma Love(aroma-love.com)."""
    people = {}
    idx = 0
    for _m, chunk in _split_by_anchor(text, r'href="https://[^"]+/s/profile\?lid=(\d+)"'):
        pid = _m.group(1)
        nm = re.search(r'c-panel__name">\s*(.*?)\s*</p>', chunk, re.S)
        if not nm:
            continue
        raw = re.sub(r'<[^>]+>', '', nm.group(1)).strip()
        am = re.search(r'\((\d{2})\)\s*$', raw)
        age = int(am.group(1)) if am else None
        name = re.sub(r'\(\d{2}\)\s*$', '', raw).replace('\u3000', ' ').strip()
        if not name:
            continue
        sz = re.search(r'c-panel__size">(.*?)</p>', chunk, re.S)
        st = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', '', sz.group(1))).strip() if sz else ''
        ht = re.search(r'身長\s*(\d{2,3})\s*cm', st) or re.search(r'T\.?\s?(\d{2,3})', st)
        cup = re.search(r'\(([A-K])\)', st)
        bust = re.search(r'B\.?\s?(\d{2,3})', st)
        img = re.search(r'<img[^>]*src="(https://[^"]+/photos/[^"]+)"', chunk)
        tm = re.search(r'c-panel__time"><span>\s*(\d{1,2}:\d{2})\s*[-～]\s*(\d{1,2}:\d{2})', chunk)
        rec = dict(name=name, age=age, cup=cup.group(1) if cup else None,
                   height=int(ht.group(1)) if ht else None,
                   bust=int(bust.group(1)) if bust else None,
                   new=bool(re.search(r'beginner[^>]*>\s*NEW', chunk)),
                   img=img.group(1) if img else None,
                   url=f'https://{host}/s/profile?lid={pid}', pid=pid, order=idx)
        if tm:
            rec['today'] = dict(open=tm.group(1), close=tm.group(2))
        key = norm_name(name)
        prev = people.get(key)
        if prev:
            for k2, v2 in prev.items():
                if not rec.get(k2) and v2 is not None:
                    rec[k2] = v2
        people[key] = rec
        idx += 1
    return people


def parse_gcms_schedule(text, host=None):
    """genkaya 계열 CMS /s/schedule — c-panel__time 이 오늘 출근시간(사진·프로필은 parse_gcms 와 동일)."""
    out = {}
    for nm, p in parse_gcms(text, host or 'aroma-love.com').items():
        t = p.get('today') or {}
        if t.get('open') and t.get('close'):
            out[nm] = dict(open=t['open'], close=t['close'], state='出勤', soldout=False)
    return out


SUKE_NAME_PAT = re.compile(r'(?:^|[\[\n])\s*([^\[\]()\n]{1,16}?)\((\d{2})\)')
SUKE_SIZE_PAT = re.compile(
    r'T\.?\s*(\d{2,3})\s*/\s*B\.?\s*(\d{2,3})\s*\(([A-K])\)\s*/\s*W\.[\d.]+\s*/\s*H\.[\d.]+')


def parse_suke(text, host='sukesukekan.com'):
    """jina 리더 텍스트(sukesukekan.com/girl·/schedule) — 이미지 카드 + 이름(나이) + T./B.(컵)/W./H. (+시간)."""
    people = {}
    idx = 0
    for chunk in re.split(r'(?=\[!\[Image \d+:)', text):
        sz = SUKE_SIZE_PAT.search(chunk)
        nm = SUKE_NAME_PAT.search(chunk)
        if not sz or not nm:
            continue
        name = nm.group(1).replace('\u3000', ' ').strip()
        if not name or name.isdigit():
            continue
        img = re.search(r'\[!\[Image \d+: ([^\]]+)\]\(([^)\s]+)\)', chunk)
        um = re.search(r'profile\?lid=(\d+)', chunk)
        tt = re.search(r'(\d{1,2}:\d{2})\s*[-～]\s*(\d{1,2}:\d{2})', chunk)
        imgname = img.group(1).strip() if img else ''
        rec = dict(name=name, age=int(nm.group(2)), cup=sz.group(3), height=int(sz.group(1)),
                   bust=int(sz.group(2)),
                   new=bool(img and 'NEW' in chunk[:chunk.find('\n', img.start()) if '\n' in chunk[img.start():] else len(chunk)]),
                   img=img.group(2) if img else None,
                   url=f'https://{host}/profile?lid={um.group(1)}' if um else f'https://{host}/girl',
                   pid=um.group(1) if um else None, order=idx)
        if tt:
            rec['today'] = dict(open=tt.group(1), close=tt.group(2))
        prev = people.get(norm_name(name))
        if prev:
            for k2, v2 in prev.items():
                if not rec.get(k2) and v2 is not None:
                    rec[k2] = v2
        people[norm_name(name)] = rec
        idx += 1
    return people


def parse_suke_schedule(text, host=None):
    out = {}
    for nm, p in parse_suke(text, host or 'sukesukekan.com').items():
        t = p.get('today') or {}
        if t.get('open') and t.get('close'):
            out[nm] = dict(open=t['open'], close=t['close'], state='出勤', soldout=False)
    return out


def _section(text, start_kw, end_kws):
    i = text.find(start_kw)
    if i < 0:
        return ''
    ends = [j for j in (text.find(k, i + 1) for k in end_kws) if j >= 0]
    return text[i:min(ends)] if ends else text[i:]


def _today_map(people):
    """parse_* 결과(각 인물의 today) → 출근표 맵 {normname: {open, close, state, soldout}}."""
    out = {}
    for nm, p in people.items():
        t = p.get('today') or {}
        if t.get('open') and t.get('close'):
            out[nm] = dict(open=t['open'], close=t['close'], state='出勤', soldout=False)
    return out


def parse_superhappy_happy_schedule(text):
    """super-happy.net /schedule?day=N — 3개 자매점 중 HAPPY 섹션만."""
    seg = _section(text, 'id="today_happy"', ['id="today_otona"', 'id="today_akuma"'])
    return parse_superhappy_schedule(seg or text)


def parse_kimiiro_schedule(text):
    return _today_map(parse_kimiiro(text))


def parse_sumeragi_schedule(text):
    return _today_map(parse_sumeragi(text))


def parse_cream_schedule(text):
    """cream-osaka.com/sp/schedule.html?day=N — <li class="scheduleOne"> 카드의 이름·시간."""
    out = {}
    blocks = re.split(r'<li class="scheduleOne"', text)[1:]
    for b in blocks:
        nm = re.search(r'<span class="name">(.*?)</span>', b, re.S)
        tm = re.search(r'(\d{1,2}:\d{2})\s*[~〜]\s*(\d{1,2}:\d{2})', b)
        if not nm or not tm:
            continue
        name = re.sub(r'<[^>]+>', '', nm.group(1)).strip()
        name = re.sub(r'さん$', '', name).strip()
        if not name:
            continue
        out[norm_name(name)] = dict(open=tm.group(1), close=tm.group(2), state='出勤', soldout=False)
    return out


def parse_next_shift(text, now, days=None):
    """gal CMS /list/ 의 '次回 11/30(月) 12:00-18:00' → {normname: (week_index, 'HH:MM~HH:MM')}."""
    days = days or WEEK_DAYS
    out = {}
    pat = re.compile(r'<h3>\s*([^<>]{1,20}?)\s*\(\s*(\d{2})\s*\)\s*</h3>(.{0,600}?)'
                     r'次回\s*(\d{1,2})/(\d{1,2})[^)]{0,6}\)\s*'
                     r'(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})', re.S)
    for m in pat.finditer(text):
        name, mm, dd, op, cl = m.group(1), int(m.group(4)), int(m.group(5)), m.group(6), m.group(7)
        try:
            year = now.year if mm >= now.month - 6 else now.year + 1
            idx = (datetime.date(year, mm, dd) - now.date()).days
        except ValueError:
            continue
        if 0 <= idx < days:
            out[norm_name(name)] = (idx, f"{op}~{cl}")
    return out


def parse_estama_week(text, now):
    """에스타마 出勤表 'セラピスト別' 주간 뷰 → {normname: [7일치 'HH:MM~HH:MM' 또는 '']}."""
    out = {}
    mstart = re.search(r'cast_sce_tgday">\s*<span[^>]*>\s*(\d{1,2})/(\d{1,2})', text)
    start = now.date()
    if mstart:
        mm, dd = int(mstart.group(1)), int(mstart.group(2))
        try:
            year = now.year if mm >= now.month - 6 else now.year + 1
            start = datetime.date(year, mm, dd)
        except ValueError:
            start = now.date()
    for block in re.split(r'<div class="cast-schedule-row">', text)[1:]:
        nm = re.search(r'class="cast-name">\s*(.*?)\s*(?:\(\d+\))?\s*</span>', block, re.S)
        if not nm:
            continue
        name = re.sub(r'<[^>]+>', '', nm.group(1)).strip()
        name = re.sub(r'\(\d+\)', '', name).strip()
        if not name:
            continue
        wk = [''] * WEEK_DAYS
        rows = re.split(r'<div class="daily-row', block)[1:]
        for ri, row in enumerate(rows[:WEEK_DAYS]):
            tms = re.findall(r'>\s*(\d{1,2}:\d{2})\s*<', row)
            dnum = re.search(r'label-pc">\s*(\d{1,2})', row)
            idx = ri
            if dnum:
                try:
                    idx = (datetime.date(start.year, start.month, int(dnum.group(1))) - now.date()).days
                except ValueError:
                    idx = ri
            if idx < 0 or idx >= WEEK_DAYS or len(tms) < 2:
                continue
            wk[idx] = f"{tms[0].zfill(5) if len(tms[0]) == 4 else tms[0]}~{tms[1]}"
        if any(wk):
            out[norm_name(name)] = wk
    return out


def fetch_estama_week(sid, timeout=25):
    """에스타마 出勤表 POST /post/shop_schedule_ctrl (view=week) — 쿠키+CSRF 필요."""
    if _ESTAMA_BLOCK['fails'] >= 3:
        return dict(url=f'https://estama.jp/shop/{sid}/schedule/ [week]', method=None, status=0,
                    text='', bytes=0, seconds=0.0, error='estama congested (skipped this run)')
    page = f'https://estama.jp/shop/{sid}/schedule/'
    out = dict(url=page + ' [week]', method=None, status=0, text='', bytes=0, seconds=0.0, error=None)
    t0 = time.time()
    jar = None
    try:
        fd, jar = tempfile.mkstemp(prefix='estama_', suffix='.jar')
        os.close(fd)
        sem = _HOST_SEM.setdefault('estama.jp', threading.Semaphore(3))
        time.sleep(0.4 + (hash(sid) % 5) * 0.15)
        with sem:
            r = subprocess.run(['curl', '-sSL', '--compressed', '-m', str(timeout), '-A', UA, '-c', jar, page],
                               capture_output=True, timeout=timeout + 5)
            html = r.stdout.decode('utf-8', errors='replace')
            m = re.search(r"CSRF_TOKEN_VALUE',\s*\"([^\"]+)\"", html) or re.search(r'name="ctk" value="([^"]+)"', html)
            if not m:
                if '混み合' in html or len(html) < 3000:
                    _ESTAMA_BLOCK['fails'] += 1
                out['error'] = 'no ctk (congested)' if '混み合' in html else 'no ctk'
                return out
            time.sleep(0.4)
            r2 = subprocess.run(['curl', '-sS', '-m', str(timeout), '-A', UA, '-b', jar, '-c', jar,
                                 '-H', 'X-Requested-With: XMLHttpRequest', '-H', f'Referer: {page}',
                                 '-H', 'Origin: https://estama.jp',
                                 '--data-urlencode', 'ctk=' + m.group(1),
                                 '--data', 'view=week&shop_id=' + str(sid),
                                 'https://estama.jp/post/shop_schedule_ctrl'],
                                capture_output=True, timeout=timeout + 5)
        body = r2.stdout.decode('utf-8', errors='replace')
        try:
            html2 = (json.loads(body) or {}).get('html') or ''
        except Exception:
            html2 = ''
        if len(html2) > 500:
            out.update(method='curl-post', status=200, text=html2, bytes=len(html2))
        else:
            out['error'] = 'empty week html :: ' + re.sub(r'\s+', ' ', body[:160])
    except Exception as e:
        out['error'] = f'{type(e).__name__}: {e}'
    finally:
        if jar:
            try:
                os.unlink(jar)
            except Exception:
                pass
    out['seconds'] = time.time() - t0
    return out


def parse_rank_rankno(text):
    """girlypink 계열 /ranking/ — 'ランキング 第N位' + <h3><a>이름(나이)</a>."""
    out = []
    for m in re.finditer(r'<p class="rankno">[^<]*?(?:第)?\s*(\d+)\s*位[^<]*</p>\s*<h3>\s*<a[^>]*>\s*([^<(]+?)\s*(?:\((\d+)\))?\s*</a>',
                         text, re.S):
        out.append(dict(rank=int(m.group(1)), name=m.group(2).strip()))
    return out


def parse_rank_pranking(text):
    """에스타마 계열 CMS /ranking/ — <div class="p-ranking-item"> 순서 + p-ranking-item__name."""
    out = []
    for b in re.split(r'<div class="p-ranking-item', text)[1:]:
        nm = re.search(r'p-ranking-item__name">\s*(?:<a[^>]*>)?\s*([^<(]+)', b)
        if nm and nm.group(1).strip():
            out.append(dict(rank=len(out) + 1, name=nm.group(1).strip()))
    return out


def parse_rank_rplace(text):
    """kimiiro /ranking/ — <div class="c-panel" id="r_N"> + c-panel__name."""
    out = []
    for m in re.finditer(r'<div class="c-panel"\s+id="r_(\d+)"(.*?)(?=<div class="c-panel"\s+id="r_|\Z)', text, re.S):
        rk, b = int(m.group(1)), m.group(2)
        nm = re.search(r'c-panel__name[^"]*">\s*([^<(]+)', b)
        if nm:
            out.append(dict(rank=rk, name=nm.group(1).strip()))
    return sorted(out, key=lambda x: x['rank'])


def parse_rank_superhappy(text):
    """super-happy /ranking/ — 본지명 탭 figure.noN + p.name."""
    seg = _section(text, 'id="tab1"', ['id="tab2"', 'id="tab3"']) or text
    out = []
    for m in re.finditer(r'<figure class="no(\d+)".*?<p class="name">\s*([^<]+)', seg, re.S):
        out.append(dict(rank=int(m.group(1)), name=m.group(2).strip()))
    return sorted(out, key=lambda x: x['rank'])


RANK_PARSERS = {
    'brand': parse_brand_ranking,
    'rankno': parse_rank_rankno,
    'pranking': parse_rank_pranking,
    'rplace': parse_rank_rplace,
    'superhappy': parse_rank_superhappy,
}


def parse_rank_page(text, kind):
    fn = RANK_PARSERS.get(kind)
    rows = fn(text) if fn else []
    seen, out = set(), []
    for r in rows:
        k = (r.get('rank'), norm_name(r.get('name')))
        if not r.get('name') or k in seen:
            continue
        seen.add(k)
        out.append(r)
    return out


def parse_brand_day_labels(text):
    """브랜드 공식 /schedule/ 캘린더의 dN 날짜 라벨 — {dN: 'M/D'}."""
    out = {}
    for m in re.finditer(r'href="[^"]*schedule/d(\d)/"[^>]*>\s*(\d{1,2})/(\d{1,2})', text, re.S):
        out[int(m.group(1))] = f"{int(m.group(2))}/{int(m.group(3))}"
    return out


NEW_ADAPTERS = ('gal', 'gcms', 'suke')

OFFICIAL_PARSERS = {
    'brand': parse_brand_therapist,
    'genkaya': parse_genkaya_girl,
    'hihouden': parse_hihouden_girl,
    'lirica': parse_lirica,
    'belle': parse_belle,
    'superhappy': parse_superhappy,
    'femmefatale': parse_femmefatale,
    'sumeragi': parse_sumeragi,
    'kimiiro': parse_kimiiro,
    'zenith': parse_zenith,
    'orenoie': parse_orenoie,
    'cream': parse_cream,
    'gal': parse_gal,
    'gcms': parse_gcms,
    'suke': parse_suke,
}


# ---------------------------------------------------------------- images
def download_image(url, dest, report=None):
    if not url or not url.startswith('http'):
        return None
    if os.path.exists(dest) and os.path.getsize(dest) > 2000:
        return dest
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + '.part'
    try:
        r = subprocess.run(['curl', '-sSL', '--compressed', '-m', '30', '-A', UA, '-o', tmp, url],
                           capture_output=True, timeout=40)
        if r.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 2000:
            os.replace(tmp, dest)
            return dest
        if os.path.exists(tmp):
            os.remove(tmp)
    except Exception as e:
        if report is not None:
            report.append(dict(url=url, error=str(e)))
    return None


def img_ext(url):
    m = re.search(r'\.(jpe?g|png|webp)(?:\?|$)', url or '')
    return 'jpg' if not m else ('jpg' if m.group(1).startswith('jpe') else m.group(1))


# ---------------------------------------------------------------- collect
def load_cache():
    with open(CACHE_PATH, encoding='utf-8') as f:
        return json.load(f)


def build_tasks(cache):
    tasks = []
    # 2026-11-28: 에스타마 월간 가게 랭킹·테라피스트 랭킹 수집은 제거 (세나 지시)
    for shop in cache['shops']:
        if not shop.get('active'):
            continue
        # 공식 랭킹 페이지(검증된 가게만) — card2 '가게 랭킹 1위' 의 1순위 근거
        if shop.get('rank_page'):
            tasks.append(dict(kind='official_rank', shop=shop['key'],
                              url=shop['rank_page']['url'], rank_kind=shop['rank_page']['kind']))
        if shop.get('adapter') in ('gal', 'gcms', 'suke'):
            # 월간 신규 가게: 공식 재적/출근 + (있으면) 에스타마 재적·출근표 보강
            if shop.get('cast_url'):
                tasks.append(dict(kind='official', shop=shop['key'], adapter=shop['adapter'], url=shop['cast_url']))
            if shop.get('schedule_url'):
                tasks.append(dict(kind='official_schedule', shop=shop['key'], adapter=shop['adapter'],
                                  url=shop['schedule_url']))
            if shop.get('fetch_estama') and shop.get('estama'):
                _sid = shop.get('estama_cast') or shop.get('estama')
                tasks.append(dict(kind='estama_shop', shop=shop['key'], sid=_sid,
                                  url=f"https://estama.jp/shop/{_sid}/cast/"))
                tasks.append(dict(kind='estama_schedule', shop=shop['key'], sid=_sid,
                                  url=f"https://estama.jp/shop/{_sid}/schedule/"))
            continue
        if shop.get('adapter') == 'brand' and shop.get('cast_url'):
            tasks.append(dict(kind='official_brand', shop=shop['key'], adapter='brand', url=shop['cast_url']))
            # 공식 재적 목록이 20명씩 잘려 있을 때: /therapist/list_json/N (N = 2페이지 이후) 를 끝까지 수집
            if shop.get('list_json_url'):
                tasks.append(dict(kind='brand_list_json', shop=shop['key'],
                                  url=shop['list_json_url'], pages_max=int(shop.get('list_json_pages') or 8)))
            if shop.get('schedule_url'):
                tasks.append(dict(kind='brand_schedule', shop=shop['key'], url=shop['schedule_url']))
                for n in (2, 3, 4):
                    tasks.append(dict(kind='brand_schedule_day', shop=shop['key'], day=n,
                                      url=shop['schedule_url'].rstrip('/') + f'/d{n}/'))
            continue
        if shop.get('adapter') == 'estama' and shop.get('estama'):
            tasks.append(dict(kind='estama_shop', shop=shop['key'], sid=shop['estama'],
                              url=f"https://estama.jp/shop/{shop['estama']}/cast/"))
            tasks.append(dict(kind='estama_schedule', shop=shop['key'], sid=shop['estama'],
                              url=f"https://estama.jp/shop/{shop['estama']}/schedule/"))
            continue
        if shop.get('ms'):
            tasks.append(dict(kind='ms_cast', shop=shop['key'], ms=shop['ms'],
                              url=f"https://ranking-mensesthe.jp/shop/{shop['ms']}/cast/"))
            tasks.append(dict(kind='ms_rank', shop=shop['key'], ms=shop['ms'],
                              url=f"https://ranking-mensesthe.jp/shop/{shop['ms']}/"))
        if shop.get('cast_url') and shop.get('adapter') in OFFICIAL_PARSERS:
            tasks.append(dict(kind='official', shop=shop['key'], adapter=shop['adapter'], url=shop['cast_url']))
        if shop.get('adapter') == 'genkaya':
            tasks.append(dict(kind='official_schedule', shop=shop['key'], adapter='genkaya',
                              url='https://www.genkaya.net/s/schedule'))
        if shop.get('adapter') == 'hihouden':
            tasks.append(dict(kind='official_schedule', shop=shop['key'], adapter='hihouden',
                              url='https://hihouden.net/schedule'))
        if shop.get('adapter') == 'superhappy':
            tasks.append(dict(kind='official_schedule', shop=shop['key'], adapter='superhappy',
                              url='https://super-happy.net/schedule'))
        if shop.get('adapter') == 'femmefatale':
            tasks.append(dict(kind='official', shop=shop['key'], adapter='femmefatale',
                              url='https://femmefatale-osaka.com/'))
        if shop.get('adapter') == 'orenoie':
            tasks.append(dict(kind='official_schedule', shop=shop['key'], adapter='orenoie',
                              url='https://ore-no-ie.com/schedule.php'))
    return tasks


def fetch_brand_list_json(t):
    """브랜드 공식 재적 AJAX 목록(list_json)을 마지막 페이지까지 순차 수집.
    응답 규약: /therapist/list_json/{n} 은 n+1번째 페이지(20명)를 점진 반환하고,
    {page: n+1, is_last: 0|1} 을 함께 준다. 페이지별 카드 HTML을 pages 로 돌려준다."""
    pages = []
    t0 = time.time()
    err = None
    for n in range(1, int(t.get('pages_max') or 8) + 1):
        url = t['url'].format(page=n)
        r = http_get(url)
        if r['status'] != 200:
            err = r.get('error')
            break
        try:
            data = json.loads(r['text'])
        except Exception as e:
            err = f'json {e}'
            break
        items = data.get('list') or []
        if items:
            pages.append(''.join(items))
        if str(data.get('is_last')) == '1' or not items:
            break
    txt = json.dumps({'pages': pages}, ensure_ascii=False)
    return dict(url=t['url'], method='json', status=200 if pages else 0, text=txt,
                bytes=len(txt.encode('utf-8')), seconds=time.time() - t0,
                error=None if pages else (err or 'empty'))


def _parse_week_day(text, r):
    """week_day 태스크 1건 → {normname: {open, close, state, soldout}}"""
    ad = r.get('adapter')
    if ad == 'brand':
        return parse_brand_schedule(text)
    if ad == 'sumeragi':
        return parse_sumeragi_schedule(text)
    if ad == 'superhappy':
        return parse_superhappy_happy_schedule(text)
    if ad == 'kimiiro':
        return parse_kimiiro_schedule(text)
    if ad == 'cream':
        return parse_cream_schedule(text)
    if ad == 'gcms':
        return parse_gcms_schedule(text, re.sub(r'^https?://', '', r['url']).split('/')[0])
    if ad == 'genkaya':
        return parse_genkaya_schedule(text)
    if ad == 'hihouden':
        return parse_hihouden_schedule(text)
    return {}


def week_spec(shop):
    """가게별 7일치 출근표 소스 설계.
    - {'estama': sid}  : 에스타마 出勤表 'セラピスト別' 주간 뷰 (7일 한 번에)
    - {'days': [(i, url)], 'adapter': ..., 'section': ...} : 공식 날짜별 페이지 (i=1..)
    둘 다 있을 수 있고, 공식 우선 + 에스타마 보충으로 합쳐진다."""
    spec = {}
    adapter = shop.get('adapter')
    wk_sid = shop.get('week_estama')
    if not wk_sid and adapter in ('estama', 'gal', 'suke', 'orenoie'):
        wk_sid = shop.get('estama_cast') or shop.get('estama')
    if wk_sid:
        spec['estama'] = str(wk_sid)
    days = []
    sched = shop.get('schedule_url')
    if adapter == 'brand' and sched:
        base = sched.rstrip('/')
        for i in range(1, WEEK_DAYS):
            days.append((i, f"{base}/d{i + 1}/"))
        spec['adapter'] = 'brand'
    elif adapter == 'sumeragi' and sched:
        base = sched.rstrip('/') + '/schedule'
        for i in range(1, WEEK_DAYS):
            days.append((i, f"{base}/d{i + 1}/"))
        spec['adapter'] = 'sumeragi'
    elif adapter in ('superhappy', 'kimiiro', 'cream', 'gcms', 'genkaya', 'hihouden'):
        if adapter == 'hihouden':
            base = 'https://hihouden.net/schedule'
        elif adapter == 'genkaya':
            base = 'https://www.genkaya.net/s/schedule'
        elif adapter == 'cream':
            base = sched or shop.get('cast_url') or ''
        else:
            base = sched or ''
        if base:
            sep = '&' if '?' in base else '?'
            for i in range(1, WEEK_DAYS):
                days.append((i, f"{base}{sep}day={i}"))
            spec['adapter'] = adapter
            if adapter == 'superhappy':
                spec['section'] = 'happy'
    if days:
        spec['days'] = days
    return spec or None


def load_week_cache():
    try:
        with open(WEEK_CACHE_PATH, encoding='utf-8') as f:
            d = json.load(f)
        if d.get('v') == 2 and isinstance(d.get('items'), dict):
            return d
    except Exception:
        pass
    return {'v': 2, 'items': {}}


def save_week_cache(wc):
    try:
        cutoff = time.time() - 3 * 86400
        items = {k: v for k, v in wc.get('items', {}).items() if (v or {}).get('ts', 0) >= cutoff}
        with open(WEEK_CACHE_PATH, 'w', encoding='utf-8') as f:
            json.dump({'v': 2, 'items': items}, f, ensure_ascii=False)
    except Exception:
        pass


def plan_week_tasks(cache, wc, now):
    """7일치 수집 작업 계획 — 내일 이후는 WEEK_TTL 캐시, 오늘/캐시 없음만 실제 fetch.
    반환: (todo, cached, stale) — stale 은 TTL 지난 예전 값(수집 실패 시 보충용)."""
    todo, cached, stale = [], {}, {}
    for shop in cache['shops']:
        if not shop.get('active'):
            continue
        spec = week_spec(shop)
        if not spec:
            continue
        key = shop['key']
        slot = cached.setdefault(key, {})
        st_slot = stale.setdefault(key, {})
        if spec.get('estama'):
            ck = f"estama|{now.date().isoformat()}|{spec['estama']}"
            ent = wc['items'].get(ck)
            if ent and (time.time() - ent.get('ts', 0)) < WEEK_TTL and ent.get('w'):
                slot['estama'] = ent['w']
            else:
                if ent and ent.get('w'):
                    st_slot['estama'] = ent['w']
                todo.append(dict(kind='estama_week', shop=key, sid=spec['estama'],
                                 url=f"https://estama.jp/shop/{spec['estama']}/schedule/ [week]",
                                 ck=ck, age=1e9 if not ent else time.time() - ent.get('ts', 0)))
        for i, url in spec.get('days') or []:
            ck = f"{spec['adapter']}|{now.date().isoformat()}|{url}"
            ent = wc['items'].get(ck)
            if ent and (time.time() - ent.get('ts', 0)) < WEEK_TTL and isinstance(ent.get('d'), dict):
                slot.setdefault('days', {})[i] = ent['d']
            else:
                if ent and isinstance(ent.get('d'), dict):
                    st_slot.setdefault('days', {})[i] = ent['d']
                todo.append(dict(kind='week_day', shop=key, adapter=spec['adapter'], day=i,
                                 url=url, ck=ck, section=spec.get('section')))
    return todo, cached, stale


def main():
    args = sys.argv[1:]
    no_images = '--no-images' in args
    want_quick = '--quick' in args
    workers = 18
    max_age = None
    out_path = 'work/secret_live.json'
    for i, a in enumerate(args):
        if a == '--workers':
            workers = int(args[i + 1])
        if a == '--max-age':
            max_age = float(args[i + 1])
        if a == '--out':
            out_path = args[i + 1]
    out_abs = os.path.join(BASE, out_path) if not os.path.isabs(out_path) else out_path
    if max_age is not None and os.path.exists(out_abs):
        age_min = (time.time() - os.path.getmtime(out_abs)) / 60.0
        if age_min < max_age:
            print(f'[secret_fetch] fresh ({age_min:.1f}m < {max_age:.1f}m), skipped')
            return
    prev_live = None
    if want_quick and os.path.exists(out_abs):
        try:
            with open(out_abs, encoding='utf-8') as f:
                prev_live = json.load(f)
        except Exception:
            prev_live = None
    quick = want_quick and prev_live is not None
    cache = load_cache()
    mon = load_monthly_state()
    if mon:
        before = {s['key'] for s in cache['shops'] if s.get('active')}
        cache = merge_monthly_shops(cache, mon)
        after = {s['key'] for s in cache['shops'] if s.get('active')}
        print(f"[monthly] TOP5 {mon.get('updated_at')}: active {len(before)}→{len(after)}")
        if after - before:
            print(f"[monthly] + 편입: {', '.join(sorted(after - before))}")
        if before - after:
            print(f"[monthly] - 제외: {', '.join(sorted(before - after))}")
        for n in mon.get('notes') or []:
            print(f"[monthly] note: {n}")
    tasks = build_tasks(cache)
    now0 = datetime.datetime.now()
    wc = load_week_cache()
    week_todo, week_cached, week_stale = plan_week_tasks(cache, wc, now0)
    if quick:
        tasks = [t for t in tasks if (
            t['kind'] in ('estama_schedule', 'official_schedule', 'ms_rank', 'brand_schedule', 'brand_rank',
                          'official_rank')
            or (t['kind'] == 'official' and t.get('adapter') in ('femmefatale', 'zenith'))
        )]
    tasks = tasks + week_todo
    estama_wk = sorted([t for t in tasks if t.get('kind') == 'estama_week'],
                       key=lambda t: -(t.get('age') or 0))
    tasks = [t for t in tasks if t.get('kind') != 'estama_week']
    t_start = time.time()
    report = []
    mode_tag = 'quick' if quick else 'full'
    print(f'[secret_fetch] {len(tasks)} sources ({mode_tag}), week {WEEK_DAYS}d: '
          f'{len(week_todo)} fetch({len(estama_wk)} estama) / '
          f'{sum(1 for v in week_cached.values() if v)} shops cached, workers={workers}')

    def run(t):
        if t.get('kind') == 'brand_list_json':
            r = fetch_brand_list_json(t)
        elif t.get('kind') == 'estama_week':
            r = fetch_estama_week(t['sid'])
        else:
            r = http_get(t['url'])
        r.update({k: v for k, v in t.items() if k != 'url'})
        return r

    results = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(run, t): t for t in tasks}
        for fu in as_completed(futs):
            r = fu.result()
            results.append(r)
            tag = r.get('shop') or r.get('area')
            print(f"  [{r.get('kind')}] {tag}: {r['method'] or 'FAIL'} {r['seconds']:.1f}s {r['bytes']}b")
    # 에스타마 주간 뷰는 혜잡 페이지가 뜨지 않도록 순차·건수 제한으로
    cap = int(os.environ.get('SECRET_ESTAMA_WEEK_CAP', '20'))
    for t in estama_wk[:cap]:
        r = run(t)
        results.append(r)
        print(f"  [{r.get('kind')}] {t['shop']}: {r['method'] or 'FAIL'} {r['seconds']:.1f}s {r['bytes']}b")
        if _ESTAMA_BLOCK['fails'] >= 3:
            print('  [estama_week] congested — remaining shops skipped this run')
            break
        time.sleep(0.8)
    elapsed = time.time() - t_start

    # ---- parse
    people = {}       # key -> record
    shops = {s['key']: dict(s) for s in cache['shops']}
    if quick:
        shop_status = prev_live.get('shop_status') or {}
        for p in (prev_live.get('people') or []):
            rec = dict(p)
            rec['today'] = None
            people[f"{rec['shop']}|{norm_name(rec['name'])}"] = rec
    else:
        estama_shops = {} # area -> ranked list
        estama_th = []    # therapist ranking rows
        shop_status = {}  # shop_key -> dict(ms_ok, official_ok, note)
    schedules = {}    # shop -> {normname: {open, close, state, soldout}}
    day_schedules = {}  # shop -> {day_no: {normname: {...}}}
    week_maps = {}    # shop -> {day_no: {normname: 'HH:MM~HH:MM'}} / {'estama': {normname: [7]}} / {'next': {...}}
    img_downloads = []

    def week_slot(shop):
        return week_maps.setdefault(shop, {})

    for r in results:
        rep = dict(url=r['url'], kind=r.get('kind'), shop=r.get('shop'), area=r.get('area'),
                   period=r.get('period'), method=r['method'], status=r['status'],
                   bytes=r['bytes'], seconds=round(r['seconds'], 2), error=r.get('error'))
        report.append(rep)
        if r['status'] != 200:
            continue
        txt = r['text']
        k = r.get('kind')
        if k == 'estama_shop':
            ps = parse_estama_cast(txt, r['shop'], r['sid'])
            st = shop_status.setdefault(r['shop'], {})
            st['estama_cast'] = len(ps)
            st['has_review'] = ('shop-nav-box__review' in txt) or bool(re.search(r'口コミ[^<]{0,20}\d+\s*件', txt))
            for pid, p in ps.items():
                key = f"{r['shop']}|{norm_name(p['name'])}"
                rec = people.setdefault(key, dict(shop=r['shop'], name=p['name'], sources=[], today=None))
                rec.update({kk: vv for kk, vv in p.items() if vv is not None})
                rec['sources'].append('estama')
                rec['pid'] = pid
                rec['img_url'] = p.get('img')
            rep['parsed'] = len(ps)
        elif k == 'estama_schedule':
            sched = parse_estama_schedule_today(txt)
            schedules.setdefault(r['shop'], {}).update(
                {nm: dict(open=s['open'], close=s['close'], state='出勤', soldout=False) for nm, s in sched.items()})
            rep['parsed'] = len(sched)
        elif k == 'ms_cast':
            ps = parse_ms_cast(txt, r['ms'])
            st = shop_status.setdefault(r['shop'], {})
            st['ms_cast'] = len(ps)
            for pid, p in ps.items():
                key = f"{r['shop']}|{norm_name(p['name'])}"
                rec = people.setdefault(key, dict(shop=r['shop'], name=p['name'], sources=[], today=None))
                rec.update({kk: vv for kk, vv in p.items() if vv is not None})
                rec['sources'].append('mensesthe')
                rec['pid'] = pid
                rec['img_url'] = p.get('img')
            rep['parsed'] = len(ps)
        elif k == 'ms_rank':
            rk = parse_ms_ranking(txt, r['ms'])
            st = shop_status.setdefault(r['shop'], {})
            st['ms_rank'] = rk[:10]
            rep['parsed'] = len(rk)
        elif k == 'brand_rank':
            rk = parse_brand_ranking(txt)
            st = shop_status.setdefault(r['shop'], {})
            st['brand_rank'] = rk[:10]
            rep['parsed'] = len(rk)
        elif k == 'official_rank':
            rk = parse_rank_page(txt, r.get('rank_kind'))
            st = shop_status.setdefault(r['shop'], {})
            if len(rk) >= 1:
                st['official_rank'] = rk[:10]
                st['rank_src'] = 'official'
                st['rank_page'] = r['url']
            rep['parsed'] = len(rk)
        elif k == 'brand_schedule':
            sched = parse_brand_schedule(txt)
            schedules.setdefault(r['shop'], {}).update(sched)
            rep['parsed'] = len(sched)
        elif k == 'brand_schedule_day':
            sched = parse_brand_schedule(txt)
            day_schedules.setdefault(r['shop'], {})[r['day']] = sched
            rep['parsed'] = len(sched)
        elif k == 'official_brand':
            host = re.sub(r'^https?://', '', r['url']).split('/')[0]
            ops = parse_brand_therapist(txt, host)
            st = shop_status.setdefault(r['shop'], {})
            st['official'] = len(ops)
            st['official_adapter'] = 'brand'
            st['official_authoritative'] = True
            if len(ops) < 5:
                st['official_note'] = 'too few names (kept portal)'
                rep['parsed'] = len(ops)
                continue
            for nm, p in ops.items():
                key = f"{r['shop']}|{nm}"
                rec = people.setdefault(key, dict(shop=r['shop'], name=p['name'], sources=[], today=None))
                rec.setdefault('official', {})
                rec['official'].update({kk: vv for kk, vv in p.items() if vv is not None})
                rec['sources'].append('brand')
                rec['official_authoritative'] = True
                if rec.get('order') is None and p.get('order') is not None:
                    rec['order'] = p['order']
                if not rec.get('name'):
                    rec['name'] = p['name']
            rep['parsed'] = len(ops)
        elif k == 'brand_list_json':
            host = re.sub(r'^https?://', '', r['url']).split('/')[0]
            try:
                pages = (json.loads(txt) or {}).get('pages') or []
            except Exception:
                pages = []
            st = shop_status.setdefault(r['shop'], {})
            total = 0
            for pi, page_html in enumerate(pages):
                ops = parse_brand_therapist(page_html, host)
                for nm, p in ops.items():
                    key = f"{r['shop']}|{nm}"
                    rec = people.setdefault(key, dict(shop=r['shop'], name=p['name'], sources=[], today=None))
                    rec.setdefault('official', {})
                    rec['official'].update({kk: vv for kk, vv in p.items() if vv is not None})
                    rec['sources'].append('brand')
                    rec['official_authoritative'] = True
                    if rec.get('order') is None and p.get('order') is not None:
                        rec['order'] = 1000 * (pi + 1) + p['order']
                    if not rec.get('name'):
                        rec['name'] = p['name']
                total += len(ops)
            st['official'] = (st.get('official') or 0) + total
            st['official_list_json'] = total
            rep['parsed'] = total
        elif k == 'official':
            fn = OFFICIAL_PARSERS[r['adapter']]
            if r['adapter'] in NEW_ADAPTERS:
                host = re.sub(r'^https?://', '', r['url']).split('/')[0]
                ops = fn(txt, host)
            else:
                ops = fn(txt)
            st = shop_status.setdefault(r['shop'], {})
            st['official'] = max(st.get('official', 0), len(ops))
            st['official_adapter'] = r['adapter']
            if len(ops) < 5:
                st['official_note'] = 'too few names (kept portal)'
                rep['parsed'] = len(ops)
                continue
            for nm, p in ops.items():
                key = f"{r['shop']}|{nm}"
                rec = people.setdefault(key, dict(shop=r['shop'], name=p['name'], sources=[], today=None))
                rec.setdefault('official', {})
                rec['official'].update({kk: vv for kk, vv in p.items() if vv is not None})
                rec['sources'].append(r['adapter'])
                if not rec.get('name'):
                    rec['name'] = p['name']
            rep['parsed'] = len(ops)
            if r['adapter'] in ('femmefatale', 'zenith'):
                nx = parse_next_shift(txt, now0)
                if nx:
                    week_slot(r['shop'])['next'] = nx
                    rep['week_next'] = len(nx)
        elif k == 'official_schedule':
            fn = {'genkaya': parse_genkaya_schedule, 'hihouden': parse_hihouden_schedule,
                  'superhappy': parse_superhappy_schedule, 'orenoie': parse_orenoie_schedule,
                  'gal': parse_gal_schedule, 'gcms': parse_gcms_schedule, 'suke': parse_suke_schedule,
                  }.get(r['adapter'])
            if fn is None:
                continue
            sched = fn(txt)
            schedules.setdefault(r['shop'], {}).update(
                {nm: dict(open=s['open'], close=s['close'], state=s.get('state', '出勤'),
                          soldout='完売' in (s.get('state') or '')) for nm, s in sched.items()})
            rep['parsed'] = len(sched)
        elif k == 'estama_week':
            wk = parse_estama_week(txt, now0)
            week_slot(r['shop'])['estama'] = wk
            if r.get('ck') and wk:
                wc['items'][r['ck']] = dict(ts=time.time(), w=wk)
                week_cached.setdefault(r['shop'], {})['estama'] = wk
            rep['parsed'] = len(wk)
        elif k == 'week_day':
            sched = _parse_week_day(txt, r)
            if sched:
                pm = {nm: f"{s['open']}~{s['close']}" for nm, s in sched.items() if s.get('open') and s.get('close')}
                week_slot(r['shop']).setdefault(r['day'], {}).update(pm)
                if r.get('ck') and pm:
                    wc['items'][r['ck']] = dict(ts=time.time(), d=pm)
                    week_cached.setdefault(r['shop'], {}).setdefault('days', {})[r['day']] = pm
            rep['parsed'] = len(sched or {})

    # ---- apply schedules (all parsed, race-free)
    for shop, sched in schedules.items():
        for rec in people.values():
            if rec['shop'] != shop:
                continue
            nm = norm_name(rec['name'])
            if nm in sched:
                rec['today'] = dict(sched[nm])

    # ---- 다음 출근일 (브랜드 공식 /schedule/d2~d4/)
    WDAY = ['월', '화', '수', '목', '금', '토', '일']
    now = datetime.datetime.now()
    for shop, days in day_schedules.items():
        for rec in people.values():
            if rec['shop'] != shop:
                continue
            nm = norm_name(rec['name'])
            rows = []
            for n in sorted(days):
                s = days[n].get(nm)
                if not s or not s.get('open'):
                    continue
                d = now + datetime.timedelta(days=n - 1)
                rows.append(f"{d.month}/{d.day:02d}({WDAY[d.weekday()]}) {s['open']}~{s['close']}")
            if rows:
                rec['next_shift'] = ' · '.join(rows[:3])

    # ---- official membership + ratio + overrides
    if not quick:
        for shop_key, st in shop_status.items():
            portal = st.get('ms_cast') or st.get('estama_cast') or 0
            st['official_ratio'] = round(st.get('official', 0) / portal, 2) if portal else None
            names = {norm_name(p['name']) for p in people.values() if p['shop'] == shop_key and p.get('official')}
            st['official_names'] = len(names)
            for rec in people.values():
                if rec['shop'] != shop_key:
                    continue
                rec['in_official'] = (norm_name(rec['name']) in names) if st.get('official', 0) >= 5 else None
                rec['official_ratio'] = st['official_ratio']
                rec['official_authoritative'] = bool(st.get('official_authoritative'))
    # merge official values over portal
    for rec in people.values():
        off = rec.get('official') or {}
        for f in ('age', 'cup', 'height', 'url', 'img', 'new'):
            if off.get(f):
                rec[f] = off[f]
        ot = off.get('today')
        if isinstance(ot, dict) and ot.get('open') and not (rec.get('today') or {}).get('open'):
            rec['today'] = dict(ot)
        if not rec.get('name'):
            rec['name'] = off.get('name', '')

    # ---- enrich with seed (정적 캐시) & registered list
    seed_by = {(p['shop'], norm_name(p['name'])): p for p in cache['people']}
    now = datetime.datetime.now()
    for key, rec in list(people.items()):
        sp = seed_by.get((rec['shop'], norm_name(rec['name'])))
        if sp:
            rec['seed'] = sp
            rec['registered'] = sp.get('registered', False)
            seed_img = sp.get('image') or ''
            if seed_img and os.path.exists(os.path.join(BASE, seed_img)):
                rec['image'] = seed_img
            rec['img_url'] = rec.get('img_url') or (sp.get('image') if seed_img.startswith('http') else None)
            for f in ('age', 'cup', 'height'):
                if not rec.get(f) and sp.get(f):
                    rec[f] = sp[f]
            rec['price_detail'] = sp.get('price_detail')
            if sp.get('kakin') is not None:
                rec['kakin'] = sp['kakin']
                if sp.get('kakin_note'):
                    rec['kakin_note'] = sp['kakin_note']
            rec['features'] = sp.get('features')
            rec['next_shift'] = sp.get('next_shift')
            rec['roles'] = sp.get('roles', [])
        else:
            rec['registered'] = False
    # registered people missing from live lists -> keep if shop official list is partial or contains the name
    existing_keys = {(r['shop'], norm_name(r['name'])) for r in people.values()}
    for sp in cache['people']:
        if not sp.get('registered'):
            continue
        if not (shops.get(sp.get('shop')) or {}).get('active'):
            continue   # 월간 TOP5 이탈로 내려간 가게의 인물은 싣지 않는다
        key = (sp['shop'], norm_name(sp['name']))
        if key in existing_keys:
            continue
        st = shop_status.get(sp['shop'], {})
        ratio = st.get('official_ratio')
        if st.get('official', 0) >= 5 and (ratio or 0) >= 0.6:
            names = {norm_name(r['name']) for r in people.values() if r['shop'] == sp['shop'] and r.get('official')}
            if norm_name(sp['name']) not in names:
                continue
        rec = dict(shop=sp['shop'], name=sp['name'], age=sp['age'], cup=sp['cup'], height=sp['height'],
                   registered=True, seed=sp, image=sp.get('image'), img=sp.get('image'),
                   price_detail=sp.get('price_detail'), features=sp.get('features'),
                   next_shift=sp.get('next_shift'), roles=sp.get('roles', []),
                   kakin=sp.get('kakin'), kakin_note=sp.get('kakin_note'),
                   today=None, sources=['seed'])
        sub = sp.get('sub_status') or ''
        if sub.startswith('오늘') and '~' in sub:
            tt = sub.replace('오늘 ', '').split(' · ')[0]
            op, cl = tt.split('~')[:2]
            cl = cl.replace('예정', '').strip()
            rec['today'] = dict(open=op.strip(), close=cl, state='', soldout=False)
        people[f"{sp['shop']}|{norm_name(sp['name'])}"] = rec
        existing_keys.add(key)

    # ---- 월간 슬롯에서 내려간 가게의 사람은 싣지 않는다(빠른 모드가 직전 live 를 재사용할 때 대비)
    for _k in [k for k, r in people.items() if not (shops.get(r.get('shop')) or {}).get('active')]:
        people.pop(_k, None)

    # ---- 오늘~+6일 출근표 (공식 날짜별 페이지 + 에스타마 주간 뷰 보충)
    week_labels = []
    for i in range(WEEK_DAYS):
        d = now + datetime.timedelta(days=i)
        week_labels.append(f"{d.month}/{d.day}({WDAY[d.weekday()]})")
    # 캐시로 대체된 가게의 7일치도 합친다
    for shk, slot in week_cached.items():
        ws = week_slot(shk)
        if slot.get('estama'):
            ws.setdefault('estama', slot['estama'])
        for i, pm in (slot.get('days') or {}).items():
            ws.setdefault(i, {}).update(pm)
    # 수집 실패한 가게는 TTL 지난 예전 값으로라도 메운다
    for shk, slot in week_stale.items():
        ws = week_slot(shk)
        if slot.get('estama') and not ws.get('estama'):
            ws['estama'] = slot['estama']
        for i, pm in (slot.get('days') or {}).items():
            cur = ws.setdefault(i, {})
            for nm, s in pm.items():
                cur.setdefault(nm, s)
    week_stats = {}
    for rec in people.values():
        nm = norm_name(rec['name'])
        slot = week_maps.get(rec['shop']) or {}
        wk = [''] * WEEK_DAYS
        t = rec.get('today') or {}
        if t.get('open') and t.get('close'):
            wk[0] = f"{t['open']}~{t['close']}"
        for i in range(1, WEEK_DAYS):
            s = (slot.get(i) or {}).get(nm)
            if s:
                wk[i] = s
        for i, s in enumerate((slot.get('estama') or {}).get(nm, [])[:WEEK_DAYS]):
            if s and not wk[i]:
                wk[i] = s
        nx = (slot.get('next') or {}).get(nm)
        if nx and not wk[nx[0]]:
            wk[nx[0]] = nx[1]
        if any(wk):
            rec['week'] = wk
            rows = [f"{week_labels[i]} {wk[i]}" for i in range(1, WEEK_DAYS) if wk[i]]
            if rows:
                rec['next_shift'] = ' · '.join(rows[:3])
            week_stats[rec['shop']] = week_stats.get(rec['shop'], 0) + 1

    # ---- today status / price / image
    for rec in people.values():
        rec['status'] = status_of(rec, now)
        shop = shops.get(rec['shop']) or {}
        rec['price_total'], rec['price_note'] = calc_price(rec, shop)

    # ---- images (--images 지정 시에만: 새 인물만, 병렬)
    if '--images' in args and not no_images:
        os.makedirs(PHOTO_DIR, exist_ok=True)
        todo = []
        for rec in people.values():
            if rec.get('image') and os.path.exists(os.path.join(BASE, rec['image'])):
                continue
            src = rec.get('img_url') or rec.get('img')
            if not src:
                continue
            prefix = 'est' if shops.get(rec['shop'], {}).get('estama') else 'ms'
            sid = shops.get(rec['shop'], {}).get('estama') if prefix == 'est' else shops.get(rec['shop'], {}).get('ms')
            fn = f"{prefix}-{sid}-{rec.get('pid') or hashlib.md5(rec['name'].encode()).hexdigest()[:6]}.{img_ext(src)}"
            todo.append((rec, os.path.join(PHOTO_DIR, fn), src))

        def _dl(item):
            rec, dest, src = item
            got = download_image(src, dest, img_downloads)
            if got:
                rec['image'] = os.path.relpath(got, BASE)
        with ThreadPoolExecutor(max_workers=8) as ex:
            list(ex.map(_dl, todo))
    # ---- output
    live = dict(
        meta=dict(fetched_at=now.isoformat(timespec='seconds'), elapsed_sec=round(elapsed, 2),
                  sources=len(tasks), workers=workers,
                  week_days=week_labels, week_fetched=len(week_todo), week_cached_shops=len(week_cached)),
        shops=shops,
        shop_status=shop_status,
        estama_shop_rank={},
        estama_therapist_rank=[],
        people=sorted(people.values(), key=lambda x: (x['shop'], -(CUP_ORDER.get(x.get('cup') or '', 0)), x.get('age') or 99)),
        images=dict(downloaded=len([1 for x in img_downloads if x.get('url')]), errors=img_downloads[:20]),
    )
    out_abs = os.path.join(BASE, out_path) if not os.path.isabs(out_path) else out_path
    with open(out_abs, 'w', encoding='utf-8') as f:
        json.dump(live, f, ensure_ascii=False, indent=1)
    rep_out = dict(meta=live['meta'], sources=report,
                   ok=sum(1 for x in report if x['status'] == 200),
                   failed=[x for x in report if x['status'] != 200],
                   browser_fallback=[x['shop'] or x.get('area') for x in report if x['status'] != 200])
    with open(os.path.join(BASE, 'work/secret_fetch_report.json'), 'w', encoding='utf-8') as f:
        json.dump(rep_out, f, ensure_ascii=False, indent=1)
    ok = rep_out['ok']
    save_week_cache(wc)
    print(f"\n[secret_fetch] done in {elapsed:.1f}s — {ok}/{len(report)} sources OK, "
          f"{len(people)} people, browser fallback: {len(rep_out['browser_fallback'])}")
    print(f"[secret_fetch] week {WEEK_DAYS}d: {len(week_stats)}/{len(cache['shops'])} shops, "
          f"people with week data {sum(1 for p in people.values() if p.get('week'))}/{len(people)}")
    for t in report:
        if t['status'] != 200:
            print(f"  FAIL {t['kind']} {t.get('shop') or t.get('area')} {t['url']} :: {t.get('error')}")
    for sh, st in shop_status.items():
        print(f"  {sh}: ms={st.get('ms_cast')} rank={len(st.get('ms_rank') or [])} "
              f"official={st.get('official')} estama={st.get('estama_cast')} {st.get('official_note','')}")


def status_of(rec, now):
    today = rec.get('today') or {}
    op, cl = today.get('open'), today.get('close')
    if today.get('soldout'):
        return '⚪'
    if op and cl:
        o, c = close_min(op, cl)
        if o is not None:
            cur = now.hour * 60 + now.minute
            if cur < o:
                return '🟡'
            if cur < (c or 0):
                return '🟢'
            return '⚪'
    nxt = rec.get('next_shift') or ''
    if nxt and '미정' not in nxt and '없음' not in nxt:
        return '📅'
    return '❔'


def calc_price(rec, shop):
    pr = shop.get('price') or {}
    base = pr.get('base')
    if not base:
        return None, ''
    nomi = pr.get('nomi', 0) or 0
    tr = pr.get('transport', 0) or 0
    hotel = pr.get('hotel', 0) or 0
    total = base + nomi + tr + (hotel if pr.get('hotel_included') else 0)
    if pr.get('hotel_included') and hotel:
        note = f"호텔 {hotel:,} 포함"
    elif tr:
        note = f"교통비 {tr:,}엔 포함"
    else:
        note = ''
    return total, note


if __name__ == '__main__':
    main()

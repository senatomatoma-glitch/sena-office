#!/usr/bin/env python3
"""비밀놀이 초고속 병렬 수집기.

- 2026-11-28 월간 기준: 에스타마 월간 TOP5(梅田·日本橋·難波·心斎橋 出張 + 4지역 통합 出張/접객) 가게 31곳
  (가게 목록은 work/secret_cache.json 이 확정 — 랭킹 수집은 폐지, 캐시의 월간 리스트 사용)
- 등록 11개 가게 + 월간 TOP5 신규 가게(10곳)의 공식/포털 재적·출근·요금 페이지
- 인물 이미지가 로컬에 없을 때만 downloads/secret-photos/ 로 다운로드
- work/secret_live.json (수집 결과) + work/secret_fetch_report.json (성공/실패·소요·fallback)

사용: python3 work/secret_fetch.py [--quick] [--max-age MIN] [--no-images] [--workers 18] [--out work/secret_live.json]
"""
import os, re, sys, json, time, ssl, subprocess, datetime, hashlib, threading
import urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed

_HOST_SEM = {}

UA = ('Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 '
      '(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1')
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JINA = 'https://r.jina.ai/'
# 직접 접속이 막히는 호스트 → 리더 프록시 우선
BLOCKED_HOSTS = {'www.genkaya.net', 'genkaya.net', 'hihouden.net', 'sumeragi-spa.com',
                 'sukesukekan.com'}
CACHE_PATH = os.path.join(BASE, 'work/secret_cache.json')
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
    _HOST_SEM.setdefault(host, threading.Semaphore(5 if host == 'estama.jp' else 8))
    for attempt in range(2):
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
    pat = re.compile(r'\[(?:日本橋\s+)?([^\[\]]+?)\s(\d+)分\s?(\d+)円\((\d+)\)\s*T\.(\d+)\s*B\.(\d+)\(([A-K])\)\s*/\s*W\.\d+\s*/\s*H\.\d+\s*'
                     r'(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})\s*【(.*?)】\]')
    for m in pat.finditer(text):
        name, _, _, age, ht, bust, cup, op, cl, st = m.groups()
        out[norm_name(name)] = dict(open=op, close=cl, state=st, cup=cup, height=int(ht), age=int(age))
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
            if shop.get('rank_url'):
                tasks.append(dict(kind='brand_rank', shop=shop['key'], url=shop['rank_url']))
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
    tasks = build_tasks(cache)
    if quick:
        tasks = [t for t in tasks if (
            t['kind'] in ('estama_schedule', 'official_schedule', 'ms_rank', 'brand_schedule', 'brand_rank')
            or (t['kind'] == 'official' and t.get('adapter') == 'femmefatale')
        )]
    t_start = time.time()
    report = []
    mode_tag = 'quick' if quick else 'full'
    print(f'[secret_fetch] {len(tasks)} sources ({mode_tag}), workers={workers}')

    def run(t):
        if t.get('kind') == 'brand_list_json':
            r = fetch_brand_list_json(t)
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
    img_downloads = []

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
                  sources=len(tasks), workers=workers),
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
    print(f"\n[secret_fetch] done in {elapsed:.1f}s — {ok}/{len(report)} sources OK, "
          f"{len(people)} people, browser fallback: {len(rep_out['browser_fallback'])}")
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

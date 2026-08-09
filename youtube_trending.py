#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
유튜브 조회수 급증 순위 수집기  (v2 · mostPopular 기반)
=======================================================
유튜브가 2025년 7월 전 카테고리 '인기 급상승' 집계를 중단했기 때문에
이 스크립트는 급증 순위를 직접 계산한다.

v1 → v2 변경
------------
v1은 search 엔드포인트로 후보를 모았는데, 검색어(q) 없이 호출하면
totalResults=0 이 돌아온다는 사실이 진단으로 확인됐다. (search는 목록 API가 아니다)
v2는 videos.list?chart=mostPopular 로 바꿨다.
  · 호출 비용 100 units → 1 unit  (100배 절감)
  · 조회수/좋아요가 같은 응답에 포함돼 추가 호출 불필요
  · 지역·카테고리별로 유튜브가 실제 집계한 인기 목록을 그대로 받음
덕분에 하루 1회가 아니라 1~2시간마다 돌릴 수 있고,
그래야 '몇 시간 만에 치고 올라온 영상'을 제대로 잡는다.

두 가지 순위
------------
🔥 오늘 급상승   업로드 24시간 이내 영상을 '시간당 조회수'로 정렬.
                첫 실행부터 바로 나온다.
📈 구간 증가량   전체 후보를 '직전 수집 대비 절대 증가량'으로 정렬.
                두 번째 실행부터 나온다.

숏츠 / 롱폼 구분
----------------
API에는 '이 영상은 숏츠다'라는 필드가 없다. 길이로 가려낸다.
  60초 이하        → 숏츠 (거의 확실)
  61~180초         → 애매한 구간. 2024년부터 숏츠 상한이 3분이 됐지만
                     짧은 뮤직비디오·예고편도 이 길이에 들어온다.
                     youtube.com/shorts/{id} 가 리다이렉트 없이 열리는지로 확인한다.
  180초 초과       → 롱폼
길이를 알 수 없는 경우(라이브 등)는 롱폼으로 둔다.

사용법
------
    $env:YOUTUBE_API_KEY = "키"      (PowerShell)
    python youtube_trending.py
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

API = "https://www.googleapis.com/youtube/v3/"
HERE = os.path.dirname(os.path.abspath(__file__))
SNAP_DIR = os.path.join(HERE, "snapshots")
OUT_HTML = os.path.join(HERE, "trending-youtube.html")

KST = timezone(timedelta(hours=9))

FRESH_MAX_AGE_H = 24     # 인기 채널에서 '신작' 으로 끌어올 업로드 경과 상한

DAY_PERIOD_H = 24         # '일간' 비교 구간
DAY_FB_MIN_AGE_H = 0.5    # 근사 모드에서 쓸 업로드 경과 하한
WEEK_PERIOD_H = 24 * 7    # '주간'
WEEK_FB_MIN_AGE_H = 1
MONTH_PERIOD_H = 24 * 30  # '월간'
MONTH_FB_MIN_AGE_H = 1

DESC_LIMIT = 140          # 카드에 보여줄 영상 설명 길이

SHORT_SURE_S = 60        # 이 이하는 확실한 숏츠
SHORT_MAX_S = 180        # 이 이하까지가 숏츠 가능 구간 (2024년 상한 확대 반영)

# 카테고리 ID 는 전 세계 공통 상수라 별칭을 하드코딩해도 안전하다.
# (API가 돌려주는 카테고리 이름은 지역·언어에 따라 달라져 매칭에 못 쓴다)
CATEGORY_ALIASES = {
    "게임": "20", "gaming": "20", "game": "20",
    "음악": "10", "music": "10",
    "스포츠": "17", "sports": "17",
    "뉴스": "25", "news": "25", "정치": "25",
    "엔터": "24", "엔터테인먼트": "24", "entertainment": "24",
    "코미디": "23", "comedy": "23",
    "영화": "1", "애니": "1", "film": "1", "animation": "1",
    "인물": "22", "브이로그": "22", "people": "22", "blogs": "22",
    "교육": "27", "education": "27",
    "과학기술": "28", "기술": "28", "science": "28", "tech": "28",
    "자동차": "2", "autos": "2",
    "반려동물": "15", "동물": "15", "pets": "15", "animals": "15",
    "여행": "19", "travel": "19",
    "howto": "26", "스타일": "26",
}

_UNITS = 0               # 소모한 API 할당량 추적


# ----------------------------------------------------------------------------
# API
# ----------------------------------------------------------------------------

def api(endpoint, key, cost=1, quiet=(), **params):
    """YouTube Data API v3 GET. quiet 에 든 reason 은 조용히 넘어간다."""
    global _UNITS
    params["key"] = key
    url = API + endpoint + "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            _UNITS += cost
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        reason = ""
        try:
            reason = json.loads(raw)["error"]["errors"][0].get("reason", "")
        except Exception:
            pass
        if reason == "quotaExceeded":
            print("\n[중단] API 일일 할당량을 모두 사용했습니다. 태평양시간 자정에 리셋됩니다.")
            sys.exit(2)
        if reason in ("badRequest", "keyInvalid") and "API key not valid" in raw:
            print("\n[중단] API 키가 유효하지 않습니다.")
            sys.exit(2)
        if reason in quiet:
            return None
        print(f"  · {endpoint} 실패 (HTTP {e.code}, {reason or '원인불명'})")
        return None
    except Exception as e:
        print(f"  · {endpoint} 실패 ({type(e).__name__}: {e})")
        return None


def get_categories(key, region_code):
    """(전체 id→이름 사전, 검색에 쓸 수 있는 [(id, 이름)] 목록)"""
    r = api("videoCategories", key, part="snippet", regionCode=region_code)
    if not r:
        return {}, []
    names, assignable = {}, []
    for it in r.get("items", []):
        cid, title = it["id"], it["snippet"]["title"]
        names[cid] = title
        if it["snippet"].get("assignable"):
            assignable.append((cid, title))
    return names, assignable


CATEGORY_DISPLAY = {
    "1": "영화·애니", "2": "자동차", "10": "음악", "15": "반려동물",
    "17": "스포츠", "19": "여행", "20": "게임", "22": "인물·브이로그",
    "23": "코미디", "24": "엔터테인먼트", "25": "뉴스·정치",
    "26": "스타일·하우투", "27": "교육", "28": "과학기술",
}


def exclusion_label(ids):
    return ", ".join(CATEGORY_DISPLAY.get(i, f"ID {i}") for i in sorted(ids))


def resolve_exclusions(spec):
    """'20,게임,music' 같은 입력을 카테고리 ID 집합으로."""
    out = set()
    for tok in (spec or "").split(","):
        tok = tok.strip()
        if not tok:
            continue
        if tok.isdigit():
            out.add(tok)
        elif tok.lower() in CATEGORY_ALIASES:
            out.add(CATEGORY_ALIASES[tok.lower()])
        else:
            print(f"  ⚠️  '{tok}' 은(는) 알 수 없는 카테고리입니다. "
                  f"숫자 ID 또는 {', '.join(sorted(set(CATEGORY_ALIASES))[:6])} … 형태로 지정하세요.")
    return out


_URL_RE = re.compile(r"(https?://\S+|www\.\S+)")
_TS_LINE_RE = re.compile(r"^\d{1,2}:\d{2}(:\d{2})?\b")
_TAG_RE = re.compile(r"[#@]\S+")
_PROMO_RE = re.compile(
    r"(구독|좋아요|알림\s*설정|채널\s*가입|멤버십|비즈니스\s*문의|제보|문의|저작권|"
    r"팔로우|인스타|트위터|틱톡|페이스북|디스코드|네이버\s*카페|공식\s*계정|"
    r"subscribe|follow|instagram|twitter|tiktok|facebook|discord|patreon)", re.I)


def clean_description(text, limit=DESC_LIMIT):
    """
    유튜브 설명란에서 사람이 읽을 만한 첫 대목만 뽑는다.
    링크·해시태그·타임스탬프 목차·구독 유도 문구는 걷어낸다.
    """
    if not text:
        return ""
    picked = []
    for raw in text.splitlines():
        s = _URL_RE.sub("", raw).strip()
        if not s or _TS_LINE_RE.match(s):
            continue
        s = _TAG_RE.sub("", s).strip(" -–—·|▶►■□◆◇★☆/")
        if len(s) < 2:
            continue
        if _PROMO_RE.search(s) and len(s) < 70:
            continue
        picked.append(s)
        if sum(len(x) for x in picked) >= limit:
            break

    out = re.sub(r"\s+", " ", " ".join(picked)).strip()
    if len(out) <= limit:
        return out
    cut = out[:limit]
    edge = max(cut.rfind("."), cut.rfind("!"), cut.rfind("?"), cut.rfind("다 "))
    if edge > limit * 0.5:
        return cut[:edge + 1].strip()
    return cut.rstrip() + "…"


_DUR_RE = re.compile(r"P(?:(\d+)D)?T(?:(\d+)H)?(?:(\d+)M)?(?:([\d.]+)S)?$")


def parse_duration(s):
    """ISO 8601 기간(PT1M30S)을 초로. 해석 불가·라이브면 None."""
    m = _DUR_RE.match(s or "")
    if not m:
        return None
    d, h, mi, sec = m.groups()
    total = (int(d or 0) * 86400 + int(h or 0) * 3600
             + int(mi or 0) * 60 + int(float(sec or 0)))
    return total or None          # PT0S / P0D (라이브 등) 은 '알 수 없음' 취급


def _pack(items):
    """videos.list 응답을 내부 표준 형태로."""
    out = {}
    for it in items:
        st, sn = it.get("statistics", {}), it.get("snippet", {})
        cd = it.get("contentDetails", {})
        if "viewCount" not in st:            # 조회수 비공개
            continue
        out[it["id"]] = {
            "title": sn.get("title", ""),
            "channel": sn.get("channelTitle", ""),
            "channelId": sn.get("channelId", ""),
            "publishedAt": sn.get("publishedAt", ""),
            "views": int(st["viewCount"]),
            "likes": int(st.get("likeCount", 0)),
            "dur": parse_duration(cd.get("duration")),
            "cat": sn.get("categoryId", ""),
            "desc": clean_description(sn.get("description", "")),
        }
    return out


# ----------------------------------------------------------------------------
# 숏츠 판별
# ----------------------------------------------------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def shorts_url_ok(vid, timeout=5):
    """
    youtube.com/shorts/{id} 가 리다이렉트 없이 열리면 숏츠.
    롱폼이면 /watch 로 302 된다. 판단 불가 시 None.
    """
    req = urllib.request.Request(
        f"https://www.youtube.com/shorts/{vid}", method="HEAD",
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
    try:
        with urllib.request.build_opener(_NoRedirect).open(req, timeout=timeout) as r:
            return r.status == 200
    except urllib.error.HTTPError as e:
        if e.code in (301, 302, 303, 307, 308):
            return False
        return None
    except Exception:
        return None


def classify_formats(pool, verify=True, verify_cap=120):
    """
    pool 의 각 영상에 isShort 를 채운다.
    60초 이하는 바로 숏츠, 180초 초과는 롱폼.
    그 사이 애매한 구간만 실제 URL로 확인한다 (요청 수를 verify_cap 으로 제한).
    """
    ambiguous = []
    for vid, v in pool.items():
        d = v.get("dur")
        if d is None:
            v["isShort"] = False           # 길이 불명(라이브 등)은 롱폼
        elif d <= SHORT_SURE_S:
            v["isShort"] = True
        elif d > SHORT_MAX_S:
            v["isShort"] = False
        else:
            v["isShort"] = True            # 잠정 — 아래에서 확인
            ambiguous.append(vid)

    if not verify or not ambiguous:
        return 0, len(ambiguous)

    # 조회수 높은 것부터 확인 — 어차피 상위권만 화면에 나온다
    ambiguous.sort(key=lambda v: pool[v]["views"], reverse=True)
    checked = flipped = 0
    for vid in ambiguous[:verify_cap]:
        res = shorts_url_ok(vid)
        checked += 1
        if res is False:
            pool[vid]["isShort"] = False   # 숏츠가 아니었음 → 롱폼으로 정정
            flipped += 1
        time.sleep(0.05)
    return checked, flipped


def most_popular(key, region_code, category_id, max_items):
    """chart=mostPopular 를 페이지네이션하며 수집. 비용 페이지당 1 unit."""
    out, token = {}, None
    while len(out) < max_items:
        params = dict(part="snippet,statistics,contentDetails", chart="mostPopular",
                      maxResults=50, regionCode=region_code or "US")
        if category_id:
            params["videoCategoryId"] = category_id
        if token:
            params["pageToken"] = token
        # 해당 지역·카테고리에 차트가 없으면 videoChartNotFound — 정상 상황이라 조용히 넘긴다
        r = api("videos", key, cost=1,
                quiet=("videoChartNotFound", "invalidVideoChart"), **params)
        if not r:
            break
        out.update(_pack(r.get("items", [])))
        token = r.get("nextPageToken")
        if not token:
            break
        time.sleep(0.05)
    return out


def fetch_stats(key, video_ids):
    """임의의 영상 ID 목록의 상세 정보. 50개당 1 unit."""
    out = {}
    for i in range(0, len(video_ids), 50):
        r = api("videos", key, cost=1, part="snippet,statistics,contentDetails",
                id=",".join(video_ids[i:i + 50]))
        if r:
            out.update(_pack(r.get("items", [])))
        time.sleep(0.05)
    return out


def recent_uploads(key, channel_ids, since_iso, per_channel=10):
    """
    인기 채널들이 '방금' 올린 영상을 잡는다.
    업로드 재생목록 ID = 채널 ID 의 UC → UU 치환. playlistItems 는 1 unit.
    mostPopular 에 아직 안 뜬 신작을 건지는 용도.
    """
    fresh_ids = []
    for cid in channel_ids:
        if not cid.startswith("UC"):
            continue
        r = api("playlistItems", key, cost=1,
                quiet=("playlistNotFound", "notFound"),
                part="contentDetails", playlistId="UU" + cid[2:],
                maxResults=per_channel)
        if not r:
            continue
        for it in r.get("items", []):
            cd = it.get("contentDetails", {})
            vid, pub = cd.get("videoId"), cd.get("videoPublishedAt", "")
            if vid and pub and pub >= since_iso:
                fresh_ids.append(vid)
        time.sleep(0.03)
    return fresh_ids


# ----------------------------------------------------------------------------
# 스냅샷
# ----------------------------------------------------------------------------

# 스냅샷은 '과거에 이런 영상이 있었다' 는 단서로만 쓰인다.
# 조회수는 어차피 다시 받아오므로 제목·설명까지 저장할 이유가 없다.
# (2시간마다 커밋하는 환경에서 저장소가 수백 MB로 불어나는 것을 막는다)
SNAP_FIELDS = ("publishedAt", "views")
SNAP_KEEP_DAYS = 35        # 월간(30일)에 여유를 둔 보관 기간
SNAP_KEEP_PER_DAY = 3      # 이틀 지난 날짜는 하루 3개만 남긴다


def _slim(pool):
    return {vid: {k: v[k] for k in SNAP_FIELDS if k in v} for vid, v in pool.items()}


def save_snapshot(data):
    os.makedirs(SNAP_DIR, exist_ok=True)
    ts = datetime.now(timezone.utc)
    path = os.path.join(SNAP_DIR, ts.strftime("%Y%m%d-%H%M%S") + ".json")
    slim = {region: _slim(pool) for region, pool in data.items()}
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"ts": ts.isoformat(), "regions": slim}, f,
                  ensure_ascii=False, separators=(",", ":"))
    return path


def prune_snapshots(now_ts):
    """보관 기간을 넘긴 스냅샷을 지우고, 오래된 날짜는 하루 몇 개만 남긴다."""
    removed, by_day = 0, {}
    for ts, path in snapshot_index():
        age_d = (now_ts - ts).total_seconds() / 86400.0
        if age_d > SNAP_KEEP_DAYS:
            os.remove(path)
            removed += 1
            continue
        if age_d <= 2:                       # 최근 이틀은 전부 보존
            continue
        day = ts.strftime("%Y%m%d")
        by_day.setdefault(day, []).append(path)
    for day, paths in by_day.items():
        for path in paths[SNAP_KEEP_PER_DAY:]:
            os.remove(path)
            removed += 1
    return removed


def snapshot_index():
    """저장된 스냅샷의 (시각, 경로) 목록을 시간순으로."""
    if not os.path.isdir(SNAP_DIR):
        return []
    out = []
    for f in sorted(os.listdir(SNAP_DIR)):
        if not f.endswith(".json"):
            continue
        try:
            ts = datetime.strptime(f[:15], "%Y%m%d-%H%M%S").replace(tzinfo=timezone.utc)
        except Exception:
            continue
        out.append((ts, os.path.join(SNAP_DIR, f)))
    return out


def load_previous():
    idx = snapshot_index()
    if not idx:
        return None
    with open(idx[-1][1], encoding="utf-8") as f:
        return json.load(f)


def archive_records(region_key, now_ts, max_age_h):
    """
    보관된 스냅샷에서 이 지역 영상들을 모은다 (같은 영상은 최신 기록으로 덮어씀).

    오늘자 인기 차트만 보면 '3주 전에 크게 터졌지만 지금은 차트에서 내려간 영상' 이
    월간·주간 목록에서 통째로 빠진다. 과거 수집분을 합쳐 그 구멍을 메운다.
    """
    out = {}
    for ts, path in snapshot_index():          # 오래된 것 → 최신 순
        if (now_ts - ts).total_seconds() / 3600.0 > max_age_h:
            continue
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue
        for vid, v in (data.get("regions", {}).get(region_key) or {}).items():
            out[vid] = v
    return out


def parse_ts(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


# ----------------------------------------------------------------------------
# 지표
# ----------------------------------------------------------------------------

def compute_period(videos, now_ts, min_age_h, max_age_h):
    """
    해당 기간에 올라온 영상을 '누적 조회수' 로 줄 세운다.

    일간이면 24시간 내 업로드, 주간이면 7일 내, 월간이면 30일 내가 대상이고
    순위는 각 영상의 누적 조회수다. 업로드 후 경과 시간으로 나누지 않으므로
    '하루 평균이 높은 순서' 가 아니라 '그 기간에 가장 많이 본 순서' 가 된다.
    """
    rows = []
    for vid, cur in videos.items():
        try:
            pub = parse_ts(cur["publishedAt"])
        except Exception:
            continue
        age_h = (now_ts - pub).total_seconds() / 3600.0
        if not (min_age_h <= age_h <= max_age_h):
            continue
        if cur.get("views", 0) <= 0:
            continue
        rows.append({"id": vid, "ageH": age_h,
                     "vph": int(cur["views"] / max(age_h, 1.0)),  # 참고용 평균 속도
                     **cur})
    rows.sort(key=lambda r: r["views"], reverse=True)
    return rows


def compute_daily(videos, now_ts):
    """일간 — 24시간 내 업로드를 누적 조회수로."""
    return compute_period(videos, now_ts, DAY_FB_MIN_AGE_H, DAY_PERIOD_H)


def compute_weekly(videos, now_ts):
    """주간 — 7일 내 업로드를 누적 조회수로."""
    return compute_period(videos, now_ts, WEEK_FB_MIN_AGE_H, WEEK_PERIOD_H)


def compute_monthly(videos, now_ts):
    """월간 — 30일 내 업로드를 누적 조회수로."""
    return compute_period(videos, now_ts, MONTH_FB_MIN_AGE_H, MONTH_PERIOD_H)


# ----------------------------------------------------------------------------
# 수집
# ----------------------------------------------------------------------------

def collect_region(key, label, region_key, region_code, per_category, deep_channels,
                   verify_shorts=True, exclude=frozenset(), archive_cap=600):
    print(f"\n[{label}] 수집 중…")
    pool = {}
    cat_names, cats = get_categories(key, region_code or "US")

    # (1) 지역 전체 인기 차트
    overall = most_popular(key, region_code, None, max_items=200)
    pool.update(overall)
    print(f"  · 전체 인기 차트        {len(overall):>4}개")

    # (2) 카테고리별 인기 차트 — 분류에 치우치지 않도록 전 카테고리를 훑는다
    #     (제외 대상 카테고리는 아예 요청하지 않아 호출도 아낀다)
    got_cat, skipped = 0, 0
    for cid, title in cats:
        if cid in exclude:
            skipped += 1
            continue
        before = len(pool)
        pool.update(most_popular(key, region_code, cid, max_items=per_category))
        got_cat += len(pool) - before
    tail = f" ({skipped}개 카테고리 건너뜀)" if skipped else ""
    print(f"  · 카테고리 {len(cats) - skipped}개 추가분   {got_cat:>4}개{tail}")

    # (3) 인기 채널의 최근 업로드 — mostPopular 에 아직 안 뜬 신작 확보
    if deep_channels > 0:
        since = (datetime.now(timezone.utc)
                 - timedelta(hours=FRESH_MAX_AGE_H)).strftime("%Y-%m-%dT%H:%M:%SZ")
        # 조회수 상위 채널부터 (중복 제거)
        seen, chans = set(), []
        for v in sorted(pool.values(), key=lambda x: x["views"], reverse=True):
            c = v.get("channelId", "")
            if c and c not in seen:
                seen.add(c)
                chans.append(c)
            if len(chans) >= deep_channels:
                break
        new_ids = [i for i in recent_uploads(key, chans, since) if i not in pool]
        if new_ids:
            pool.update(fetch_stats(key, new_ids))
        print(f"  · 인기 채널 {len(chans)}곳 신작   {len(new_ids):>4}개")

    # (3.5) 과거 수집분 합치기 — 주간·월간이 '오늘 뜬 것' 에만 쏠리지 않게
    if archive_cap > 0:
        known = archive_records(region_key, datetime.now(timezone.utc), MONTH_PERIOD_H)
        missing = [v for v in known if v not in pool]
        # 지난 30일 안에 올라온 것만, 마지막으로 본 조회수가 큰 순으로 추린다
        cutoff = datetime.now(timezone.utc) - timedelta(hours=MONTH_PERIOD_H)
        def _fresh_enough(vid):
            p = known[vid].get("publishedAt", "")
            try:
                return parse_ts(p) >= cutoff
            except Exception:
                return False
        missing = [v for v in missing if _fresh_enough(v)]
        missing.sort(key=lambda v: known[v].get("views", 0), reverse=True)
        missing = missing[:archive_cap]
        if missing:
            refreshed = fetch_stats(key, missing)
            pool.update(refreshed)
            print(f"  · 과거 수집분 복원      {len(refreshed):>4}개 (조회수 최신화)")

    # (4) 제외 카테고리 걸러내기
    #     전체 인기 차트와 채널 신작 경로로도 들어오므로 여기서 한 번 더 쳐낸다
    if exclude:
        dropped = {v["cat"] for v in pool.values() if v.get("cat") in exclude}
        before = len(pool)
        pool = {k: v for k, v in pool.items() if v.get("cat") not in exclude}
        names = ", ".join(cat_names.get(c, c) for c in sorted(dropped)) or "-"
        print(f"  · 제외 카테고리 제거   -{before - len(pool):>4}개  ({names})")

    # (5) 숏츠 / 롱폼 분류
    checked, ambiguous = classify_formats(pool, verify=verify_shorts)
    n_short = sum(1 for v in pool.values() if v.get("isShort"))
    extra = f", 애매한 {ambiguous}개 중 {checked}개 확인" if ambiguous else ""
    print(f"  · 숏츠 {n_short}개 / 롱폼 {len(pool) - n_short}개{extra}")

    # 카테고리 분포 — 특정 분류가 과대 대표되는지 눈으로 확인하라고 찍어준다
    dist = {}
    for v in pool.values():
        dist[v.get("cat", "")] = dist.get(v.get("cat", ""), 0) + 1
    top = sorted(dist.items(), key=lambda x: x[1], reverse=True)[:6]
    share = ", ".join(f"{cat_names.get(c, c)} {n}({n*100//max(len(pool),1)}%)"
                      for c, n in top)
    print(f"  · 카테고리 분포        {share}")

    print(f"  → 후보 {len(pool)}개 (누적 {_UNITS} units 사용)")
    return pool


# ----------------------------------------------------------------------------
# 출력
# ----------------------------------------------------------------------------

def fmt(n):
    return f"{n:,}"


def durlabel(sec):
    if not sec:
        return ""
    m, s = divmod(int(sec), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def kind_word(r):
    return "숏츠" if r.get("isShort") else "롱폼"


def with_ro(word):
    """받침 여부에 따라 '로 / 으로' 를 붙인다. (숏츠로 / 롱폼으로)"""
    last = word[-1]
    if not ("가" <= last <= "힣"):
        return word + "로"
    jong = (ord(last) - 0xAC00) % 28
    return word + ("로" if jong in (0, 8) else "으로")   # 받침 없음·ㄹ 받침은 '로'


def summary_period(r, rank, within_word, per_day):
    """일간·주간·월간 공용 요약. 순위 기준인 '누적 조회수' 를 앞세운다."""
    days = r["ageH"] / 24.0
    dl = f"{days:.0f}일 전" if days >= 1 else f"{int(r['ageH'])}시간 전"
    head = (f"{within_word}({dl}) 올라온 {with_ro(kind_word(r))} "
            f"누적 {fmt(r['views'])}회를 기록했습니다.")
    ref = (f"(참고: 하루 평균 약 {fmt(r['vph'] * 24)}회)" if per_day
           else f"(참고: 시간당 약 {fmt(r['vph'])}회)")
    tail = "이 기간 최다입니다." if rank == 1 else ""
    return " ".join(x for x in (head, tail, ref) if x)


def summary_daily(r, rank):
    return summary_period(r, rank, "오늘 ", per_day=False)


def summary_weekly(r, rank):
    return summary_period(r, rank, "이번 주 ", per_day=True)


def summary_monthly(r, rank):
    return summary_period(r, rank, "이번 달 ", per_day=True)


def split_by_format(rows, top, make_item):
    """전체 / 숏츠 / 롱폼 각각 독립적으로 순위를 매겨 상위 top개씩."""
    buckets = {
        "all": rows,
        "shorts": [r for r in rows if r.get("isShort")],
        "long": [r for r in rows if not r.get("isShort")],
    }
    return {k: [make_item(x, i + 1) for i, x in enumerate(v[:top])]
            for k, v in buckets.items()}


def build_html(payload):
    return HTML_TEMPLATE.replace("/*__DATA__*/null", json.dumps(payload, ensure_ascii=False))


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>유튜브 급상승 추적</title>
<meta name="theme-color" content="#0b0b0f">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="급상승">
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Crect width='64' height='64' rx='14' fill='%230b0b0f'/%3E%3Cpath d='M12 44 L26 28 L36 36 L52 18' stroke='%23fb923c' stroke-width='6' fill='none' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E">
<style>
  :root{--bg:#0b0b0f;--card:#16161d;--card2:#1d1d26;--txt:#f2f2f5;--sub:#a1a1ad;
        --line:#26262f;--rise:#4ade80;--hot:#fb923c}
  *{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
  body{margin:0;background:var(--bg);color:var(--txt);padding-bottom:48px;
    font-family:-apple-system,BlinkMacSystemFont,"Apple SD Gothic Neo","Pretendard","Noto Sans KR",sans-serif}
  header{position:sticky;top:0;z-index:20;background:rgba(11,11,15,.92);
    backdrop-filter:saturate(180%) blur(14px);border-bottom:1px solid var(--line)}
  .hd{padding:16px 16px 8px}
  h1{margin:0;font-size:20px;letter-spacing:-.4px;font-weight:800}
  .stamp{margin-top:4px;font-size:12px;color:var(--sub);line-height:1.5}
  .seg{display:flex;gap:6px;padding:8px 16px 0}
  .seg div{flex:1;padding:9px 0;border-radius:11px;border:1px solid var(--line);
    background:var(--card);color:var(--sub);font-size:13.5px;font-weight:700;
    text-align:center;cursor:pointer;transition:.15s}
  .seg div.on{background:var(--hot);border-color:var(--hot);color:#1a0f06}
  .tabs{display:flex;gap:6px;padding:8px 16px 0}
  .tabs div{flex:1;padding:8px 0;border-radius:10px;border:1px solid var(--line);
    background:var(--card);color:var(--sub);font-size:13px;font-weight:700;
    text-align:center;cursor:pointer;transition:.15s}
  .tabs div.on{background:var(--card2);border-color:#3f3f4a;color:var(--txt)}
  .fmt{display:flex;gap:6px;padding:8px 16px 12px}
  .fmt div{flex:1;padding:7px 0;border-radius:999px;border:1px solid var(--line);
    background:transparent;color:var(--sub);font-size:12.5px;font-weight:700;
    text-align:center;cursor:pointer;transition:.15s}
  .fmt div.on{background:#2a2a34;border-color:#4b4b58;color:var(--txt)}
  .fmt div span{opacity:.6;font-weight:600}
  main{padding:14px 12px 0}
  .note{font-size:12px;line-height:1.65;color:var(--sub);background:var(--card);
    border:1px solid var(--line);border-radius:12px;padding:11px 13px;margin-bottom:14px}
  .note b{color:#d4d4dc}
  .warn{border-color:#7c2d12;background:#1f1410;color:#fca5a5}
  .card{display:block;text-decoration:none;color:inherit;background:var(--card);
    border:1px solid var(--line);border-radius:16px;overflow:hidden;margin-bottom:12px}
  .card:active{background:var(--card2)}
  .thumbwrap{position:relative;aspect-ratio:16/9;background:#000}
  .thumbwrap img{width:100%;height:100%;object-fit:cover;display:block}
  .rank{position:absolute;top:10px;left:10px;min-width:32px;height:32px;padding:0 9px;
    display:flex;align-items:center;justify-content:center;border-radius:10px;
    background:rgba(0,0,0,.72);backdrop-filter:blur(6px);
    font-size:15px;font-weight:900;font-variant-numeric:tabular-nums}
  .rank.top{background:var(--hot);color:#1a0f06}
  .badge{position:absolute;top:10px;right:10px;padding:5px 9px;border-radius:9px;
    background:rgba(0,0,0,.75);backdrop-filter:blur(6px);
    font-size:11.5px;font-weight:800;color:var(--rise)}
  .badge.hot{color:var(--hot)}
  .age{position:absolute;bottom:10px;left:10px;padding:4px 8px;border-radius:8px;
    background:rgba(0,0,0,.75);backdrop-filter:blur(6px);font-size:11px;font-weight:700;color:#fde68a}
  .body{padding:13px 14px 15px}
  .title{font-size:15px;font-weight:700;line-height:1.42;letter-spacing:-.2px;
    display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
  .meta{margin-top:7px;font-size:12px;color:var(--sub);
    display:flex;flex-wrap:wrap;gap:6px;align-items:center}
  .chip{background:var(--card2);border:1px solid var(--line);border-radius:7px;padding:3px 7px}
  .desc{margin-top:10px;font-size:12.5px;line-height:1.6;color:#9a9aa6;
    padding-left:9px;border-left:2px solid #33333f;
    display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
  .sum{margin-top:10px;font-size:13px;line-height:1.62;color:#d4d4dc}
  .go{margin-top:11px;font-size:12.5px;font-weight:700;color:var(--hot)}
  .empty{padding:30px 16px;text-align:center;color:var(--sub);font-size:13px;line-height:1.7}
  footer{padding:18px 18px 34px;font-size:11.5px;line-height:1.75;color:#6b6b77;text-align:center}
</style>
</head>
<body>
<header>
  <div class="hd">
    <h1>📈 유튜브 급상승 추적</h1>
    <div class="stamp" id="stamp"></div>
  </div>
  <div class="seg" id="seg">
    <div data-m="daily" class="on">일간</div>
    <div data-m="weekly">주간</div>
    <div data-m="monthly">월간</div>
  </div>
  <div class="tabs" id="tabs"></div>
  <div class="fmt" id="fmt"></div>
</header>
<main>
  <div class="note" id="note"></div>
  <div id="list"></div>
</main>
<footer>
  일간 / 주간 / 월간 · 각각 <b>최근 24시간 · 7일 · 30일</b> 안에 올라온 영상을
  <b>누적 조회수</b> 순으로 정렬<br>
  후보 풀에는 과거 수집분도 합쳐, 지금은 차트에서 내려갔지만 그 기간에 크게 터진
  영상도 포함됩니다.<br>
  회색 인용 부분은 <b>채널이 작성한 영상 설명</b>이며, 링크·해시태그·타임스탬프를 걷어낸 것입니다.<br>
  후보 풀 · 지역 전체 인기 차트 + 전 카테고리 인기 차트 + 인기 채널 신작<br>
  숏츠 판별 · 60초 이하는 숏츠, 180초 초과는 롱폼.
  그 사이는 shorts URL 응답으로 확인했습니다.<br>
  <span id="exnote"></span>
  글로벌 탭은 미국 차트 기준입니다 (유튜브는 지역별로만 집계합니다).<br>
  요약문은 수집된 수치만으로 자동 생성되었습니다.
</footer>
<script>
const DATA = /*__DATA__*/null;
let MODE = "daily", REGION = Object.keys(DATA.regions)[0], FORMAT = "all";
const FMT_LABEL = {all:"전체", shorts:"⚡ 숏츠", long:"🎬 롱폼"};
const PERIOD = {
  daily:   {span:"최근 24시간", word:"오늘"},
  weekly:  {span:"최근 7일",    word:"이번 주"},
  monthly: {span:"최근 30일",   word:"이번 달"},
};

function short(n){
  if(n >= 100000000) return (n/100000000).toFixed(1).replace(/\.0$/,"") + "억";
  if(n >= 10000)     return Math.round(n/10000).toLocaleString("ko-KR") + "만";
  return n.toLocaleString("ko-KR");
}
function esc(s){
  return String(s).replace(/[&<>"']/g, c =>
    ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}
function ageLabel(h){
  if(h < 1)  return Math.round(h*60) + "분 전";
  if(h < 48) return Math.round(h) + "시간 전";
  return Math.round(h/24) + "일 전";
}

document.getElementById("stamp").textContent =
  DATA.stamp + (DATA.excluded ? " · " + DATA.excluded + " 제외" : "");
if(DATA.excluded){
  document.getElementById("exnote").innerHTML =
    "제외된 카테고리 · <b>" + DATA.excluded + "</b> (후보 수집 단계에서 걸러내 순위에 반영되지 않았습니다)<br>";
}
document.getElementById("tabs").innerHTML = Object.keys(DATA.regions).map((k,i) =>
  `<div data-k="${k}" class="${i===0?"on":""}">${DATA.regions[k].label}</div>`).join("");

function renderFmt(){
  const p = DATA.regions[REGION].pools;
  document.getElementById("fmt").innerHTML = ["all","shorts","long"].map(k =>
    `<div data-f="${k}" class="${k===FORMAT?"on":""}">${FMT_LABEL[k]} <span>${p[k].toLocaleString("ko-KR")}</span></div>`
  ).join("");
}

function drawView(){
  const reg = DATA.regions[REGION];
  const items = (reg[MODE] || {})[FORMAT] || [];
  const note = document.getElementById("note");
  const list = document.getElementById("list");
  renderFmt();

  note.className = "note";
  const scope = FORMAT === "all" ? "" : "<b>" + FMT_LABEL[FORMAT].replace(/^\S+\s/,"") + "</b>만 추려 ";
  const pool = (reg.pools[FORMAT] || 0).toLocaleString("ko-KR");
  const P = PERIOD[MODE];
  note.innerHTML = scope + "<b>" + P.span + "</b> 안에 올라온 영상을 <b>누적 조회수</b>가 많은 "
    + "순서로 줄 세웠습니다. 후보 " + pool + "개.";

  if(!items.length){
    list.innerHTML = '<div class="empty">조건에 맞는 영상이 없습니다.</div>';
    return;
  }

  list.innerHTML = items.map((v,i) => `
    <a class="card" href="https://www.youtube.com/watch?v=${v.id}" target="_blank" rel="noopener">
      <div class="thumbwrap">
        <img src="https://i.ytimg.com/vi/${v.id}/hqdefault.jpg" alt="" loading="lazy"
             onerror="this.onerror=null;this.src='https://i.ytimg.com/vi/${v.id}/mqdefault.jpg'">
        <div class="rank${i<3?' top':''}">${i+1}</div>
        <div class="badge">▶ ${short(v.views)}</div>
        <div class="age">${ageLabel(v.ageH)} 업로드</div>
      </div>
      <div class="body">
        <div class="title">${v.title}</div>
        <div class="meta">
          <span class="chip">${v.isShort ? '⚡ 숏츠' : '🎬 롱폼'}${v.dur ? ' ' + v.dur : ''}</span>
          <span class="chip">${v.channel}</span>
        </div>
        ${v.desc ? `<div class="desc">${esc(v.desc)}</div>` : ''}
        <div class="sum">${v.summary}</div>
        <div class="go">YouTube에서 보기 →</div>
      </div>
    </a>`).join("");
}

document.getElementById("seg").addEventListener("click", e => {
  const t = e.target.closest("div[data-m]"); if(!t) return;
  document.querySelectorAll("#seg div").forEach(x => x.classList.remove("on"));
  t.classList.add("on"); MODE = t.dataset.m; render(); window.scrollTo({top:0,behavior:"smooth"});
});
document.getElementById("tabs").addEventListener("click", e => {
  const t = e.target.closest("div[data-k]"); if(!t) return;
  document.querySelectorAll("#tabs div").forEach(x => x.classList.remove("on"));
  t.classList.add("on"); REGION = t.dataset.k; render(); window.scrollTo({top:0,behavior:"smooth"});
});
document.getElementById("fmt").addEventListener("click", e => {
  const t = e.target.closest("div[data-f]"); if(!t) return;
  FORMAT = t.dataset.f; render(); window.scrollTo({top:0,behavior:"smooth"});
});

// 렌더링이 실패하면 빈 화면 대신 원인을 보여준다.
// (조용히 비어 있으면 데이터 문제인지 코드 문제인지 구분할 수 없다)
//
// 실제 그리기는 drawView 가 하고 render 는 감싸기만 한다.
// 이름을 나누지 않고 window.render 에 래퍼를 다시 대입하면,
// 브라우저에서는 window.render 가 전역 render 와 같은 것이라
// 래퍼가 자기 자신을 호출해 무한 재귀에 빠진다.
function render(){
  try { drawView(); }
  catch(err){
    const note = document.getElementById("note");
    note.className = "note warn";
    note.textContent = "화면을 그리는 중 오류가 발생했습니다: " + err.message;
    document.getElementById("list").innerHTML =
      '<div class="empty">데이터는 정상적으로 수집됐지만 표시에 실패했습니다.<br>' +
      '이 메시지를 그대로 알려주세요.</div>';
    throw err;
  }
}
render();
</script>
</body>
</html>
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--regions", default="KR,GLOBAL")
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--per-category", type=int, default=50,
                    help="카테고리별로 가져올 최대 개수")
    ap.add_argument("--deep", type=int, default=40,
                    help="신작 확보용으로 훑을 인기 채널 수 (0이면 생략)")
    ap.add_argument("--no-verify-shorts", action="store_true",
                    help="61~180초 영상의 숏츠 여부 확인을 생략 (더 빠름, 정확도 하락)")
    ap.add_argument("--exclude", default="",
                    help="집계에서 뺄 카테고리. 예: --exclude 게임  /  --exclude 20,17")
    ap.add_argument("--archive", type=int, default=600,
                    help="과거 스냅샷에서 되살릴 영상 수 상한 (0이면 오늘 차트만 사용)")
    ap.add_argument("--out", default=OUT_HTML,
                    help="결과 HTML 경로 (기본: 스크립트 폴더의 trending-youtube.html)")
    args = ap.parse_args()

    exclude = resolve_exclusions(args.exclude)

    key = os.environ.get("YOUTUBE_API_KEY", "").strip()
    if not key:
        print("YOUTUBE_API_KEY 환경변수가 없습니다.  설정방법.md 를 참고하세요.")
        sys.exit(1)

    region_map = {"KR": ("🇰🇷 한국", "KR"), "GLOBAL": ("🌍 글로벌", "US")}
    wanted = [r.strip().upper() for r in args.regions.split(",")
              if r.strip().upper() in region_map]

    now_ts = datetime.now(timezone.utc)
    prev = load_previous()
    prev_ts = parse_ts(prev["ts"]) if prev else None
    if prev_ts:
        gap = (now_ts - prev_ts).total_seconds() / 3600.0
        print(f"직전 수집: {prev_ts.astimezone(KST):%m-%d %H:%M} KST ({gap:.1f}시간 전)")

    print("순위 기준: 해당 기간에 올라온 영상의 누적 조회수")

    collected = {r: collect_region(key, region_map[r][0], r, region_map[r][1],
                                   args.per_category, args.deep,
                                   verify_shorts=not args.no_verify_shorts,
                                   exclude=exclude, archive_cap=args.archive)
                 for r in wanted}

    if not any(collected.values()):
        print("\n수집된 영상이 없습니다. python 진단.py 로 원인을 확인하세요.")
        sys.exit(1)

    path = save_snapshot(collected)
    pruned = prune_snapshots(now_ts)
    kept = len(snapshot_index())
    print(f"\n스냅샷 저장: {os.path.basename(path)}  "
          f"(보관 {kept}개, 정리 {pruned}개, 총 {_UNITS} units 사용 / 일 10,000)")

    payload = {"stamp": now_ts.astimezone(KST).strftime("%Y.%m.%d %H:%M") + " 수집 (KST)",
               "excluded": exclusion_label(exclude) if exclude else "",
               "regions": {}}

    for r in wanted:
        label = region_map[r][0]
        cur = collected.get(r, {})

        def period_item(x, rank, summarize):
            return {"id": x["id"], "title": x["title"], "channel": x["channel"],
                    "views": x["views"], "vph": x["vph"],
                    "ageH": round(x["ageH"], 2), "desc": x.get("desc", ""),
                    "isShort": bool(x.get("isShort")), "dur": durlabel(x.get("dur")),
                    "summary": summarize(x, rank)}

        def build(compute, summarize):
            return split_by_format(compute(cur, now_ts), args.top,
                                   lambda x, rank: period_item(x, rank, summarize))

        daily = build(compute_daily, summary_daily)
        weekly = build(compute_weekly, summary_weekly)
        monthly = build(compute_monthly, summary_monthly)

        n_short = sum(1 for v in cur.values() if v.get("isShort"))
        payload["regions"][r] = {
            "label": label,
            "pools": {"all": len(cur), "shorts": n_short, "long": len(cur) - n_short},
            "daily": daily, "weekly": weekly, "monthly": monthly}

        for title, data, unit in (("📅 일간 (24h 내 업로드 · 누적 조회수)", daily, "views"),
                                  ("📅 주간 (7일 내 업로드 · 누적 조회수)", weekly, "views"),
                                  ("📅 월간 (30일 내 업로드 · 누적 조회수)", monthly, "views")):
            print(f"\n[{label}] {title}")
            for bucket, name in (("shorts", "숏츠"), ("long", "롱폼")):
                items = data[bucket][:3]
                print(f"  · {name}")
                if not items:
                    print("      (해당 없음)")
                    continue
                for i, it in enumerate(items, 1):
                    when = (f"{it['ageH']/24:.0f}d" if it["ageH"] >= 24
                            else f"{it['ageH']:.0f}h")
                    print(f"      {i}. {fmt(it['views']):>12}회  ({when:>4} 전, "
                          f"{it['dur'] or '?'})  {it['title'][:34]}")

    out_path = os.path.abspath(args.out)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(build_html(payload))
    print(f"\n완성: {out_path}")


if __name__ == "__main__":
    main()

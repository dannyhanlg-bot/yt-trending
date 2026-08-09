#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
유튜브 트렌드 추적기  (v3)
==========================
세 가지 순위를 만든다.

  🔥 인기   해당 기간에 올라온 영상을 누적 조회수 순으로
  💥 터짐   그 채널이 평소 받던 조회수 대비 몇 배가 났는지 순으로
  📡 채널   구독자가 많이 늘어난 채널 순으로

수집 경로와 한계
----------------
YouTube API에는 '이 나라에서 최근 24시간에 올라온 영상 전체' 를 열거하는 기능이 없다.
그래서 다음 네 경로의 합집합으로 후보를 만든다.

  chart     지역 전체 인기 차트 (mostPopular, 지역당 최대 200)
  category  카테고리별 인기 차트
  channel   추적 중인 인기 채널의 최근 업로드
  archive   과거 수집분 (조회수는 매번 최신화)

이 그물에 걸리지 않은 영상은 볼 수 없다. 완전한 망라는 불가능하며,
대신 영상마다 어느 경로로 들어왔는지 기록해 커버리지를 측정한다.
(chart 이외 경로의 비중이 크다면, 인기 차트만으로는 부족하다는 뜻이다)

사용법
------
    $env:YOUTUBE_API_KEY = "키"      (PowerShell)
    python youtube_trending.py --exclude 게임
"""

import argparse
import json
import os
import re
import statistics
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

API = "https://www.googleapis.com/youtube/v3/"
HERE = os.path.dirname(os.path.abspath(__file__))
SNAP_DIR = os.path.join(HERE, "snapshots")
CH_CACHE = os.path.join(HERE, "channels.json")
OUT_HTML = os.path.join(HERE, "trending-youtube.html")

KST = timezone(timedelta(hours=9))

FRESH_MAX_AGE_H = 24      # 인기 채널에서 '신작' 으로 끌어올 업로드 경과 상한

PERIODS = {               # 기간별 (업로드 경과 하한, 상한) 시간
    "daily":   (0.5, 24),
    "weekly":  (1, 24 * 7),
    "monthly": (1, 24 * 30),
}
MONTH_PERIOD_H = 24 * 30

DESC_LIMIT = 140          # 카드에 보여줄 영상 설명 길이

SHORT_SURE_S = 60         # 이 이하는 확실한 숏츠
SHORT_MAX_S = 180         # 이 이하까지가 숏츠 가능 구간 (2024년 상한 확대 반영)

# --- 터짐 지수 ---------------------------------------------------------------
CH_REFRESH_H = 24         # 채널 정보(구독자·기준선) 갱신 주기
CH_REFRESH_CAP = 90       # 한 번 실행에서 갱신할 채널 수 상한
BASE_MIN_AGE_D = 3        # 기준선에 쓸 과거 영상의 최소 나이 (조회수가 안정된 뒤)
BASE_MAX_AGE_D = 180
BASE_MIN_COUNT = 5        # 기준선을 신뢰하려면 이만큼의 과거 영상이 필요
BREAKOUT_MIN_VIEWS = 10_000   # 너무 작은 영상은 배수가 튄다
BREAKOUT_MIN_BASE = 1_000     # 기준선이 너무 낮아도 배수가 튄다

# 스냅샷은 '과거에 이런 영상이 있었다' 는 단서로만 쓰인다.
# 조회수는 어차피 다시 받아오므로 제목·설명까지 저장할 이유가 없다.
SNAP_FIELDS = ("publishedAt", "views")
SNAP_KEEP_DAYS = 35
SNAP_KEEP_PER_DAY = 3

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

CATEGORY_DISPLAY = {
    "1": "영화·애니", "2": "자동차", "10": "음악", "15": "반려동물",
    "17": "스포츠", "19": "여행", "20": "게임", "22": "인물·브이로그",
    "23": "코미디", "24": "엔터테인먼트", "25": "뉴스·정치",
    "26": "스타일·하우투", "27": "교육", "28": "과학기술",
}

SRC_LABEL = {"chart": "전체 인기차트", "category": "카테고리 차트",
             "channel": "채널 신작", "archive": "과거 수집분"}

_UNITS = 0


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


# ----------------------------------------------------------------------------
# 텍스트 정리
# ----------------------------------------------------------------------------

_URL_RE = re.compile(r"(https?://\S+|www\.\S+)")
_TS_LINE_RE = re.compile(r"^\d{1,2}:\d{2}(:\d{2})?\b")
_TAG_RE = re.compile(r"[#@]\S+")
_PROMO_RE = re.compile(
    r"(구독|좋아요|알림\s*설정|채널\s*가입|멤버십|비즈니스\s*문의|제보|문의|저작권|"
    r"팔로우|인스타|트위터|틱톡|페이스북|디스코드|네이버\s*카페|공식\s*계정|"
    r"subscribe|follow|instagram|twitter|tiktok|facebook|discord|patreon)", re.I)


def clean_description(text, limit=DESC_LIMIT):
    """유튜브 설명란에서 사람이 읽을 만한 첫 대목만 뽑는다."""
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
    return total or None


def _pack(items, src):
    """videos.list 응답을 내부 표준 형태로. src 는 수집 경로."""
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
            "src": src,
        }
    return out


# ----------------------------------------------------------------------------
# 숏츠 판별
# ----------------------------------------------------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def shorts_url_ok(vid, timeout=5):
    """youtube.com/shorts/{id} 가 리다이렉트 없이 열리면 숏츠."""
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
    """60초 이하는 숏츠, 180초 초과는 롱폼. 그 사이만 실제 URL로 확인."""
    ambiguous = []
    for vid, v in pool.items():
        d = v.get("dur")
        if d is None:
            v["isShort"] = False
        elif d <= SHORT_SURE_S:
            v["isShort"] = True
        elif d > SHORT_MAX_S:
            v["isShort"] = False
        else:
            v["isShort"] = True
            ambiguous.append(vid)

    if not verify or not ambiguous:
        return 0, len(ambiguous)

    ambiguous.sort(key=lambda v: pool[v]["views"], reverse=True)
    checked = flipped = 0
    for vid in ambiguous[:verify_cap]:
        res = shorts_url_ok(vid)
        checked += 1
        if res is False:
            pool[vid]["isShort"] = False
            flipped += 1
        time.sleep(0.05)
    return checked, flipped


# ----------------------------------------------------------------------------
# 영상 수집
# ----------------------------------------------------------------------------

def most_popular(key, region_code, category_id, max_items, src):
    """chart=mostPopular 를 페이지네이션하며 수집. 페이지당 1 unit."""
    out, token = {}, None
    while len(out) < max_items:
        params = dict(part="snippet,statistics,contentDetails", chart="mostPopular",
                      maxResults=50, regionCode=region_code or "US")
        if category_id:
            params["videoCategoryId"] = category_id
        if token:
            params["pageToken"] = token
        r = api("videos", key, cost=1,
                quiet=("videoChartNotFound", "invalidVideoChart"), **params)
        if not r:
            break
        out.update(_pack(r.get("items", []), src))
        token = r.get("nextPageToken")
        if not token:
            break
        time.sleep(0.05)
    return out


def fetch_stats(key, video_ids, src="archive"):
    """임의의 영상 ID 목록의 상세 정보. 50개당 1 unit."""
    out = {}
    for i in range(0, len(video_ids), 50):
        r = api("videos", key, cost=1, part="snippet,statistics,contentDetails",
                id=",".join(video_ids[i:i + 50]))
        if r:
            out.update(_pack(r.get("items", []), src))
        time.sleep(0.05)
    return out


def uploads_playlist(channel_id):
    """업로드 재생목록 ID = 채널 ID 의 UC → UU 치환."""
    return "UU" + channel_id[2:] if channel_id.startswith("UC") else None


def recent_uploads(key, channel_ids, since_iso, per_channel=10):
    """인기 채널이 방금 올린 영상 — mostPopular 에 아직 안 뜬 신작 확보용."""
    fresh = []
    for cid in channel_ids:
        pl = uploads_playlist(cid)
        if not pl:
            continue
        r = api("playlistItems", key, cost=1,
                quiet=("playlistNotFound", "notFound"),
                part="contentDetails", playlistId=pl, maxResults=per_channel)
        if not r:
            continue
        for it in r.get("items", []):
            cd = it.get("contentDetails", {})
            vid, pub = cd.get("videoId"), cd.get("videoPublishedAt", "")
            if vid and pub and pub >= since_iso:
                fresh.append(vid)
        time.sleep(0.03)
    return fresh


# ----------------------------------------------------------------------------
# 채널 캐시 — 구독자 수와 '평소 조회수' 기준선
# ----------------------------------------------------------------------------

def load_channel_cache():
    if not os.path.exists(CH_CACHE):
        return {}
    try:
        with open(CH_CACHE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_channel_cache(cache):
    with open(CH_CACHE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, separators=(",", ":"))


def _stale(entry, now_ts):
    if not entry or "ts" not in entry:
        return True
    try:
        return (now_ts - parse_ts(entry["ts"])).total_seconds() / 3600.0 > CH_REFRESH_H
    except Exception:
        return True


def refresh_channels(key, channel_ids, cache, now_ts, cap=CH_REFRESH_CAP):
    """
    채널의 구독자 수와 '평소 조회수 기준선' 을 갱신한다.

    기준선 = 그 채널의 과거 업로드(3~180일 전) 조회수의 중앙값.
    갓 올라온 영상은 조회수가 아직 안 익었으므로 기준선에서 제외한다.
    평균 대신 중앙값을 쓰는 이유는, 과거에 한 번 크게 터진 영상이
    기준선을 통째로 끌어올려 이후 터짐을 못 잡는 것을 막기 위해서다.

    캐시가 24시간 안에 갱신된 채널은 건너뛰므로 매 실행 비용이 낮다.
    """
    todo = [c for c in channel_ids if _stale(cache.get(c), now_ts)][:cap]
    if not todo:
        return 0

    # 1) 구독자 수·채널명·썸네일
    for i in range(0, len(todo), 50):
        chunk = todo[i:i + 50]
        r = api("channels", key, cost=1, part="snippet,statistics",
                id=",".join(chunk))
        if not r:
            continue
        for it in r.get("items", []):
            sn, st = it.get("snippet", {}), it.get("statistics", {})
            thumbs = sn.get("thumbnails", {})
            cache.setdefault(it["id"], {}).update({
                "title": sn.get("title", ""),
                "thumb": (thumbs.get("default") or {}).get("url", ""),
                "subs": (None if st.get("hiddenSubscriberCount")
                         else int(st.get("subscriberCount", 0) or 0)),
                "ts": now_ts.isoformat(),
            })
        time.sleep(0.05)

    # 2) 과거 업로드를 모아 기준선 계산
    lo = now_ts - timedelta(days=BASE_MAX_AGE_D)
    hi = now_ts - timedelta(days=BASE_MIN_AGE_D)
    want = {}
    for cid in todo:
        pl = uploads_playlist(cid)
        if not pl:
            continue
        r = api("playlistItems", key, cost=1,
                quiet=("playlistNotFound", "notFound"),
                part="contentDetails", playlistId=pl, maxResults=30)
        if not r:
            continue
        ids = []
        for it in r.get("items", []):
            cd = it.get("contentDetails", {})
            vid, pub = cd.get("videoId"), cd.get("videoPublishedAt", "")
            if not vid or not pub:
                continue
            try:
                p = parse_ts(pub)
            except Exception:
                continue
            if lo <= p <= hi:
                ids.append(vid)
        if ids:
            want[cid] = ids[:20]
        time.sleep(0.03)

    flat = [v for ids in want.values() for v in ids]
    stats = {}
    for i in range(0, len(flat), 50):
        r = api("videos", key, cost=1, part="statistics", id=",".join(flat[i:i + 50]))
        if r:
            for it in r.get("items", []):
                vc = it.get("statistics", {}).get("viewCount")
                if vc is not None:
                    stats[it["id"]] = int(vc)
        time.sleep(0.05)

    for cid, ids in want.items():
        vals = [stats[v] for v in ids if v in stats]
        entry = cache.setdefault(cid, {})
        if len(vals) >= BASE_MIN_COUNT:
            entry["base"] = int(statistics.median(vals))
            entry["baseN"] = len(vals)
        else:
            entry["base"] = None
            entry["baseN"] = len(vals)

    return len(todo)


# ----------------------------------------------------------------------------
# 스냅샷
# ----------------------------------------------------------------------------

def _slim(pool):
    return {vid: {k: v[k] for k in SNAP_FIELDS if k in v} for vid, v in pool.items()}


def save_snapshot(video_data, channel_subs):
    os.makedirs(SNAP_DIR, exist_ok=True)
    ts = datetime.now(timezone.utc)
    path = os.path.join(SNAP_DIR, ts.strftime("%Y%m%d-%H%M%S") + ".json")
    payload = {"ts": ts.isoformat(),
               "regions": {r: _slim(p) for r, p in video_data.items()},
               "channels": channel_subs}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    return path


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


def load_snapshot_near(hours_ago, tol_h, now_ts):
    """N시간 전에 가장 가까운 스냅샷. 허용 오차 밖이면 (None, None)."""
    idx = snapshot_index()
    if not idx:
        return None, None
    target = now_ts - timedelta(hours=hours_ago)
    ts, path = min(idx, key=lambda m: abs((m[0] - target).total_seconds()))
    if abs((ts - target).total_seconds()) / 3600 > tol_h:
        return None, None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f), ts
    except Exception:
        return None, None


def prune_snapshots(now_ts):
    """보관 기간을 넘긴 스냅샷을 지우고, 오래된 날짜는 하루 몇 개만 남긴다."""
    removed, by_day = 0, {}
    for ts, path in snapshot_index():
        age_d = (now_ts - ts).total_seconds() / 86400.0
        if age_d > SNAP_KEEP_DAYS:
            os.remove(path)
            removed += 1
            continue
        if age_d <= 2:
            continue
        by_day.setdefault(ts.strftime("%Y%m%d"), []).append(path)
    for paths in by_day.values():
        for path in paths[SNAP_KEEP_PER_DAY:]:
            os.remove(path)
            removed += 1
    return removed


def archive_records(region_key, now_ts, max_age_h):
    """보관된 스냅샷에서 이 지역 영상들을 모은다 (최신 기록으로 덮어씀)."""
    out = {}
    for ts, path in snapshot_index():
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
# 순위 계산
# ----------------------------------------------------------------------------

def in_period(cur, now_ts, period):
    """업로드 경과 시간이 해당 기간 안이면 경과 시간(h)을, 아니면 None."""
    lo, hi = PERIODS[period]
    try:
        pub = parse_ts(cur["publishedAt"])
    except Exception:
        return None
    age_h = (now_ts - pub).total_seconds() / 3600.0
    return age_h if lo <= age_h <= hi else None


def compute_views(videos, now_ts, period):
    """🔥 인기 — 해당 기간 업로드를 누적 조회수 순으로."""
    rows = []
    for vid, cur in videos.items():
        age_h = in_period(cur, now_ts, period)
        if age_h is None or cur.get("views", 0) <= 0:
            continue
        rows.append({"id": vid, "ageH": age_h,
                     "vph": int(cur["views"] / max(age_h, 1.0)), **cur})
    rows.sort(key=lambda r: r["views"], reverse=True)
    return rows


def compute_breakout(videos, channels, now_ts, period):
    """
    💥 터짐 — 그 채널이 평소 받던 조회수 대비 몇 배인지로 정렬.

    기준선이 없거나(과거 업로드 부족) 너무 낮은 채널, 조회수가 아주 작은 영상은
    배수가 쉽게 튀므로 제외한다. 배수를 못 믿을 바에는 빼는 편이 낫다.
    """
    rows = []
    for vid, cur in videos.items():
        age_h = in_period(cur, now_ts, period)
        if age_h is None:
            continue
        views = cur.get("views", 0)
        if views < BREAKOUT_MIN_VIEWS:
            continue
        ch = channels.get(cur.get("channelId", "")) or {}
        base = ch.get("base")
        if not base or base < BREAKOUT_MIN_BASE:
            continue
        rows.append({"id": vid, "ageH": age_h, "base": base,
                     "baseN": ch.get("baseN", 0),
                     "ratio": round(views / base, 1),
                     "subs": ch.get("subs"),
                     "vph": int(views / max(age_h, 1.0)), **cur})
    rows.sort(key=lambda r: r["ratio"], reverse=True)
    return rows


def compute_channel_growth(now_subs, now_ts, period, channels, tol_h):
    """
    📡 채널 — 구독자가 많이 늘어난 순.

    API가 주는 구독자 수는 반올림값이라(대형 채널일수록 눈금이 거칠다)
    작은 변동은 잡히지 않는다. 증가가 확인된 채널만 보여준다.
    """
    hours = {"daily": 24, "weekly": 24 * 7, "monthly": 24 * 30}[period]
    base_snap, base_ts = load_snapshot_near(hours, tol_h, now_ts)
    if not base_snap:
        return [], None
    old = base_snap.get("channels") or {}
    rows = []
    for cid, subs in now_subs.items():
        prev = old.get(cid)
        if prev is None or subs is None or subs <= prev:
            continue
        info = channels.get(cid) or {}
        rows.append({"id": cid, "title": info.get("title", ""),
                     "thumb": info.get("thumb", ""),
                     "subs": subs, "gained": subs - prev,
                     "pct": round((subs - prev) / prev * 100, 2) if prev else 0})
    rows.sort(key=lambda r: r["gained"], reverse=True)
    return rows, base_ts


def split_by_format(rows, top, make_item):
    """전체 / 숏츠 / 롱폼 각각 독립적으로 순위를 매겨 상위 top개씩."""
    buckets = {"all": rows,
               "shorts": [r for r in rows if r.get("isShort")],
               "long": [r for r in rows if not r.get("isShort")]}
    return {k: [make_item(x, i + 1) for i, x in enumerate(v[:top])]
            for k, v in buckets.items()}


# ----------------------------------------------------------------------------
# 수집 본체
# ----------------------------------------------------------------------------

def collect_region(key, label, region_key, region_code, per_category, deep_channels,
                   verify_shorts=True, exclude=frozenset(), archive_cap=600):
    print(f"\n[{label}] 수집 중…")
    pool = {}
    cat_names, cats = get_categories(key, region_code or "US")

    def merge(new):
        """이미 있는 영상은 먼저 들어온 경로를 유지한다 (출처 추적용)."""
        added = 0
        for vid, v in new.items():
            if vid not in pool:
                pool[vid] = v
                added += 1
        return added

    n = merge(most_popular(key, region_code, None, 200, "chart"))
    print(f"  · 전체 인기 차트        {n:>4}개")

    got, skipped = 0, 0
    for cid, _ in cats:
        if cid in exclude:
            skipped += 1
            continue
        got += merge(most_popular(key, region_code, cid, per_category, "category"))
    tail = f" ({skipped}개 카테고리 건너뜀)" if skipped else ""
    print(f"  · 카테고리 {len(cats) - skipped}개 추가분   {got:>4}개{tail}")

    if deep_channels > 0:
        since = (datetime.now(timezone.utc)
                 - timedelta(hours=FRESH_MAX_AGE_H)).strftime("%Y-%m-%dT%H:%M:%SZ")
        seen, chans = set(), []
        for v in sorted(pool.values(), key=lambda x: x["views"], reverse=True):
            c = v.get("channelId", "")
            if c and c not in seen:
                seen.add(c)
                chans.append(c)
            if len(chans) >= deep_channels:
                break
        ids = [i for i in recent_uploads(key, chans, since) if i not in pool]
        n = merge(fetch_stats(key, ids, "channel")) if ids else 0
        print(f"  · 인기 채널 {len(chans)}곳 신작   {n:>4}개")

    if archive_cap > 0:
        now = datetime.now(timezone.utc)
        known = archive_records(region_key, now, MONTH_PERIOD_H)
        cutoff = now - timedelta(hours=MONTH_PERIOD_H)

        def fresh_enough(vid):
            try:
                return parse_ts(known[vid].get("publishedAt", "")) >= cutoff
            except Exception:
                return False

        missing = [v for v in known if v not in pool and fresh_enough(v)]
        missing.sort(key=lambda v: known[v].get("views", 0), reverse=True)
        missing = missing[:archive_cap]
        n = merge(fetch_stats(key, missing, "archive")) if missing else 0
        print(f"  · 과거 수집분 복원      {n:>4}개 (조회수 최신화)")

    if exclude:
        before = len(pool)
        dropped = {v["cat"] for v in pool.values() if v.get("cat") in exclude}
        pool = {k: v for k, v in pool.items() if v.get("cat") not in exclude}
        names = ", ".join(cat_names.get(c, c) for c in sorted(dropped)) or "-"
        print(f"  · 제외 카테고리 제거   -{before - len(pool):>4}개  ({names})")

    checked, ambiguous = classify_formats(pool, verify=verify_shorts)
    n_short = sum(1 for v in pool.values() if v.get("isShort"))
    extra = f", 애매한 {ambiguous}개 중 {checked}개 확인" if ambiguous else ""
    print(f"  · 숏츠 {n_short}개 / 롱폼 {len(pool) - n_short}개{extra}")

    dist = {}
    for v in pool.values():
        dist[v.get("cat", "")] = dist.get(v.get("cat", ""), 0) + 1
    top = sorted(dist.items(), key=lambda x: x[1], reverse=True)[:5]
    print("  · 카테고리 분포        " + ", ".join(
        f"{cat_names.get(c, c)} {k}({k*100//max(len(pool),1)}%)" for c, k in top))

    return pool


def audit_sources(pool, rows, label):
    """
    커버리지 감사 — 상위권 영상이 어느 경로로 들어왔는지 본다.

    'chart' 비중이 100%가 아니라는 것은, 인기 차트만 봤다면 그만큼을
    놓쳤을 것이라는 뜻이다. 완전한 망라는 불가능하므로 이 수치로 가늠한다.
    """
    if not rows:
        return
    dist = {}
    for r in rows[:20]:
        s = pool.get(r["id"], {}).get("src", "?")
        dist[s] = dist.get(s, 0) + 1
    parts = [f"{SRC_LABEL.get(s, s)} {n}" for s, n in
             sorted(dist.items(), key=lambda x: -x[1])]
    other = sum(n for s, n in dist.items() if s != "chart")
    print(f"  · {label} 상위 20 출처   " + ", ".join(parts)
          + (f"   ← 전체 차트 밖에서 {other}개" if other else ""))


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
    """받침 여부에 따라 '로 / 으로' 를 붙인다."""
    last = word[-1]
    if not ("가" <= last <= "힣"):
        return word + "로"
    return word + ("로" if (ord(last) - 0xAC00) % 28 in (0, 8) else "으로")


PERIOD_WORD = {"daily": "오늘", "weekly": "이번 주", "monthly": "이번 달"}


def summary_views(r, rank, period):
    days = r["ageH"] / 24.0
    dl = f"{days:.0f}일 전" if days >= 1 else f"{int(r['ageH'])}시간 전"
    head = (f"{PERIOD_WORD[period]}({dl}) 올라온 {with_ro(kind_word(r))} "
            f"누적 {fmt(r['views'])}회를 기록했습니다.")
    ref = (f"(참고: 시간당 약 {fmt(r['vph'])}회)" if period == "daily"
           else f"(참고: 하루 평균 약 {fmt(r['vph'] * 24)}회)")
    tail = "이 기간 최다입니다." if rank == 1 else ""
    return " ".join(x for x in (head, tail, ref) if x)


def summary_breakout(r, rank, period):
    days = r["ageH"] / 24.0
    dl = f"{days:.0f}일 전" if days >= 1 else f"{int(r['ageH'])}시간 전"
    head = (f"이 채널이 평소 받던 조회수는 {fmt(r['base'])}회 정도인데, "
            f"이 영상은 {dl} 올라와 {fmt(r['views'])}회를 기록했습니다.")
    mult = f"평소의 약 {r['ratio']}배입니다."
    tail = "이 기간 가장 큰 이변입니다." if rank == 1 else ""
    return " ".join(x for x in (head, mult, tail) if x)


def build_html(payload):
    return HTML_TEMPLATE.replace("/*__DATA__*/null",
                                 json.dumps(payload, ensure_ascii=False))


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>유튜브 트렌드</title>
<meta name="theme-color" content="#0b0b0f">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="트렌드">
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Crect width='64' height='64' rx='14' fill='%230b0b0f'/%3E%3Cpath d='M12 44 L26 28 L36 36 L52 18' stroke='%23fb923c' stroke-width='6' fill='none' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E">
<style>
  :root{--bg:#0b0b0f;--card:#16161d;--card2:#1d1d26;--txt:#f2f2f5;--sub:#a1a1ad;
        --line:#26262f;--hot:#fb923c;--pop:#60a5fa;--boom:#f472b6;--chan:#4ade80}
  *{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
  body{margin:0;background:var(--bg);color:var(--txt);padding-bottom:48px;
    font-family:-apple-system,BlinkMacSystemFont,"Apple SD Gothic Neo","Pretendard","Noto Sans KR",sans-serif}
  header{position:sticky;top:0;z-index:20;background:rgba(11,11,15,.93);
    backdrop-filter:saturate(180%) blur(14px);border-bottom:1px solid var(--line)}
  .hd{padding:14px 16px 6px;position:relative}
  h1{margin:0;font-size:19px;letter-spacing:-.4px;font-weight:800}
  .stamp{margin-top:3px;font-size:11.5px;color:var(--sub);line-height:1.45}
  .reg{position:absolute;right:14px;top:14px;display:flex;gap:4px}
  .reg div{padding:5px 10px;border-radius:999px;border:1px solid var(--line);
    background:var(--card);color:var(--sub);font-size:11.5px;font-weight:700;cursor:pointer}
  .reg div.on{background:var(--card2);border-color:#4b4b58;color:var(--txt)}
  .seg{display:flex;gap:6px;padding:8px 16px 0}
  .seg div{flex:1;padding:9px 0;border-radius:11px;border:1px solid var(--line);
    background:var(--card);color:var(--sub);font-size:13.5px;font-weight:700;
    text-align:center;cursor:pointer;transition:.15s}
  .seg div.on{color:#12060b}
  .seg div.on[data-v=views]{background:var(--pop);border-color:var(--pop)}
  .seg div.on[data-v=breakout]{background:var(--boom);border-color:var(--boom)}
  .seg div.on[data-v=channels]{background:var(--chan);border-color:var(--chan)}
  .sub{display:flex;gap:6px;padding:8px 16px 0}
  .sub div{flex:1;padding:7px 0;border-radius:9px;border:1px solid var(--line);
    background:transparent;color:var(--sub);font-size:12.5px;font-weight:700;
    text-align:center;cursor:pointer;transition:.15s}
  .sub div.on{background:var(--card2);border-color:#4b4b58;color:var(--txt)}
  .fmt{display:flex;gap:6px;padding:8px 16px 0}
  .fmt div{flex:1;padding:6px 0;border-radius:999px;border:1px solid var(--line);
    background:transparent;color:var(--sub);font-size:12px;font-weight:700;
    text-align:center;cursor:pointer}
  .fmt div.on{background:#2a2a34;border-color:#4b4b58;color:var(--txt)}
  .fmt div span{opacity:.55;font-weight:600}
  .pad{height:12px}
  main{padding:0 12px}
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
    background:rgba(0,0,0,.78);backdrop-filter:blur(6px);
    font-size:11.5px;font-weight:800;color:var(--pop)}
  .badge.boom{color:var(--boom)}
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
  .crow{display:flex;align-items:center;gap:12px;padding:12px 14px;background:var(--card);
    border:1px solid var(--line);border-radius:14px;margin-bottom:10px;
    text-decoration:none;color:inherit}
  .crow:active{background:var(--card2)}
  .cnum{min-width:26px;font-size:15px;font-weight:900;color:var(--sub);
    font-variant-numeric:tabular-nums;text-align:center}
  .cav{width:44px;height:44px;border-radius:50%;background:#000;flex:none;object-fit:cover}
  .cinfo{flex:1;min-width:0}
  .cname{font-size:14px;font-weight:700;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .csub{margin-top:3px;font-size:11.5px;color:var(--sub)}
  .cgain{text-align:right;font-size:14px;font-weight:800;color:var(--chan);flex:none}
  .cgain small{display:block;font-size:10.5px;color:var(--sub);font-weight:600;margin-top:2px}
  .empty{padding:30px 16px;text-align:center;color:var(--sub);font-size:13px;line-height:1.7}
  footer{padding:18px 18px 34px;font-size:11.5px;line-height:1.75;color:#6b6b77;text-align:center}
</style>
</head>
<body>
<header>
  <div class="hd">
    <h1>📈 유튜브 트렌드</h1>
    <div class="stamp" id="stamp"></div>
    <div class="reg" id="reg"></div>
  </div>
  <div class="seg" id="viewsel">
    <div data-v="views" class="on">🔥 인기</div>
    <div data-v="breakout">💥 터짐</div>
    <div data-v="channels">📡 채널</div>
  </div>
  <div class="sub" id="persel">
    <div data-p="daily" class="on">일간</div>
    <div data-p="weekly">주간</div>
    <div data-p="monthly">월간</div>
  </div>
  <div class="fmt" id="fmt"></div>
  <div class="pad"></div>
</header>
<main>
  <div class="note" id="note"></div>
  <div id="list"></div>
</main>
<footer>
  🔥 인기 · 그 기간에 올라온 영상을 <b>누적 조회수</b> 순으로<br>
  💥 터짐 · 그 채널이 <b>평소 받던 조회수 대비 몇 배</b>인지로<br>
  📡 채널 · 그 기간에 <b>구독자가 늘어난 수</b>로<br>
  <span id="exnote"></span>
  후보 풀 · 전체 인기차트 + 카테고리 차트 + 인기 채널 신작 + 과거 수집분<br>
  유튜브 API에는 영상 전체를 열거하는 기능이 없어 <b>완전한 망라는 불가능</b>합니다.<br>
  구독자 수는 API가 반올림해 제공하므로 작은 변동은 잡히지 않습니다.<br>
  요약문은 수집된 수치만으로 자동 생성되었습니다.
</footer>
<script>
const DATA = /*__DATA__*/null;
let VIEW = "views", PERIOD = "daily", FORMAT = "all";
let REGION = Object.keys(DATA.regions)[0];
const FMT_LABEL = {all:"전체", shorts:"⚡ 숏츠", long:"🎬 롱폼"};
const P_SPAN = {daily:"최근 24시간", weekly:"최근 7일", monthly:"최근 30일"};
const P_WORD = {daily:"하루", weekly:"일주일", monthly:"한 달"};

function short(n){
  if(n === null || n === undefined) return "-";
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
    "제외 카테고리 · <b>" + DATA.excluded + "</b><br>";
}
document.getElementById("reg").innerHTML = Object.keys(DATA.regions).map((k,i) =>
  `<div data-k="${k}" class="${i===0?"on":""}">${DATA.regions[k].short}</div>`).join("");

function renderFmt(){
  const box = document.getElementById("fmt");
  if(VIEW === "channels"){ box.style.display = "none"; return; }
  box.style.display = "flex";
  const p = DATA.regions[REGION].pools;
  box.innerHTML = ["all","shorts","long"].map(k =>
    `<div data-f="${k}" class="${k===FORMAT?"on":""}">${FMT_LABEL[k]} <span>${p[k].toLocaleString("ko-KR")}</span></div>`
  ).join("");
}

function videoCard(v, i, mode){
  const badge = mode === "breakout"
    ? `<div class="badge boom">💥 ${v.ratio}배</div>`
    : `<div class="badge">▶ ${short(v.views)}</div>`;
  return `
    <a class="card" href="https://www.youtube.com/watch?v=${v.id}" target="_blank" rel="noopener">
      <div class="thumbwrap">
        <img src="https://i.ytimg.com/vi/${v.id}/hqdefault.jpg" alt="" loading="lazy"
             onerror="this.onerror=null;this.src='https://i.ytimg.com/vi/${v.id}/mqdefault.jpg'">
        <div class="rank${i<3?' top':''}">${i+1}</div>
        ${badge}
        <div class="age">${ageLabel(v.ageH)} 업로드</div>
      </div>
      <div class="body">
        <div class="title">${esc(v.title)}</div>
        <div class="meta">
          <span class="chip">${v.isShort ? '⚡ 숏츠' : '🎬 롱폼'}${v.dur ? ' ' + v.dur : ''}</span>
          <span class="chip">${esc(v.channel)}</span>
          ${mode === "breakout" ? `<span class="chip">▶ ${short(v.views)}</span>` : ''}
        </div>
        ${v.desc ? `<div class="desc">${esc(v.desc)}</div>` : ''}
        <div class="sum">${esc(v.summary)}</div>
        <div class="go">YouTube에서 보기 →</div>
      </div>
    </a>`;
}

function channelRow(c, i){
  const av = c.thumb
    ? `<img class="cav" src="${c.thumb}" alt="" loading="lazy">`
    : `<div class="cav"></div>`;
  return `
    <a class="crow" href="https://www.youtube.com/channel/${c.id}" target="_blank" rel="noopener">
      <div class="cnum">${i+1}</div>
      ${av}
      <div class="cinfo">
        <div class="cname">${esc(c.title || c.id)}</div>
        <div class="csub">구독자 ${short(c.subs)}명</div>
      </div>
      <div class="cgain">+${short(c.gained)}<small>${c.pct}%</small></div>
    </a>`;
}

function drawView(){
  const reg = DATA.regions[REGION];
  const note = document.getElementById("note");
  const list = document.getElementById("list");
  renderFmt();
  note.className = "note";

  if(VIEW === "channels"){
    const box = reg.channels[PERIOD] || {};
    const items = box.rows || [];
    if(!box.base){
      note.className = "note warn";
      note.innerHTML = "<b>" + P_WORD[PERIOD] + " 전 기록이 아직 없습니다.</b> "
        + "구독자 증가는 두 시점을 비교해야 나오는 값이라, 그만큼 수집이 쌓여야 계산됩니다.";
      list.innerHTML = "";
      return;
    }
    note.innerHTML = "<b>" + box.base + "</b> 기록과 비교해 <b>" + P_WORD[PERIOD]
      + " 동안 구독자가 늘어난</b> 순서입니다. 추적 중인 채널 "
      + (box.tracked||0).toLocaleString("ko-KR") + "곳 기준.";
    list.innerHTML = items.length
      ? items.map((c,i) => channelRow(c,i)).join("")
      : '<div class="empty">증가가 확인된 채널이 없습니다.<br>구독자 수는 반올림되어 제공되므로 작은 변동은 잡히지 않습니다.</div>';
    return;
  }

  const items = ((reg[VIEW] || {})[PERIOD] || {})[FORMAT] || [];
  const scope = FORMAT === "all" ? "" : "<b>" + FMT_LABEL[FORMAT].replace(/^\S+\s/,"") + "</b>만 추려 ";
  const pool = (reg.pools[FORMAT] || 0).toLocaleString("ko-KR");

  if(VIEW === "breakout"){
    note.innerHTML = scope + "<b>" + P_SPAN[PERIOD] + "</b> 안에 올라온 영상 중, "
      + "그 채널이 <b>평소 받던 조회수 대비 배수</b>가 큰 순서입니다. "
      + "기준선을 낼 만큼 과거 영상이 없는 채널은 제외했습니다.";
  } else {
    note.innerHTML = scope + "<b>" + P_SPAN[PERIOD] + "</b> 안에 올라온 영상을 "
      + "<b>누적 조회수</b>가 많은 순서로 줄 세웠습니다. 후보 " + pool + "개.";
  }

  list.innerHTML = items.length
    ? items.map((v,i) => videoCard(v,i,VIEW)).join("")
    : '<div class="empty">조건에 맞는 영상이 없습니다.</div>';
}

function bind(id, attr, fn){
  document.getElementById(id).addEventListener("click", e => {
    const t = e.target.closest("div[" + attr + "]");
    if(!t) return;
    document.querySelectorAll("#" + id + " div").forEach(x => x.classList.remove("on"));
    t.classList.add("on");
    fn(t.getAttribute(attr));
    render();
    window.scrollTo({top:0, behavior:"smooth"});
  });
}
bind("viewsel", "data-v", v => { VIEW = v; });
bind("persel",  "data-p", v => { PERIOD = v; });
bind("reg",     "data-k", v => { REGION = v; });
document.getElementById("fmt").addEventListener("click", e => {
  const t = e.target.closest("div[data-f]"); if(!t) return;
  FORMAT = t.getAttribute("data-f"); render(); window.scrollTo({top:0,behavior:"smooth"});
});

// 렌더링이 실패하면 빈 화면 대신 원인을 보여준다.
// 실제 그리기는 drawView 가 하고 render 는 감싸기만 한다.
// (window.render 에 래퍼를 다시 대입하면 전역 render 를 덮어써 무한 재귀가 된다)
function render(){
  try { drawView(); }
  catch(err){
    const note = document.getElementById("note");
    note.className = "note warn";
    note.textContent = "화면을 그리는 중 오류가 발생했습니다: " + err.message;
    document.getElementById("list").innerHTML =
      '<div class="empty">데이터는 수집됐지만 표시에 실패했습니다.<br>이 메시지를 그대로 알려주세요.</div>';
    throw err;
  }
}
render();
</script>
</body>
</html>
"""


# ----------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--regions", default="KR,GLOBAL")
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--per-category", type=int, default=50)
    ap.add_argument("--deep", type=int, default=40,
                    help="신작 확보용으로 훑을 인기 채널 수 (0이면 생략)")
    ap.add_argument("--channels", type=int, default=CH_REFRESH_CAP,
                    help="한 번에 갱신할 채널 정보 수 (0이면 터짐·채널 순위 생략)")
    ap.add_argument("--no-verify-shorts", action="store_true")
    ap.add_argument("--exclude", default="",
                    help="집계에서 뺄 카테고리. 예: --exclude 게임")
    ap.add_argument("--archive", type=int, default=600)
    ap.add_argument("--out", default=OUT_HTML)
    args = ap.parse_args()

    exclude = resolve_exclusions(args.exclude)
    key = os.environ.get("YOUTUBE_API_KEY", "").strip()
    if not key:
        print("YOUTUBE_API_KEY 환경변수가 없습니다.  설정방법.md 를 참고하세요.")
        sys.exit(1)

    region_map = {"KR": ("🇰🇷 한국", "KR", "KR"),
                  "GLOBAL": ("🌍 글로벌", "US", "US")}
    wanted = [r.strip().upper() for r in args.regions.split(",")
              if r.strip().upper() in region_map]

    now_ts = datetime.now(timezone.utc)
    print("순위 기준 · 인기=누적 조회수 / 터짐=평소 대비 배수 / 채널=구독자 증가")

    collected = {r: collect_region(key, region_map[r][0], r, region_map[r][1],
                                   args.per_category, args.deep,
                                   verify_shorts=not args.no_verify_shorts,
                                   exclude=exclude, archive_cap=args.archive)
                 for r in wanted}
    if not any(collected.values()):
        print("\n수집된 영상이 없습니다. python 진단.py 로 원인을 확인하세요.")
        sys.exit(1)

    # 채널 정보 갱신 (구독자 수 + 평소 조회수 기준선)
    cache = load_channel_cache()
    if args.channels > 0:
        cids, seen = [], set()
        for pool in collected.values():
            for v in sorted(pool.values(), key=lambda x: x["views"], reverse=True):
                c = v.get("channelId", "")
                if c and c not in seen:
                    seen.add(c)
                    cids.append(c)
        n = refresh_channels(key, cids, cache, now_ts, cap=args.channels)
        with_base = sum(1 for c in cache.values() if c.get("base"))
        print(f"\n채널 정보: {n}곳 갱신, 누적 {len(cache)}곳 "
              f"(기준선 확보 {with_base}곳)")
        save_channel_cache(cache)

    # 지역별 현재 구독자 수 → 스냅샷에 함께 저장
    subs_now = {}
    for pool in collected.values():
        for v in pool.values():
            c = v.get("channelId", "")
            if c in cache and cache[c].get("subs"):
                subs_now[c] = cache[c]["subs"]

    path = save_snapshot(collected, subs_now)
    pruned = prune_snapshots(now_ts)
    print(f"\n스냅샷 저장: {os.path.basename(path)}  "
          f"(보관 {len(snapshot_index())}개, 정리 {pruned}개, "
          f"총 {_UNITS} units / 일 10,000)")

    payload = {"stamp": now_ts.astimezone(KST).strftime("%Y.%m.%d %H:%M") + " 수집",
               "excluded": exclusion_label(exclude) if exclude else "",
               "regions": {}}

    tol = {"daily": 6, "weekly": 36, "monthly": 96}

    for r in wanted:
        label, _, short_label = region_map[r]
        cur = collected.get(r, {})

        def item(x, rank, period, mode):
            d = {"id": x["id"], "title": x["title"], "channel": x["channel"],
                 "views": x["views"], "vph": x["vph"],
                 "ageH": round(x["ageH"], 2), "desc": x.get("desc", ""),
                 "isShort": bool(x.get("isShort")), "dur": durlabel(x.get("dur"))}
            if mode == "breakout":
                d["ratio"] = x["ratio"]
                d["base"] = x["base"]
                d["summary"] = summary_breakout(x, rank, period)
            else:
                d["summary"] = summary_views(x, rank, period)
            return d

        views, breakout, channels = {}, {}, {}
        for period in ("daily", "weekly", "monthly"):
            vrows = compute_views(cur, now_ts, period)
            views[period] = split_by_format(
                vrows, args.top, lambda x, k: item(x, k, period, "views"))

            brows = compute_breakout(cur, cache, now_ts, period)
            breakout[period] = split_by_format(
                brows, args.top, lambda x, k: item(x, k, period, "breakout"))

            crows, base_ts = compute_channel_growth(
                subs_now, now_ts, period, cache, tol[period])
            channels[period] = {
                "rows": crows[:args.top],
                "base": base_ts.astimezone(KST).strftime("%m월 %d일 %H시") if base_ts else "",
                "tracked": len(subs_now)}

            if period == "daily":
                audit_sources(cur, vrows, f"[{label}] 인기")

        n_short = sum(1 for v in cur.values() if v.get("isShort"))
        payload["regions"][r] = {
            "label": label, "short": short_label,
            "pools": {"all": len(cur), "shorts": n_short,
                      "long": len(cur) - n_short},
            "views": views, "breakout": breakout, "channels": channels}

        print(f"\n[{label}] 일간 상위 3")
        for name, rows in (("🔥 인기", views["daily"]["all"]),
                           ("💥 터짐", breakout["daily"]["all"])):
            print(f"  · {name}")
            if not rows:
                print("      (해당 없음)")
                continue
            for i, it in enumerate(rows[:3], 1):
                extra = f"{it['ratio']}배" if "ratio" in it else f"{fmt(it['views'])}회"
                print(f"      {i}. {extra:>12}  {it['title'][:34]}")
        crows = channels["daily"]["rows"]
        print("  · 📡 채널")
        if crows:
            for i, c in enumerate(crows[:3], 1):
                print(f"      {i}. +{fmt(c['gained']):>9}명  {c['title'][:30]}")
        else:
            print("      (하루 전 기록이 아직 없습니다)")

    out_path = os.path.abspath(args.out)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(build_html(payload))
    print(f"\n완성: {out_path}")


if __name__ == "__main__":
    main()

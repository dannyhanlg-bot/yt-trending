#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
search 호출이 왜 0건인지 찾아내는 진단 스크립트.

파라미터를 하나씩 더해가며 어느 지점에서 결과가 사라지는지 확인한다.
실행:  python 진단.py
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

API = "https://www.googleapis.com/youtube/v3/"
KEY = os.environ.get("YOUTUBE_API_KEY", "").strip()

if not KEY:
    print("YOUTUBE_API_KEY 환경변수가 없습니다.")
    sys.exit(1)


def call(endpoint, **params):
    """호출 결과를 (성공여부, 건수, 메모) 로 돌려준다. 키는 절대 출력하지 않는다."""
    params["key"] = KEY
    url = API + endpoint + "?" + urllib.parse.urlencode(params)
    shown = url.replace(KEY, "***KEY***")
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            body = json.loads(r.read().decode("utf-8"))
        items = body.get("items", [])
        total = body.get("pageInfo", {}).get("totalResults", "?")
        first = ""
        if items:
            sn = items[0].get("snippet", {})
            first = sn.get("title", "") or str(items[0].get("id", ""))[:40]
        return True, len(items), f"totalResults={total}  첫 항목: {first[:46]}"
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            err = json.loads(raw)["error"]
            reason = err.get("errors", [{}])[0].get("reason", "?")
            msg = err.get("message", "")[:150]
            return False, 0, f"HTTP {e.code} / reason={reason} / {msg}"
        except Exception:
            return False, 0, f"HTTP {e.code} / {raw[:150]}"
    except Exception as e:
        return False, 0, f"예외: {type(e).__name__} {e}"
    finally:
        pass


def step(label, endpoint, **params):
    ok, n, memo = call(endpoint, **params)
    mark = "✅" if (ok and n > 0) else ("⚠️ " if ok else "❌")
    print(f"{mark} {label}")
    print(f"     건수={n}  {memo}")
    return ok and n > 0


print("=" * 66)
print(" YouTube Data API 진단")
print("=" * 66)
print(f" 키 형식: {KEY[:6]}…{KEY[-4:]}  (길이 {len(KEY)}자)")
if not KEY.startswith("AIza"):
    print(" ⚠️  일반적인 API 키는 'AIza' 로 시작합니다. 키 종류를 확인하세요.")
print()

print("[1] 기본 연결 — videoCategories (비용 1)")
step("카테고리 목록", "videoCategories", part="snippet", regionCode="KR")
print()

print("[2] 가장 단순한 검색 — 필터 없음 (비용 100)")
base = step("q=music 만", "search", part="snippet", type="video", q="music", maxResults=5)
print()

print("[3] 파라미터를 하나씩 추가")
step("+ order=viewCount", "search", part="snippet", type="video",
     q="music", order="viewCount", maxResults=5)
step("+ regionCode=KR", "search", part="snippet", type="video",
     q="music", regionCode="KR", maxResults=5)
step("+ publishedAfter (7일)", "search", part="snippet", type="video",
     q="music", publishedAfter="2026-08-01T00:00:00Z", maxResults=5)
print()

print("[4] 실제 스크립트가 쓰는 조합 (q 없음)")
step("q 없이 order=date + regionCode", "search", part="snippet", type="video",
     order="date", regionCode="KR", publishedAfter="2026-08-07T00:00:00Z", maxResults=5)
step("q 없이 order=viewCount + regionCode", "search", part="snippet", type="video",
     order="viewCount", regionCode="KR", publishedAfter="2026-08-01T00:00:00Z", maxResults=5)
step("+ videoCategoryId=10 (음악)", "search", part="snippet", type="video",
     order="viewCount", regionCode="KR", videoCategoryId="10",
     publishedAfter="2026-08-01T00:00:00Z", maxResults=5)
print()

print("[5] 대안 경로 — videos.list chart=mostPopular (비용 1, search의 1/100)")
step("mostPopular 한국 전체", "videos", part="snippet,statistics",
     chart="mostPopular", regionCode="KR", maxResults=5)
step("mostPopular 한국 + 음악(10)", "videos", part="snippet,statistics",
     chart="mostPopular", regionCode="KR", videoCategoryId="10", maxResults=5)
step("mostPopular 한국 + 게임(20)", "videos", part="snippet,statistics",
     chart="mostPopular", regionCode="KR", videoCategoryId="20", maxResults=5)
step("mostPopular 글로벌(US)", "videos", part="snippet,statistics",
     chart="mostPopular", regionCode="US", maxResults=5)
print()

print("=" * 66)
print(" 해석 가이드")
print("=" * 66)
print(" [1] 실패          → 키 또는 네트워크 문제")
print(" [1] 성공 [2] 실패 → search 자체가 막힘. 위 reason 값을 확인:")
print("                     quotaExceeded    = 할당량 소진")
print("                     accessNotConfig  = YouTube Data API v3 사용 설정 누락")
print("                     forbidden        = 키 제한(리퍼러/IP)이 걸려 있음")
print(" [2] 성공 [4] 0건  → q(검색어) 없는 검색이 안 되는 것 → [5]로 전환하면 해결")
print(" [5] 성공          → 후보 풀을 mostPopular 기반으로 다시 짜면 됨")
print("                     (비용도 1/100 로 줄고 조회수까지 한 번에 받아옴)")

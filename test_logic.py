#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
순위 계산 로직 검사 — API 호출 없이 가짜 데이터로 돌린다.

    python test_logic.py
"""

import importlib.util
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location(
    "yt", os.path.join(HERE, "youtube_trending.py"))
yt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(yt)

NOW = datetime.now(timezone.utc)
FAILED = []


def check(ok, label):
    print(("  OK   " if ok else "  FAIL ") + label)
    if not ok:
        FAILED.append(label)


def iso(d):
    return d.isoformat().replace("+00:00", "Z")


def v(title, age_h, views, ch="UCa", short=False):
    return {"title": title, "channel": "채널", "channelId": ch,
            "publishedAt": iso(NOW - timedelta(hours=age_h)),
            "views": views, "isShort": short, "dur": 30 if short else 600,
            "cat": "10", "desc": "", "src": "chart"}


# ---------------------------------------------------------------- 인기
print("\n[인기] 누적 조회수 순인가")
pool = {
    "big":  v("25일 전 대형작", 24 * 25, 20_000_000),
    "mid":  v("2일 전", 48, 5_000_000),
    "fast": v("6시간 전 폭발", 6, 2_000_000),
}
mo = [r["id"] for r in yt.compute_views(pool, NOW, "monthly")]
check(mo == ["big", "mid", "fast"], f"월간 누적 조회수 순 ({mo})")
check([r["id"] for r in yt.compute_views(pool, NOW, "daily")] == ["fast"],
      "일간은 24시간 내 업로드만")
check([r["id"] for r in yt.compute_views(pool, NOW, "weekly")] == ["mid", "fast"],
      "주간은 7일 내 업로드만")
vph = {r["id"]: r["vph"] for r in yt.compute_views(pool, NOW, "monthly")}
check(vph["fast"] > vph["big"],
      "하루평균은 fast 가 높은데도 순위는 big 이 위 (평균 정렬이 아님)")

# ---------------------------------------------------------------- 터짐
print("\n[터짐] 평소 대비 배수 순인가")
pool2 = {
    "huge":   v("대형 채널 평범한 영상", 5, 3_000_000, ch="UCbig"),
    "boom":   v("소형 채널 대박", 5, 900_000, ch="UCsmall"),
    "tiny":   v("조회수 너무 작음", 5, 3_000, ch="UCsmall"),
    "nobase": v("기준선 없는 채널", 5, 500_000, ch="UCnew"),
    "lowbase": v("기준선이 너무 낮음", 5, 400_000, ch="UClow"),
}
channels = {
    "UCbig":   {"base": 2_500_000, "baseN": 12, "subs": 10_000_000},
    "UCsmall": {"base": 30_000, "baseN": 8, "subs": 120_000},
    "UCnew":   {"base": None, "baseN": 2, "subs": 5_000},
    "UClow":   {"base": 100, "baseN": 6, "subs": 900},
}
rows = yt.compute_breakout(pool2, channels, NOW, "daily")
ids = [r["id"] for r in rows]
check(ids and ids[0] == "boom", f"소형 채널 대박이 1위 ({ids})")
check("huge" in ids and ids.index("boom") < ids.index("huge"),
      "조회수는 huge 가 3배 많지만 배수는 boom 이 위")
check("tiny" not in ids, "조회수 1만 미만은 제외 (배수가 튐)")
check("nobase" not in ids, "기준선 없는 채널은 제외")
check("lowbase" not in ids, "기준선이 너무 낮은 채널은 제외")
if rows:
    check(rows[0]["ratio"] == 30.0, f"배수 계산 정확 (900000/30000 = {rows[0]['ratio']})")

# ---------------------------------------------------------------- 채널 증가
print("\n[채널] 구독자 증가 순인가")
tmp = tempfile.mkdtemp()
yt.SNAP_DIR = tmp
base_ts = NOW - timedelta(hours=24)
json.dump({"ts": base_ts.isoformat(), "regions": {},
           "channels": {"UCa": 1_000_000, "UCb": 50_000,
                        "UCc": 700_000, "UCd": 300_000}},
          open(os.path.join(tmp, base_ts.strftime("%Y%m%d-%H%M%S") + ".json"),
               "w", encoding="utf-8"))
subs_now = {"UCa": 1_030_000,   # +30,000
            "UCb": 57_000,      # +7,000  (증가율은 더 큼)
            "UCc": 700_000,     # 변화 없음
            "UCd": 290_000,     # 감소
            "UCe": 10_000}      # 기준 기록 없음
info = {c: {"title": f"채널 {c}", "thumb": ""} for c in subs_now}
rows, bts = yt.compute_channel_growth(subs_now, NOW, "daily", info, tol_h=6)
ids = [r["id"] for r in rows]
check(ids == ["UCa", "UCb"], f"증가한 채널만, 증가량 순 ({ids})")
check(bts is not None, "기준 스냅샷을 찾음")
if rows:
    check(rows[0]["gained"] == 30_000, "증가량 계산")
    check(rows[1]["pct"] == 14.0, f"증가율 계산 ({rows[1]['pct']}%)")
rows2, bts2 = yt.compute_channel_growth(subs_now, NOW, "monthly", info, tol_h=96)
check(rows2 == [] and bts2 is None, "기준 기록이 없는 기간은 빈 목록")
shutil.rmtree(tmp)

# ---------------------------------------------------------------- 포맷/품질
print("\n[포맷] 롱폼·숏츠 두 갈래인가")
rows = [{"id": "L1", "isShort": False}, {"id": "S1", "isShort": True},
        {"id": "L2", "isShort": False}]
out = yt.split_by_format(rows, 10, lambda x, k: {"id": x["id"], "rank": k})
check(list(out.keys()) == ["long", "shorts"], f"버킷 {list(out.keys())} — 전체 없음, 롱폼 우선")
check([i["id"] for i in out["long"]] == ["L1", "L2"], "롱폼만 추출")
check([i["rank"] for i in out["shorts"]] == [1], "숏츠도 1위부터 재부여")

print("\n[저품질] 둘 이상 겹칠 때만 걸러지는가")


def q(title="평범한 제목", views=500_000, likes=15_000, comments=300):
    return {"title": title, "views": views, "likes": likes, "comments": comments}


for label, item, expect in [
    ("정상 영상", q(), False),
    ("참여율만 낮음", q(likes=200), True),
    ("댓글차단만", q(comments=None), False),
    ("제목패턴만", q(title="충격!!!"), False),
    ("제목패턴+댓글차단", q(title="충격!!!", comments=None), True),
    ("조회수 적어 판단보류", q(views=1000, likes=0), False),
]:
    score, why = yt.quality_flags(item)
    check((score >= yt.LOWQ_CUT) == expect, f"{label} → {score}점 {why}")

check(not yt.title_spam("이거 진짜 웃기다ㅋㅋㅋㅋㅋㅋ"), "한국어 ㅋㅋㅋ 는 낚시로 보지 않음")
check(yt.title_spam("🔥🔥🔥🔥🔥 대박"), "이모지 도배는 낚시로 판정")

pool_q = {"good": q(), "bad": q(likes=100, comments=None)}
kept, removed, _ = yt.apply_quality(dict(pool_q), "filter")
check(set(kept) == {"good"} and removed == 1, "filter 모드 — 제외")
marked, _, _ = yt.apply_quality(dict(pool_q), "mark")
check(set(marked) == {"good", "bad"} and marked["bad"].get("lowq"), "mark 모드 — 표시만")
untouched, rm, _ = yt.apply_quality(dict(pool_q), "off")
check(set(untouched) == set(pool_q) and rm == 0, "off 모드 — 판정 안 함")

# ---------------------------------------------------------------- 기타
print("\n[기타]")
check(yt.with_ro("숏츠") == "숏츠로" and yt.with_ro("롱폼") == "롱폼으로",
      "조사 처리 (숏츠로 / 롱폼으로)")
check(yt.parse_duration("PT2M30S") == 150 and yt.parse_duration("P0D") is None,
      "영상 길이 파싱")
d = yt.clean_description("제목\nhttps://x.com 구독하기\n본문입니다.\n00:00 인트로\n#태그")
check("http" not in d and "00:00" not in d and "본문입니다" in d,
      "설명 정리 (링크·타임스탬프·해시태그 제거)")

pool3 = {"a": v("x", 5, 100), "b": v("y", 5, 200)}
pool3["b"]["src"] = "channel"
yt.audit_sources(pool3, yt.compute_views(pool3, NOW, "daily"), "테스트")

print()
if FAILED:
    print(f"실패 {len(FAILED)}건: " + ", ".join(FAILED))
    sys.exit(1)
print("로직 검사 통과")

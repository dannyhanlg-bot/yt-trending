#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
뮤직비디오 월드컵 검사 — 곡 선정 규칙과 토너먼트 로직을 확인한다.

    python test_music.py        (node 필요)
"""

import importlib.util
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))


def load(name):
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(HERE, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


yt = load("youtube_trending")
mg = load("music_game")

NOW = datetime.now(timezone.utc)
FAILED = []


def check(ok, label):
    print(("  OK   " if ok else "  FAIL ") + label)
    if not ok:
        FAILED.append(label)


def v(title, views, cat="10", short=False, channel="가수"):
    return {"title": title, "channel": channel, "channelId": "UCx",
            "publishedAt": (NOW - timedelta(days=2)).isoformat().replace("+00:00", "Z"),
            "views": views, "isShort": short, "dur": 210,
            "cat": cat, "desc": "", "src": "chart"}


print("[곡 선정]")
pool = {
    "mv1":   v("정식 뮤직비디오 A", 5_000_000),
    "mv2":   v("정식 뮤직비디오 B", 3_000_000),
    "short": v("숏츠 버전", 9_000_000, short=True),
    "game":  v("게임 영상", 8_000_000, cat="20"),
    "topic": v("자동 생성 트랙", 7_000_000, channel="아티스트 - Topic"),
}
tracks = mg.select_music(pool, yt.compute_views, NOW)
ids = [t["id"] for t in tracks]
check(ids == ["mv1", "mv2"], f"음악·롱폼·정식채널만 ({ids})")
check("short" not in ids, "숏츠 제외")
check("game" not in ids, "음악 아닌 카테고리 제외")
check("topic" not in ids, "'- Topic' 자동 채널 제외")

items = [mg.track_item(t, i + 1) for i, t in enumerate(tracks)]
check(items[0]["rank"] == 1 and items[0]["views"] == 5_000_000, "실제 순위·조회수 보존")

print("\n[토너먼트 로직]")
JS_STUB = r"""
const vm = require("vm");
const els = {};
const mk = id => ({ id, innerHTML:"", textContent:"", className:"", style:{},
                    addEventListener(){}, closest(){ return null; },
                    getAttribute(){ return null; }, querySelector(){ return null; },
                    querySelectorAll(){ return []; } });
for (const id of ["stamp","app","backlink"]) els[id] = mk(id);
globalThis.document = { getElementById: id => els[id] || mk(id), querySelectorAll: () => [] };
globalThis.window = globalThis;

const code = require("fs").readFileSync(process.argv[2], "utf8");
try { vm.runInThisContext(code); }
catch(e){ console.log("FAIL 실행 오류: " + e.constructor.name + " - " + e.message); process.exit(1); }

const res = [];
function check(ok, label){ res.push([!!ok, label]); }

check(els.app.innerHTML.includes("시작하기"), "시작 화면 렌더링");
check(els.app.innerHTML.includes("8곡") || els.app.innerHTML.includes("한국"), "차트 선택지 표시");

// 8강을 끝까지 진행하면 7경기 뒤 우승자가 나와야 한다 (4+2+1)
startGame("KR", 8);
check(G.round.length === 8, "8강 대진 구성");
check(currentPair().length === 2, "매치마다 두 곡");
let matches = 0, champ = null;
while(!champ && matches < 50){ champ = pick(matches % 2); matches++; }
check(matches === 7, "8강은 7경기 (실제 " + matches + ")");
check(!!champ, "우승자 확정");
check(G.picks.length === 7, "선택 기록 7건");
check(G.picks[0].round === "8강" && G.picks[6].round === "결승", "라운드 이름 (8강→준결승→결승)");
check(G.picks.every(p => p.winner.id !== p.loser.id), "매 경기 승자와 패자가 다름");

// 우승자는 실제로 참가한 곡 중 하나여야 한다
const ids8 = new Set(DATA.regions.KR.tracks.map(t => t.id));
check(ids8.has(champ.id), "우승자가 후보 안에 있음");

// 결과 화면
screenResult();
check(els.app.innerHTML.includes("👑"), "우승 화면 렌더링");
check(els.app.innerHTML.includes("실제 순위"), "실제 순위 비교표");
check(els.app.innerHTML.includes("youtube-nocookie.com/embed/" + champ.id), "우승곡 임베드");

// 4강으로도 동작 (3경기)
startGame("KR", 4);
let m2 = 0, c2 = null;
while(!c2 && m2 < 20){ c2 = pick(0); m2++; }
check(m2 === 3, "4강은 3경기 (실제 " + m2 + ")");

// 곡이 없는 지역을 골라도 죽지 않아야 한다
G.region = "EMPTY"; screenStart();
check(els.app.innerHTML.length > 0, "빈 차트에서도 화면 유지");

let failed = 0;
for (const [ok, label] of res){
  console.log((ok ? "  OK   " : "  FAIL ") + label);
  if(!ok) failed++;
}
process.exit(failed ? 1 : 0);
"""


def track(i, views):
    return {"id": f"vid{i:02d}", "title": f"곡 {i}", "channel": f"가수 {i}",
            "views": views, "rank": i}


payload = {
    "stamp": "2026.01.01 00:00 수집",
    "regions": {
        "KR": {"label": "🇰🇷 한국",
               "tracks": [track(i, 10_000_000 - i * 100_000) for i in range(1, 17)]},
        "EMPTY": {"label": "빈 차트", "tracks": []},
    },
}

html = mg.build_music_html(payload)
m = re.search(r"<script>(.*?)</script>", html, re.S)
if not m:
    print("FAIL: script 블록 없음")
    sys.exit(1)

tmp = tempfile.mkdtemp()
js, stub = os.path.join(tmp, "page.js"), os.path.join(tmp, "stub.js")
open(js, "w", encoding="utf-8").write(m.group(1))
open(stub, "w", encoding="utf-8").write(JS_STUB)

try:
    r = subprocess.run(["node", stub, js], capture_output=True, text=True, timeout=60)
    print(r.stdout.strip())
    if r.stderr.strip():
        print(r.stderr.strip()[:400])
    if r.returncode:
        FAILED.append("토너먼트 JS")
except FileNotFoundError:
    print("  node 없음 — JS 검사 건너뜀")

print(f"\nHTML {len(html):,} bytes")
if FAILED:
    print(f"실패 {len(FAILED)}건: " + ", ".join(FAILED))
    sys.exit(1)
print("뮤직 월드컵 검사 통과")

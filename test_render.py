#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
화면 렌더링 검사 — API 호출 없이 돌아간다.

가짜 데이터로 HTML을 만들고, 그 안의 자바스크립트를 브라우저와 같은 조건
(window === 전역 객체, 최상위 스크립트)에서 실제로 실행해 본다.

'pool is not defined' 나 무한 재귀 같은 오류는 파이썬 문법 검사로는 잡히지 않고
브라우저에서만 터진다. 그러면 화면이 조용히 비어버린다.

    python test_render.py        (node 필요)
"""

import importlib.util
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location(
    "yt", os.path.join(HERE, "youtube_trending.py"))
yt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(yt)


def vid(v, is_short, ratio=None, lowq=None):
    d = {"id": v, "title": f"제목 {v}", "channel": "채널명",
         "views": 1234567, "vph": 5000, "ageH": 10.0,
         "isShort": is_short, "dur": "0:45",
         "desc": "설명입니다", "summary": "수치 요약"}
    if ratio is not None:
        d["ratio"] = ratio
        d["base"] = 50000
    if lowq:
        d["lowq"] = lowq
    return d


def bucket(items):
    return {"long": [x for x in items if not x["isShort"]],
            "shorts": [x for x in items if x["isShort"]]}


def periods(items):
    return {p: bucket(items) for p in ("daily", "weekly", "monthly")}


CHANNEL_ROWS = [
    {"id": "UC1", "title": "채널 하나", "thumb": "", "subs": 1200000,
     "gained": 30000, "pct": 2.56},
    {"id": "UC2", "title": "채널 둘", "thumb": "", "subs": 45000,
     "gained": 5000, "pct": 12.5},
]

PAYLOAD = {
    "stamp": "2026.01.01 00:00 수집",
    "excluded": "게임",
    "regions": {
        "KR": {
            "label": "🇰🇷 한국", "short": "KR",
            "pools": {"shorts": 560, "long": 133},
            "chanStats": {"registered": 1840, "withBase": 920, "scanned": 250},
            "views": periods([vid("A", True), vid("B", False),
                              vid("F", False, lowq="참여율 낮음, 댓글 차단")]),
            "breakout": periods([vid("C", False, ratio=18.4),
                                 vid("D", True, ratio=7.1)]),
            "channels": {
                "daily": {"rows": CHANNEL_ROWS, "base": "01월 01일 00시", "tracked": 312},
                "weekly": {"rows": CHANNEL_ROWS, "base": "12월 25일 00시", "tracked": 312},
                # 월간은 아직 기준 기록이 없는 상태를 재현
                "monthly": {"rows": [], "base": "", "tracked": 312},
            },
        },
        "GLOBAL": {
            "label": "🌍 글로벌", "short": "US",
            "pools": {"shorts": 300, "long": 200},
            "views": periods([vid("E", True)]),
            "breakout": periods([]),
            "channels": {p: {"rows": [], "base": "", "tracked": 0}
                         for p in ("daily", "weekly", "monthly")},
        },
    },
}

DOM_STUB = r"""
const vm = require("vm");

const els = {};
const mk = id => ({ id, innerHTML:"", textContent:"", className:"", style:{},
                    dataset:{}, addEventListener(fn){}, closest(){ return null; },
                    getAttribute(){ return null; } });
for (const id of ["stamp","exnote","reg","viewsel","persel","fmt","note","list"]) els[id] = mk(id);

globalThis.document = { getElementById: id => els[id] || mk(id), querySelectorAll: () => [] };
// 브라우저에서 window 는 전역 객체 그 자체다. 별개 객체로 두면
// window.foo 대입이 전역 함수 foo 를 덮어쓰는 상황이 재현되지 않아
// 무한 재귀 같은 버그를 놓친다.
globalThis.window = globalThis;
globalThis.scrollTo = () => {};

const code = require("fs").readFileSync(process.argv[2], "utf8");
try {
  // 최상위 스크립트로 실행해야 함수 선언이 전역에 붙는다 (new Function 은 지역 스코프)
  vm.runInThisContext(code);
} catch (e) {
  console.log("FAIL 실행 오류: " + e.constructor.name + " - " + e.message);
  process.exit(1);
}

const out = { checks: [] };
function check(ok, label){ out.checks.push([!!ok, label]); }

// 1) 기본 화면 = 인기 / 일간 / 롱폼
let list = els.list.innerHTML, note = els.note.innerHTML;
check(FORMAT === "long", "기본 포맷이 롱폼");
check(!els.fmt.innerHTML.includes("전체"), "'전체' 선택지 없음");
check(els.fmt.innerHTML.indexOf("롱폼") < els.fmt.innerHTML.indexOf("숏츠"),
      "롱폼이 숏츠보다 앞");
check((list.match(/class="card"/g) || []).length >= 2, "인기 탭 카드 렌더링");
check(list.includes("제목 B"), "롱폼 영상이 목록에 있음");
check(!list.includes("제목 A"), "숏츠 영상은 롱폼 탭에 없음");
check(note.includes("133"), "안내문에 롱폼 후보 수");
check(note.includes("누적 조회수"), "인기 탭 순위 기준 안내");
check(list.includes("설명입니다"), "영상 설명 블록");
check(list.includes("123만"), "조회수 축약 표기");
check(list.includes("youtube.com/watch"), "원본 링크");
check(list.includes("참여율 낮음"), "저품질 표시 칩");
check(els.stamp.textContent.includes("게임"), "제외 카테고리 표기");
check(els.reg.innerHTML.includes("KR") && els.reg.innerHTML.includes("US"), "지역 토글");

// 1b) 숏츠 탭으로 전환
FORMAT = "shorts"; render();
check(els.list.innerHTML.includes("제목 A"), "숏츠 탭 전환");
check(els.note.innerHTML.includes("560"), "안내문에 숏츠 후보 수");
FORMAT = "long"; render();

// 2) 터짐 탭
VIEW = "breakout"; render();
list = els.list.innerHTML; note = els.note.innerHTML;
check(list.includes("18.4배"), "터짐 배수 배지");
check(note.includes("평소 받던 조회수"), "터짐 탭 안내문");
check(note.includes("1,840") && note.includes("250"), "터짐 탭에 등록·훑기 채널 수 표기");
check(!list.includes("▶ 123만</div>") || list.includes("💥"), "터짐 배지가 조회수 배지를 대체");

// 3) 채널 탭 — 기록 있는 기간
VIEW = "channels"; PERIOD = "daily"; render();
list = els.list.innerHTML; note = els.note.innerHTML;
check(list.includes("채널 하나"), "채널 행 렌더링");
check(list.includes("+3만") || list.includes("30,000"), "구독자 증가량 표기");
check(list.includes("youtube.com/channel/UC1"), "채널 링크");
check(els.fmt.style.display === "none", "채널 탭에서는 숏츠/롱폼 칩 숨김");
check(note.includes("312"), "추적 채널 수 표기");

// 4) 채널 탭 — 기록 없는 기간은 안내만
PERIOD = "monthly"; render();
check(els.note.className.includes("warn"), "기준 기록 없을 때 경고 표시");
check(els.list.innerHTML === "", "기준 기록 없을 때 목록 비움");

// 5) 데이터가 빈 지역으로 바꿔도 죽지 않아야 한다
REGION = "GLOBAL"; VIEW = "breakout"; PERIOD = "daily"; render();
check(els.list.innerHTML.includes("조건에 맞는"), "빈 목록 안내");

let failed = 0;
for (const [ok, label] of out.checks) {
  console.log((ok ? "  OK   " : "  FAIL ") + label);
  if (!ok) failed++;
}
process.exit(failed ? 1 : 0);
"""


def main():
    html = yt.build_html(PAYLOAD)
    m = re.search(r"<script>(.*?)</script>", html, re.S)
    if not m:
        print("FAIL: HTML에서 script 블록을 찾지 못했습니다")
        return 1

    tmp = tempfile.mkdtemp()
    js_path = os.path.join(tmp, "page.js")
    stub_path = os.path.join(tmp, "stub.js")
    with open(js_path, "w", encoding="utf-8") as f:
        f.write(m.group(1))
    with open(stub_path, "w", encoding="utf-8") as f:
        f.write(DOM_STUB)

    print(f"HTML {len(html):,} bytes / script {len(m.group(1)):,} bytes")
    try:
        r = subprocess.run(["node", stub_path, js_path],
                           capture_output=True, text=True, timeout=60)
    except FileNotFoundError:
        print("node 를 찾을 수 없어 렌더링 검사를 건너뜁니다.")
        return 0

    print(r.stdout.strip())
    if r.stderr.strip():
        print(r.stderr.strip()[:500])
    if r.returncode:
        print("\n렌더링 검사 실패 — 이대로 배포하면 화면이 깨집니다.")
        return 1
    print("\n렌더링 검사 통과")
    return 0


if __name__ == "__main__":
    sys.exit(main())

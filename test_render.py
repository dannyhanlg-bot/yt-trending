#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
화면 렌더링 검사 — API 호출 없이 돌아간다.

가짜 데이터로 HTML을 만들고, 그 안의 자바스크립트를 최소 DOM 스텁 위에서
실제로 실행해 카드가 그려지는지 확인한다.

'pool is not defined' 같은 ReferenceError 는 파이썬 문법 검사로는 잡히지 않고
브라우저에서만 터진다. 그러면 화면이 조용히 비어버린다.
이 검사는 그런 실수가 배포되는 것을 막는다.

    python test_render.py        (node 필요)
"""

import importlib.util
import json
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


def sample(vid, is_short):
    return {"id": vid, "title": f"제목 {vid}", "channel": "채널명",
            "views": 1234567, "vph": 5000, "ageH": 10.0,
            "isShort": is_short, "dur": "0:45",
            "desc": "설명입니다", "summary": "수치 요약"}


PAYLOAD = {
    "stamp": "2026.01.01 00:00 수집 (KST)",
    "excluded": "게임",
    "regions": {
        "KR": {"label": "🇰🇷 한국",
               "pools": {"all": 693, "shorts": 560, "long": 133},
               "daily": {"all": [sample("A", True), sample("B", False)],
                         "shorts": [sample("A", True)],
                         "long": [sample("B", False)]},
               "weekly": {"all": [sample("C", False)], "shorts": [],
                          "long": [sample("C", False)]},
               "monthly": {"all": [sample("D", False)], "shorts": [],
                           "long": [sample("D", False)]}},
        "GLOBAL": {"label": "🌍 글로벌",
                   "pools": {"all": 500, "shorts": 300, "long": 200},
                   "daily": {"all": [sample("E", True)],
                             "shorts": [sample("E", True)], "long": []},
                   "weekly": {"all": [], "shorts": [], "long": []},
                   "monthly": {"all": [], "shorts": [], "long": []}}},
}

DOM_STUB = r"""
const els = {};
const mk = id => ({ id, innerHTML:"", textContent:"", className:"",
                    dataset:{}, addEventListener(){}, closest(){ return null; } });
for (const id of ["stamp","exnote","tabs","fmt","note","list"]) els[id] = mk(id);
global.document = { getElementById: id => els[id] || mk(id), querySelectorAll: () => [] };
global.window = { scrollTo(){} };

const code = require("fs").readFileSync(process.argv[2], "utf8");
try { new Function(code)(); }
catch (e) {
  console.log("FAIL 실행 오류: " + e.constructor.name + " - " + e.message);
  process.exit(1);
}

const list = els.list.innerHTML;
const note = els.note.innerHTML;
const cards = (list.match(/class="card"/g) || []).length;

const checks = [
  [cards >= 2,                      "카드가 2장 이상 그려짐 (실제 " + cards + "장)"],
  [note.includes("693"),            "안내문에 후보 수 표시"],
  [note.includes("누적 조회수"),      "안내문에 순위 기준 표시"],
  [list.includes("제목 A"),          "카드 제목"],
  [list.includes("설명입니다"),       "영상 설명 블록"],
  [list.includes("수치 요약"),        "수치 요약"],
  [list.includes("123만"),           "조회수 축약 표기"],
  [list.includes("youtube.com/watch"), "원본 링크"],
  [list.includes("i.ytimg.com"),     "썸네일"],
  [els.fmt.innerHTML.includes("560"), "숏츠/롱폼 칩"],
  [els.stamp.textContent.includes("게임"), "제외 카테고리 표기"],
];

let failed = 0;
for (const [ok, label] of checks) {
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
        print(r.stderr.strip())
    if r.returncode:
        print("\n렌더링 검사 실패 — 이대로 배포하면 화면이 비어 보입니다.")
        return 1
    print("\n렌더링 검사 통과")
    return 0


if __name__ == "__main__":
    sys.exit(main())

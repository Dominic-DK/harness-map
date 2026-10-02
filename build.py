#!/usr/bin/env python3
"""harness.json 을 화면 템플릿에 넣어 브라우저로 바로 열 수 있는 HTML 을 만든다.

사용:
  python3 build.py [harness.json] [-o harness-map.html]
"""
import argparse
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent

ap = argparse.ArgumentParser()
ap.add_argument("data", nargs="?", default="harness.json", help="harness-map.py 로 뽑은 JSON (기본 harness.json)")
ap.add_argument("-o", "--out", default="harness-map.html")
ap.add_argument("--artifact", action="store_true", help="doctype 없이 본문만 출력 (claude.ai 아티팩트 게시용)")
a = ap.parse_args()

template = (HERE / "template.html").read_text(encoding="utf-8")
if "__HARNESS_DATA__" not in template:
    raise SystemExit("template.html 에 데이터 자리 표시(__HARNESS_DATA__)가 없습니다.")
data = json.dumps(json.loads(Path(a.data).read_text(encoding="utf-8")), ensure_ascii=False, separators=(",", ":"))
body = template.replace("__HARNESS_DATA__", data.replace("</", "<\\/"))  # script 태그가 중간에 닫히지 않게

if a.artifact:
    page = body
else:
    page = ('<!doctype html>\n<html lang="ko"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"></head>\n<body>\n'
            + body + "\n</body></html>\n")
Path(a.out).write_text(page, encoding="utf-8")
print(f"{a.out} 를 만들었습니다 ({len(page)//1024}KB). 브라우저로 여세요.")

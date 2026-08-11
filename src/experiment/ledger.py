"""원장(Ledger) — 실험은 로그가 곧 결과물.

[What] 매일의 컨텍스트·결정(근거 포함)·체결·손익을 JSONL로 남긴다.
[Why]  재현성·복기(LLM이 왜 그때 팔았나)·블로그/포트폴리오 자산. 이게 실험의 핵심 산출물.
[How]  한 줄 = 하루. 나중에 pandas 없이도 json으로 읽어 분석 가능.
"""
from __future__ import annotations

import json
import os
from datetime import datetime


class Ledger:
    def __init__(self, path: str) -> None:
        self.path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # 새 실험마다 파일 초기화
        open(self.path, "w").close()
        self.rows: list[dict] = []

    def record_day(self, day: dict) -> None:
        self.rows.append(day)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(day, ensure_ascii=False, default=_json_default) + "\n")


def _json_default(o):  # datetime 등 직렬화
    if isinstance(o, datetime):
        return o.isoformat()
    return str(o)

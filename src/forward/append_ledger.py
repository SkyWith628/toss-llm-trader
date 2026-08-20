"""AppendLedger — 매일 한 줄씩 누적하는 append 전용 원장.

[What] 하루치 결정·체결·자산 스냅샷을 JSONL로 이어붙인다.
[Why]  기존 experiment.Ledger는 __init__에서 파일을 초기화(open 'w')한다 — 백테스트엔 맞지만
       하루 1회 프로세스인 포워드 테스트에선 매일 누적분을 지워버린다. 그래서 append 전용을 분리.
[How]  파일을 열 때 절대 비우지 않고(open 'a') 한 줄씩 추가. 디렉터리는 필요 시 생성.
"""
from __future__ import annotations

import json
import os


class AppendLedger:
    def __init__(self, path: str) -> None:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self.path = path

    def append(self, row: dict) -> None:
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")

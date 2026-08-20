"""Forward 페이퍼 테스트 — 하루 1회 실시간 모의매매로 상태를 누적해간다.

백테스트(src.main)는 한 프로세스에서 전 구간을 루프하지만, 포워드 테스트는
'하루 1회 프로세스'라 상태(포트폴리오·메모리·Buy&Hold 기준점)를 디스크에 지속해야 한다.
- state.py         : 프로세스 간 지속되는 실험 상태(JSON, 원자적 저장)
- append_ledger.py : 매일 한 줄씩 누적하는 append 전용 원장
- runner.py        : 하루 1회 오케스트레이션 + 엔트리포인트(python -m src.forward.runner)
"""

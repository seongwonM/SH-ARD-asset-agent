"""
실험 실행 단위 로깅.

exp 폴더
--------
실행할 때마다 base_dir 아래 exp{N}_{KST 타임스탬프} 폴더를 새로 만든다.
그 안에 run.log(시작/종료 배너 + 호출·에이전트 흐름별 로그)와 결과 파일이
같이 들어가므로, exp 폴더 하나만 옮기면 로그와 결과가 함께 딸려온다.

Job 재시도 시에도 새 exp 폴더가 생긴다(이어달리기 없음) — 의도된 트레이드오프.
"""

from __future__ import annotations

import logging
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict

KST = timezone(timedelta(hours=9))
_EXP_DIR_RE = re.compile(r"^exp(\d+)_")


def new_exp_dir(base_dir: Path) -> Path:
    """base_dir 아래 다음 exp{N}_{KST now} 폴더를 만들고 반환한다."""
    base_dir.mkdir(parents=True, exist_ok=True)
    last_n = 0
    for child in base_dir.iterdir():
        if not child.is_dir():
            continue
        m = _EXP_DIR_RE.match(child.name)
        if m:
            last_n = max(last_n, int(m.group(1)))

    now = datetime.now(KST)
    exp_dir = base_dir / f"exp{last_n + 1}_{now:%Y%m%d%H%M}"
    exp_dir.mkdir(parents=True, exist_ok=False)
    return exp_dir


def setup_logging(exp_dir: Path, name: str = "agent") -> logging.Logger:
    """stdout(kubectl logs용) + exp_dir/run.log(영구 저장) 양쪽에 찍는 로거를 구성한다."""
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for h in list(logger.handlers):
        logger.removeHandler(h)

    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    file_handler = logging.FileHandler(exp_dir / "run.log", encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger


def _log_kv_block(logger: logging.Logger, title: str, data: Dict[str, Any]) -> None:
    logger.info("=" * 20 + f" {title} " + "=" * 20)
    for key, value in data.items():
        logger.info("  %s: %s", key, value)
    logger.info("=" * (42 + len(title)))


def log_start(logger: logging.Logger, metadata: Dict[str, Any]) -> None:
    _log_kv_block(logger, "EXPERIMENT START", metadata)


def log_end(logger: logging.Logger, summary: Dict[str, Any]) -> None:
    _log_kv_block(logger, "EXPERIMENT END", summary)

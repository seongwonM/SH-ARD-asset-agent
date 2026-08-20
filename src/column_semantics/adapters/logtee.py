"""표준출력을 파일에도 같이 쓴다.

배치로 돌 때 각 실행의 로그가 그 실행의 결과 폴더 안에 남아야 한다 - 결과만
있고 로그가 없으면 "왜 이렇게 나왔는지"를 되짚을 수 없고, 로그가 다른 곳에
쌓이면 어느 실행 것인지 맞춰봐야 한다.

파일을 여는 일이라 adapters에 있다. 파이프라인은 이걸 알지 못하고, 평소처럼
print만 한다.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, TextIO


class _Tee:
    def __init__(self, stream: TextIO, sink: TextIO):
        self._stream = stream
        self._sink = sink

    def write(self, text: str) -> int:
        self._sink.write(text)
        self._sink.flush()
        return self._stream.write(text)

    def flush(self) -> None:
        self._sink.flush()
        self._stream.flush()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


@contextmanager
def tee_output(path: Path) -> Iterator[None]:
    """이 블록 안의 stdout/stderr를 path에도 남긴다(예외 트레이스백 포함).

    스레드에서 찍는 것도 같이 담긴다 - sys.stdout을 갈아끼우기 때문이다.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as sink:
        saved_out, saved_err = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = _Tee(saved_out, sink), _Tee(saved_err, sink)
        try:
            yield
        finally:
            sys.stdout, sys.stderr = saved_out, saved_err

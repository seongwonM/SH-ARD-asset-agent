"""`python -m column_semantics <csv>` 진입점.

예전에는 레포 루트의 run.py가 진입점이었다. k8s Job과 업로드 스크립트가 그
경로를 잡고 있어서 얇은 shim을 남겨뒀었는데, 파일을 열어봐도 아무것도 없어
구조만 헷갈리게 했다. 지금은 패키지가 곧 진입점이다.
"""

from column_semantics.cli import main

if __name__ == "__main__":
    main()

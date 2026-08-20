FROM python:3.11-slim

# 이 이미지가 어느 커밋으로 구워졌는지. 로그만 보고 "내가 방금 고친 코드가
# 도는 게 맞나"를 확인할 수 있어야 한다(CI가 --build-arg로 넣는다).
ARG GIT_SHA=unknown

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app:/app/src \
    BUILD_SHA=$GIT_SHA

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt

# run.py(진입점) + src(구현) + prompts(고정 단계) + skills(보완)가 한 세트다.
# 넷 중 하나라도 빠지면 컨테이너 안에서 아무것도 못 돈다 - prompts가 없으면
# 시작조차 못 하고, skills가 없으면 gap 보완에서 죽는다.
# experiments는 배치/강건성 Job이 쓴다.
COPY run.py ./
COPY src ./src
COPY prompts ./prompts
COPY skills ./skills
COPY experiments ./experiments

RUN printf '%s' "$GIT_SHA" > /app/BUILD_SHA

CMD ["sleep", "infinity"]

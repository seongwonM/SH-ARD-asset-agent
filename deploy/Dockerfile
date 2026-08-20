FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app:/app/src

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

CMD ["sleep", "infinity"]

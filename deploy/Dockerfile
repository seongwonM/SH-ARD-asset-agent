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

# run.py(진입점) + src(구현) + skills(프롬프트)가 한 세트다. 셋 중 하나라도
# 빠지면 컨테이너 안에서 아무것도 못 돈다. experiments는 배치/강건성 Job이 쓴다.
COPY run.py ./
COPY src ./src
COPY skills ./skills
COPY experiments ./experiments

CMD ["sleep", "infinity"]

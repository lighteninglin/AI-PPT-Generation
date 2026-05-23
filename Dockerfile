# ── PPT-Web Dockerfile ──
# 适用于离线内网部署，构建时使用国内镜像源
# ──────────────────────────────────────────────

FROM python:3.11-slim-bookworm

ENV LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai

# 国内镜像源
RUN set -ex && \
    if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
      sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list.d/debian.sources; \
      sed -i 's|security.debian.org|mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list.d/debian.sources; \
    else \
      sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list; \
      sed -i 's|security.debian.org|mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list; \
    fi

# 系统依赖：cairo(svglib需要)、字体(中文支持)
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      libcairo2 libffi-dev \
      fonts-wqy-zenhei fonts-wqy-microhei fonts-noto-cjk-extra \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ── 第一层：Python 依赖（缓存优化）──
COPY backend/requirements.txt /app/backend/requirements.txt
COPY skills/ppt-master/requirements.txt /tmp/skills-requirements.txt

RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple \
      -r /app/backend/requirements.txt \
    && pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple \
      -r /tmp/skills-requirements.txt \
    || { echo "清华源失败，尝试默认源"; \
         pip install --no-cache-dir -r /app/backend/requirements.txt \
      && pip install --no-cache-dir -r /tmp/skills-requirements.txt; }

# ── 第二层：应用代码 ──
COPY backend/      /app/backend/
COPY skills/       /app/skills/
COPY frontend/     /app/frontend/
COPY projects/     /app/projects/

RUN mkdir -p /app/projects

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health')" || exit 1

CMD ["python", "backend/run.py", "--host", "0.0.0.0", "--port", "8000", "--no-reload"]

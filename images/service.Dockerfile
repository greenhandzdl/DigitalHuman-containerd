# syntax=docker/dockerfile:1
# 康养后端服务（FastAPI + SQLAlchemy + Alembic + APScheduler）。
# 上下文同样是项目根： docker build -f containerd/images/service.Dockerfile -t dh-service:local .
#
# 不改 service 仓库任何代码：依赖清单镜像它自己的 pyproject.toml，
# 但不走 `pip install .`（该仓库无 [build-system] 且有 5 个顶层目录，
# setuptools flat-layout 会直接报错）。运行期以 WORKDIR=/app 的包名导入。
ARG BASE_IMAGE=docker.m.daocloud.io/python:3.12-slim
FROM ${BASE_IMAGE} AS runtime

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple \
    PIP_TRUSTED_HOST=mirrors.aliyun.com \
    TZ=Asia/Shanghai

# pymysql/SQLAlchemy 是纯 Python，但 cryptography 需要构建工具链兜底（有轮子时不编译）
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc libpq5 curl ca-certificates patch \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 依赖清单单独先拷，保证改业务代码不会击穿 pip 层缓存。
COPY containerd/overlay/service/requirements-docker.txt /tmp/requirements-docker.txt
RUN pip install --no-cache-dir -r /tmp/requirements-docker.txt

COPY service/ /app/

# 补丁层：containerd/patches/service/*.patch 在构建时 `patch -p1` 到 /app，
# service 仓库工作树保持零改动（详见 images/fay.Dockerfile 的同名段落）。
COPY containerd/patches/service/ /tmp/patches/
RUN set -eux; \
    for p in /tmp/patches/*.patch; do \
        [ -e "$p" ] || continue; \
        echo "[patch] $(basename "$p")"; \
        patch -p1 -d /app --no-backup-if-mismatch --silent < "$p"; \
    done; \
    rm -rf /tmp/patches

# 只暴露容器内端口，宿主映射交给 compose
EXPOSE 8000

# 先跑迁移再起服务：service 仓库自带 9 个 alembic 版本，建表完全依赖它。
CMD ["sh", "-c", "python -m alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]

# ============================================================ test 目标（可选）
# `--target test` / compose `build.target: test` 多装一层 pytest。
# tests 与补丁都在 runtime 阶段就烧进镜像（.dockerignore 已放行 service/tests），
# 所以 test 阶段不再 bind mount 宿主 tests —— 一挂就会盖掉 patches/service 的补丁。
FROM runtime AS test

COPY containerd/overlay/service/requirements-test.txt /tmp/requirements-test.txt
RUN pip install --no-cache-dir -r /tmp/requirements-test.txt

WORKDIR /app
CMD ["python", "-m", "pytest", "-q"]

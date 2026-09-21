# syntax=docker/dockerfile:1
# Fay 数字人框架 —— 无音频/无桌面环境的容器化构建。
#
# 构建上下文是项目根目录（不是 fay/），因此不在镜像里写死任何本地仓库路径：
#   docker build -f containerd/images/fay.Dockerfile -t dh-fay:local .
#
# 关键约束：不改动 fay 仓库的任何代码。所有环境差异都靠
#   1) 这一层精简依赖（requirements-docker.txt 来自 containerd/overlay/）
#   2) 运行期 volume 覆盖 /app/system.conf 与 /app/config.json
# 解决。system.conf 在仓库里根本不存在（上游已 gitignore），所以它是必须提供的覆盖文件。

ARG BASE_IMAGE=docker.m.daocloud.io/python:3.12-slim
FROM ${BASE_IMAGE}

# Python 3.12 是硬要求：main.py 顶层 `import audioop`，该模块在 3.13 已被移除。
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple \
    PIP_TRUSTED_HOST=mirrors.aliyun.com \
    TZ=Asia/Shanghai

# portaudio: pyaudio 在 fay_booter.py / gui/flask_server.py 是模块级 import，必须能装上；
#            容器里没有声卡，只要 import 成功即可（record.enabled=false 不会打开设备）。
# ffmpeg:    ms_tts_sdk.convert_mp3_to_wav 用 pydub 转 edge_tts 的输出。
# gcc/libasound2: pyaudio 需源码编译（PyPI 无 manylinux 轮子）。
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc g++ make libportaudio2 portaudio19-dev libasound2-dev \
        ffmpeg libsndfile1 curl ca-certificates patch \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 源码就是 ../fay 那一份（chuan918/Fay，上游 v4.8.1 的直接后代）。
# 曾经还有第二份「上游直出」参照实例（origin_fay，靠 FAY_SRC/PATCH_DIR 换目录），
# fork 与上游的 Python 源码已逐字节相同、对照只剩 config 差别，那个实例已撤掉，
# 三个 ARG 一起删了。想再看上游原样跑一遍：`git -C ../fay worktree add ../origin_fay upstream/main`。

# 依赖清单单独先拷，保证改业务代码不会击穿 pip 层缓存。
COPY containerd/overlay/fay/requirements-docker.txt /tmp/requirements-docker.txt
RUN pip install --no-cache-dir -r /tmp/requirements-docker.txt

# 原样拷贝上游代码。
COPY fay/ /app/

# ---- 补丁层 ---------------------------------------------------------------
# 发现上游代码有 bug 时，补丁写在 containerd/patches/<repo>/*.patch，
# 构建时用 `patch -p1` 盖到 /app 上；上游仓库的工作树保持零改动，
# `git -C <repo> status` 永远是干净的。目录里只有 .gitkeep 时循环空转。
COPY containerd/patches/fay/ /tmp/patches/
RUN set -eux; \
    for p in /tmp/patches/*.patch; do \
        [ -e "$p" ] || continue; \
        echo "[patch] $(basename "$p")"; \
        patch -p1 -d /app --no-backup-if-mismatch --silent < "$p"; \
    done; \
    rm -rf /tmp/patches
# -------------------------------------------------------------------------

# 这些目录在运行期被写（sqlite 记忆库、日志、音频、配置缓存），
# 在 compose 里挂成 volume；先建目录并交给运行用户。
RUN mkdir -p /app/memory /app/logs /app/samples /app/cache_data

EXPOSE 5000 10002 10003 10001 9001 5010 8765

# 注意：不传 -config_center。该参数会强制走远程配置中心（现已不可达），
# 去掉它 Fay 才回落到本地 system.conf + config.json。
CMD ["python", "main.py", "start"]

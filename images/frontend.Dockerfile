# syntax=docker/dockerfile:1
# CareEcho H5 前端（伙伴方仓库 gitee.com/xie-zha-zha/carecho_final，以 submodule 挂在 ../frontend）。
# 上下文同样是项目根： docker build -f containerd/images/frontend.Dockerfile -t dh-frontend:local .
#
# 不改他们仓库任何一行：在他们自己的 package-lock.json 上 npm ci，构建产物 dist-h5
# 交给 containerd/frontend/carecho_web.py 这个外壳同源托管。
#
# 为什么必须有个外壳：那份工程只有 `npm run dev` 时靠 vite 的 proxy 把 /api 转到它假想的
# 127.0.0.1:5001（vite.config.js 里那三条），`npm run build` 出来的静态产物没有这层代理，
# /api/chat/send 直接 404。所以托管方得同时发静态文件 + 顶起 /api，二合一就是 carecho_web.py。
# 两阶段的价值在于：node 只在构建阶段（~1GB），最终镜像是那个 120MB 的 python-slim。
ARG NODE_IMAGE=docker.m.daocloud.io/node:22-alpine
ARG BASE_IMAGE=docker.m.daocloud.io/python:3.12-slim

# ============================================================ build（vite 产物）
FROM ${NODE_IMAGE} AS build
WORKDIR /build
ENV npm_config_registry=https://registry.npmmirror.com \
    npm_config_update_notifier=false \
    npm_config_fund=false \
    npm_config_audit=false

# 清单与锁文件先拷：改前端源码不会击穿依赖层（和 pip 那几份 Dockerfile 同一手法）。
# 用 npm ci 而不是 npm install：有锁文件时它按锁装，产物可复现。
COPY frontend/careecho-h5/package.json frontend/careecho-h5/package-lock.json ./
RUN npm ci

COPY frontend/careecho-h5/ ./
# 魔珐（Xmov）数字人的三个参数是**构建期**内联进产物的（代码里读 import.meta.env.VITE_XMOV_*，
# 见 src/api/xmov.js），运行期换 env 不生效 —— 这是那份前端的既成事实，不是本容器的选择。
# 默认留空：产物走它自己的「缺少 Xmov APP_ID」分支，数字人区域显示占位错误、聊天照常。
#
# 实测（2026-09-21，本机 vite 6.4.3）：只给 APP_ID 不给 APP_SECRET 时产物里连那个 ID 都搜不到
# —— xmov.js:79 那个 `if (!XMOV_APP_ID || !XMOV_APP_SECRET) { reject; return }` 被常量折叠成
# 永真分支，return 之后的初始化代码整段被摇掉（122.39kB）。两个都给才留得下（126.77kB，
# SENTINELID/SENTINELSEC/网关域名三个串都能 grep 到）。所以这个开关要传就两个一起传。
#
# 另外两条后果要先想清楚再传：密钥会被打进浏览器要下载的那份 JS 里（魔珐这个 SDK 的用法
# 决定的，不是我们选的），并且 ARG 值会留在镜像 history 中（BuildKit 构建时就警告过）。
# 带密钥的产物别往公开仓库推。
ARG VITE_XMOV_APP_ID=
ARG VITE_XMOV_APP_SECRET=
ARG VITE_XMOV_GATEWAY=https://nebula-agent.xingyun3d.com/user/v1/ttsa/session
ENV VITE_XMOV_APP_ID=${VITE_XMOV_APP_ID} \
    VITE_XMOV_APP_SECRET=${VITE_XMOV_APP_SECRET} \
    VITE_XMOV_GATEWAY=${VITE_XMOV_GATEWAY}
# 产物名 dist-h5 是他们 vite.config.js 里 build.outDir 定的，不是这里选的
RUN npm run build && test -f dist-h5/index.html

# ============================================================ runtime（静态 + /api 外壳）
FROM ${BASE_IMAGE} AS runtime
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai \
    CARECHO_STATIC=/app/dist \
    CARECHO_PORT=8080

# curl 只为 compose 的 healthcheck 存在（backend 镜像装它的理由相同）
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY containerd/frontend/carecho_web.py /app/carecho_web.py
COPY --from=build /build/dist-h5 /app/dist

EXPOSE 8080
CMD ["python", "/app/carecho_web.py"]

# syntax=docker/dockerfile:1
# yueshen_rag —— fay/mcp_servers/yueshen_rag 的独立容器镜像。
#
# 构建上下文是项目根目录（与 fay.Dockerfile 同一个约定，不在镜像里写死本地仓库路径）：
#   docker build -f containerd/images/yueshen_rag.Dockerfile -t dh-yueshen-rag:local .
#
# 上游代码零改动：transport 开关是构建期 `patch -p1` 盖上去的
# （containerd/patches/yueshen_rag/），依赖清单在 containerd/overlay/yueshen_rag/。
# 为什么单开一个镜像而不是把 chromadb 塞进 Fay 镜像，见那份 requirements 顶部的说明。

ARG BASE_IMAGE=docker.m.daocloud.io/python:3.12-slim
FROM ${BASE_IMAGE}

ENV PYTHONUNBUFFERED=1 \
    PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple \
    PIP_TRUSTED_HOST=mirrors.aliyun.com \
    TZ=Asia/Shanghai \
    # chromadb 的遥测会在无网环境里拖出重试，和 Fay 那边一样关掉。
    # server.py:25 自己也 setdefault 了一遍，这里是双保险（它在 import 之前就生效更好）。
    CHROMA_TELEMETRY=FALSE \
    CHROMA_SERVER_NO_ANALYTICS=1

WORKDIR /app

# patch: 打 transport 补丁用（python:3.12-slim 不自带）。
# ca-certificates: pip 走 https 镜像。
# 不需要 gcc —— 这一组（chromadb 连同它拖进来的 onnxruntime / tokenizers / grpcio /
# kubernetes，加 pdfplumber / python-docx / uvicorn）在 cp312 x86_64 上全有 manylinux 轮子。
# 但别把镜像想小：chromadb 的 onnxruntime 是硬依赖不是 extra，压不掉，
# 装了却因为 ChromaStore 永远显式传 embeddings 而一次也不被调用（见那份 requirements 的注释）。
RUN apt-get update && apt-get install -y --no-install-recommends \
        patch ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# 依赖先拷，保证改 server.py 不会击穿 pip 层缓存。
# --mount=type=cache 是这台机器特有的：到镜像站的实测吞吐只有 ~30 KiB/s，而 chromadb
# 那一组（连 numpy / onnxruntime / pyarrow / grpcio）上百 MB，一次下载要一个多小时；
# 层缓存的键是 requirements 的内容哈希，改一行注释就会把这一层整个作废重下。挂一个
# BuildKit 缓存卷把轮子留在层外，之后重建只花解析+安装的时间。
# Fay / service 两个镜像故意不加：它们的 requirements 已经冻结，加了反而让「重建时
# 到底装了什么」多出一份不受镜像管的状态。
COPY containerd/overlay/yueshen_rag/requirements-docker.txt /tmp/requirements-docker.txt
RUN --mount=type=cache,target=/root/.cache/pip pip install -r /tmp/requirements-docker.txt

# 上游服务器代码：保持 mcp_servers/yueshen_rag/ 这段目录形状。server.py:34 的
# _project_root() 是「本文件往上两级」，只有保持这个形状，它算出来的 PROJECT_ROOT
# 才是 /app，默认的语料/持久化目录才会落在 /app/{新知识库,cache_data/chromadb_yueshen}。
COPY fay/mcp_servers/yueshen_rag/server.py /app/mcp_servers/yueshen_rag/server.py
COPY fay/mcp_servers/yueshen_rag/README.md /app/mcp_servers/yueshen_rag/README.md

COPY containerd/patches/yueshen_rag/ /tmp/patches/
RUN set -eux; \
    for p in /tmp/patches/*.patch; do \
        [ -e "$p" ] || continue; \
        echo "[patch] $(basename "$p")"; \
        patch -p1 -d /app --no-backup-if-mismatch --silent < "$p"; \
    done; \
    rm -rf /tmp/patches

# 验证补丁真的贴上了：贴不上时 patch 会非零退出（set -e 已经能拦），这里再查一遍
# 运行期真的去读的那两个常量，防止以后有人改了补丁文件名或 -p 级别却没人发现。
# 逐份补丁查一个它引入的名字，不是查得越多越好 —— 要的是「0001 和 0002 各自都在」。
RUN python -c "import ast,sys; \
src=open('/app/mcp_servers/yueshen_rag/server.py').read(); \
names={n.id for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Name)}; \
missing=[k for k in ('TRANSPORT','EMBED_TIMEOUT') if k not in names]; \
sys.exit(0 if not missing else 'patch 没贴上：server.py 里缺 '+','.join(missing))"

# 语料。仓库里一份 pdf/docx 都没有（上游 README 说的默认目录 `新知识库` 也不存在），
# 所以构建期就地生成一份验证语料，产物只进镜像层、不落进仓库。内容与探针的对应关系
# 见 containerd/tools/make_yueshen_corpus.py 的 docstring。
COPY containerd/tools/make_yueshen_corpus.py /tmp/make_yueshen_corpus.py
RUN python /tmp/make_yueshen_corpus.py /app/corpus && rm -f /tmp/make_yueshen_corpus.py

# 向量库持久化目录（compose 里挂成 volume）。
RUN mkdir -p /app/persist /app/logs

ENV YUESHEN_TRANSPORT=sse \
    YUESHEN_CORPUS_DIR=/app/corpus \
    YUESHEN_PERSIST_DIR=/app/persist \
    # 启动即扫描会在每次开机时对语料发一串 embedding，而宿主显存此刻可能正压着 9b
    # （换入换出的代价见 docker-compose.yml 里 EMBEDDING_* 那段实测）。这个服务不需要
    # 常驻自动补扫：入库由探针按 `ingest_yueshen` 真调用触发，见 README「yueshen 知识库」。
    YUESHEN_AUTO_INGEST=0

EXPOSE 8766

CMD ["python", "mcp_servers/yueshen_rag/server.py"]

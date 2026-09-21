# syntax=docker/dockerfile:1
# FunASR 流式语音识别（H5 麦克风那一跳的真后端）。
#
# 构建上下文是项目根（与其他 Dockerfile 同一约定）：
#   在 containerd/ 下：docker build -f images/funasr.Dockerfile -t dh-funasr:local ..
#
# 服务本体是我们自己写的一份（containerd/asr/server.py），**线上协议与伙伴方那份逐字段兼容**
# （前端 src/api/funasr.js 不改一个字），差别只在实现细节，见文件头的说明。
# 为什么不直接复用本机那个现成镜像 careecho-integrated-dockerd-funasr:latest：
# 它的 Dockerfile 与 server.py 不在任何仓库里，别人 clone 出来跑不起来，
# 而且它的依赖一个版本都没钉（`docker history` 可复核）。
#
# 为什么这个 AI 服务不在宿主 Ollama 上：ollama 的 API 没有音频输入口
# （/api/chat 的 messages 只收 text + images），paraformer 也不在其模型清单里。
# 这是本栈第二个「不走 ollama」的 AI 依赖，另一个是魔珐 Xmov 云 TTS（在我们代码之外）。

ARG BASE_IMAGE=docker.m.daocloud.io/python:3.10-slim
FROM ${BASE_IMAGE}

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple \
    PIP_TRUSTED_HOST=mirrors.aliyun.com \
    TZ=Asia/Shanghai \
    # 模型缓存的唯一落点。funasr 的 snapshot_download 不传 cache_dir，
    # 所以卷挂在哪里由这个变量决定（挂载点必须与它一致，见 docker-compose.yml）。
    MODELSCOPE_CACHE=/models \
    # modelscope 的埋点/更新检查在无外网环境里会拖重试，和 Fay/yueshen 那边一样关掉。
    MODELSCOPE_LOG_LEVEL=40

WORKDIR /app

# torch 单独一层：它要的是 PyTorch 自己的 CPU index（PyPI 上同名包默认带 CUDA 运行库，
# 白两 GB —— 而这台机器的显存已经被别的常驻服务占满，装了也用不上）。
# 与下面的 aliyun 层分开，改业务代码或改 funasr 版本都不会击穿这一层的缓存。
# 版本号与 overlay/funasr/requirements-docker.txt 同源；+cpu 后缀只在 PyTorch 的 index 上有。
#
# --extra-index-url 不是可选项，是被一次构建失败教出来的：PyTorch 的 cpu index 只收
# torch 家族的 wheel，torch 自己的依赖里 sympy 在那上面只有 sdist，pip 于是转头去编译它，
# 而 PEP 517 的构建依赖 flit_core 同样只能从这个 index 找 —— 实测报
# `Could not find a version that satisfies the requirement flit_core<4,>=3.11 (from versions: none)`。
# 补一个真 PyPI 镜像当兜底后，torch/torchaudio 仍从 PyTorch index 取 +cpu wheel
# （显式钉了 `==2.13.0+cpu`，PyPI 上那些不带后缀的版本匹配不上），依赖走 aliyun 的 wheel。
COPY containerd/overlay/funasr/requirements-torch.txt /tmp/requirements-torch.txt
RUN pip install --no-cache-dir \
        --index-url https://download.pytorch.org/whl/cpu \
        --extra-index-url "$PIP_INDEX_URL" \
        -r /tmp/requirements-torch.txt

COPY containerd/overlay/funasr/requirements-docker.txt /tmp/requirements-docker.txt
RUN pip install --no-cache-dir -r /tmp/requirements-docker.txt

# 服务本体。跟 adapter / carecho_web 一样，属于「本层自己写的代码」，不进任何上游仓库。
COPY containerd/asr/server.py /app/server.py

# 只声明容器内端口；宿主映不映射由档位决定 —— 生产走前端外壳的 /funasr-ws 同源转发，
# 只有 dev 档位才把它直发出来（见 docker-compose.dev.yml）。
EXPOSE 10095

# 就绪 = 模型真的加载完了（server.py 写这个文件），不是端口 bind 了。
# 首次启动要先下 1.3GB 模型，所以下面给的容忍度很大：start_period 300s + 60 次×10s。
HEALTHCHECK --interval=10s --timeout=5s --start-period=300s --retries=60 \
    CMD ["python", "-c", "import os,sys;sys.exit(0 if os.path.exists('/tmp/asr.ready') else 1)"]

CMD ["python", "/app/server.py"]

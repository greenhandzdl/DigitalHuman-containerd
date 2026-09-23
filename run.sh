#!/usr/bin/env bash
# DigitalHuman 容器集成入口。所有命令都在 containerd/ 下执行（脚本自动切换）。
#   ./run.sh up        生成 .env（若缺）→ 构建 → 起全栈（档位看 .env 的 DH_ENV，缺省 prod）
#   ./run.sh dev       同一套东西，但以 DH_ENV=dev 起：应用面端口全开、绑定局域网 IP，
#                      方便从另一台机器（手机 / 跑 UE 的那台 Windows）连过来测
#   ./run.sh test      跑全套功能测试：service pytest + 后端活体探针 + adapter 契约测试
#                      + 探针时机自测 + 外壳与 WS 转发 + 两个 Fay 实例的契约探针
#                      + ue 体检 + 前端两组 + ASR 两组
#   ./run.sh test <组>  只跑指定测试件（backend-test / backend-probe / adapter-test /
#                      probe-selftest / frontend-test / ws-relay-test / asr-test /
#                      probe-fay-lite / ue-audit / fay-probe / probe-yueshen /
#                      frontend-probe / asr-probe / kb-fay），改探针时不用等全套十几分钟。
#                      kb-fay 能点名跑，但不在常态清单里（一轮问答一组的代价，见下）
#   ./run.sh build     只构建镜像
#   ./run.sh smoke     打通链路冒烟测试：后端 → adapter → Fay → 对话端点（.env 里那个）→ 回库
#   ./run.sh asr-seed  本机若已有伙伴方那个 FunASR 模型缓存卷，直接拷过来（省 1.3GB 下载）
#   ./run.sh audit     核账：fay/service/ue/frontend 四个仓库必须零改动、与上游不分叉，并列出 containerd 侧产物
#   ./run.sh upstream  跟上上游：fetch xszyou/Fay，报 fork 落后/领先几条，
#                      并把 containerd/patches/fay/*.patch 逐个 dry-run 预检一遍
#   ./run.sh kbslice [包] 把项目方语料包（默认 ../uploads/老年康养通用科普.zip）切成
#                      seed/kb_corpus/ 里的语料文件
#   ./run.sh kb        科普语料入库 + 拿 12 条真实问法抽测检索（切片变了或换库后跑一次）
#   ./run.sh kbq [问] [期望词]
#                      知识库的业务判据：从 Fay 的业务口问一句话（默认「血压的正常参考值
#                      是多少？」），取那一问的原始回帧判注入与引用；换问法时不给期望词则
#                      「片段含词/正文用上了」两条按 SKIP 处理，只判注入这条路通不通
#   ./run.sh logs [s]  看日志（s 可为 fay/backend/adapter/frontend/funasr/mysql/redis）
#   ./run.sh env       .env → 容器的映射视图：每一行落到哪个服务（容器里的变量名与 .env 里
#                      的名字不一致时会标出来），以及两份对账 —— compose 会读但 .env 没写的
#                      键（走默认值）、.env 里写了但没有任何服务读的键（改了不生效）
#   ./run.sh ps|down|reset
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

# $COMPOSE 是全局的字符串，所有子命令都用它。它带哪几个 compose 文件由 DH_ENV 决定，
# 见 use_profile —— 所以下面这些命令分支必须在 ensure_env 之后调一次 use_profile。
COMPOSE="docker compose --env-file .env -f docker-compose.yml"

envval() { # envval KEY —— 命令行环境优先，其次 .env；都没有就空
  local v=${!1:-}
  [ -n "$v" ] && { printf '%s' "$v"; return; }
  [ -f .env ] || return 0
  awk -F= -v k="$1" 'index($0, k "=") == 1 {sub(/^[^=]*=/, ""); gsub(/\r/, ""); print; exit}' .env
}

detect_lan_ip() { # 本机在局域网里的那个地址（默认路由的源地址），拿不到就空
  ip route get 1.1.1.1 2>/dev/null |
    awk '{for (i = 1; i <= NF; i++) if ($i == "src") {print $(i + 1); exit}}'
}

use_profile() {
  # 档位只改两件事：用不用 docker-compose.dev.yml，以及 dev 下 BIND_ADDR 落到哪里。
  # 缺省 prod —— 「严谨关闭映射」是默认姿态，放开端口得是显式决定。
  DH_ENV=${DH_ENV:-$(envval DH_ENV)}
  DH_ENV=${DH_ENV:-prod}
  local bind=${BIND_ADDR:-$(envval BIND_ADDR)}
  case "$DH_ENV" in
    prod) COMPOSE="docker compose --env-file .env -f docker-compose.yml" ;;
    dev)  COMPOSE="docker compose --env-file .env -f docker-compose.yml -f docker-compose.dev.yml" ;;
    *) echo "[run] DH_ENV 只认 prod|dev，当前是 '$DH_ENV'" >&2; exit 2 ;;
  esac
  if [ "$DH_ENV" = prod ] && [ -n "$bind" ] \
     && [ "$bind" != "127.0.0.1" ] && [ "$bind" != "localhost" ]; then
    # prod 的整套端口（含 13306 / 16379 那两个管理口）都靠 ${BIND_ADDR:-127.0.0.1}
    # 收在 loopback 上；把它改成 0.0.0.0 或某个局域网地址，等于把 Fay 的管理台、无鉴权的
    # OpenAI 兼容 façade 和数据库一起放出去。这一步不做「提醒后继续」而直接拒：
    # 这类泄漏的常见成因就是有人临时改过一次 .env。
    echo "[run] DH_ENV=prod 不接受 BIND_ADDR=$bind —— 那会把管理台、无鉴权接口与数据库发布到外部。" >&2
    echo "      要给别的机器连就用 ./run.sh dev（那个档位按设计就是绑 0.0.0.0 全开）。" >&2
    exit 1
  fi
}

ensure_env() {
  # 与 .env 同族的一件事：overlay 那三份 json 是 Fay 运行期会整份回写的 bind-mount 源，
  # 仓库里只留 .example 模板，本地那份由 tools/gen_overlay.py 首跑复制。放在早退之前，
  # 这样每一条会起容器的命令都覆盖得到；文件都在时它完全静默。
  python3 tools/gen_overlay.py
  if [ -f .env ]; then return; fi
  # 随机 DB 密码与 JWT 密钥的生成逻辑收敛到 tools/gen_keys.py（单一来源）
  python3 tools/gen_keys.py
}

_COMPOSE_CONFIG=""
compose_config_once() { # 渲染一次 compose 文件给下面的探测复用（每次 config 都要半秒左右）
  [ -n "$_COMPOSE_CONFIG" ] || _COMPOSE_CONFIG=$($COMPOSE config 2>/dev/null || true)
  printf '%s\n' "$_COMPOSE_CONFIG"
}

image_tag_of() { # 服务在 compose 文件里配的那个 image:，不是「容器此刻在跑哪个镜像」
  compose_config_once | awk -v want="$1" '
    $0 == "  " want":" {insvc = 1; next}
    /^  [A-Za-z0-9_-]+:$/ {insvc = 0}
    insvc && $0 ~ /^    image: / {print $2; exit}'
}

image_id_of() { # 服务当前 tag 指向的镜像 ID；没建过就返回空
  # 不能用 `compose images`：它列的是「已创建容器在用的镜像」。重建之后 tag 挪到了新镜像，
  # 而旧容器还没换，那一行就成了 <none>:<none> —— 探测结果于是取决于「这台机器上有没有一个
  # 还没重启的旧容器」。run #32 里 frontend 每轮被报成「本地没有镜像」（白建一次），
  # funasr 重建后 ID 印成空，两个都是这么来的。
  # run #29 的教训是反方向的同一件事：拿容器在跑的镜像 ID 比，会把一次真变更印成「没变」。
  # ID 一律按 tag 现查（tag 从 compose 文件的 image: 字段拿），两头就都只取决于镜像本身。
  local tag
  tag=$(image_tag_of "$1")
  [ -n "$tag" ] || return 0
  docker image inspect --format '{{.Id}}' "$tag" 2>/dev/null | sed 's/^sha256://' || true
}

wait_http() { # wait_http <url> <deadline_sec> <name>
  local url=$1 deadline=$((SECONDS + $2)) name=$3
  until curl -fsS --max-time 3 "$url" >/dev/null 2>&1; do
    if (( SECONDS > deadline )); then echo "[run] 超时：$name 未就绪 ($url)" >&2; return 1; fi
    sleep 2
  done
  echo "[run] 就绪：$name"
}

probe_host() { # 探针该打哪个地址：以「栈现在真把端口发布在哪」为准，而不是照配置猜档位。
  # dev 档位绑的是局域网 IP，这时 127.0.0.1 上根本没人听 —— 本轮 ./run.sh dev 的
  # 「backend 未就绪」就是这么红的（compose 那边报的却是 Healthy）。容器没起或没发布
  # 端口时退回 loopback，那正是 prod 档位的实际地址。
  local ip
  for c in dh-backend dh-frontend; do
    ip=$(docker inspect -f '{{range $k, $v := .NetworkSettings.Ports}}{{if $v}}{{(index $v 0).HostIp}}{{println}}{{end}}{{end}}' \
          "$c" 2>/dev/null | head -1)
    [ -n "$ip" ] && break
  done
  case "$ip" in
    ""|0.0.0.0|::|\[::\]) printf '127.0.0.1' ;;
    *:*) printf '[%s]' "$ip" ;;          # IPv6 不加方括号进不了 URL
    *) printf '%s' "$ip" ;;
  esac
}

wait_fay_agent() { # wait_fay_agent <cid> <deadline_sec> —— 等 Fay 的「代理实例」真建好，而不是等 :5000 应答。
  # 这两件事差着一整个启动过程：flask 先起来就能答 /api/get-system-status，而 main.py 还要
  # 预热 embedding、才 创建代理实例。中间那段窗口里 /api/send 会 0.01s 抛 500 —— 实测
  # 2026-09-21 22:18 那次 restart 后 :5000 立刻可问，紧接着那一问就 502；到第 10s 才接得住。
  # 判据取 fay_booter.py:478（start() 最后一行那句「服务启动完成!」）。上游若改了这句文案，
  # 这里会红并点名，不会退化成"默默等满 deadline"。--since 用容器本次启动时刻，
  # 免得把上一次生命周期的同一句读成"已经好了"。
  local cid=$1 deadline=$((SECONDS + $2)) started t0=$SECONDS
  started=$(docker inspect -f '{{.State.StartedAt}}' "$cid" 2>/dev/null) || return 1
  until docker logs --since "$started" "$cid" 2>&1 | grep -q "服务启动完成!"; do
    if (( SECONDS > deadline )); then
      echo "[run] 超时：$cid 本次启动没打出「服务启动完成!」（容器没起来 / 上游改了这句文案）" >&2
      return 1
    fi
    sleep 2
  done
  echo "[run] 就绪：fay 代理实例（等待 $((SECONDS - t0))s）"
}

ai_endpoints() { # smoke 前把两类 AI 能力的真实落点打出来：对话端点是什么、嵌入宿主忙不忙
  # 两类能力是两台服务，所以分两行。对话那一行只报 `.env` 里覆盖后的落点（**绝不碰
  # FAY_GPT_API_KEY**）；留空时回落 overlay/fay/system.conf 里那份示例，那是宿主 ollama。
  local chat_base="${FAY_GPT_BASE_URL:-}"
  [ -n "$chat_base" ] || chat_base="（.env 未覆盖 → 回落 system.conf 的 http://host.docker.internal:11434/v1）"
  printf '  对话端点: %s\n            模型 %s\n' "$chat_base" \
    "${FAY_GPT_MODEL_ENGINE:-（未覆盖 → 回落 system.conf 里的模型名）}"
  local port="${OLLAMA_PORT:-11434}"
  local out
  out=$(curl -fsS --max-time 3 "http://127.0.0.1:${port}/api/ps" 2>/dev/null |
    OLLAMA_PS_PORT="$port" python3 -c '
import os, sys, json
try:
    models = json.load(sys.stdin).get("models", [])
except Exception:
    print("  嵌入宿主 ollama :%s 不可达 —— 对话不经过它，问答类判据照样可能过；"
          "代价是仿生记忆与知识库检索静默退化成模拟向量（不报错）" % os.environ["OLLAMA_PS_PORT"])
    raise SystemExit(0)
print("  嵌入宿主 ollama :%s" % os.environ["OLLAMA_PS_PORT"]
      + ("（没有已加载模型，首个嵌入请求要先换入，实测 70s 起）" if not models else ""))
for m in models:
    name = m.get("name")
    size = float(m.get("size") or 0)
    vram = float(m.get("size_vram") or 0)
    if size <= 0:
        print("           %s（size 未知）" % name)
        continue
    frac = vram / size * 100.0
    print("           %s 驻留显存 %.0f%%（%.0f/%.0fMB）" % (name, frac, vram / 1e6, size / 1e6))
    if frac < 50:
        print("           → 大半权重在内存里，那一发是分钟级；显存只够放一个模型，"
              "和对话服务抢的就是这一格")
' 2>/dev/null) || true
  [ -n "$out" ] && printf '%s\n' "$out"
}

smoke() {
  set -u
  source .env
  local base="http://$(probe_host):${BACKEND_PORT:-8000}"
  # 先把两类 AI 端点的真实落点打出来：这条链路的耗时几乎全在模型那一发上，而对话与嵌入
  # 是两台服务。少了这句，第 6 步慢下来会被读成「后端或 Fay 坏了」。历史上那句是：对话还
  # 落在宿主 ollama 时，15.2GB 显存被别人占走、qwen3.5:9b 只有 6.3% 进显存，一句话要
  # 172~301s（fork 的规划器链路 >600s）；现在对话打的是 .env 里那个端点，量出来的数在下面
  # 这一行输出里，别拿上面那组历史数字当今天的基线。
  ai_endpoints
  # 注意：health 路由挂在 api_v1_prefix 下，真实路径是 /api/v1/health（见 app/api/router.py:25）
  echo "[smoke] 1/6 后端健康检查"
  curl -fsS "$base/api/v1/health" && echo
  echo "[smoke] 2/6 Fay Web API（经 adapter 的上游）"
  wait_http "http://$(probe_host):${ADAPTER_PORT:-8010}/healthz" 60 "adapter"
  echo "[smoke] 3/6 dev-login 取 token"
  # DevLoginRequest 只有 openid 字段；不传则服务端自动生成
  local token
  token=$(curl -fsS -X POST "$base/api/v1/auth/dev-login" \
            -H 'Content-Type: application/json' \
            -d '{"openid":"smoke-elder-001"}' |
          python3 -c 'import sys,json;d=json.load(sys.stdin);print(d.get("access_token",""))')
  [ -n "$token" ] || { echo "[smoke] dev-login 失败（需 DEBUG=true）" >&2; return 1; }
  echo "  token ok (user_id=$(curl -fsS -X POST "$base/api/v1/auth/dev-login" -H 'Content-Type: application/json' -d '{"openid":"smoke-elder-001"}' | python3 -c 'import sys,json;print(json.load(sys.stdin).get("user_id"))'))"
  echo "[smoke] 4/6 建会话（该接口无请求体）"
  local sid
  sid=$(curl -fsS -X POST "$base/api/v1/chat/sessions" \
          -H "Authorization: Bearer $token" |
        python3 -c 'import sys,json;d=json.load(sys.stdin);print(d.get("id") or d.get("session_id",""))')
  [ -n "$sid" ] || { echo "[smoke] 建会话失败" >&2; return 1; }
  # --- 5/6 状态持久化：卷挂对了，重启后记忆还在 ---
  # 前面几步只证明「此刻能用」，而容器层最容易坏的是「下一次还在不在」：上游 Fay 假设
  # 自己有本地磁盘，容器把那块磁盘换成了具名卷。卷漏挂或名字写错时数据落进容器的可写层，
  # restart 后照样读得到（还是同一个容器），但 `up -d` 重建实例就全丢 —— 所以既看
  # Mounts 的结构，也真的 restart 一次把同一条行读回来。
  # 挑 fay 下手：产品真正服务的就是它，而且第 6 步紧接着走后端→adapter→fay→对话端点，
  # 重启完接不回来当场就红。原先这步用 origin-fay 参照实例（没人 depends_on 它），
  # 那个实例已随「fork 是上游直接后代、代码逐字节相同」撤掉了。
  local cid mounts marker="smoke-persist-$$"
  cid=$($COMPOSE ps -q fay)
  [ -n "$cid" ] || { echo "[smoke] fay 没在跑，持久化这一步无从判起" >&2; return 1; }
  mounts=$(docker inspect --format '{{range .Mounts}}{{.Type}}:{{.Destination}} {{end}}' "$cid")
  case "$mounts" in
    *volume:/app/memory*) echo "  结构：fay 的 /app/memory 是具名卷" ;;
    *) echo "[smoke] 结构：/app/memory 不是具名卷（mounts: $mounts）" >&2; return 1 ;;
  esac
  # /api/send 会把这一行立刻写进记忆库（实测 type='member'，不等模型回话），
  # 所以这条判据与显存够不够、LLM 快不快都无关。
  local persist_py='
import json, sys, time, urllib.request, urllib.parse
mode, marker = sys.argv[1], sys.argv[2]
BASE = "http://127.0.0.1:5000"
def post(path, payload):
    body = urllib.parse.urlencode({"data": json.dumps(payload, ensure_ascii=False)}).encode()
    req = urllib.request.Request(BASE + path, data=body, method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    return json.loads(urllib.request.urlopen(req, timeout=25).read().decode())
def hits():
    rows = post("/api/get-msg", {"username": "smoke_persist", "limit": 40, "offset": 0}).get("list") or []
    return [r.get("id") for r in rows if marker in (r.get("content") or "")]
if mode == "write":
    post("/api/send", {"username": "smoke_persist", "msg": marker})
    for _ in range(10):
        time.sleep(2)
        if hits():
            print("  行为：写入已落库 id=%s" % hits()[0])
            raise SystemExit(0)
    raise SystemExit("重启前就查不到这一行：/api/send 的落库本身不通")
if hits():
    print("  行为：restart 之后仍读得到 id=%s" % hits()[0])
    raise SystemExit(0)
raise SystemExit("restart 之后读不到那一行：/app/memory 没有真正持久化")
'
  printf '%s' "$persist_py" | docker exec -i "$cid" python - write "$marker" \
    || { echo "[smoke] 持久化：写入侧失败" >&2; return 1; }
  echo "[smoke]     restart fay（宿主被压满时 :8765 可能要两分钟才回来）"
  $COMPOSE restart fay >/dev/null \
    || { echo "[smoke] restart fay 失败" >&2; return 1; }
  wait_fay_agent "$cid" 240 || return 1
  printf '%s' "$persist_py" | docker exec -i "$cid" python - verify "$marker" \
    || { echo "[smoke] 持久化：重启后读不回来，卷没挂对" >&2; return 1; }

  echo "[smoke] 6/6 发一句话（走 Fay + .env 里那个对话端点）"
  echo "        耗时几乎全在对话那一发，而且一问可能是好几发（fork 的规划器链路）。"
  echo "        上面那行「驻留显存」说的是嵌入宿主，它低不代表这一发慢；那组"
  echo "        172~301s 量自对话还落在 ollama 上的几轮。上限 SMOKE_MAX_TIME=${SMOKE_MAX_TIME:-600}s"
  curl -fsS --max-time "${SMOKE_MAX_TIME:-600}" -X POST "$base/api/v1/chat/sessions/$sid/messages" \
    -H "Authorization: Bearer $token" -H 'Content-Type: application/json' \
    -d '{"content":"你好，用一句话说说你能为康复期的老人做什么。","content_type":"text"}' |
  python3 -c '
import sys, json
d = json.load(sys.stdin)
am = d.get("assistant_message") or {}
print("  fay_forwarded =", d.get("fay_forwarded"))
print("  fay_error     =", d.get("fay_error"))
print("  tier          =", (d.get("tier_classification") or {}).get("response_tier"))
print("  回复          =", (am.get("content") or "(空)")[:160])
sys.exit(0 if d.get("fay_forwarded") and (am.get("content") or "").strip() else 1)
' || {
    # 区分「链路坏了」和「这台机器带不动那个模型」：前者要查，后者不是这条栈的缺陷。
    # 与对话端点无关的那几段（后端→adapter→Fay 的 HTTP/WS/MCP/TTS）由 ./run.sh test
    # 里的探针确定性判掉，其中轻模型实例 fay-lite 专门证明问答链路本身是通的。
    echo "[smoke] 第 6 步没拿到非空回复。先看上面那行对话端点是谁：" >&2
    echo "        它是宿主 ollama 且驻留比例低 → 显存不够、模型在 CPU 上推，不是链路坏，" >&2
    echo "        跑 ./run.sh test 看 fay-lite 那组，或把模型换成能装进剩余显存的；" >&2
    echo "        它是别的服务 → 用 ./run.sh env 核对 FAY_GPT_BASE_URL 与 FAY_GPT_MODEL_ENGINE，" >&2
    echo "        再用 curl {base}/v1/chat/completions 直连量一发，把环境问题与功能问题分开。" >&2
    return 1
  }
  echo "[smoke] 全链路通过：后端 → adapter → Fay → 对话端点 → 落库"
}

case "${1:-up}" in
  up|dev)
    [ "${1:-up}" = dev ] && DH_ENV=dev   # 子命令就是档位的显式写法，不必去改 .env
    ensure_env
    if [ "${DH_ENV:-}" = dev ]; then
      # dev 按设计就是全开：BIND_ADDR 落到 0.0.0.0，本机每个地址（LAN、tailscale、以后
      # 再插的网卡）上同一批端口各有一份，所以不存在"第二个绑定口"那种东西。
      # 局域网地址仍然探 —— 但它用于两件事：打印给人点的链接，和喂 Fay 的 FAY_URL
      # （音频地址必须是远端真能取到的那个 IP，写 0.0.0.0 等于把对端引向它自己）。
      : "${DH_LAN_IP:=$(detect_lan_ip)}"
      : "${BIND_ADDR:=0.0.0.0}"
      export DH_LAN_IP BIND_ADDR
    fi
    use_profile
    echo "[run] 档位 DH_ENV=$DH_ENV，BIND_ADDR=${BIND_ADDR:-（compose 默认）}"
    # 打印用的地址：绑 0.0.0.0 时不能把 http://0.0.0.0:5173 交给用户当链接。
    case "${BIND_ADDR:-}" in
      ""|0.0.0.0) DISP=${DH_LAN_IP:-127.0.0.1} ;;
      *)          DISP=$BIND_ADDR ;;
    esac
    $COMPOSE build
    $COMPOSE up -d
    shift 2>/dev/null || true
    # 不 source .env：那会把上面为 dev 导出的 DH_ENV / BIND_ADDR 按文件里的值盖回去
    # （首次 up 之后 .env 里就是 DH_ENV=prod），档位于是只影响到 compose 文件的选择、
    # 却不影响真正绑地址的那一步。逐个取端口时 envval 认「命令行优先」，两边都对。
    for k in FRONTEND_PORT BACKEND_PORT ADAPTER_PORT FAY_HTTP_PORT FAY_HUMAN_WS_PORT; do
      v=$(envval "$k"); [ -n "$v" ] && export "$k=$v"
    done
    wait_http "http://$(probe_host):${BACKEND_PORT:-8000}/api/v1/health" 180 "backend"
    wait_http "http://$(probe_host):${ADAPTER_PORT:-8010}/healthz" 180 "adapter"
    wait_http "http://$(probe_host):${FRONTEND_PORT:-5173}/api/health" 120 "frontend"
    # FunASR 不等：首次起要下 1.3GB 模型（健康检查给了 300s 起步 + 60×10s），
    # 把 up 卡在那儿十分钟不值。只报当前状态，等它就去看 ./run.sh logs funasr。
    echo "[run] funasr 就绪状态：$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' dh-funasr 2>/dev/null || echo 未起)"
    echo
    echo "CareEcho H5       : http://${DISP}:${FRONTEND_PORT:-5173}/"
    echo "Fay Web 管理台    : http://${DISP}:${FAY_HTTP_PORT:-5000}/"
    echo "后端接口文档      : http://${DISP}:${BACKEND_PORT:-8000}/docs"
    echo "数字人 WS         : ws://${DISP}:${FAY_HUMAN_WS_PORT:-10002}"
    if [ "${DH_ENV}" = dev ]; then
      # 跨机三行：UE 在另一台电脑上时，那台机器需要的就只是这三件事。
      echo
      echo "[dev] 另一台电脑（UE / 手机）要连过来："
      echo "  端口绑在 0.0.0.0：本机每个地址都收（LAN / tailscale / 以后新插的网卡），换网卡不必重起。"
      echo "  1. Windows 的 C:\\Windows\\System32\\drivers\\etc\\hosts 加一行：  ${DISP}  dh-host"
      echo "  2. UE 里填 ws://dh-host:${FAY_HUMAN_WS_PORT:-10002}（或直接写 ${DISP}，跳过 hosts 那步）"
      echo "  3. 复核 UE 拿到的音频地址不是 127.0.0.1： docker exec dh-fay python -c \"from utils import config_util as c;c.load_config();print(c.fay_url)\""
      echo "     （那行由 FAY_URL 控制，dev 档位已设成 http://${DH_LAN_IP:-127.0.0.1}:5000 —— 音频地址要是对端真能取到的那个 IP，不能是 0.0.0.0；见 patches/fay/0007 与 README「跨机流量」）"
      echo "  麦克风：H5 的 ws://<页面同源>/funasr-ws 由外壳转发进 funasr，不需要单独开端口。"
      echo "  代价：管理口 13306 / 16379 与应用口一起出去了，能不能进去只看口令 —— dev 用完 ./run.sh down。"
      echo "  复核开了哪些口： docker compose --env-file .env -f docker-compose.yml -f docker-compose.dev.yml config | grep -E 'host_ip: ' | sort -u"
      echo "  注意 getUserMedia 只在 https 或 localhost 是安全上下文 —— 用 http://${DISP}:5173 打开时，"
      echo "        非 localhost 的页面上麦克风按钮必然失败，那是浏览器策略不是本栈故障（README「未覆盖能力」）。"
    fi
    echo "跑 ./run.sh smoke 做端到端验证；./run.sh test 跑全套功能测试"
    ;;
  test)
    ensure_env
    use_profile
    shift 2>/dev/null || true
    # 只在「构建输入比镜像新」时重建对应服务。两个方向都咬过人：
    #  - `up -d` 只认镜像存不存在，不认镜像是不是比补丁新。改完 patches/ 或
    #    overlay/*requirements 直接跑 test，会静默测上一次构建的镜像 —— 这种绿什么都没测。
    #  - 但也不能无条件 build：这台机器的 BuildKit 缓存已经顶到 GC 上限（45GB / 只有
    #    13 条活跃），`compose build` 会把 pip 层当冷缓存重跑，单个镜像 5~15 分钟。
    #    实测 2026-09-20 15:55 那次重建就是纯粹被淘汰：输入一个没变（拿镜像的 Created
    #    去 find -newermt，结果为空）。所以按输入时间判断，需要时才建。
    # overlay/ 只点名 requirements*.txt 而不是整个目录：Dockerfile 从 overlay 拿走的只有
    # 那几份清单（`grep -n overlay images/*.Dockerfile` 就 4 行），而 system.conf /
    # config.json / mcp_servers.json 是运行期 bind-mount，Fay 自己会回写它们 —— 把整个目录
    # 算进输入等于「每轮都必重建一次」。run #29/#30 连续两轮的 trigger 都是
    # `overlay/*/mcp_servers.json` 的 connection_time，就是这个。
    # 建失败立刻停在这里，绝不拿旧镜像继续测（补丁贴不上去时 patch 非零退出 -> build 失败）。
    stale=""
    before=""
    for svc in fay backend yueshen-rag frontend funasr; do
      case "$svc" in
        fay)        src="images/fay.Dockerfile patches/fay overlay/fay/requirements-docker.txt" ;;
        backend)    src="images/service.Dockerfile patches/service overlay/service/requirements-docker.txt overlay/service/requirements-test.txt" ;;
        yueshen-rag) src="images/yueshen_rag.Dockerfile patches/yueshen_rag overlay/yueshen_rag/requirements-docker.txt" ;;
        # 前端只点名 vite 真会读的输入，不写整个 ../frontend：那会把 .git/ 算进来，而且
        # 谁在宿主上 npm install 过一次，node_modules 的 mtime 就每轮都比镜像新 —— 每轮白建一次。
        frontend)   src="images/frontend.Dockerfile frontend/carecho_web.py ../frontend/careecho-h5/src ../frontend/careecho-h5/index.html ../frontend/careecho-h5/package.json ../frontend/careecho-h5/package-lock.json ../frontend/careecho-h5/vite.config.js" ;;
        # ASR 那份是 torch 镜像：漏点名一份 = 改了不重建，测的还是旧镜像里那版 server.py。
        funasr)     src="images/funasr.Dockerfile asr/server.py overlay/funasr/requirements-docker.txt overlay/funasr/requirements-torch.txt" ;;
      esac
      # 镜像名不写死，让 compose 自己报；报不出 ID = 本地没有 = 必须建。
      iid=$(image_id_of "$svc")
      created=""
      # 写成 if 而不是 `[ -n "$iid" ] && created=…`：后者在没镜像时整个 AND 列表返回非 0，
      # bash 恰好豁免了（AND-OR 列表里非最后一条命令的失败不触发 set -e），但这层依赖
      # 太脆，换个写法就中招，所以不赌。
      if [ -n "$iid" ]; then
        created=$(docker image inspect --format '{{.Created}}' "$iid" 2>/dev/null || true)
      fi
      if [ -z "$created" ] || [ -n "$(find $src -newermt "$created" -print -quit 2>/dev/null || true)" ]; then
        if [ -z "$created" ]; then
          echo "[test] $svc 本地没有镜像，先建"
        else
          trigger=$(find $src -type f -newermt "$created" -printf '%T@ %p\n' 2>/dev/null | sort -rn | head -1 | cut -d' ' -f2-)
          # 判新只看文件会让「删掉一个补丁」这种变更漏掉（那时只有目录 mtime 变了），
          # 所以上面的探测带目录、这里点名只找文件，找不到就如实说没有具体文件。
          [ -n "$trigger" ] || trigger="没有比它更新的文件，是目录本身变了（增删过条目）"
          echo "[test] $svc 的构建输入不比镜像旧（比它新的输入里最新一个是 $trigger），先重建它"
        fi
        stale="$stale $svc"
        before="$before $svc=${iid:-none}"
      fi
    done
    if [ -n "$stale" ]; then
      if ! $COMPOSE build $stale >/dev/null 2>&1; then
        echo "[test] 镜像构建失败（多半是 patches/ 里的补丁贴不上去），不用旧镜像继续测：" >&2
        $COMPOSE build $stale 2>&1 | tail -15 >&2
        exit 1
      fi
      # 重建完逐个核对镜像 ID，把「补丁真的没进旧镜像」和「文件只是被碰过」分开说：
      # 这道闸量的是 mtime，而 mtime 会骗人 —— 只改 Dockerfile 注释、或者 touch 一下
      # overlay 里的文件，都会触发重建，但 BuildKit 全量命中缓存，镜像 ID 一字不差。
      # 报成「先重建它」而不说结果，会让人以为测的是新内容（其实什么都没变）。
      for pair in $before; do
        svc=${pair%%=*}; old=${pair#*=}
        new=$(image_id_of "$svc")
        if [ "$old" = none ]; then
          echo "[test] $svc 镜像已建出 ${new:0:19}"
        elif [ "$new" != "$old" ]; then
          echo "[test] $svc 镜像 ID 变了（${old:0:12} → ${new:0:12}）：旧镜像里确实没有这些输入的当前内容，这道闸起作用了"
        else
          echo "[test] $svc 重建后镜像 ID 没变（${old:0:12}）：只是 mtime 比镜像新、内容没变，BuildKit 全量命中缓存 —— 这轮测的还是原来那个镜像"
        fi
      done
    fi
    # 依赖必须先就绪：探针要打 Fay 的 HTTP/WS，pytest 要打 MySQL 里的 schema，
    # backend-probe 打的是正在应答 HTTP 的那个 backend 进程（depends_on 会把
    # adapter 一起带起来，--wait 会等 backend 自己的健康检查过）。
    $COMPOSE up -d --wait mysql redis fay backend 2>&1 | tail -3
    # fay-lite 是 test profile 里的轻模型实例（qwen2.5:1.5b），只有它能在被其他
    # 服务占满显存的机器上秒级答完，用来判定「问答链路是否真的通」。
    $COMPOSE --profile test up -d --wait fay-lite 2>&1 | tail -3
    ALL_GROUPS="backend-test backend-probe adapter-test probe-selftest frontend-test ws-relay-test asr-test probe-fay-lite ue-audit fay-probe probe-yueshen frontend-probe asr-probe kb-ingest kb-fay"
    # 允许 ./run.sh test fay-probe 只跑一组：改探针时不必再等 20 分钟全套。
    # 变量名不能叫 GROUPS —— bash 的内置只读数组（当前用户的 gid），赋值会被吞掉。
    # 默认顺序把 probe-fay-lite 排在主实例那两组问答之前。两条理由现在都还在：
    # 一问在 fork 里会触发多次嵌入请求，而嵌入宿主 ollama 是单条队列（对话端点搬到栈外
    # 之后这一条没跟着搬）；并且问答组的「客户端超时」不等于「服务端停止生成」，探针不
    # 等了模型还在推，于是排在后面的 lite 只能排队。run #5 里 lite 的直连探测就是这么被
    # 挤到 180s 超时的（qwen2.5:1.5b 暖态本来 0.2s 答完，那一轮对话还在 ollama 上）。
    # lite 是唯一能给出「问答链路真的通」正面证据的一组，必须先拿。
    # adapter-test 紧跟其后：它量的是本目录自己写的适配器（containerd/adapter），
    # 全程打假 Fay、不碰任何模型端点，几秒跑完。把「我们自己有没有写坏」和「机器忙不忙」
    # 这两件事分开，前者不该被后者的抖动掩盖。
    # probe-selftest 排在它后面，同一个道理：它量的是探针自己的「收工时机」
    # （probes/ws_timing_test.py），假 Fay 出帧、不碰任何模型端点，也是几十秒。
    # backend-probe 紧跟 backend-test：同一条理由 —— 它量的是这个正在应答 HTTP 的
    # 进程 + 业务库的真实 schema，也不碰任何模型端点，十一条判据几秒钟。
    # probe-yueshen 排在全套最后，为的是显存而不是逻辑：它要打宿主 ollama 的**嵌入**模型
    # 才能把语料灌进 chromadb，而这台机器显存只够放一个模型，换入换出实测 70s 起。
    # 放在三组问答之前，等于我们自己制造一次模型换出、把问答判据挤成超时（那红的是
    # 排队，不是被测对象）。它自己第一发就是奔着「量嵌入出口 + 顺手把模型预热进显存」
    # 去的，所以这条链路的耗时证据仍然取得到。
    # frontend-test 挨着 probe-selftest：同一个位置逻辑 —— 它量的是本目录自己写的外壳
    # （containerd/frontend/carecho_web.py）与构建产物，假后端起在同容器里，不碰任何模型
    # 端点，17 条判据秒级。frontend-probe 排在后面：它那一问要穿完整条链（对话还落在
    # ollama 时本机 9b 实测 172~301s），是唯一还会新增一次问答开销的一组，排在谁后面都
    # 不影响谁的判据。
    # ws-relay-test / asr-test 紧跟 frontend-test：同一条理由的 ASR 版 —— 前者量的是外壳
    # 里那段 WS 转发（假上游起在同容器里，stdlib 手搓帧），后者用 ASR_FAKE_MODEL=1 量
    # 识别服务的线上协议与累计文本那条性质，都不加载 torch、不碰任何模型端点，两组都是秒级。
    # asr-probe 排在全套最末：它是唯一真的跑一次 CPU 推理的一组（paraformer-large 会从
    # 宿主上那些没驻留显存的模型权重嘴里抢核），所以它既不能排在问答组之前，也不该被它们排队。
    # kb-ingest 排在 asr-probe 之后、成为新的最末一组：它比 asr-probe 更贵 —— 每次都
    # reset 重嵌入那 516 个片段（几分钟），还要把 qwen3-embedding 换进唯一那块 16GB 显存。
    # 排在最后它谁也不抢，而且它跑完留下的正是真语料（probe-yueshen 在它前面把集合
    # 换成了 3 段合成语料）。这条组名就是 compose 服务名 ./run.sh test kb-ingest；
    # 单独调 top_k 用 ./run.sh kb --sweep 3,5,8，那条不占测试组的位。
    # kb-fay 只进 ALL_GROUPS、**不进 DEFAULT_GROUPS**：它一组要发两整轮问答（正常轮 +
    # 把 query_yueshen 的 prestart 注册摘掉再问一轮的对照轮）。挂进常态清单等于让
    # `./run.sh test` 在问答端点慢的机器上必然卡出一条与回归无关的红 —— 那三组问答还
    # 在宿主 ollama 上时实测一轮 172~301s；端点搬到栈外那台 26B 之后 kbq 单发只要 8.1s，
    # 但这一组仍是全套里唯一要发两整轮的一组，判据本身也不随快慢改变。列进 ALL_GROUPS
    # 是为了让它能被点名（改 Fay 的 prestart/注入这条链时值得跑），日常入口是 ./run.sh kbq。
    DEFAULT_GROUPS="backend-test backend-probe adapter-test probe-selftest frontend-test ws-relay-test asr-test probe-fay-lite ue-audit fay-probe probe-yueshen frontend-probe asr-probe kb-ingest"
    TEST_GROUPS="$*"
    [ -n "$TEST_GROUPS" ] || TEST_GROUPS="$DEFAULT_GROUPS"
    for g in $TEST_GROUPS; do
      case " $ALL_GROUPS " in
        *" $g "*) ;;
        *) echo "[test] 没有这组测试件：$g（可选：$ALL_GROUPS）" >&2; exit 2 ;;
      esac
    done
    # frontend-probe 要的是一个正在应答的 frontend 容器，asr-probe 要的是一个**模型已经
    # 加载完**的 funasr 容器（它的健康判据就是 server.py 写的 /tmp/asr.ready），其余组都用
    # 不到这两个，就不为它们多起一份（下面那行 run 带 --no-deps，不会替我们把依赖拉起来）。
    case " $TEST_GROUPS " in
      *frontend-probe*) $COMPOSE up -d --wait frontend 2>&1 | tail -2 ;;
    esac
    case " $TEST_GROUPS " in
      *asr-probe*)
        echo "[test] asr-probe 前先等 dh-funasr 健康（首次启动要下约 1.3GB 模型，健康检查给到 300s 起）"
        $COMPOSE up -d --wait funasr 2>&1 | tail -2 ;;
    esac
    rc=0
    for svc in $TEST_GROUPS; do
      echo
      echo "=============== $svc ==============="
      $COMPOSE --profile test build "$svc" >/dev/null 2>&1 || true
      if ! $COMPOSE --profile test run --rm --no-deps -T "$svc"; then
        echo "[test] $svc 失败" >&2
        rc=1
      fi
    done
    # 顺手把「优雅停止的退出码」变成一条判据。上面那行 stop 就是 docker 发给 Fay 的
    # SIGTERM，而上游 main.py 的 signal_handler 只给清理 5 秒预算，thread_manager.stopAll()
    # 又是每个线程各排 2 秒的 join —— 两个卡在 recv 上的 __connect 就能把预算吃光，
    # 走进 os._exit(1)。于是一次完全正常的 docker stop 被容器运行时记成崩溃退出，
    # restart: on-failure 下还会把本该停掉的实例重新拉起来。实测（2026-09-20，
    # 未打补丁的镜像）：fay-lite 5.27s/exit=1、当时的 origin-fay 参照实例 5.45s/exit=1。
    # 只在「这一刻它真的在跑」时才判：单独跑某一组时 lite 可能压根没起来，那时
    # inspect 读到的是上次停止留下的旧退出码，拿它判等于伪造证据。
    lite_cid=$($COMPOSE --profile test ps -q fay-lite 2>/dev/null || true)
    lite_running=""
    [ -n "$lite_cid" ] && lite_running=$(docker inspect --format '{{.State.Running}}' "$lite_cid" 2>/dev/null || true)
    $COMPOSE --profile test stop fay-lite >/dev/null 2>&1 || true
    if [ "$lite_running" = "true" ]; then
      case "$(docker inspect --format '{{.State.ExitCode}}' "$lite_cid" 2>/dev/null || echo '?')" in
        0) echo "[test] fay-lite 优雅停止（SIGTERM）退出码 0：一次正常的 stop 没被记成崩溃" ;;
        *) echo "[test] fay-lite 优雅停止退出码为 $(docker inspect --format '{{.State.ExitCode}}' "$lite_cid" 2>/dev/null)：正常的 docker stop 被记成了崩溃退出（见 patches/fay/0005）" >&2; rc=1 ;;
      esac
    else
      echo "[test] fay-lite 停止前就不在跑，优雅停止这条判据本轮没有证据可取（不算通过）"
    fi
    echo
    [ $rc -eq 0 ] && echo "[test] 全部通过" || echo "[test] 有失败项，见上" >&2
    exit $rc
    ;;
  audit)
    # README 里"不改上游任何代码"不能只是一句承诺：把四个仓库的真实状态打出来。
    # 有任何一个脏文件或与上游分叉就非零退出，可直接当 CI 断言用。
    shift
    rc=0
    # 表头用 ASCII：printf %-Ns 数的是字节，中文列头会把对齐整个搞坏
    printf '%-11s %-14s %-22s %-11s %s\n' REPO BRANCH UPSTREAM AHEAD/BEHIND WORKTREE
    for r in fay service ue frontend; do
      d="../$r"
      # -e 而不是 -d：../fay 作为 submodule，它的 .git 是一个指回
      # ../.git/modules/fay 的文件而不是目录；写成 -d 会把它误判成"不是仓库"。
      [ -e "$d/.git" ] || { echo "$r 不是 git 仓库" >&2; rc=1; continue; }
      branch=$(git -C "$d" rev-parse --abbrev-ref HEAD)
      up=$(git -C "$d" rev-parse --abbrev-ref '@{u}' 2>/dev/null || echo NO-UPSTREAM)
      # rev-list --count 用 TAB 分隔左右计数，不换掉就跟下面的 "0/0" 对不上
      counts=$(git -C "$d" rev-list --left-right --count "$up...HEAD" 2>/dev/null | tr '\t' '/' || true)
      counts=${counts:-?/?}
      dirty=$(git -C "$d" status --porcelain | wc -l)
      { [ "$counts" = "0/0" ] && [ "$dirty" = "0" ]; } || rc=1
      printf '%-11s %-14s %-22s %-11s %s\n' "$r" "$branch" "$up" "$counts" "${dirty} dirty"
    done
    echo
    echo "[audit] containerd 侧的全部改动都在这些目录里（上游零改动）："
    echo "  patches/  $(find patches -name '*.patch' | wc -l) 个补丁：$(find patches -name '*.patch' -printf '%P ' 2>/dev/null || echo 无)"
    # overlay 的计数要说清两件事：这里 find 到的是**本地目录**，含首跑生成、不入库的那几份；
    # 而 .example 的份数正好等于「Fay 会整份回写、所以只把模板交给 git」的那几份。
    echo "  overlay/  $(find overlay -type f | wc -l) 个覆盖文件（其中 $(find overlay -type f -name '*.example' | wc -l) 份是 .example 模板，配对的实值由首跑生成、不入库）"
    echo "  seed/     $(find seed -type f | wc -l) 个参考语料"
    echo "  probes/   $(find probes -name '*.py' 2>/dev/null | wc -l) 个测试件：$(find probes -name '*.py' -printf '%P ' 2>/dev/null || echo 无)"
    # tools/ 也是"不改上游、全在 containerd 侧"的一部分（跑 pytest 前重建测试库那个脚本），
    # 漏在这一行外面就等于 audit 少报一样东西。目录不存在时find 会报错，所以带 2>/dev/null。
    echo "  tools/    $(find tools -type f 2>/dev/null | wc -l) 个 containerd 自带工具：$(find tools -type f -printf '%P ' 2>/dev/null || echo 无)"
    # 本栈自己写的三份运行时代码也在这个口径里：adapter 是 backend↔Fay 那一跳的翻译，
    # frontend/ 是 CareEcho H5 的同源外壳，asr/ 是麦克风那条 FunASR 服务。它们不改上游，
    # 但 audit 不列就等于"改动藏在哪儿"少答了三处。
    echo "  自研服务  $(find adapter frontend asr -type f -name '*.py' 2>/dev/null | wc -l) 个 python 源文件：$(find adapter frontend asr -type f -name '*.py' -printf '%P ' 2>/dev/null || echo 无)"
    [ $rc -eq 0 ] && echo "[audit] 上游四个仓库干净" || echo "[audit] 有仓库不干净或与上游分叉" >&2
    exit $rc
    ;;
  upstream)
    # fork（chuan918/Fay）里配了 `upstream` = xszyou/Fay，合并上游前真正想知道的是
    # 「上游这几条会不会把 containerd/patches/fay 那几份补丁顶掉」—— 补丁是按字节贴的，
    # 上游一动被贴的那个文件，要等构建才炸。这里提前把每份补丁对 upstream/main 干跑一遍。
    # 不为它建 worktree（314MB 全量 checkout 换个只读判断不值），改成按补丁头把目标文件
    # 单独取出来摆一棵小树，patch -p1 --dry-run 只看这些文件，效果一样。
    shift
    here=$PWD
    [ -n "$(git -C ../fay remote get-url upstream 2>/dev/null)" ] \
      || { echo "[upstream] ../fay 里没有 upstream remote：git -C ../fay remote add upstream https://github.com/xszyou/Fay.git" >&2; exit 1; }
    git -C ../fay fetch --quiet upstream \
      || { echo "[upstream] 拉不到 upstream（xszyou/Fay）" >&2; exit 1; }
    echo "fork   origin/main  = $(git -C ../fay rev-parse --short origin/main)"
    echo "上游   upstream/main = $(git -C ../fay rev-parse --short upstream/main)"
    behind=$(git -C ../fay rev-list --count origin/main..upstream/main)
    ahead=$(git -C ../fay rev-list --count upstream/main..origin/main)
    echo "落后 $behind 条 / 领先 $ahead 条"
    [ "$behind" = 0 ] && { echo "[upstream] 没有要合的"; exit 0; }
    git -C ../fay log --oneline origin/main..upstream/main | sed 's/^/  待合 /'
    tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
    rc=0
    for p in "$here"/patches/fay/*.patch; do
      for f in $(grep -oE '^\+\+\+ b/[^[:space:]]+' "$p" | sed 's|^+++ b/||'); do
        mkdir -p "$tmp/$(dirname "$f")"
        git -C ../fay show "upstream/main:$f" > "$tmp/$f" 2>/dev/null || : > "$tmp/$f"
      done
      # --fuzz=0：容差不算数。合并后要的是「一字不差还能贴」，能靠 fuzz 糊过去的
      # 补丁在真实合并里通常已经贴错位置了。
      if (cd "$tmp" && patch -p1 --dry-run --silent --fuzz=0 < "$p") >/dev/null 2>&1; then
        echo "  可贴    $(basename "$p")"
      else
        echo "  贴不上  $(basename "$p") ← 合并之后这份要照新字节重做" >&2; rc=1
      fi
      rm -rf "$tmp"/*
    done
    if [ $rc -eq 0 ]; then
      # 用单引号：这行里的命令名写在双引号里会被当命令替换执行掉 —— 实测把
      # upstream/main 直接 merge 进了 fay 的 main，靠 reflog 才复位。
      echo '[upstream] 补丁全部还能贴，git -C ../fay merge upstream/main 之后 ./run.sh build 即可'
    else
      echo "[upstream] 有补丁贴不上：先合再按 containerd/README「补丁为什么贴不上：源文件是 CRLF」那节重做" >&2
    fi
    exit $rc
    ;;
  kbslice)
    # 项目方又投递语料包时跑一次。切片产物落 seed/kb_corpus/（**不进 git**：内容属于伙伴方，
    # 而两个仓库都是 PUBLIC —— 见 containerd/.gitignore 里那段），原件留在 ../uploads/
    # （根 .gitignore 挡着，27MB 里 22MB 是 Word 内嵌字体，不该进历史）。
    # 必须在容器里跑：宿主 python 没有 python-docx，而 yueshen-rag 镜像里已经有了。
    # --user 是必须的：不带的话产物归 root，下一次 `git checkout` 覆盖它会报权限。
    shift
    src=${1:-../uploads/老年康养通用科普.zip}; shift || true
    [ -e "$src" ] || { echo "[kbslice] 找不到语料包：$src（也可以 ./run.sh kbslice <zip|目录>）" >&2; exit 1; }
    src=$(readlink -f "$src")
    mkdir -p seed/kb_corpus
    docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp \
      -v "$PWD/tools:/tools:ro" -v "$PWD/seed/kb_corpus:/out" -v "$src:/src/in:ro" \
      dh-yueshen-rag:local python /tools/slice_kb_corpus.py /src/in /out "$@"
    echo '[kbslice] 切片已更新，接着 ./run.sh kb 才真的进知识库'
    ;;
  kb)
    # 「切片」只是准备，「导进知识库」要有一次真入库 + 真检索为证：上游 upsert_chunks
    # 对 embedding 失败是静默跳过这一条（server.py:363），只看出料数看不出问题。
    shift
    ensure_env
    use_profile
    # 不需要重建镜像：seed/kb_corpus 是运行期挂载，但挂载本身变了要 recreate 才生效。
    $COMPOSE up -d yueshen-rag
    # 这里写全命令而不是 `$COMPOSE run --rm kb-ingest "$@"`：compose 的 run 把服务名
    # 后面的参数当成**替换整条 command**，不是追加 —— 带参数时它会拿 "--sweep" 当可执行
    # 文件（实测：exec: "--sweep": executable file not found in $PATH）。脚本自己的默认值
    # 就是 compose 那条 command 里的 --base/--timeout 两个值，所以只写脚本名，参数交给默认。
    $COMPOSE run --rm kb-ingest python /probe/kb_ingest.py "$@"
    ;;
  kbq)
    # 知识库的**业务**判据：不直调工具，而是从 Fay 的业务口问一句话、取那一问的原始回帧，
    # 判「这一问有没有真的驱动 query_yueshen、检索回来的话有没有被用进回答」。为什么必须
    # 这样测：`kb` 那组走 :5010 管理面，它红了只能说明链路断，说明不了业务里用户在听谁说话。
    # 判据细节与代价在 probes/kb_fay_probe.py 的 docstring 里。
    #   ./run.sh kbq                      默认那一问 + 默认期望词（135/85），7 条全判
    #   ./run.sh kbq <问题> [期望词]        换问法；不给期望词时「含词/用上了」两条记 SKIP
    shift
    ensure_env
    use_profile
    # 这一组不灌库、不重嵌入，但必须等 fay 过健康检查：判据读的是 :5010 的 prestart 清单，
    # 而那份清单要 Fay 的 MCP 管理面起来才读得到。yueshen-rag 只 service_started 就够 ——
    # 它没起的话第 2 条会红，而那正是这条判据想告诉你的事，不该由探针先去把它扶起来。
    $COMPOSE up -d --wait fay yueshen-rag 2>&1 | tail -3
    ask=${1:-}; want=${2:-}
    if [ -z "$ask" ]; then
      $COMPOSE run --rm kb-fay python /probe/kb_fay_probe.py
    else
      $COMPOSE run --rm kb-fay python /probe/kb_fay_probe.py --ask "$ask" --want "$want"
    fi
    # 6b 那条对照会把 query_yueshen 的 prestart 注册摘掉，问一轮再按摘之前读到的那份参数
    # 注册回去。正常退出（含 Ctrl-C）都走 finally 恢复；只有 `docker compose kill` 掉这个
    # 探针容器才会把注册留在摘掉的状态 —— 那份配置写在挂载进去的
    # faymcp/data/mcp_prestart_tools.json 里，症状是之后每一轮都没有知识库注入。
    # 恢复：管理台 :5010 里给这台工具重新勾上预启动，或直接改那个 JSON 后重启 dh-fay。
    ;;
  asr-seed)
    # 纯粹省一次下载：本机若跑过伙伴方那台 FunASR，它那个卷里已经下好了约 1.3GB 模型
    # （布局就是 MODELSCOPE_CACHE 期待的 models/<组>--<名>/snapshots/<revision>/）。
    # 拷过来 dh-funasr 首次启动就是本地命中（日志里那句 ASR MODEL src=local）。
    # 这一步**不是前提**：没有那个卷的新克隆，正常启动自己下载即可（健康检查给了 300s+）。
    shift
    ensure_env
    use_profile
    from=${1:-careecho-funasr-cache}
    docker volume inspect "$from" >/dev/null 2>&1 \
      || { echo "[asr-seed] 宿主没有卷 $from：这一步只是省下载，跳过即可（首次启动会自己下约 1.3GB）" >&2; exit 1; }
    # 卷名不写死第二份：compose 的卷名是 `<top-level name>_<短名>`，前半从 docker-compose.yml 取。
    proj="$(awk -F': *' '$1=="name"{gsub(/[ "]/,"",$2); print $2; exit}' docker-compose.yml)"
    to="${proj}_funasr-cache"
    # 那两个 label 不是可选的：compose 认卷只认 label（`project` + `volume`），不带它们建出来的卷，
    # 之后**每一条 up** 都要警告一句 "already exists but was not created by Docker Compose"
    # （实测 2026-09-21：asr-seed 建过卷之后 test/up 全带这行噪音，ps 不带）。
    # 但 label 只能在**建卷那一刻**给，`docker volume create` 对已存在的卷是静默 no-op
    # （同一天实测：本修复之前建的卷，重跑 asr-seed 之后 Labels 仍是 null、警告照旧），
    # 所以已存在又没 label 时只能把清理步骤如实说出来，绝不代删（那 1.3GB 可能是别人下好的）。
    have=$(docker volume inspect "$to" --format '{{json .Labels}}' 2>/dev/null || echo '')
    case "$have" in
      *com.docker.compose.project*)
        echo "[asr-seed] 卷 $to 已存在且带 compose label，直接往里拷" ;;
      '')
        docker volume create --label "com.docker.compose.project=$proj" \
          --label com.docker.compose.volume=funasr-cache "$to" >/dev/null
        echo "[asr-seed] 新建卷 $to（带 compose 那两个 label）" ;;
      *)
        echo "[asr-seed] 警告：卷 $to 已存在但**没有** compose label，那条 up 的警告这轮消不掉。" >&2
        echo "[asr-seed]       label 事后加不了，要消掉就得删卷重建。删的是这个卷里的缓存，" >&2
        echo "[asr-seed]       而本命令下一条会立刻从源卷拷满回去（源卷不在时才需要重下 1.3GB），" >&2
        echo "[asr-seed]       所以这一步留给你自己按下面这行执行，不代删：" >&2
        echo "[asr-seed]       $COMPOSE rm -sf funasr && docker volume rm $to && ./run.sh asr-seed" >&2 ;;
    esac
    echo "[asr-seed] $from → $to"
    # 用我们自己的镜像做拷贝：不为此多拉一个 busybox/alpine，而它本身就是 debian slim。
    docker run --rm --entrypoint sh -v "$from:/from:ro" -v "$to:/to" dh-funasr:local \
      -c 'cp -a /from/. /to/ && printf "  文件 %s 个 / %s\n" "$(find /to -type f | wc -l)" "$(du -sh /to | cut -f1)"'
    echo '[asr-seed] 已拷入；下次起 funasr 就是本地命中（docker logs dh-funasr 里看 ASR MODEL src=local）'
    ;;
  env)
    # 「我改的这行到底进不进那个容器」——一条命令看清 .env → 容器的映射，顺带对三份账。
    # 统计口径固定把**两份 compose 与两个 profile 全算上**：变量表是跨档位的，只按当前档位
    # 扫会把 dev 才引用的 FAY_BRIDGE_PORT / FUNASR_PORT 误报成没人读，也会把只有
    # fay-lite 读的 FAY_LITE_MODEL_ENGINE 报成死键。
    shift
    ensure_env
    use_profile
    python3 tools/env_map.py \
      --compose docker-compose.yml --compose docker-compose.dev.yml \
      --tier "$DH_ENV" "$@"
    ;;
  build)   ensure_env; use_profile; $COMPOSE build ;;
  smoke)   shift; ensure_env; use_profile; smoke ;;
  ps)      shift; use_profile; $COMPOSE ps ;;
  logs)    shift; use_profile; $COMPOSE logs -f --tail=120 ${1:-} ;;
  down)    shift; use_profile; $COMPOSE down ${1:-} ;;
  reset)   shift; use_profile; echo "[run] 将删除全部卷（DB/记忆/日志），5 秒内 Ctrl-C 取消"; sleep 5; $COMPOSE down -v ${1:-} ;;
  *)       echo "用法: $0 {up|build|test|smoke|audit|upstream|kbslice|kb|kbq|env|logs [svc]|ps|down|reset}" >&2; exit 1 ;;
esac

#!/usr/bin/env bash
# DigitalHuman 容器集成入口。所有命令都在 containerd/ 下执行（脚本自动切换）。
#   ./run.sh up        生成 .env（若缺）→ 构建 → 起全栈
#   ./run.sh test      跑全套功能测试：service pytest + 后端活体探针 + adapter 契约测试
#                      + 探针时机自测 + 三个 Fay 实例的契约探针 + ue 体检
#   ./run.sh test <组>  只跑指定测试件（backend-test / backend-probe / adapter-test /
#                      probe-selftest / probe-fay-lite / fay-probe / probe-origin-fay /
#                      ue-audit / probe-yueshen），改探针时不用等全套十几分钟
#   ./run.sh build     只构建镜像
#   ./run.sh smoke     打通链路冒烟测试：后端 → adapter → Fay → Ollama → 回库
#   ./run.sh audit     核账：四个上游仓库必须零改动、与上游不分叉，并列出 containerd 侧全部产物
#   ./run.sh logs [s]  看日志（s 可为 fay/backend/adapter/mysql/redis）
#   ./run.sh ps|down|reset
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

COMPOSE="docker compose --env-file .env -f docker-compose.yml"

ensure_env() {
  if [ -f .env ]; then return; fi
  # 随机 DB 密码与 JWT 密钥的生成逻辑收敛到 tools/gen_keys.py（单一来源）
  python3 tools/gen_keys.py
}

wait_http() { # wait_http <url> <deadline_sec> <name>
  local url=$1 deadline=$((SECONDS + $2)) name=$3
  until curl -fsS --max-time 3 "$url" >/dev/null 2>&1; do
    if (( SECONDS > deadline )); then echo "[run] 超时：$name 未就绪 ($url)" >&2; return 1; fi
    sleep 2
  done
  echo "[run] 就绪：$name"
}

llm_residency() { # 宿主机 Ollama 上每个模型的显存驻留比例（问答耗时的唯一解释变量）
  local port="${OLLAMA_PORT:-11434}"
  local out
  out=$(curl -fsS --max-time 3 "http://127.0.0.1:${port}/api/ps" 2>/dev/null |
    OLLAMA_PS_PORT="$port" python3 -c '
import os, sys, json
try:
    models = json.load(sys.stdin).get("models", [])
except Exception:
    print("  LLM 宿主 :%s 不可达 —— 第 5 步一定会失败，先起 ollama" % os.environ["OLLAMA_PS_PORT"])
    raise SystemExit(0)
if not models:
    print("  LLM 宿主: 没有已加载模型，首个请求要先换入（可能几分钟）")
for m in models:
    name = m.get("name")
    size = float(m.get("size") or 0)
    vram = float(m.get("size_vram") or 0)
    if size <= 0:
        print("  LLM 宿主: %s（size 未知）" % name)
        continue
    frac = vram / size * 100.0
    print("  LLM 宿主: %s 驻留显存 %.0f%%（%.0f/%.0fMB）" % (name, frac, vram / 1e6, size / 1e6))
    if frac < 50:
        print("           → 大半权重在内存里，问答是分钟级；要秒级请换小模型或腾显存")
' 2>/dev/null) || true
  [ -n "$out" ] && printf '%s\n' "$out"
}

smoke() {
  set -u
  source .env
  local base="http://127.0.0.1:${BACKEND_PORT:-8000}"
  # 先把 LLM 宿主的真实状态打出来：这条链路的耗时几乎全在宿主机 Ollama 上，
  # 权重有没有驻留显存决定这一发是 3 秒还是 300 秒。少了这句，第 5 步慢下来
  # 会被读成「后端或 Fay 坏了」——本机实测 15.2GB 显存被别的常驻服务占走，
  # qwen3.5:9b 只有 6.3% 进显存，一句话要 172~301s（fork 的规划器链路 >600s）。
  llm_residency
  # 注意：health 路由挂在 api_v1_prefix 下，真实路径是 /api/v1/health（见 app/api/router.py:25）
  echo "[smoke] 1/6 后端健康检查"
  curl -fsS "$base/api/v1/health" && echo
  echo "[smoke] 2/6 Fay Web API（经 adapter 的上游）"
  wait_http "http://127.0.0.1:${ADAPTER_PORT:-8010}/healthz" 60 "adapter"
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
  # 挑 origin-fay 下手：没有任何服务 depends_on 它，重启不打扰后端那条链。
  local cid mounts marker="smoke-persist-$$"
  cid=$($COMPOSE ps -q origin-fay)
  [ -n "$cid" ] || { echo "[smoke] origin-fay 没在跑，持久化这一步无从判起" >&2; return 1; }
  mounts=$(docker inspect --format '{{range .Mounts}}{{.Type}}:{{.Destination}} {{end}}' "$cid")
  case "$mounts" in
    *volume:/app/memory*) echo "  结构：origin-fay 的 /app/memory 是具名卷" ;;
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
  echo "[smoke]     restart origin-fay（宿主被压满时 :8765 可能要两分钟才回来，这里只等 :5000）"
  $COMPOSE restart origin-fay >/dev/null \
    || { echo "[smoke] restart origin-fay 失败" >&2; return 1; }
  wait_http "http://127.0.0.1:${ORIGIN_FAY_HTTP_PORT:-5100}/api/get-system-status" 240 "origin-fay 重启后" \
    || return 1
  printf '%s' "$persist_py" | docker exec -i "$cid" python - verify "$marker" \
    || { echo "[smoke] 持久化：重启后读不回来，卷没挂对" >&2; return 1; }

  echo "[smoke] 6/6 发一句话（走 Fay + 宿主机 Ollama）"
  echo "        耗时几乎全在 LLM：模型全驻显存时暖态 3~20s；上面那行驻留比例低，"
  echo "        这一发就是分钟级（本机实测 172~301s）。上限 SMOKE_MAX_TIME=${SMOKE_MAX_TIME:-600}s"
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
    # 区分「链路坏了」和「这台机器的显存不够」：前者要查，后者不是这条栈的缺陷。
    # 与 LLM 无关的那几段（后端→adapter→Fay 的 HTTP/WS/MCP/TTS）由 ./run.sh test
    # 里的探针确定性判掉，其中轻模型实例 fay-lite 专门证明问答链路本身是通的。
    echo "[smoke] 第 6 步没拿到非空回复。若上面那行驻留显存 < 50%，这是显存不够、"
    echo "        模型在 CPU 上推，不是链路坏：跑 ./run.sh test 看 fay-lite 那组，"
    echo "        或把 overlay/fay/system.conf 的 gpt_model_engine 换成能装进剩余显存的模型" >&2
    return 1
  }
  echo "[smoke] 全链路通过：后端 → adapter → Fay → Ollama → 落库"
}

case "${1:-up}" in
  up)
    ensure_env
    $COMPOSE build
    $COMPOSE up -d
    shift 2>/dev/null || true
    source .env
    wait_http "http://127.0.0.1:${BACKEND_PORT:-8000}/api/v1/health" 180 "backend"
    wait_http "http://127.0.0.1:${ADAPTER_PORT:-8010}/healthz" 180 "adapter"
    echo
    echo "Fay Web 管理台    : http://127.0.0.1:${FAY_HTTP_PORT:-5000}/"
    echo "上游 Fay (v4.8.1) : http://127.0.0.1:${ORIGIN_FAY_HTTP_PORT:-5100}/"
    echo "后端接口文档      : http://127.0.0.1:${BACKEND_PORT:-8000}/docs"
    echo "数字人 WS         : ws://127.0.0.1:${FAY_HUMAN_WS_PORT:-10002}"
    echo "跑 ./run.sh smoke 做端到端验证；./run.sh test 跑全套功能测试"
    ;;
  test)
    ensure_env
    shift 2>/dev/null || true
    # 只在「构建输入比镜像新」时重建对应服务。两个方向都咬过人：
    #  - `up -d` 只认镜像存不存在，不认镜像是不是比补丁新。改完 patches/ 或
    #    overlay/*requirements 直接跑 test，会静默测上一次构建的镜像 —— 这种绿什么都没测。
    #  - 但也不能无条件 build：这台机器的 BuildKit 缓存已经顶到 GC 上限（45GB / 只有
    #    13 条活跃），`compose build` 会把 pip 层当冷缓存重跑，单个镜像 5~15 分钟。
    #    实测 2026-09-20 15:55 那次重建就是纯粹被淘汰：输入一个都没变（拿镜像的 Created
    #    去 find -newermt，结果为空）。所以按输入时间判断，需要时才建。
    # 建失败立刻停在这里，绝不拿旧镜像继续测（补丁贴不上去时 patch 非零退出 -> build 失败）。
    stale=""
    before=""
    for svc in fay origin-fay backend yueshen-rag; do
      case "$svc" in
        fay)        src="images/fay.Dockerfile patches/fay overlay/fay" ;;
        origin-fay) src="images/fay.Dockerfile patches/origin_fay overlay/origin_fay" ;;
        backend)    src="images/service.Dockerfile patches/service overlay/service" ;;
        yueshen-rag) src="images/yueshen_rag.Dockerfile patches/yueshen_rag overlay/yueshen_rag" ;;
      esac
      # 镜像名不写死，让 compose 自己报；报不出 ID = 本地没有 = 必须建。
      # 每步都带 || true：这个脚本跑在 set -euo pipefail 下，探测性命令不能把脚本带崩。
      iid=$($COMPOSE images -q "$svc" 2>/dev/null | head -1 || true)
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
        new=$($COMPOSE images -q "$svc" 2>/dev/null | head -1 || true)
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
    $COMPOSE up -d --wait mysql redis fay origin-fay backend 2>&1 | tail -3
    # fay-lite 是 test profile 里的轻模型实例（qwen2.5:1.5b），只有它能在被其他
    # 服务占满显存的机器上秒级答完，用来判定「问答链路是否真的通」。
    $COMPOSE --profile test up -d --wait fay-lite 2>&1 | tail -3
    ALL_GROUPS="backend-test backend-probe adapter-test probe-selftest fay-probe probe-origin-fay probe-fay-lite ue-audit probe-yueshen"
    # 允许 ./run.sh test fay-probe 只跑一组：改探针时不必再等 20 分钟全套。
    # 变量名不能叫 GROUPS —— bash 的内置只读数组（当前用户的 gid），赋值会被吞掉。
    # 默认顺序把 probe-fay-lite 排在两个 9b 实例之前：ollama 是单条队列，
    # 9b 那两组「客户端超时」不等于「服务端停止生成」，探针不等了模型还在推，
    # 于是排在后面的 lite 只能排队。run #5 里 lite 的直连探测就是这么被挤到
    # 180s 超时的（qwen2.5:1.5b 暖态本来 0.2s 答完）。lite 是唯一能给出
    # 「问答链路真的通」正面证据的一组，必须先拿。
    # adapter-test 紧跟其后：它量的是本目录自己写的适配器（containerd/adapter），
    # 全程打假 Fay、不碰 ollama，几秒跑完。把「我们自己有没有写坏」和「机器忙不忙」
    # 这两件事分开，前者不该被后者的抖动掩盖。
    # probe-selftest 排在它后面，同一个道理：它量的是探针自己的「收工时机」
    # （probes/ws_timing_test.py），假 Fay 出帧、不碰 ollama，也是几十秒。
    # backend-probe 紧跟 backend-test：同一条理由 —— 它量的是这个正在应答 HTTP 的
    # 进程 + 业务库的真实 schema，也不碰 ollama，十一条判据几秒钟。
    # probe-yueshen 排在全套最后，为的是显存而不是逻辑：它要打宿主 ollama 的**嵌入**模型
    # 才能把语料灌进 chromadb，而这台机器显存只够放一个模型，换入换出实测 70s 起。
    # 放在三组问答之前，等于我们自己制造一次模型换出、把问答判据挤成超时（那红的是
    # 排队，不是被测对象）。它自己第一发就是奔着「量嵌入出口 + 顺手把模型预热进显存」
    # 去的，所以这条链路的耗时证据仍然取得到。
    DEFAULT_GROUPS="backend-test backend-probe adapter-test probe-selftest probe-fay-lite ue-audit fay-probe probe-origin-fay probe-yueshen"
    TEST_GROUPS="$*"
    [ -n "$TEST_GROUPS" ] || TEST_GROUPS="$DEFAULT_GROUPS"
    for g in $TEST_GROUPS; do
      case " $ALL_GROUPS " in
        *" $g "*) ;;
        *) echo "[test] 没有这组测试件：$g（可选：$ALL_GROUPS）" >&2; exit 2 ;;
      esac
    done
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
    # 未打补丁的镜像）：fay-lite 5.27s/exit=1、origin-fay 5.45s/exit=1。
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
    printf '%-11s %-6s %-20s %-11s %s\n' REPO BRANCH UPSTREAM AHEAD/BEHIND WORKTREE
    for r in fay origin_fay service ue; do
      d="../$r"
      [ -d "$d/.git" ] || { echo "$r 不是 git 仓库" >&2; rc=1; continue; }
      branch=$(git -C "$d" rev-parse --abbrev-ref HEAD)
      up=$(git -C "$d" rev-parse --abbrev-ref '@{u}' 2>/dev/null || echo NO-UPSTREAM)
      # rev-list --count 用 TAB 分隔左右计数，不换掉就跟下面的 "0/0" 对不上
      counts=$(git -C "$d" rev-list --left-right --count "$up...HEAD" 2>/dev/null | tr '\t' '/' || true)
      counts=${counts:-?/?}
      dirty=$(git -C "$d" status --porcelain | wc -l)
      { [ "$counts" = "0/0" ] && [ "$dirty" = "0" ]; } || rc=1
      printf '%-11s %-6s %-20s %-11s %s\n' "$r" "$branch" "$up" "$counts" "${dirty} dirty"
    done
    echo
    echo "[audit] containerd 侧的全部改动都在这些目录里（上游零改动）："
    echo "  patches/  $(find patches -name '*.patch' | wc -l) 个补丁：$(find patches -name '*.patch' -printf '%P ' 2>/dev/null || echo 无)"
    echo "  overlay/  $(find overlay -type f | wc -l) 个覆盖文件"
    echo "  seed/     $(find seed -type f | wc -l) 个参考语料"
    echo "  probes/   $(find probes -name '*.py' 2>/dev/null | wc -l) 个测试件：$(find probes -name '*.py' -printf '%P ' 2>/dev/null || echo 无)"
    # tools/ 也是"不改上游、全在 containerd 侧"的一部分（跑 pytest 前重建测试库那个脚本），
    # 漏在这一行外面就等于 audit 少报一样东西。目录不存在时find 会报错，所以带 2>/dev/null。
    echo "  tools/    $(find tools -type f 2>/dev/null | wc -l) 个 containerd 自带工具：$(find tools -type f -printf '%P ' 2>/dev/null || echo 无)"
    [ $rc -eq 0 ] && echo "[audit] 上游四个仓库干净" || echo "[audit] 有仓库不干净或与上游分叉" >&2
    exit $rc
    ;;
  build)   ensure_env; $COMPOSE build ;;
  smoke)   shift; smoke ;;
  ps)      shift; $COMPOSE ps ;;
  logs)    shift; $COMPOSE logs -f --tail=120 ${1:-} ;;
  down)    shift; $COMPOSE down ${1:-} ;;
  reset)   shift; echo "[run] 将删除全部卷（DB/记忆/日志），5 秒内 Ctrl-C 取消"; sleep 5; $COMPOSE down -v ${1:-} ;;
  *)       echo "用法: $0 {up|build|test|smoke|audit|ps|logs [svc]|down|reset}" >&2; exit 1 ;;
esac

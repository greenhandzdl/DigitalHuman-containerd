# containerd/AGENTS.md —— 改这个仓库之前该知道的那一半

这份和 `README.md` 是**分工**，不是主附：README 讲"这东西怎么配、怎么起、边界在哪"，
能照着敲；这里放的是**决策为什么这么做**、**每一次失败当时怎么排出来的**、以及
**这台机器上不属于本栈的前提**。想部署不必读它，想改配置、改判据、改补丁必须先读它 ——
否则很容易把一条被实测否证过的决定再改回去，或者把"当时那台机器的数字"当成今天的承诺。

> 这里每一节都带日期和当轮 run 号。数字是**当时那套配置**（端点、模型、显存占用）量出来的，
> 换配置之后它们只是历史证据，不是判据。2026-09-23 那次挪进来时，README 里指向本节的位置
> 都留了 `AGENTS.md`「小节名」的指引，本节里指向 README 未挪内容的位置反过来写着 `README.md`。

## 决策与理由（为什么长成这样）

### 这一条命令是被一次真事逼出来的（2026-09-23）

把对话切到那台 26B（栈外那个 OpenAI 兼容服务）时往 `.env` 写了：

```
FAY_GPT_MODEL_ENGINE=gemma-4-26b-a4b-nvfp4
FAY_BIG_MODEL_ENGINE=gemma-4-26b-a4b-nvfp4
```

而 `docker exec dh-fay env | grep MODEL_ENGINE` **一条都没有**，`docker exec dh-fay python -c
"from utils import config_util as c; c.load_config(); print(c.gpt_model_engine)"` 印出来的是
`system.conf` 里的 `qwen3.5:9b`。原因是 `x-llm-endpoint` 那个锚点只透传了
patches/fay/0009 放开的端点与密钥，漏了 0006 放开的模型名两条 —— lite 实例反而有
（`FAY_LITE_MODEL_ENGINE` 单独写在它自己那段里），所以这个洞只有主实例会踩。

它为什么没被发现：**那个端点不校验 `model` 字段**。它 `/v1/models` 实测只返回
`['gemma-4-26b-a4b-nvfp4']` 一条，请求里写 `qwen3.5:9b` 它照答，于是"配的模型名"和
"真正在答的那个模型"这两件事一直没对上过账面，而 `kbq` / `smoke` 全绿。换一台严格一点的
OpenAI 兼容服务端（vLLM 默认就校验），症状立刻变成每一发 500 —— 那才是这个洞真正的代价。

修法就是把两条键补进锚点，默认值仍是空：补丁写的是
`os.environ.get('FAY_GPT_MODEL_ENGINE') or 原值`，空串走 `or` 分支，所以
**不设这两条时逐字等于改之前**（回落 `system.conf`），新克隆的行为一点没动。
复跑 `./run.sh kbq` 仍是 9/10 + 1 SKIP、退出码 0、8.1s 一发 —— 这一问的注入与正文与换名之前
同构，因为对端本来就只有一个模型。

**同一个洞在探针身上又踩了一次**（同日修）。`fay-probe` 那组只挂了 `*llm-timeouts`，
没挂 `*llm-endpoint`，于是它的「本地 LLM 主机就绪（直连，不经 Fay）」判据读的是
`system.conf` 里那两份示例值 —— 拿着宿主 ollama + `qwen3.5:9b` 去量一个已经在打那台 26B 的
实例。它没坏，只是量的不是这台机器今天的事，而这种"证据悄悄失效"比判红更糟。修法同样是
把锚点挂全（`<<: [*llm-timeouts, *llm-endpoint]`），判据语义一个字没动，改的是它量的对象。
复查方式就是 `./run.sh env`：那一行现在写着 `FAY_GPT_BASE_URL → fay fay-probe`。

### 真实瓶颈是显存，不是代码（为此打的补丁）

探针有一阵子反复出现同一类失败：HTTP 问答 90s 拿不到一个字、`:10002` 收不到
`Data.Key=text`、`/v1/chat/completions` 精确地在 **180.0s** 返回空 `content`。
查下来根因在这台机器的显存上，与容器封装无关：

| 测到的事实 | 数字 |
|---|---|
| GPU | RTX 4080 SUPER 16GB，`nvidia-smi` 显示 **15.2GB 已被占走**（当时记成「本机其他常驻服务」；2026-09-23 复核：主体是栈外那台对话推理服务自己，单进程 10742 MiB） |
| `ollama ps` | `qwen3.5:9b` 标着 `94%/6% CPU/GPU` —— 实际是 CPU 在推 |
| `ollama /api/ps` 的字节账 | `size_vram=387029401` / `size=6149206178` = **只有 6.3% 权重进了显存** |
| 直连 Ollama 一发 | 只生成 8 个 token 用了 **51.4s**（另一轮实测 39.1s） |
| Fay 容器日志 | `请求超时 (尝试 N/3): ...port=11434... Read timed out. (read timeout=60)` 成串 |
| 一轮问答触发的 embedding 请求 | **8 次**（日志 `发送 embedding 请求` 计数） |

同一个模型、同一份 `system.conf`，两个实例的表现却不一样 —— 这是把「容器封装坏了」
排除掉的关键一条对照：

| 实例 | 模型 | 一句话问答 |
|---|---|---|
| `origin-fay`（上游 v4.8.1） | qwen3.5:9b | 出真回复，**172.1s / 301.2s** |
| `fay`（fork） | qwen3.5:9b | **>600s** 仍拿不到字，最后落进管线兜底语 |

差额是 fork 自己加的那层规划器（`llm/nlp_cognitive_stream.py:3059` 的
`_call_planner_llm`，走同一张图但多几发 LLM）。在显存够用的机器上是产品力的差别，
在这台显存被占满的机器上是「一个数量级的墙钟时间」，与容器无关。

> 这一行对照属于**旧基线**：那时的 `fay/` 是 `45b44e9` 那份源码导入，规划器那层是它自己加的。
> 2026-09-21 起 fork 换成 `chuan918/Fay`（v4.8.1 的直接后代），Python 源码与上游逐字节相同，
> 这张表量不出现在了 —— 现在两个实例的差别只有 config，不再有代码档。保留它是为了说明
> 「墙钟时间可以差一个数量级而两处都不是容器的问题」这条判据的来历。

放大链路写死在上游几个数字里 —— `utils/api_embedding_service.py:110` 是
`timeout=60` + `max_retries=2`，`llm/execution_manager.py:203` 是小模型 `timeout=60`。
显存不够 → 加载 609MB 的 embedding 模型就得把 6.6GB 的对话模型挤出去，下一轮再换回来 →
一发 embedding 必然 >60s → 超时 → **整条请求重发 3 遍 = 180s** →
正好撞上 `gui/flask_server.py:51` 的 `_STREAM_READ_IDLE_TIMEOUT = 180`，
非流式问答于是「180s 内一个字都没等到」，按兜底分支返回空串。
看起来像 Fay 坏了，实际是重试把时间预算吃光，而 180s 的空闲上限又小于
LLM 自己的超时 —— 两条时间线本来就没对齐（实测 `/v1/chat/completions`
精确地在 180.0s 返回，而同一发请求在 origin-fay 身上 172.1s 就出字了）。

修复仍然不进 Fay 仓库：`containerd/patches/` 把这四个数字开成环境变量，
compose 的 `x-llm-timeouts` 锚点给 `fay` / `fay-lite` 两个服务注入 ——

| 环境变量 | 容器里的值 | 作用 | 补丁 |
|---|---|---|---|
| `LLM_REQUEST_TIMEOUT` / `LLM_REQUEST_MAX_RETRIES` | `420` / `0` | 真正的回答给足时间，且不再重发（重发只会让回答更晚） | `fay/0001` |
| `EMBEDDING_TIMEOUT` / `EMBEDDING_MAX_RETRIES` | `90` / `1` | 仿生记忆检索那发 embedding 的预算。**这里吃过一次教训**：起初按「容器里不要死等」压成 `20` / `0`，run #7 否证了它 —— ollama 换入模型实测要 72.9s，20s 必然超时，除了静默落回 `simulation_engine/gpt_structure.py:294` 的模拟向量，还把开机线程堵在一串重试后面，`:8765` 拖到第 61 秒才 bind（见下面「就绪判据」）。90s 按实测最坏值给，换入完成后一发 embedding 只要 0.06s | `fay/0002` |
| `STREAM_REPLY_IDLE_TIMEOUT` | `480` | 回复空闲上限必须 ≥ 上面那条 LLM 超时，否则「模型正在慢慢想」被判成「Fay 卡住」 | `fay/0003` |
| `FAY_GPT_MODEL_ENGINE` / `FAY_BIG_MODEL_ENGINE` | 缺省不注入 | 换掉 `system.conf` 里的模型名，给显存不够的机器留一条「换个吃得下的小模型」的路（`fay-lite` 用这个跑 qwen2.5:1.5b） | `fay/0006` |

补丁里未设置环境变量时的默认值与上游逐字一致（不打补丁 = 原行为）。
显存被挤走时问答侧仍会慢到分钟级，那是环境而不是配置问题，探针按下面的三态处理。

#### 就绪判据：五个契约端口全监听，不是「`:5000` 能连就算起」

两个 Fay 实例（`fay` / `fay-lite`）共用一份镜像，也共用 compose 里
`x-fay-healthcheck` 这一个锚点：`5000`（Flask）、`5010`（MCP 管理面）、`8765`（MCP SSE）、
`10002` / `10003`（两条 WS）全 bind 才算 healthy。原先那份只看 `:5000`，实测开机
`:5000/:5010/:10002/:10003` 第 6 秒就全在听，`:8765` 要等到第 24~61 秒 ——
`--wait` 于是提前放行，探针打进一个只起了一半的实例，run #7 的
`MCP SSE 握手 ConnectionRefused`、`OpenAI 兼容层 超时`、`/api/send 空回复` 三条 FAIL
全是这一个原因（同一轮里 WS 侧的问答、audio 帧、wav 取回反倒全绿，因为那几个口先起）。
探针侧另加一道 `wait_port()`：握手前先等 `:8765` 监听，等不到才判 FAIL，
详情里带上「等了几秒」—— 晚起是事实，不是故障，但不该由测试替它圆场。

`x-fay-healthcheck` 的**实现方式**也改过一版，原因是它自己在污染容器日志。旧写法对
五个口各做一遍 `socket.create_connection` 半开握手，而 `:10002`/`:10003` 是
`websockets` 的 legacy server：一次只连不发的 TCP 探测会让它在 `websockets/legacy/server.py:230`
打一对 `connection failed (400 Bad Request)` + `connection closed`。健康检查 `interval: 10s`
× 每轮两条 WS 口 = 每个空闲实例每小时约 720 对这种纯自己制造的噪音，把基于日志取证的其他判据
一起淹了。换成读 `/proc/net/tcp`+`/proc/net/tcp6` 的 LISTEN 表（`st == '0A'`，端口取本地地址
hex 段），零字节打到业务端口就能判定五口全听；`:8765` 是 uvicorn、裸连不打日志，但为一致性
一并走这张表。run #7 那条「`:8765` 到第 24~61 秒才起」的经验仍然守住 —— 五口全 LISTEN 才 healthy。
换完实测：一个空闲实例 60s 内新增 `connection failed` 行为 `0`（此前是每 10s 一对）。

这条判据在 **restart 场景**下被独立验证过一次（比开机更能说明问题）：
`docker compose restart fay-lite` 5.9s 就返回，容器里 `:5000/:5010/:10002/:10003`
立刻可连，而 `:8765` 与 `:10001` 在 **140s 之后仍未监听**（当时宿主被两组 9b 压满）。
健康检查因此一直保持 `starting`，`--wait` 不肯放行 —— 这正是把它纳入判据的意义：
宁可慢放行，也不放行一个半启动的实例去制造假红。同一轮里也顺手量到了另一件事：
重启前后 `/api/get-msg` 都能读回同一条 `id=83` 的消息行，说明记忆确实落在
`fay-memory` 具名卷里而不是容器的可写层（这条后来固化成 `./run.sh smoke` 的第 5 步，
故意排在最后那次问答**之前**：它不依赖 LLM，就不该被显存不够连累）。

还有一处 60s 是**故意不碰**的：`simulation_engine/gpt_structure.py:21` 那个
`httpx.Timeout(60.0) + max_retries=1` 的 openai 客户端只服务 `ask_gpt` /
`gpt4_vision`（写死 `model="gpt-4o"`），全仓 grep 不到问答链路上的调用方，
`llm/nlp_cognitive_stream.py:50` 从这里只 import `get_text_embedding`，
而那个函数走的是已开成环境变量的 `utils/api_embedding_service`。

#### 探针怎么判：PASS / FAIL / SKIP 三态 + 一条不依赖显存的功能路

光把超时放宽会变成「等到天荒地老然后报个红」，测试就失去意义了。`probes/fay_probe.py`
因此定了六条规矩（前四条管判定语义，第五条管时机，第六条管覆盖面）：

**1) 兜底语不算回复。** run #2 里有一条被记成
`PASS HTTP 问答链路 … 243.3s, 22 字: 抱歉，我的大脑暂时开了小差，请稍后再试一下。`
——那是管线自己承认失败的兜底串（fork 里 `nlp_cognitive_stream.py:2258/2871/2945/3066`
四处之一），不是回答。现在 `reply_verdict()` 先按 `PIPELINE_FALLBACKS` 三串过滤。

**2) 环境不够就降级成 SKIP，但必须带客观证据。** `check_llm_host()` 先绕过 Fay
直连 `.env` 里那个对话端点量一发下限耗时 —— **判不判降级由这一发决定**；再从
`ollama /api/ps` 读 `size_vram/size` 作为解释性证据。驻留不足 50% 或单发 >30s 才置
`DEGRADED_LLM_HOST`（阈值来自实测分布：1.5b 全权重进显存时暖态 0.2s，9b 6% 驻留时 39~240s）。
此后耗时类判据走 `latency_verdict()`，
真不过 = FAIL，只有拿着这条证据才允许 SKIP，退出码只数 FAIL。
问答预算也不再一路抬到 `--max-timeout`：显存不足时按 `--timeout` 快速判掉
（run #3 就是每条问答都撞满 600s 客户端超时，白烧墙钟时间）。**直连探测自己超时**
也算证据而不是故障：run #5 的 origin-fay 那组，同一条日志里 `frac_txt` 已经写了
「驻留显存 6%（387/6149MB）」，判据却报 FAIL —— 自相矛盾，现在这类超时进 SKIP 分支，
只有连不上 / 4xx（= 对话端点配错）才留 FAIL。

`/api/ps` 那一条从"取不到就判红"改成"取不到只进 detail"，是 2026-09-23 被一次假红逼出来的：
对话端点搬到栈外那台 26B 之后它不再是 ollama，没有 `/api/ps` 这个口，于是直连 1.9s 拿到
「好」的一发被记成 FAIL —— 证据取不到和被测对象坏了是两件事，前者不该冒充后者。反向的
负面自检也跑了：把端点指到一个没人监听的口，这条判据确实红（`URLError(ConnectionRefusedError)`）。

**3) 下游判据不重复计账。** `audio` 帧的存在性、以及按 `HttpValue` 去 HTTP 取文件，
都以「这轮真有了回复」为前提。run #5 里这两条被记成
`FAIL …（Keys=['log','question']）`—— 一个字都没播，音频自然没有，它是同一条 LLM
依赖的下游，不是独立的容器故障，所以走 `latency_verdict()`。反方向仍然硬失败：
客户端声明 `Output=false` 却收到 audio 帧，那是服务端漏推，与模型无关。

同一组判据还有第三种错法：**收工收早了**。run #10 里 `Output=true` 那条被判成
FAIL（`Keys=['log','question','text']`，text 明明到了），而同一份代码在 run #9 是
PASS —— 差别只在 audio 帧并不和 text 同时到（#10 里 text 到了、之后 6s 静默内什么都没来；
#9/#11 里它在稍后被收到），而 `check_ws()` 收到 text 之后只等 6s 静默就收工。它的下游
是「整句合成完才推」，这个节奏不是 6s 能盖住的。现在窗口按「这个会话还欠不欠 audio」
分两档（`AUDIO_GRACE_SECONDS=45` / 6s），服务端中途关连接也不再丢弃已收到的帧。
run #12 给了这条修复最想要的对照：lite 与 origin-fay 两个实例的 `Output=true` 会话
都等到了 audio 帧并取回 wav（264642 / 563110 字节），而 `fay` 那一轮的会话根本没等到
文字，于是它按 `--timeout` 就收工、没有多花那 45s —— 窗口只放宽给「确实还欠一帧」的会话，
不给死掉的宿主。
**判据自己也会写错**这件事不该只靠再跑 20 分钟全套去发现，所以它有一份不打 LLM、
不看显存的时机自测：`probes/ws_timing_test.py`（七种时机各量一次，其中一条把窗口
退回 6s 并断言"必须收不到"，防止这条判据变成摆设；另有三条量收尾那行的统计口径）。

**4) 没执行到的判据不能算 PASS。** MCP 那组的 `ping` 在 `origin_fay` 上能不能调到，
取决于它 `test/mcp_stdio_example.py` 的 `autostart=false` 之后那条「现场连接离线服务器
tools」有没有成功 —— 在线的原本只有知识库。run #4 却把这条印成
`PASS … —— 该实例在线的服务器里没有暴露 ping`。
假绿比红更糟，改成 SKIP 并把原因写进详情。判据是「没执行到就 SKIP」，不是「环境不配合
就默认通过」，所以它在 run #10 记 SKIP、在 run #12 记 PASS 都是这套语义的正常结果。

**5) 量之前先确认「它真的起完了」。** 端口 bind ≠ 服务可用，这条是 run #7/#8 用两次
误报换来的：`compose --wait` 只看健康检查，健康检查原先只摸 `:5000`，而 `:8765`
（MCP SSE）要晚 5~40 秒、MCP 的「开机自连」又要再晚 1.6 秒。探针打早了就会得到
`ConnectionRefused`（SSE 还没监听）和 `在线 无`（自连还没跑完）这种看着像故障的红。
现在分两层堵：compose 侧健康检查要求五个契约端口全监听（`x-fay-healthcheck`），
探针侧 `wait_port()` 等 `:8765`、`check_mcp_tools()` 先等 `autostart=true` 那几台
在线再取快照（上限 90s，等不到才 FAIL）。等待本身算进详情，方便下次判断是不是又起慢了。

**6) 只管得着容器化的判据也要有。** `check_console()` 取 `:5000` 根页面，再把页面里
引用的每个 `/static/*` 逐个 GET，要求 200 且非空。三个 Fay 实例实测各 **22 个资源全过**。
这条与上游功能无关（少拷一层目录、`.dockerignore` 排掉静态资源、镜像里 `web` 路径不对
都会红），而问答链路对它完全无感 —— 容器化改坏的东西，得有一条判据专门盯着容器化。

**测试件的顺序**也被这条链路咬过：ollama 是单条队列，而且**同一时刻只保留装得下的
模型**。9b 占着 6GB 时，1.5b 第一次请求要先换出 9b 再换入自己 —— 实测 72.9s，
换入后同一发请求 14.9s。更要命的是「探针客户端超时」不等于「服务端停止生成」，
前面那两组 9b 的残留生成会一直堵着队列，于是 run #5 里 `probe-fay-lite` 的直连探测
被挤到 180s 超时，唯一能给出正面证据的一组反而降级了。现在 `./run.sh test` 的默认
顺序把不打模型端点的那些组排在前面，lite 排在主实例那两组问答之前
（清单与逐组的位置理由写在 `run.sh` 的 `ALL_GROUPS` 那段注释里）。
对话端点搬到栈外那台 26B 之后，上面那组排队数字成了历史，但**这条顺序不变**：两组问答
每问一次都还要往宿主 ollama 打好几发仿生记忆的 embedding，而 lite 的对话和嵌入本来就
在同一台 ollama 上 —— 队列还在，只是从"对话队列"缩成了"嵌入队列"。

SKIP 只说明「这台机器判不了这一条」，所以问答链路的**真实功能**由 compose 里第三个
Fay 实例 `fay-lite` 兜住：同一个镜像、同一套补丁、同一份 `system.conf`，只是模型换成
装得进剩余显存的 `qwen2.5:1.5b`（`FAY_LITE_MODEL_ENGINE` 可改），不开宿主端口、
有自己的一套 memory/cache/logs 卷和一份可写的 `overlay/fay-lite/config.json`
（config.json 是人设可写状态，两个容器不能共用同一份）。它 43 条判据里 **41 PASS /
2 SKIP / 0 FAIL**（run #21 首次 36/39，run #24 加上决策面谈那三条后 39/42，run #27 再因
连上的 yueshen 加一条 `MCP 工具清单 server_id=4`、`yueshen rag` 退出白名单 SKIP 变两条 PASS，
成 41/43），
包含 `Data.Key=audio` 出帧、`HttpValue`
真的取回 wav（#21 469970 字节 / #23 385298 字节 / #24 544058 字节 / #27 328146 字节），
以及 MCP 那一整组不打 LLM 的确定性判据（含 prestart 那三档；剩下 2 条 SKIP 是无头白名单里的
`window capture` 与本机没起的 FunASR，`yueshen rag` 这一轮已经是两条正面 PASS，见规矩 4）。
**2026-09-22 打开 `autostart` + 配好 prestart 之后复跑：42 条里 40 PASS / 2 SKIP / 0 FAIL**。
少的那一条是 `MCP 现场连接离线服务器 yueshen rag` —— yueshen 现在开机就在 online 池里，探针不必
再替它现场连（正是这轮要的效果），`MCP 工具清单 server_id=4` 那条仍在。同一次复跑里
`MCP 预启动工具注销后清单回到空` **红了第一次**：它假设探针登记前清单是空的，而 overlay 那份配置让
实例一开机就带着 `(4, query_yueshen)`，探针登记的却是 id=6 上的另一个工具。判据已改成「回到登记前
那一份」（`MCP 预启动工具注销后清单回到登记前`）—— 配置是配置、残留是残留，而注册/注销共用一次
**整表回写**，误删会顺着 bind-mount 写进宿主那份 JSON 等着被提交，所以这条等式要比「我那条没了」更严。

一个一直咬人的坑：v4.8.1 这份源码里 `utils/api_embedding_service.py`、
`gui/flask_server.py`、`fay_booter.py`、`utils/config_util.py`、`main.py`（还带 BOM）都是
**CRLF** 行尾，补丁必须在字节层对得上 —— GNU patch 2.8 没有 `--strip-trailing-cr`，
`-l/--ignore-whitespace` 也不认 CR，用 LF 上下文生成的补丁一份都贴不上去（旧基线上那 5 份
就是这么作废的）。补丁生成器因此必须按 **bytes** 捕获 `diff` 的输出，`text=True` 会走
universal newlines 把 `\r` 吞掉，生成出的补丁再也贴不回 CRLF 仓库。
另外 `max_tokens` 在这条链路上是有害的：qwen3.5 带思考段，实测 `max_tokens=16`
时 68.8s 后 `content` 仍是空（直连 ollama 也一样，`eval_count=8` 全花在 reasoning）。

### 远程音频那两个缺陷：`patches/fay/0004` 的来历

`README.md`「远程音频输入」那张"打补丁前 / 后"的表里，第二条（远程 PCM 跨过服务端 VAD）
是这次新加判据时**第一次跑就抓出来的**，而且是两条独立的缺陷：

1. **`origin_fay`（贴近上游那份）的监听线程一上线就自杀。** 上游提交
   `74b49ae`（"大小模型逻辑重构 + 单模型模式 + 多处优化"）把 `except` 从 `pass`
   改成「打日志 + `__running = False`」并在循环后 `close()` socket，但
   `DeviceInputListener.__init__` 里仍是 `self.thread.start()` 在
   `self.deviceConnector = deviceConnector` **之前** —— `run()` 第一句就读
   `self.deviceConnector`，于是新线程几乎必然抛 `AttributeError`。改之前这只是
   1 秒后重试自愈的一次静默异常；改之后它变成「线程永久退出 + 关掉刚 accept 的
   socket」，客户端表现就是 connect 后立刻 `ConnectionReset`。服务端日志是决定性
   证据，每条连接一行：
   `[系统] 远程音频设备 User 监听异常，停止监听线程: 'DeviceInputListener' object has no attribute 'deviceConnector'`
   （用户名还是默认的 `User`，说明线程在读到注册帧之前就死了。）
   补丁只调这两行的顺序，不动上游那套「记日志 + 退出」的语义。
2. **（旧基线）`fay`（fork 那份）每条远程音频连接泄漏一个 fd 和两个线程。** 它的
   `while self.deviceConnector:` 既不查 `__running` 也不判 `recv()` 返回空字节，
   对端优雅关闭（FIN）后线程就在这条死连接上空转，`stop()` 置的 `__running`
   也退不出内层循环。实测（`/proc/1/task` / `/proc/1/fd`，PID 1 就是 `main.py`）：
   5 次 connect/disconnect 让 `dh-fay` 从 `线程=66 fd=96` 涨到 `线程=76 fd=101`，
   再等 15 秒（keepalive 走完一轮）只回落到 `75/100`；断开完全靠 10 秒一次的心跳
   扫出来（日志里断开时刻总是落在 10s 网格上）。同一份测量在 `dh-origin-fay` 上
   是 `90→92→90` 且 fd 全程不动 —— 它不泄漏，代价是连接根本用不了。
   补丁把上游 `74b49ae` 的三段（`and self.__running` / `if not data: break` /
   线程退出前 `close()`）移植过来，**并且同时带上顺序修正**：只移植上游那段会把
   fork 也变成第 1 条那个死线程，等于用一个缺陷换另一个。
   打完补丁复测：fd `38→40→38`、线程 `31→36→34`，约 40 秒后回到基线 31
   （线程要等自己那一轮 `sleep(1)` 收尾，比 fd 慢是正常的），
   服务端日志出现补丁新增的 `[系统] 远程音频设备 <user> 连接已关闭，停止监听线程`。

> **基线已换，读这两条前先记一句**：上面量的是**旧** `fay/`（`45b44e9`，与上游无共同历史的
> 一份源码导入）。2026-09-21 起 `fay/` 子模块指向 `chuan918/Fay@f702528`，那是 v4.8.1 的直接
> 后代、Python 源码与上游逐字节相同 —— 于是**第 2 条那个 fd/线程泄漏在新基线上不存在**（它
> 属于旧导入版），只剩第 1 条竞态；两个实例现在共用 `patches/fay/0004` 这一份补丁。两段复测
> 数字都保留原文，因为它们是当时那两个镜像的真实测量，只是不再描述当前的 `dh-fay`。

还有一处**没分离干净的变量**，留在这里免得下一个人把它当成已证结论：`fay` 那组在打补丁前
是 0 帧，而 `core/recorder.py:267` 在 `wake_word_enabled=false` 时只要 `FeiFei.speaking`
为 True 就丢弃拾音、`speaking` 又挂在共享对象上（`fay_core.py:1888` 置位，只有 `play_end()`
清）与用户名无关 —— 这条 latch 看着能解释 0 帧，**但实测不成立**：在 `fay-lite` 上先做一次
`Output=true` 的真回复（audio 帧在 +7.6s 到达），立刻喂同一段 PCM，VAD 照样出 `聆听中...`，
等 120s 再喂还是出 —— 播放模拟循环走完会调 `play_end` 把 `speaking` 清掉。那个实例当时
已经跑了 4 小时、中间有大量超时中断的会话，重启之后（同时打上 0004）才变绿，两个变量
没有分离。能确定的是竞态和泄漏都读得出来、补丁后测量值真的变了；不能确定的是那次 0 帧的
成因，所以这里不下结论。

**探针里那条"远程音频判据排在同组任何问答之前"仍然成立**（`main()` 里 `check_mcp_sse` 之后、
`check_llm_baseline` 之前）：`speaking` 那把锁虽不解释上面的红，但它确实存在，一次成功的
带音频回复会短暂打死拾音 —— 排在问答前是零成本的预防。

### 依赖层为什么不能直接用 fay/requirements.txt

镜像里的依赖清单是 `overlay/fay/requirements-docker.txt`（约 1.1GB 镜像），
按启动链模块级 import 收窄，其中五处是**必须显式处理**的版本冲突：

| 处理 | 原因 |
|---|---|
| 不装 `langchain` 伞包，只装 `langchain-core` + `langchain-openai` | 伞包会把 langgraph 解析到最新，而 `langgraph>=1.2.11` 依赖 `langgraph-sdk>=0.4.2`，后者才要求 `websockets>=14` —— 与上游 `websockets~=10.4` 冲突（`core/wsa_server.py:7` 用 `websockets.legacy`，`core/socket_bridge_service.py:44` 是两参数 handler） |
| ~~`langgraph>=1.2,<1.2.11` + `langgraph-sdk<0.4`~~（**2026-09-21 起不装**） | 旧基线（`45b44e9` 那份导入）里 `llm/nlp_cognitive_stream.py:2556` 有一道 fork 自己加的门禁：不装 langgraph 就把 `tool_registry` 清空、日志 "workflow tools are disabled and the app will use direct LLM mode"，MCP 工具整条路是死的 —— 当时确实按上面那两行钉版本装上了（实测 `langgraph 1.2.2 + sdk 0.3.15`，`websockets` 保持 10.4）。换成 `chuan918/Fay` 之后全仓只剩 `requirements.txt:28` 那一行提到 langgraph、没有任何 `.py` import 它（`_LANGGRAPH_AVAILABLE` 也 grep 不到），门禁随旧代码一起没了，所以共用的一份清单里不再装 | 
| 钉 `uvicorn<0.35` | `mcp` 带进来的 uvicorn 0.53 在 `protocols/websockets/auto.py` 直接 import `websockets.server.ServerProtocol`（10.4 没有），MCP SSE 8765 起不来；0.34.3 实测正常 |
| 钉 `mcp>=1.2,<2`（两个 Fay 都钉，实测解析 1.30.0） | 上游 `requirements.txt:31` 是裸 `mcp`，如今解析到 2.2.0，而 2.x 把 `mcp.server.Server` 换成了高层 `MCPServer`，低层那套 `@server.list_tools()` / `@server.call_tool()` 注册装饰器没有了。两个仓库各有 6 个文件按 1.x 低层 API 写：`faymcp/mcp_server.py:26` + `mcp_servers/{logseq,schedule_manager,window_capture,yueshen_rag}/server.py` + `test/mcp_stdio_example.py`，2.x 下全是 `AttributeError: 'Server' object has no attribute 'list_tools'`。最后一台是 `faymcp/data/mcp_servers.json` 里 `autostart: true` 的「tools」，于是每次开机都白记一条「tools 连接失败」。钉回 1.x 后开机 `tools 已连接`、`/api/mcp/servers/1/tools` 出 5 个工具、`ping` 真回 `pong`。另一条佐证：这台机器开发者自己的 `fay/.venv` 装的就是 `mcp-1.6.0.dist-info` |
| 补 `aliyun-python-sdk-core` | `main.py:86` 无条件 `from asr import ali_nls` → `asr/ali_nls.py:9` 需要 `aliyunsdkcore`，即使完全不用阿里云 ASR |

不装 torch / sentence-transformers / chromadb / opencv / PyQt5 / pygame：
它们在文字问答启动链上都不出现，装了镜像要涨数 GB。
（chromadb 后来确有去处，但不是这份镜像 —— 见 `images/yueshen_rag.Dockerfile` 与
`README.md`「yueshen 知识库」那节，理由是它会把 `uvicorn[standard]` 的上界冲开。）

构建上下文本身也要收窄（根目录 `.dockerignore`），但**排除必须按"到底哪一部分大"来切，
不能整目录一刀切**：`fay/test/` 289MB 里 289MB 全是 `ovr_lipsync`（一个 Meta 的
UE 唇形插件源码），那几个 `.py` 加起来才 300KB —— 而其中 `test/mcp_stdio_example.py`
正是 `faymcp/data/mcp_servers.json` 里 `autostart: true` 那台 stdio 示例服务器要跑的
文件。整个目录排掉，等于每次开机少一个 MCP 服务器 + 白记一条失败日志（这条排查了
两轮才定位到，因为报错在容器里长得像"上游代码的问题"）。
而且 `.dockerignore` 的语义是**目录一旦排除，就没法再按文件放回来**（`!fay/test/x.py`
不生效），所以只能写成 `fay/test/ovr_lipsync` 这种精确路径。

### pip 缓存挂载只给还在改 requirements 的那一层（2026-09-21 量过）

给 `images/yueshen_rag.Dockerfile` 的 `pip install` 层加
`--mount=type=cache,target=/root/.cache/pip`（并去掉 `PIP_NO_CACHE_DIR`）之后量到的：
向镜像源的实际吞吐只有 ~22–170 kB/s，一次冷缓存要拉 chromadb 那个 23.3 MB 的 wheel 约
1067s；缓存挂载命中后同一层重放只要 **2.1s**（整轮 pip 阶段冷建累计到 t≈3263s）。

但这道挂载对**已经全绿**的 Fay/service 镜像是有害无益的，所以那两层故意不给：pip 层的缓存
key 是被 COPY 进去的 requirements 文件的**内容哈希**，requirements 没变时 BuildKit 本来就
整层复用、根本不重跑 pip，加不加 cache mount 结果一样；而 cache mount 会把构建产物偷偷留在
BuildKit 那侧（这台机器的 BuildKit 缓存已顶到 GC 上限 45GB），一旦哪天换 requirements，
它会用缓存里的旧 wheel 补上，绕开「requirements 是这一层唯一输入」这个干净假设。
所以只给全新、还在反复调 requirements 的 yueshen 层用，成熟镜像维持不带。

另一个坑是同一件事的副作用：同一份 Dockerfile 并发跑两次 build 会把这条本就慢的链路带宽
对半劈、两次都更慢 —— 一次建、盯到底。

### 知识库那三个旋钮：这轮只动了结论撑得住的那一个（2026-09-22 06:44）

`./run.sh kb --sweep 3,5,8`（同一套 12 问、同一份 576 向量库）量出来 recall@k 在 k=3/5/8
三档都是 **12/12** —— 表是平的。三个旋钮里这轮只动了结论能支撑的那一个：

- **`top_k` 取 3，不取 5。** k=3 已经把 12 问全捞回，多注入的两条只是让 prompt 变长、
  模型更可能整段抄原文而不是答问题。所以 `overlay/{fay,fay-lite}/mcp_prestart_tools.json`
  的 `params.top_k` 钉在 3；判据饱和之前不该靠加大 k 换"看起来更准"。
- **距离度量维持 L2、不加阈值。** 上游建 collection 没传 `hnsw:space`，所以是默认 L2，
  且 `query` 不做任何阈值过滤（空库才返回空）。开 cosine + 阈值是
  `patches/yueshen_rag/0003` 的活，触发条件是「top-3 里混进无关片段导致 MISS」——
  这轮 12/12 没有 MISS，**所以那份补丁不打**，别让一次没有证据的改动进补丁表。
  留一手可判断据：这轮的 L2 距离实测量程是 0.367（最紧的「康复训练有哪些禁忌情况？」）
  到 1.008（「人上了年纪肌肉…」第 8 名），真要做阈值，落在 0.55 附近才切得开
  "期望那份"与"顺带回来的那份"，且必须先有一批 MISS 来验它切掉的是噪声不是答案。
- **切片粒度这轮不动。** `--max-chars 900` + 服务端二次切 600/120 是那次
  516 片段 → 575 chunk 对账的前提，而 12 问的 recall 在 k=3 就饱和了：**换成 900/150 再跑
  一遍也不会让 12/12 变成 13/12**，这把尺子量不出差别。要动它得先加更难的金标问题
  （跨片段、需要上下文拼接才答得对的那种），否则比较没有读数。

这条「按数据择优」本身有反向对照守着：`--sweep` 末尾那条 PASS 会临时把某条金标的期望词
换成一个语料里确实不存在的串，要求这一档**必须变红**；它红了才说明 12/12 是检索给的，
不是判据写松了。

## 逐轮实测记录与失败复盘

这一节分两种组织方式：**先按轮次**（run #27 ~ #32，一轮一段，那段完整的时间线读起来
就是一轮的现场），**再按类型**（同一种错在不同轮里反复出现时，按轮切开反而看不出根因 ——
于是「假红的来源」「判据反证」「探针收工时机」「空回复」各收成一节 —— 标题里能带 run 号的
都带了，读的人才知道它收的是哪几轮。

### run #27：换基线前最后一轮九组全绿（2026-09-21 02:13，`[test] 全部通过`）

它是第一次把 `probe-yueshen` 组、`patches/service/0002`（服药漏扫时钟冻结）和探针的
120s 排空重问一起放进完整一轮 —— 前两件各治一个 run #25 暴露的红，这一轮两者都没再红，
重试分支本轮也没触发（`重问` 全轮 0 次，见下面「`:10002` 上的空回复」那节）。
与 #21/#23 一样，这一轮环境也不给退路：两个 9b 实例的权重都是 100% 驻留显存
（`qwen3.5:9b 5490/5490MB`，直连 fay 侧 6.3s / origin 侧 9.4s），lite 侧
`qwen2.5:1.5b 1166/1166MB`、直连 1.1s，于是 `check_llm_host()` 没有置 `DEGRADED_LLM_HOST`
—— 所有耗时类判据这次都是**硬判**：过就是真过，不过就记红。

这一轮之后 `dh-fay` 换了基线（见 `README.md`「跟上上游」），所以下面这些数字连同 #24 那两张
表都是**旧基线**上的证据；新基线上又跑过五轮完整的 —— **#28**（换基线后九组）、**#29**
（撤掉参照实例后八组）、**#30**（外部语料进库）、**#31**（接上 CareEcho H5，十组）、
**#32**（补上麦克风这条链，十三组，当前最新一次完整运行，但它**不是全绿**：两条红 +
一处按环境收工），逐轮记在本节后面。

| 组 | run #27 实测 |
|---|---|
| `backend-test` | **39 passed（2.76s）**（service 自带 pytest，跑在每轮重建的空测试库里，见 `README.md`「测试库必须是空的」） |
| `backend-probe` | **11 PASS / 0 SKIP / 0 FAIL**（十一条，见 `README.md`「后端活体探针」） |
| `adapter-test` | **11/11**（本目录自己写的 adapter，全程假 Fay，不碰显存） |
| `probe-selftest` | **[ws-timing] 10/10**（探针自己的收工时机自测，假 Fay WS 服务端，不碰显存也不出网）。第八到第十条是 run #21 之后补的收尾口径判据 |
| `probe-fay-lite` | **41/43 通过 · 2 SKIP** —— 问答、MCP（含 yueshen rag 3 工具）、prestart、TTS、音频、决策面谈全部正面通过 |
| `ue-audit` | **2 PASS / 0 SKIP / 0 FAIL**（两条构建完整性判据，见 `README.md`「UE 仓库的容器侧处置」） |
| `fay-probe` | **41/43 通过 · 2 SKIP**（fork，qwen3.5:9b；本轮与 lite 同底数 43，重试分支没触发） |
| `probe-origin-fay` | **42/44 通过 · 2 SKIP**（上游那份，同一模型；多的两条见下） |
| `probe-yueshen` | **6/6**（chromadb 那条链路进容器后的独立一组：嵌入出口、工具清单、入库 `chunks=1 inserted=1`、检索带 MARKER、`stats.vectors == inserted`、配置指本栈容器） |
| 收尾一条 | `[test] fay-lite 优雅停止（SIGTERM）退出码 0：一次正常的 stop 没被记成崩溃`（见「优雅停止」一节） |

退出码只数 FAIL，所以九组都是绿的。三个实例的 SKIP 数相同（43/43/44 条里各 2 条），
而且**这一轮没有一条是环境降级来的**：剩下的两条 SKIP 是 `window capture`
（无头容器里连不上的服务器，`README.md`「未覆盖能力」记的边界，与显存无关）和
`远程音频的 ASR 认出了文字`（卡在本机当时没起的 FunASR，宿主侧 `ws://host.docker.internal:10197`）
—— 语音输入那条链上能测的两级反而是实打实的 PASS：`:10001` 连得上并注册了用户名，
`远程 PCM 跨过了服务端的 VAD` 在 12s 内收到 `['聆听中...']`。上一版记的第三条 SKIP
`yueshen rag` 这一轮已经变成两条正面 PASS（`MCP 现场连接离线服务器 yueshen rag` 连上取到
3 个工具、`MCP 工具清单 server_id=4 yueshen rag`），所以每实例的 SKIP 从 3 降到 2、
lite/fork 底数从 42 升到 43。这六条 SKIP 的收尾文案这次也是被量过的：三组印的都是
「**其余 2 项 SKIP（成因逐条列在下面，与本轮 LLM 证据无关）**」，没有再替环境揽下
「因本机 LLM 证据降级」这个没发生过的嫌疑。

问答那一档每格都是当轮实测的字数，不是兜底语：

| 实例 | 单次问答预算 | `/api/send → /api/get-msg` | :10002 `Output=false` | :10002 `Output=true` |
|---|---|---|---|---|
| `fay-lite` | 45s | 3.3s / 1 字 | 44 字 | 17 字，audio 帧 HTTP 取回 328146 字节 |
| `fay`（fork） | 90s（LLM 基线 18.0s） | 20.9s / 1 字 | 43 字 | 52 字，audio 326028 字节 |
| `origin-fay` | 90s（LLM 基线 2.5s，直连 9.4s 是换入成本） | 27.4s / 1 字 | 68 字 | 56 字，audio 266758 字节 |

三格 `Output=true` 都是第一发命中 —— 重试分支没有触发（全轮 `重问` 0 次），所以 lite 与
fork 这轮底数同为 43。

origin 组比 lite/fork 多的那一条判据（44 vs 43）与重试**不是同一回事**：origin 的
`tools` 服务器 `autostart=false`，开机不在线（这轮它的管理面只报 `在线 [(6, '课程知识库')]`），
于是「现场连接离线服务器 tools」本身成了一条判据；连上之后 `MCP stdio 示例工具真调用`
才有东西可调，这次它 PASS。run #10 那轮这条没执行到，按 SKIP 记 —— 它在两次运行之间浮动的
原因是现场连接的时序，不是容器集成。没执行到不等于通过，所以宁可记 SKIP
（即上面规矩 4）。

`probe-yueshen` 那六条在这一轮各看到了什么（README 只留了链条形状，数字在这里）：
配置 `id=4 transport=sse ip=http://yueshen-rag:8766/sse` → 嵌入出口直连 ollama
`/v1/embeddings` **1024 维 0.0s**（`qwen3-embedding:0.6b`，已在显存里，所以是 0.0s；
下一轮冷的时候它就不是这个数）→ 工具清单 `['ingest_yueshen','query_yueshen',
'yueshen_stats']` → 入库 `chunks=1 inserted=1 用时 0.1s`（语料 `/app/corpus`，嵌入端点
`http://host.docker.internal:11434/v1`）→ 检索「编号里的专项随访要求里，血压与体重要在多长
时间内录回系统？」回 **1 条命中 251 字**、里面带着构建期造进 docx 的 MARKER →
`yueshen_stats vectors=1` 对上 ingest 的 `inserted=1`（持久化 `/app/persist`、集合
`yueshen_kb`）。注意这一轮的语料只有镜像层里那份合成的 1 条 —— 外部那 576 块是下一轮
（run #30）才进来的，所以这里的 `1` 与后面的 `576` 不是同一件事被改动了两次。

#### run #24 那份逐条输出：lite 组 43 条每一条当时看到什么

同一份 `probes/fay_probe.py` 分别打三个实例（`fay` = fork、`origin-fay` = 上游那份、
`fay-lite` = 同镜像换 1.5b —— 前两个实例的那份表是撤掉参照实例之前的，现在探针打的是 `fay`
与 `fay-lite`）。下表是 run #24（2026-09-20 23:44）那次 lite 组的逐条实际输出，作为最细颗粒
的留档保留（run #27 相对它的结构变化只有两处：`yueshen rag` 那行从 SKIP 变两条 PASS、
`:10002` 文字播报第一发命中没有触发重试）：

| 检查项 | 实测 |
|---|---|
| 就绪 + Web 控制台 | `/api/get-system-status` 通；页面引用的 **22 个 `/static/*` 全 200 且非空** |
| 直连 LLM（不经 Fay） | 0.0s，`qwen2.5:1.5b` 权重驻留显存 100%（1166/1166MB） |
| MCP 管理面 `:5010` | 6 台配置，在线 `[(1,'tools'), (6,'课程知识库')]`，2 台 autostart 自连完成（等 2s） |
| MCP 现场 connect 离线服务器 | `Fay日程管理` 5 个工具、`logseq` 9 个工具，验完 disconnect；`yueshen rag` / `window capture` 在无头白名单内 → SKIP |
| MCP 工具清单 | `tools` → `['add','echo','now','ping','upper']`；知识库 → **8 个** `kb_*`；日程 5 个；logseq 9 个 |
| MCP 工具真调用 | `ping` → `text='pong'`；`kb_list_sources` → 4045 字、`count=8`；`get_schedules` → 2 条真实日程 |
| MCP SSE `:8765/sse` | `event: endpoint` + `session_id=…`（验 `uvicorn<0.35`+`websockets~=10.4` 钉法） |
| MCP 预启动（prestart）三档 | 注册进 `:5010` 且 runnable 清单看得到 → 一句普通问答后 **`:10003` 的 `panelReply` 里出现 `<prestart keep="true">` 包着的 `kb_list_sources` 真实输出** → 注销后清单回到**登记前那一份**。不是"回到空"：overlay 里那条 `(4, query_yueshen)` 是配置，探针登记的是 id=6 上另一个工具，而注册/注销共用一次整表回写 —— 要求全空就会把配置当成残留摘掉，顺著 bind-mount 写进宿主的 JSON |
| 远程音频输入口 TCP `:10001` | 连得上、用户名注册成功（`probe_mic_…`），随后 16k 单声道 PCM 推 12s |
| 远程 PCM 过 VAD | 通：12s 内收到 1 条 `log` 帧，内容 `['聆听中...']` —— recorder 真的判定"有人在说话" |
| 远程音频的 ASR | **SKIP**：VAD 已过、识别结果为空，ASR 后端是宿主侧 `ws://host.docker.internal:10197`（`ASR_mode=funasr`），本机没起 FunASR，与容器化无关 |
| 决策面谈 `:5001`（genagents）三条 | `POST /api/start-genagents` → HTTP 200 且 `:5001` 0.0s 内 bind；`GET :5001/` → 200、30094 字节且含那句指令；`POST :5001/api/shutdown` → monitor 线程 1.5s 内 release 端口 |
| OpenAI 兼容层 `/v1/chat/completions` | 通，0.4s |
| HTTP `/api/send` → `/api/get-msg` | 通，3.3s，1 字「好」 |
| WS :10003 面板契约 | 通，12 帧，扁平 `panelMsg/panelReply/robot`（顶层还有 `Username`） |
| WS :10002 human 契约 | 通，`{Topic:'human', Data:{Key,Value}}`；`Output=false` 会话 4 帧、`Output=true` 会话 5 帧 |
| `Data.Key=text` 文字播报 | 通，35 字 / 31 字真实中文回复（「我是ChatGPT，我能够回答各种问题、提供信息、执行任务等多种任务。」/「我不需要介绍我自己，因为我是一个用于提供学习资料和工具的工具。」，不是兜底语） |
| `Data.Key=audio` 音频帧 | **只跟客户端注册的 `Output` 走**：`Output=false` 无 audio，`Output=true` 的 `Keys=['audio','log','question','text']` |
| audio 帧的 `HttpValue` | 通，`GET http://fay-lite:5000/audio/sample-*.wav` 取回 544058 字节 |
| TTS `ms_tts_sdk.Speech.to_sample()` | 通，`edge_tts` 出网合成 188436 字节 wav |
| 仿生记忆向量真伪 | 通：**真向量 1024 维、0.0s**，与上游兜底算法逐维比对不一致（首维差 0.0285），出口 `qwen3-embedding:0.6b` |
| `Lips` 口型字段 | 三个实例都没有 —— 符合预期（上游只在 Windows 分支产出） |

与模型无关的那半张表（`/api/get-system-status`、22 个静态资源、WS 注册、:10003 面板契约、
`human` 帧形、`Output=false` 不推音频、`Lips` 缺失、TTS 出网、MCP 管理面/清单/SSE、
prestart 那三档、`远程 PCM 跨过 VAD`、日程工具真调用、**决策面谈那三条**）在两个 9b 实例上
同样是 PASS —— run #24 的 fork 组连 audio 帧都取回了 389532 字节、origin 组 577928 字节
（正文 80/56 字与 65/59 字）。容器集成部分没有回归。而 run #12 里 origin-fay 连问答前提那几条
也是正面通过的（230 字回复 + audio 帧 + 563110 字节 wav + 1024 维真向量，耗时 55.4s），
这是「`AUDIO_GRACE_SECONDS` 修的是探针自己的时机、不是把故障糊过去」最直接的对照：
同一份探针、同一条 9b 链路，#10 假红、#12 真绿。

#### 后端那 39 个用例当时跑在什么形状上

9 个 alembic revision 在干净的 `dh-mysql` 上跑到 head，`care_echo_rehab`（业务库）与
`care_echo_rehab_test`（pytest 独立库）各 38 个对象（33 张 ORM 表 + `alembic_version` +
4 个视图：`v_admin_incident_dashboard` / `v_elder_home_summary` / `v_elder_training_today` /
`v_volunteer_queue_pending`）。其中 7 个用例原本是红的，两类原因：参考语料缺失（6 个，
见 `README.md`「三种手段」里的 seed 层）和上游测试自身的 bug（1 个，
`patches/service/0001`）。跑多之后又冒出第二处上游测试 bug，以及一类"跑多了才红"的 bug，
各成一节记在下面。

### run #28：换基线之后的第一轮完整九组（2026-09-21 下午）

`fay/` 换成 `chuan918/Fay@f702528`、补丁与依赖整套重落之后跑的。九组全绿、退出码 0，
但**不是一口气一轮跑完的**：换基线后先单跑 `probe-fay-lite`（13:4x），两组 9b 随后
14:13~14:58，其余六组 15:02~15:20（其中四组为把每组数字逐条抄全又重跑了一遍，
`39 passed` 那次就是这次记的）。

| 组 | run #28 实测 |
|---|---|
| `backend-test` | **39 passed（3.34s）** |
| `backend-probe` | **11 PASS / 0 SKIP / 0 FAIL** |
| `adapter-test` | **11/11** |
| `probe-selftest` | **[ws-timing] 10/10** |
| `probe-fay-lite` | **41/43 通过 · 2 SKIP** —— 与 run #27 同一档：问答、MCP（含 yueshen rag 3 工具）、prestart、TTS、远程 PCM 跨 VAD、决策面谈全部正面通过 |
| `ue-audit` | **2 PASS / 0 SKIP / 0 FAIL** |
| `fay-probe` | **35/45 通过 · 8 SKIP（LLM 证据降级）+ 2 SKIP（边界）** |
| `probe-origin-fay` | **38/46 通过 · 6 SKIP（降级）+ 2 SKIP（边界）**（跑完之后这个参照实例被撤掉了，见本节开头）|
| `probe-yueshen` | **6/6** |
| 收尾一条 | `[test] fay-lite 优雅停止（SIGTERM）退出码 0` |

与 run #27 最要紧的差别是**这一轮 `check_llm_host()` 置了 `DEGRADED_LLM_HOST`**：
`qwen3.5:9b` 不再 100% 驻留（同机还有别的进程占着显存），直连第一发 fay 侧 73.6s、
origin 侧 34.5s，都是"要先换入"的量级。于是探针按设计把问答耗时类判据降成 SKIP 而不是
记红 —— 两组合计 14 条降级 SKIP 全是这个成因，逐条理由印在探针输出里，没有一条是 FAIL。
能在 9b 上正面过的仍然是那批不依赖首字延迟的：MCP 清单/真调用/SSE 握手、prestart 三档、
TTS 出网、`:10003`/`:10002` WS 契约、决策面谈三条、`远程 PCM 跨过 VAD`（12s 内 `['聆听中...']`）。
QA 链的正面证据由 lite 那组给。**这不是换基线换来的回归**：run #27 之所以能全是硬判，
是因为当时显存刚好腾得下两份 9b。

两组各多出的那 2 条底数（45/46 vs run #27 的 43/44）同理 —— `fay_probe.py` 这轮只改了
一处路径字符串（`patches/origin_fay/0004` → `patches/fay/0004`），断言集没变，
是「收帧不足就重连一遍」的重试分支这轮真触发了，日志里能看到
`probe_..._ws100020_r2` 这样的二次注册判据。run #27 特意记过「`重问` 全轮 0 次」，
这次反过来证明了那条分支是可达的。

另有两条与本轮 LLM 无关的老 SKIP，两个 9b 实例都是它们：`window capture`（无头容器连不上
的桌面服务器，「未覆盖能力」记的边界）与 `远程音频的 ASR 认出了文字`（VAD 已过、
本机没起 FunASR）。容器集成部分没有回归。

`overlay/*/mcp_servers.json` 会被运行期回写这件事，本轮也留下了具体后果：跑完测试
`git status` 必脏（探针现场 `connect`/`disconnect` 与 Fay 自己写 `connection_time` 都落在这
三份 bind-mount 的可写文件上，当时还挂着 origin-fay 那一份），而 `run.sh test` 的
「构建输入是否比镜像新」又是按 mtime 判的，所以下一轮一上来就白重建一次镜像 —— 镜像 ID 没变（这三份是运行时
挂载、根本没进镜像），BuildKit 全量命中缓存，所以只是空转。**没有**为此改挂载方式：
把 `faymcp/data/` 换命名卷要动运行期注册表的落盘位置，收益不值那个风险。提交前
`git checkout -- overlay/<实例>/mcp_servers.json` 即可，纯时间戳漂移。
（**run #30 之后"下一轮白重建一次"这半句失效了**：那道闸的输入清单改成只点名
`overlay/**/requirements*.txt`，见 run #30 那节；`git status` 必脏照旧，`checkout --` 仍是正确动作。）
`config.json` 那份回写值得单说一句，因为它让"纯漂移"这个判断不能凭 diff 行数下：Fay 用
`json.dump` 默认口径写回，中文全变成 `\uXXXX` 转义、行尾换行也没了，26 行的 diff 解码回来
与种子一字不差 —— 但同一份文件里可能夹着**真状态**：`record.enabled` 就被运行期翻成了 `true`
（**不是探针干的** —— `grep -rn record probes/` 只命中读源码注释那几行，是管理台/配置面板那侧写的）。
所以 `checkout --` 之前先读 diff 的**语义**而不是形状。三份可写挂载各自的脏法、以及为什么
上面那句"`checkout --` 仍是正确动作"从 2026-09-23 起只对 `mcp_servers.json` 无条件成立，
见 `README.md`「不改上游代码的三种手段」那一节。

### run #29：撤掉参照实例之后的第一轮八组（2026-09-21 17:24~17:45）

`origin_fay` 整条撤掉（compose 服务、镜像 ARG、四个卷、overlay、探针组、文档）之后跑的，
21 分钟走完，八组全绿、退出码 0。

| 组 | run #29 实测 |
|---|---|
| `backend-test` | **39 passed（3.30s）** |
| `backend-probe` | **11 PASS / 0 SKIP / 0 FAIL** |
| `adapter-test` | **11/11** |
| `probe-selftest` | **[ws-timing] 10/10** |
| `probe-fay-lite` | **41/43 通过 · 2 SKIP（都是边界，本轮无降级）** |
| `ue-audit` | **2 PASS / 0 SKIP / 0 FAIL** |
| `fay-probe` | **39/43 通过 · 2 SKIP（LLM 证据降级）+ 2 SKIP（边界）** |
| `probe-yueshen` | **6/6** |
| 收尾一条 | `[test] fay-lite 优雅停止（SIGTERM）退出码 0` |

底数从 run #28 的 45/46 回到 43/43：多出来的那 2 条是「收帧不足就重连一遍」的重试分支
（日志里形如 `probe_..._ws100020_r2`），这轮没触发。43~46 浮动是设计内的，不是断言集变了。

显存比 run #28 还紧（`qwen3.5:9b` 权重只驻留 6%，387/6149MB，直连第一发 47.3s），
`DEGRADED_LLM_HOST` 照样置起，但**降级只剩 2 条而不是 8 条**：问答链本身这轮是拿到真回复的
（`:10002` 两轮 `Data.Key=text` 分别 60 字、43 字，音频帧也按 Output 开关各就各位），
被降级的只有「直连下限」那一条测量和 `HTTP /api/send → /api/get-msg`（90.1s 内 0 字，
按环境判 SKIP 而不是红）。同一轮 lite 侧 1.5b 满驻留（1166/1166MB、直连 3.4s），
所以那组一条降级都没有。

`fay` 镜像这轮被 mtime 判成「构建输入比镜像新」而重建了一次 —— 因为 `images/fay.Dockerfile`
真被改过（`FAY_SRC`/`PATCH_DIR`/`REQS_DIR` 三个 ARG 删掉，而它们正在 `COPY` 的指令文本里）。
日志当场把这次印成「重建后镜像 ID 没变（2738eb20c5bc）」，**那句话是错的**：`dh-fay:local`
现在指向 `0db394dea218`、`Created=17:30:35` 正落在这轮里。错因在核对 ID 那一步用
`docker compose images -q <svc>` 读 ID，而那张表第一列是 CONTAINER —— 旧容器还在跑时它报的是
「容器用的镜像」而不是 tag 指向的镜像，于是重建完 tag 已经挪走、它还在拿旧 ID 跟旧 ID 比。
run #30 起改成只跟 compose 拿 `REPOSITORY:TAG`、ID 用 `docker image inspect` 按 tag 现查
（`run.sh` 的 `image_id_of()`）。**但"内容没变"这个结论本身是对的**，只是它不该由那条日志担保：
`docker image inspect` 对两个 ID 逐层比 `.RootFS.Layers`，前 7 层（base + apt + pip）摘要一字不差，
第 8~12 层全换了摘要 —— 而 `COPY` 会把源文件的 mtime 打进 tar 头，`fay/requirements.txt`
在 17:05 被 touch 过一次（内容与 HEAD 一致，`git -C fay status` 到这一步仍是 0 dirty），
再加上那三条 `COPY` 的指令文本本身改了 cache key。重新导出的是层，不是代码。

### run #30：外部语料进库之后的第一轮八组（2026-09-21 18:03~18:25，22 分钟）

第一次把 `seed/kb_corpus/`（项目方那 14 份科普文档的切片，见 `README.md`「外部语料」）挂进
`yueshen-rag` 之后跑完整一轮，八组全绿、退出码 0。

| 组 | run #30 实测 |
|---|---|
| `backend-test` | **39 passed（3.64s）** |
| `backend-probe` | **11 PASS / 0 SKIP / 0 FAIL** |
| `adapter-test` | **11/11** |
| `probe-selftest` | **[ws-timing] 10/10** |
| `probe-fay-lite` | **41/43 通过 · 2 SKIP（都是边界，本轮无降级）** |
| `ue-audit` | **2 PASS / 0 SKIP / 0 FAIL** |
| `fay-probe` | **40/45 通过 · 3 条 LLM 证据降级 + 2 条边界 SKIP** |
| `probe-yueshen` | **6/6 —— 入库 `chunks=576 inserted=576 用时 5.1s`、`vectors=576`** |
| 收尾一条 | `[test] fay-lite 优雅停止（SIGTERM）退出码 0` |

**知识库那组是这一轮的主角**：向量库从 1 条涨到 576 条之后，六条判据一条没松动，MARKER
检索仍然排进 top3（回来的还是那 251 字）。唯一的物理变化是嵌入模型换入的成本 ——
`知识库嵌入出口可用` 这条单独跑时是 0.1s，这一轮 9b 正压着显存，它花了 **58.1s**；
0002 补丁把这条超时开到 180s 就是为了这种情况。`fay-probe` 侧同一轮里
`仿生记忆向量真伪` 也是 56.3s 真向量，两条对得上。

`fay-probe` 的分母是 45 而不是 43：多出来的 2 条是「收帧不足就重连一遍」的重试分支
（日志里 `probe_..._ws100020_r2` / `_ws100021_r2`）。这轮它真的触发了两次 —— 两处
`WS :10002 收到文字播报` 第 1 发都是正文为空（只回 `log` + `question` 两帧），探针按
120s 排空重问，第二发分别拿到 45 字、42 字，判据按第二发计。**这条正好是 run #27 给
「排空重问」写下的用途的第一次实际命中**。本轮环境依旧不给退路：9b 权重只驻留 6%
（387/6149MB、直连第一发 32.9s），`DEGRADED_LLM_HOST` 置起，被降级的三条是「直连下限」
那条测量、`OpenAI 兼容层 /v1/chat/completions`（TimeoutError）和 `HTTP /api/send →
/api/get-msg`（90.1s 内 0 字，按环境判 SKIP 而不是红）；lite 侧 1.5b 满驻留
（1166/1166MB、直连 3.7s），一条降级都没有。

这一轮仍然出现了一次空转重建（trigger 是 `overlay/fay/mcp_servers.json`），报的
「重建后镜像 ID 没变（0db394dea218）」这次是对的 —— 全轮结束后 `Created` 还是 17:30:35。
**这两件事在 run #30 之后都收掉了**，根因比"mtime 会骗人"具体：那道闸把 `overlay/<src>/`
整个目录当构建输入，而 Dockerfile 从 overlay 拿走的只有 requirements（`grep -n overlay
images/*.Dockerfile` 一共 4 行），`system.conf` / `config.json` / `mcp_servers.json` 全是
运行期 bind-mount —— Fay 起来就把 `connection_time` 回写进去，于是每一轮的 mtime 必然比镜像新、
每轮必空转重建一次。清单改成点名那几份 requirements 之后，复跑 `./run.sh test backend-test`
已经不再打印「先重建它」（39 passed / 2.93s）。另一个假阴性（run #29 末段那条）也修了：
核对 ID 改成 `image_id_of()`，只跟 compose 拿 `REPOSITORY:TAG`、ID 用 `docker image inspect`
按 tag 现查，不再读容器上的旧 ID。

### run #31：CareEcho H5 接进来之后的两组（2026-09-21 19:08~19:10）

这一轮**只跑了新加的两组**，不是完整十组 —— `frontend-test` 全程打假后端、
`frontend-probe` 只发一问，两组各自 1 分多钟，没必要为它们重烧那三组 9b。
集成细节见 `README.md`「CareEcho H5 前端」一节。

| 组 | run #31 实测 |
|---|---|
| `frontend-test` | **17/17 通过**（含负面自检：改坏 `Set-Cookie` 后判据 8 确实变红） |
| `frontend-probe` | **3/3** —— 外壳发产物 1583 字节、`/api/health` ok、真实一问 **86.5s / 47 字 / 会话 7 / cookie 有** |
| 收尾一条 | `[test] fay-lite 优雅停止（SIGTERM）退出码 0`（两组各带一次） |

86.5s 这一发比 run #30 里 9b 那三组的 172~301s 快 —— 但这一轮 ollama 那条单队列上
只有它自己（另外三组 9b 问答没跑），所以这是"没有排队"的数，不能记成"机器变快了"，
也不作为对其它轮次的基线。它证明的是：**从外壳进去的那一问，能穿过后端→adapter→Fay→
宿主机 Ollama（那一轮对话还没搬到栈外）拿到真回复再回来**。外壳那 90s 的上限当时是**擦着过的**（86.5s / 90s），
浏览器那 30s 从一开始就不够 —— 这就是超时链一节里"H5 一定先报错"的具体数字。
（这句当时写成"90s 够用"，run #32 把它证伪了：同一条判据那一轮 90.4s 撞死，见下一节。）

### run #32：麦克风这条链接进来之后的第一轮十三组（2026-09-21 22:53:21~23:27:13，33 分 52 秒）

第一次把 `ws-relay-test` / `asr-test` / `asr-probe` 三组放进完整一轮。这一轮**没有**拿到
`[test] 全部通过`：十三组里十组全绿，`fay-probe` 与 `frontend-probe` 各红一条，
`probe-yueshen` 按环境就地收工（退出码 0，但只判了两条）。三条的成因是同一条，见本节第二段。

| 组 | run #32 实测 |
|---|---|
| `backend-test` | **39 passed（3.41s）** |
| `backend-probe` | **11 PASS / 0 SKIP / 0 FAIL** |
| `adapter-test` | **11/11** |
| `probe-selftest` | **[ws-timing] 10/10** |
| `frontend-test` | **19/19**（17、18 两条是本轮为 WS 转发与 `DEBUG` 耦合补的，19 是负面自检） |
| `ws-relay-test` | **14/14**（新组：外壳那条同源转发，含 3 条负面自检 A/B/C，见「426」那节） |
| `asr-test` | **13/13**（新组：`ASR_FAKE_MODEL=1`，不加载 torch；12 条线上协议判据 + 1 条负面自检） |
| `probe-fay-lite` | **41/43 通过 · 2 SKIP**（`window capture` + `远程音频的 ASR`，两条都是划出的边界） |
| `ue-audit` | **2 PASS / 0 SKIP / 0 FAIL** |
| `fay-probe` | **33/44 · 8 项按环境降级 SKIP · 1 FAIL**（红的是 `MCP 现场连接离线服务器 yueshen rag`） |
| `probe-yueshen` | **只判了 2 条**：第 1 条 PASS，嵌入出口那条 240.1s 超时记降级 SKIP，后面 4 条没跑 |
| `frontend-probe` | **2/3 · 1 FAIL**（那一问 90.4s 后拿到 502，`后端 POST /chat/sessions/11/messages 不可达：timed out`） |
| `asr-probe` | **9/9**（新组，全套最末） |
| 收尾一条 | `[test] fay-lite 优雅停止（SIGTERM）退出码 0：一次正常的 stop 没被记成崩溃` |

三条红/收工共用一个成因，而且**在栈外**：这轮开跑前显存就被别的进程占掉了 13.2 GiB
（`nvidia-smi` 里那台 `freetoken` 的 python，16376 MiB 总量下只剩约 1.2 GiB 给 ollama），
于是 `ollama ps` 报的是 `qwen3.5:9b 6.1 GB 94%/6% CPU/GPU` —— **94% 的权重在 CPU 上**。
宿主侧同时是 `available 6.0 GiB / swap 已用 18.7 GiB`、`si` 长期十万量级。后果按链长度依次是：
9b 问答从 60s 起（run #27 那轮 GPU 满驻留时是 6.3s）、嵌入模型换入 240s 不返回、
`frontend-probe` 那一问在 90s 处超时。`check_llm_host()` 据此置了 `DEGRADED_LLM_HOST`，
所以那 8 条问答类判据是**按环境降级**而不是硬判红 —— 这正是这套三态设计要挡住的事：
把"这台机器今天没显存"写成"这条链坏了"。

**两条红里只有一条在隔离重跑里翻绿了**（23:31 起只跑 `fay-probe probe-yueshen`，同一台机器、同一批镜像、
显存占用没变）：`fay-probe` 那组重跑是 **41/45 通过 · 2 项降级 SKIP · 2 项边界 SKIP · 0 FAIL**，
其中 run #32 里红的那条 `MCP 现场连接离线服务器 yueshen rag` 直接 PASS（"连上并取到 3 个工具，
验完断开"），`WS :10002 收到文字播报` 走到了 120s 排空重问那条分支 ——
第 1 发正文为空、第 2 发（换用户名）拿到 70 字，判据按第 2 发计；`HTTP 取到 audio 帧指向的音频文件`
也从降级里回来（`http://fay:5000/audio/sample-….wav` 362014 字节）。
同一轮里 `本地 LLM 主机就绪` 那条直连测到的是 **61.3s 且"模型未常驻(首次请求要先换入)"** ——
run #27 那个 6.3s 的数要 GPU 满驻留才拿得到，两者差的那十倍就是本节上一段说的显存被占。
底数从 44 变 45 也不是漂移：重问分支一旦触发，那次重问自己会多记一条同名判据。

`frontend-probe` 那条重跑**复现了**（23:51 单跑，仍是 `90.3s → 502`，msg 一字不差是
`后端 POST /chat/sessions/12/messages 不可达：timed out`），于是它不该被记成排队抖动：
这是**超时链的最短那一环**在起作用。外壳给后端的预算是 `CARECHO_UPSTREAM_TIMEOUT`（默认 90s），
9b 不驻显存时这一发要 172~301s（run #30 量过），所以 90s 处必然 502 ——
把它调到 300s 也只是把错误往后挪，因为**浏览器侧 axios 那 30s 是伙伴方代码里写死的**
（`request.js:6`）。也就是说这条链的真实边界是：`9b 答得比 30s 慢，H5 就已经放弃了`。
所以 run #32 的两条红一条是排队（重跑即绿，但没写进白名单：完整一轮里它就是红，
`./run.sh test` 的退出码就是 1），一条是**结构**（重跑仍红，改判据不如改部署前提：
要么让 9b 常驻显存，要么把语音/问答放到显存充裕的机器上）。

同一轮里 `probe-yueshen` 补跑回 **6/6**，代价的对比很干净：嵌入出口那一发 **57.0s**
（run #30 那轮它是几秒），576 块语料 ingest **74.8s**（run #30 记的是 **5.1s**），
检索 3 条命中 251 字、`vectors=576` 与 `inserted` 对上。同一条链、同一个模型、同一批镜像，
差的只有"嵌模型今天有没有坐在显存里"—— 这就是为什么这组的预检只给自己找降级、
不给后面的判据找通过的理由。

`probe-yueshen` 那条"只判了 2 条"顺带暴露了一个观测漏洞：嵌入出口不通时它 `return fail_count()`
= 0，退出码绿、收尾那行 tally 却整个没打，日志看上去像"这组跑完了"。已补一行收工说明
（判了几条、后面几条为什么测不了），并把 `远程音频的 ASR` 那条 SKIP 的理由改了 ——
它原文写"本机没有起 FunASR 服务"，而这一轮之后 `dh-funasr` 就在栈里跑着，那句话变成假的；
现在它点名的是真正的边界：10197 上那是 Fay 自己的另一套方言（裸文本 + `{"vad_need":…}`），
与本栈为 H5 起的 `{"text","is_final"}` 未接。

`asr-probe` 那 9/9 里有两个数值得单独记：15.26s 的真实语音（`course_package_player_intro_abin_final.wav`，
edge_tts 产出的）只认出 **0.5 字/秒**，而 2.64s 的合成句是 3.4 字/秒 —— 前者 final 是
`飞。Ai.Ai.Ai.飞。飞。Z.Hip h.Ttp.Api.现在。Exceed.Mcp.拍上来。Cp.`。这不是判据松（那条判的是
"字数与时长同量级 0.5~15 字/秒"），是 `CHUNK_BYTES=32000` 每次一发无上下文的 `generate` 的代价，
详见 `README.md`「一个必须写下来的质量边界」。同一条链路的耗时构成也在这一组里第一次量到：
15.9s 推流 + 0.5s 收尾 → 全组约 32s。

本轮收尾又跑了几件，都归到上面那条成因里、不另开编号（23:52~00:06）：
`./run.sh audit` 报四个上游仓库仍 `0/0` 且 `0 dirty`（containerd 侧 11 份补丁、13 个测试件、
3 份自研 python 源文件）；`./run.sh smoke` **仍红在第 6 步**，前 5 步全过 ——
`500.46s` 拿到 `HTTP 502: 等待 Fay 回答超时（500s，用户名 elder_1）`。它开跑那行自报的是
`qwen3.5:9b 驻留显存 6%（387/6149MB）`，而等它收工再 `ollama ps`，9b 已经**根本不在清单里**
（只剩嵌模型 `45%/55%`）：这发的失败连"换入"都没在 500s 内完成，与 `frontend-probe`
撞的 90s 是同一件事的两个长度 —— 一个是外壳替浏览器做的决定，一个是 adapter 替后端做的决定。
麦克风那三组在按上节那条把 `funasr-cache` 卷删了重建之后重跑是 **14/14 · 13/13 · 9/9**、
`ASR READY secs=17.8 src=local bytes=1299078750`，与卷建好那次的 19.7s 同量级：
这卷是**缓存不是状态**，删了重拷不影响任何判据，也因此才敢为消那行警告删它。

本轮改动过的那两份探针，在提交之后各复跑了一次（00:14~00:38，同一台机器、显存占用没变）。
`probe-yueshen` 这轮是**完整 6/6**：预检那发 21.5s 把嵌模型换进显存，576 块 ingest 只花 **5.5s**
—— 与 run #30 的 5.1s 同量级，而 run #32 那轮它是 74.8s。也就是说这条链本来该有多快，
是排队顺序决定的一半、显存决定另一半。
`fay-probe` 是 **36/45 通过 · 8 项 SKIP（4 项按环境降级）· 1 FAIL**，但红的那条**换了**：
run #32 里红的 `MCP 现场连接离线服务器 yueshen rag` 这次 PASS，红的改成 `TTS edge_tts 合成`
（`返回 None`），而同一条判据在 run #32 的日志里是 `PASS … sample-….wav 188436 字节`。
把它拖下来的还在栈外，而且这次连"去哪看证据"都要照判据说的做：`FAIL` 那行只写
`返回 None｜原因被 ms_tts_sdk 吞在 except 里，只打到本容器 stdout 的那一行，去看它`，
去翻那行是 `Cannot connect to host speech.platform.bing.com:443 … [Temporary failure in
name resolution]`（00:21:44）—— 这台机器的 DNS 出口到 00:2x 之后连 Bing 那条也不通了，
而 `check_tts` 是硬判、不给自己找降级，所以它红；同一轮两条 audio 判据的成因行也明写了
"这是外部依赖，不是容器化回归"。这一条同时说明这组的判据分布是活的：**同一轮里可以一条红
换一条绿**（run #32 红的那条 MCP 这次 PASS），拿"上次那几条红"当白名单就会把这次的漏
看成上次的抖动。

我给自己那处改动补了一个必做的对照：`probe-yueshen` 新加的收工 tally 只在嵌入出口不通时才走，
而上面那次是通的 —— 等于那行**没被执行过**，光"跑过 6/6"不能算它验证了。
于是用 `--timeout 0.01` 人为把预检做坏，它就打出来了：
`[probe] 2 条已判（其中按环境降级 1 条），后面 4 条（工具清单/入库/检索/stats）没有嵌入出口
就测不了，本组就地收工`，退出码 0。对比的意义就在这一行：改之前**同一条路径也是 rc=0，
但一个字都不打**，日志读起来像"这组把 6 条跑完了"。

### 测试库跨轮存活：run #14 的那条红与"每轮都绿"的代价

run #14（2026-09-20 18:55）第一次抓到 `backend-test` 红：**38 passed / 1 failed**，
红的是上游自己的 `tests/test_human_queue.py::test_human_queue_flow`（`assert False`，
断的是"我刚排的那条队，志愿者在待接单列表里能看见"）。根因不在容器集成，也不在这条
业务逻辑 —— 在**测试库跨轮存活**：

  * `service/tests/conftest.py` 全文 81 行，**一句清表都没有**（没有 `DELETE`、没有
    `TRUNCATE`、没有 `drop_all`），而每个 fixture 都用随机 openid 走 dev-login 新建用户
    （`openid = f"pytest_{uuid4().hex[:12]}"`）。也就是说这套用例隐含假设"库是新建的"，
    上游 CI 每次都是空库跑，所以它那边不会红。
  * 本栈的 MySQL 数据在 `mysql-data` 卷上，测试库 `care_echo_rehab_test` 跟着一起跨轮存活。
    攒到第 14 轮：631 个用户、50 多条 `pending` 排队行。
  * 于是 `GET /volunteer/human-queue/pending` 露馅了：应用层是
    `list_pending_queues(... limit=50)` + `ORDER BY priority DESC, enqueued_at ASC`，
    本轮这条是 `medium`（priority 低）又最新，被历轮遗留的 pending 行挤到 50 名之外，
    断言读不到自己刚写的 `id`。

这是"跑一次绿"和"每轮都绿"的区别，也正是持久卷带来的新型假红。修法的操作口径（每轮 pytest
前重建测试库、前三步任一失败就不跑 pytest、库名白名单）写在
`README.md`「测试库必须是空的」；量到的数记在这里：一次重置报
`丢掉 38 张表 / 约 2890 行残留`（`information_schema.table_rows` 对 InnoDB 是估算值，
这里只当量级看），而业务库 `care_echo_rehab` 不满足白名单形状、脚本见到就退出 2
（拿 `MYSQL_DB=care_echo_rehab` 试过，确实拒了）。

顺带把 `sql/init/01-create-test-database.sql` 的注释改了 —— 它原来写着"pytest 里的 fixture
会建表/清表"，那句话不成立，留着就是下一个人踩同一个坑的理由。

### 只在午夜红的上游用例：run #25 与之前的 service/0002 补丁

第二处上游测试 bug 是 `test_medication_missed_scan`，补丁是
`patches/service/0002-medication-missed-scan-clock-frozen.patch`。它是**只在午夜附近才红**的
时钟 bug —— 用例用 `now - 2h` 造一个"过去的提醒时刻"，而上游
`notification_service.slot_local_datetime` 把 `"HH:MM"` 钉到**今天**、`run_medication_missed_scan`
又跳过相对 `local_now + grace` 仍在未来的时刻，于是当容器本地钟走到 00:00~01:59 时，
`now - 2h` 落到了昨天、被当成未来时刻全部跳过，`assert 0 >= 1` 必红（run #25 就是 02:0x
撞上的）。修法不碰被测代码，只在该用例里 `monkeypatch.setattr` 冻结扫描钟 `_local_now`
到今天 12:00，让"过去两小时"这个语义与真实挂钟解耦。

**验证没有等午夜、也没造假**：本应用经 `ZoneInfo("Asia/Shanghai")` 取钟而用例的 slot 用容器
本地朴素时间，把容器 `TZ` 临时改成 `Asia/Dhaka`（UTC+6，真实 02:08 时容器钟 00:08）就能在
0.3s 复现 —— 打补丁后 `1 passed`、还原原始文件同一 `TZ` 下 `1 failed`。这类"与挂钟日期绑定"
的红值得留的就是这一招：不改被测代码、也不睡到零点再跑，而是把时区当成可控输入。

### 后端活体探针的七次变异验证

一条判据要能红才算存在，所以那十一条里有七条各自被"故意改坏"过一次（都往 `/tmp/appcopy`
那份副本上动，不打扰在跑的实例）：

  * 往 `chat.py` 里加一条活服务没有的路由 → `FAIL 路由挂载完整性 80/81 命中，缺
    ['chat:/chat/probe-mutation-only']`
  * 把 `BACKEND_URL` 指到没人监听的 9999 → 第一条 FAIL、其余**各自** SKIP（不是被短路成
    "没跑"）、退出码 1
  * 把探针发出的 `Authorization` 换成垃圾 token → `FAIL 带鉴权重跑 GET 扫描 —— token 都签
    出来了却一条都没多走到 2xx（匿名 3 → elder 3，volunteer 3，admin 3）`
  * `REMINDER_TIMEZONE=Asia/NotAZone` → `FAIL 定时任务调度器起得来 —— ZoneInfoNotFoundError`，
    说明这条判据真的在解析镜像里的 tzdata，不是"调用没抛异常就算过"
  * `REMINDER_SCHEDULER_ENABLED=false` → 那条判据 SKIP 并写明"这台没开调度器"，不假绿
  * 把探针看到的 `GET /volunteer/home-summary` 换成一个空列表（只骗读、不骗写）→
    `FAIL 活体写路径 —— queue_id=5 写成功了，视图 v_volunteer_queue_pending 里却没有它
    （读到 0 条：[]）`
  * 把第一次 `cancel` 改成 500 → `FAIL 活体写路径 —— 读回没问题，但
    POST /elder/human-queue/6/cancel → 500`

最后两次变异之后再去读真视图，`pending_count` 都回到变异前的样子，说明 `finally` 里那次兜底
取消真的跑了 —— 悬在 pending 的行会让下一轮的 `join` 直接复用旧行，判据就从"我写的这条"
变成"上轮遗留的那条"，红绿都不再可信。这也是那条写路径判据固定 openid、写完必取消的理由。

它跑完之后 `app/api/router.py` 里的 `dev` 路由是 `DEBUG` 才挂的，所以 DEBUG 关掉时最后两条
按 SKIP 记，理由写"dev 路由没挂载"，不去猜（见 `README.md`「后端活体探针」的十一条）。

### 探针的收工时机与收尾文案：run #10 / #20 / #21 各逼出一条

`probes/ws_timing_test.py` 那十条里有三条是把历史上的三次假红钉成断言的：

  * **第六条 = run #10 那次误报的形状**：当时服务端一关连接，`check_ws` 走
    `except Exception` 把整段会话的帧全丢了，看起来像"一帧都没收到"。
  * **第七条 = run #20 的那次红**：`answered` 原先用 `'"text"' in json.dumps(frame)` 判断
    "回答开始了没有"，而 `"text"` 这个串在帧里的 `Data.Key` 上恒真 —— 于是那帧空终止标记
    一到位就把 6 秒静默窗口武装起来，会话在 21:27:44 收工，同一发回答的句子 21:28:25 才落地。
    现在改看 `_has_substance()`（`panelReply` 非空，或 `Key=text` 的 `Value` 非空）。这条判据
    自己也验过反证：把 `_has_substance` 换回旧规则再跑，7 条里就是这第 7 条红
    （`6/7`，详情 `7.0s text帧=['']`），形状与 run #20 那次完全一致。
  * **第八到第十条量的不是时机，是收尾那行的口径**。旧版不管几条 SKIP 都印成「N 项因本机
    LLM 证据降级为 SKIP」，可 run #21 那三条一条都不是降级来的（两条无头白名单、一条本机
    没起的 FunASR）—— 这一句话替环境揽下了没发生过的嫌疑，读日志的人会以为这台机器还有
    显存问题。现在 `skip_tail()` 按详情里的 `ENV_SKIP_MARK` 把 SKIP 分成「LLM 证据降级」和
    「其余，成因逐条列在下面」两类分别计数；其中一条自测不手写记录，而是调
    `latency_verdict()` 现造一条 —— 它测的是「谁负责写上这个标记」，以后改降级文案时漏掉
    标记，收尾会立刻少算并红在那条自测上，而不是悄悄把一条降级算成边界。

十条各自钉住什么行为是操作口径，留在 `README.md`「探针自己的时机测试」。

### 「Fay 会调工具」这句话，探针只敢证到中间那一档

外部看一个数字人有工具，容易把三件不同的事混成一句「支持 MCP 工具调用」。这三档
的强度差别很大，而**探针只证到了前两档**，所以分开记：

| 档 | 含义 | 探针判据 | 结论 |
|---|---|---|---|
| ① 管理面能调 | **我们**通过 `:5010` 的 HTTP 管理接口连服务器、列工具、真调用一次 | `MCP 管理面 /api/mcp/servers`、`MCP 工具清单`、`MCP stdio 示例工具真调用`、`MCP 知识库工具真调用`、`MCP 日程工具真调用` | PASS，与模型无关（三个实例同绿） |
| ② 一轮对话顺带执行了工具 | 用户说一句自然语言，Fay **因为工具被配置成 prestart** 而在进程内执行它，结果进回答流 | `MCP 预启动工具注册与可运行清单`、`一轮对话真的执行了 MCP 工具（prestart 结果进回答流）`、`MCP 预启动工具注销后清单回到登记前` | PASS（run #20 首次，run #21 三个实例各现造一帧）；与模型无关，见下 |
| ③ 模型自己决定调工具 | 模型读了工具清单，**主动**规划出「该调哪一发」再执行 | —— | **未证**，见下 |

②这条判据的可信度全在「**这条执行不经 :5010**」：注册 prestart 之后，探针只发一句
普通问答，收到的是 `:10003` 上一帧 `panelReply`，里面出现 `<prestart keep="true">`
包着的 `kb_list_sources` 真实输出（`count` 字段那种）。而 in-process 那条路走的是
`faymcp/runtime_bridge.call_tool`，压根不经过 `:5010` 的 Flask 路由 —— 探针没有能力
在自己进程里造出这一帧。所以「一帧里出现工具输出」只能来自 Fay 自己的执行。

代码里这条链是通的，读得出来：`llm/nlp_cognitive_stream.py:2374` 的
`_run_prestart_tools(content)` 在**拼 prompt 之前**跑（:462 的调用点），拿清单走
`faymcp/runtime_bridge.py:53 list_runnable_prestart_tools()`，它要求服务器
`status == "online"`（`faymcp/mcp_service.py:1248` 那个注册口），执行走
`call_tool(sid, tool, params, skip_enabled_check=True)`，结果在 :2664 用
`write_sentence(prestart_stream_text, force_first=…)` 播出去。

两个 surface 对 prestart 的处理不对称，这是踩到才知道的：`core/fay_core.py:2929-2931`
在往 `:10002`（数字人）发之前把 `<prestart>` 标签剥掉了，`gui/flask_server.py:1407`
对 HTTP 回复同样剥；但 `:10003`（面板）上 `panelMsg` 会被 :2835 跳过，而 :2856
`if content_id is not None` 那条 `panelReply` 照发，文本只过
`__truncate_think_for_panel`。所以判据必须盯 `:10003` 的 `panelReply`，盯 `:10002`
会稳定假红。

③ 到现在仍然没有判据，也就仍然没证到。此前把它归给「这台机器显存不够」，run #21
把这半句证伪了：那一轮 9b 权重 100% 驻留、直连 3.0s，单发问答在 :10002 上拿到
34~42 字，显存不是卡点。真正没量的是 fork 那条决策路 ——
`llm/nlp_cognitive_stream.py:3059` 的 `_call_planner_llm`，一次问答要多发好几发 LLM，
而 run #20 观测到的自适应预算（`fay-lite` 45s→90s、直连基线 0.0s→7.0s）说明
「一次生成」和「一轮问答」不是一个数量级：预算翻倍仍然可能不够那几发排队。
判不了就是判不了，不写成 PASS，也不顺手编一条「模型自己决定调工具」的判据 ——
那条判据要能区分「模型选了工具」和「prestart 替它选了」，得先设计怎么把 ② 关掉。

### `:10002` 上的空回复：一次「同一次生成、三个落点」的排查

run #20 那条唯一的红是 `WS :10002 收到文字播报 —— 一帧 text 都没收到`。这条判据在
run #9 是绿的，代码同一份。当时先按探针自己的时机缺陷处理（见上面
`probes/ws_timing_test.py` 第七条），修完还剩一个说不通的问题：**为什么只有 :10002 空**。

嫌疑有两个，得分开量。一是 `Output` 这个开关（`core/fay_core.py:1568` / `:2212` 用
`get_client_output(username)` 决定往 :10002 推什么），二是 :10002 发送前的标签剥离
（`fay_core.py:2931-2933` 依次过 `__remove_prestart_tags` / `__remove_think_tags`，
剥完为空且不是结束标记就在 :2946 整段 `return`）。要区分它们就得**在同一次生成上比落点**：
不同轮次的观察拼不出结论，因为每一轮都是新的一次生成，模型这轮吐空、下轮不吐空，
就足以解释「同一条判据两次运行结果不同」。

于是 22:33 拿一个用户名同时注册 :10003 与 :10002（`Output=false`），只 POST 一次
`/api/send`，三个落点一起收：

| 落点 | 实测 |
|---|---|
| `:10003` 面板 | `panelReply` 两句（正文 42 字，payload 字段合计 498 字） |
| `:10002` human | **2 帧真正文，42 字**：`'我是一个人工智能助手，由蜻蜓菲菲智能科技开发，'` (IsFirst=1,IsEnd=0) + `'致力于为您提供准确、有用的信息和帮助。'` (0,1) |
| 记忆库 | 同一句 `type='fay'` 行 |

而 `Output=false` 这同一格（fork 镜像 × `qwen3.5:9b` × 这句长问题）在 22:27 那一发是
**空**：3 帧 = `log` / `question` + 一帧 `Value=''` 的 text 终止帧，探针按修好的时机
等满 240s 预算才收工。所以：

- **「`Output=false` 会掐断 :10002 的文字」被否证** —— 同一格既能空也能不空；
  换 1.5b 的 `fay-lite` 同参数给出 69 字，`只回一个字：好` 这种短问题给出「好」。
  `Output` 不是解释变量。
- **剥离路径没有吃字** —— 有正文时它正常放行，而这轮回答里本来就没有 `<prestart>` /
  `<think>` 标签可剥。
- 剩下能说的是：**这是一次概率性的生成侧吐空**。:10002 通道本身没坏（run #20 里
  origin-fay 同问题、同模型、同机器给了 64 字真回复），它跟着模型走（换 1.5b 就没再出现）。
  静态读代码读不出一个可以下补丁的点，所以这里**不打补丁** —— 没有根因的补丁就是猜测。

判据的处置是把它量出来，而不是糊过去：:10002 那两格第一发拿到空正文时，探针明确
**再开一个会话量第二发**，两发各自的形状（帧数 / `Keys` / text 帧的 `(IsFirst,IsEnd)`）
都写进同一条判据的详情里；第二发有正文才按第二发计 PASS，**两发都空仍然判红**。
代价是真发生吐空时那一格要多等一个完整预算（今天实测等满 240s —— 这正是 #35 修好之后
的必然后果：不再被空终止帧提前武装 6s 静默窗口）。三种形状都用合成帧单独验过走向：
只有空终止帧 → FAIL 且详情带形状、有正文 → PASS、一帧 text 都没有 → FAIL。
要说清的是：**run #21 与 run #23 那三组各六个 :10002 格子全部第一发命中**，所以那两轮
这条分支只有合成帧自测的凭据。这个状态在 **run #24 结束了**：fork 组的
`:10002 Output=true` 第一发拿到 3 帧、`Keys` 里没有 `audio`、text 帧
`(IsFirst,IsEnd)=(1,1)` 且正文为空，探针换用户名重问，第 2 发 8 帧 / 56 字，判据按第 2 发
计 PASS，原文照抄：

```
PASS  WS :10002 收到文字播报 (Data.Key=text)  —— 56 字: 我是您的智能助手，熟悉 Fay 大小模型协同、
      OfficeEcho 功能介绍等知｜第 1 发正文为空 [3 帧、Keys=['log', 'question', 'text']、
      text 帧 (IsFirst,IsEnd)=[(1, 1)]]，第 2 发（换用户名重问）拿到 56 字，判据按第 2 发计
```

也就是说这条分支现在既有合成帧的三种走向，也有一次活体触发 —— 而且活体那次吐出的形状与
#35/#36 静态推断的空终止帧形状逐字段一致：**3 帧、`Keys` 里没有 `audio`、text 帧
`(IsFirst,IsEnd)=(1,1)`** —— 服务端把这条回复当成"已经说完且说完了空话"结的尾，不是探针
提前收工；探针能看到 `IsEnd=1` 却拿不到正文，正是它必须重发而不能傻等的理由。
顺带纠一处用词：旧文档里"`重试` 出现 0 次"的 grep 关键字是错的，探针的措辞是 `重问`；
#21/#23 两个日志里两个词都是 0 次，所以结论没错、依据不稳，本轮起按 `重问` 计。

**#24 之后紧接着又红了一轮，那一红改掉了重试的实现。** run #25 在同一格里两发都空：
容器日志显示上一轮 `:10003` 那次问答被 fork 的大小模型协同判成「闲聊判断器 finish 过长
(106字)，追加核实」、转给了会独占单条 9b 约 98s 的后台工具链，紧跟着的 `:10002` 这发正好
落在这条链把显存占满的窗口里。两发都空不是巧合，而是**没给显存留排空时间** —— 换用户名
立刻重问只换掉了用户名，没换掉排队的位置。所以重试改成了**先排空再重问**：
`probes/fay_probe.py` 里 `EMPTY_REPLY_DRAIN_SECONDS = 120.0`，两发之间 `time.sleep` 掉这
120s 让上一轮遗留的工具链释放显存。run #26 现场印证了它：
`等 120s 让上一轮遗留的后台工具链释放显存后重问，第 2 发（换用户名）拿到 56 字，判据按第 2 发计`
→ PASS。run #27 这一格第一发就直接拿到 52 字，重试分支没再触发，lite 与 fork 因此底数相同。
重试路径触发时会多印一条 `WS :10002 … 注册并收到服务端帧`（第 1、2 发各一条），那是它自己的
记账，不影响不触发时的底数。

### 「外部 TTS 出口不通」和「容器 audio 推送坏了」是两件事

`:10002` 上收不到 audio 帧、或者 audio 帧的 `HttpValue` 取不回文件，红可能有两种成因，
而它们指向完全不同的地方：一是**外部依赖**（`edge_tts` 要出公网到
`speech.platform.bing.com`，容器没网/被墙/配额没了都算这条），二是**容器自己**
（`core/fay_core.py:2212` 那个 `get_client_output` 分支不推帧、或 `:5000/audio/…` 那个
静态落地路径坏了）。把前者判成后者，会让人去改本来没坏的容器代码。

分法写死在探针里：`check_tts()` 直接调 `tts/ms_tts_sdk.Speech.to_sample()`，结论除了
自己那条判据，还写进 `TTS_OK`；audio 那条判据先看会话里有没有带 `HttpValue` 的帧，
没有的话 —— `TTS_OK is False` 就记 **SKIP**（理由写"出口不通，见 check_tts 那条"），
`TTS_OK` 为真才记 **FAIL**（"会话里没有带 HttpValue 的 audio 帧"，这才该怀疑容器）。

这个口径用一次受控变异验过（23:13、23:15 第一次；把实验台收进 `tools/` 之后 23:37、23:38
两臂复跑，同一份 `probes/fay_probe.py`、同一个 `dh-fay:local` 镜像、同样的只读挂载，
唯一差别是一行 `--add-host`）：

| 臂 | `check_tts` | `TTS_OK` | 下游那条（两臂喂的是同一份「一帧 audio 都没有」的会话） |
|---|---|---|---|
| 出口正常 | PASS，1.8s（复跑 5.4s），`./samples/sample-…wav 188436 字节` | True | **FAIL** —— 会话里没有带 HttpValue 的 audio 帧 |
| `--add-host speech.platform.bing.com:127.0.0.1` | FAIL，0.5s（复跑 0.7s），`返回 None` | False | **SKIP** —— 出口不通（见 check_tts 那条）=> 没有 audio 帧，也就没有 URL 可取 |

同一份下游输入，两臂给出两种判据、且各自指对方向 —— 这就是「分清」的全部含义。
顺带量到两个事实：出口不通时 `to_sample()` **不抛异常，返回 `None`**（`ms_tts_sdk.py:116`
那个 `except Exception` 只 `util.log` 出「[x] 原因: …」，探针拿不到原因，所以现在 FAIL
的详情里直接写明去哪看），而且**失败得很快**（0.3~0.7s），不会把整组拖到超时。

复现命令（一次性容器，挂载全是 `:ro`，不碰任何在跑的实例；不打 LLM、不占显存）。
实验台本身是仓库里的一份工具，不在 `/tmp` 里，所以这条命令在别的机器上也照抄得动：

```
docker run --rm --network digitalhuman_default \
  --add-host speech.platform.bing.com:127.0.0.1 \
  -v "$PWD/probes:/probe:ro" \
  -v "$PWD/overlay/fay/system.conf:/app/system.conf:ro" \
  -v "$PWD/overlay/fay-lite/config.json:/app/config.json:ro" \
  -v "$PWD/tools:/tools:ro" -w /app dh-fay:local python /tools/tts_negative_control.py
```

（基线臂就是删掉 `--add-host` 那一行。`tools/tts_negative_control.py` 里只有三件事：
`fp.check_tts()`、`fp.check_audio_url([{一帧 log，没有 audio}], "http://fay-lite:5000")`、
把 `fp.TTS_OK` 和那条下游判据的三态打出来，两臂判据没分开时它自己非零退出。）

要说清边界：**这是诊断口径的反证，不是"外部出口真的挂过"的观察**。本栈每一次完整轮里
TTS 都是通的（run #21、#23、#24 各三个实例，九次都是 188436 字节），所以 `TTS_OK is False` 那条 SKIP 分支
至今没有在真实一轮里走过 —— 它今天是被我用 `--add-host` 造出来的。

### 决策面谈 `:5001`：一个按需拉起的子服务，起落两向都判

Fay 的 `genagents` 那条链以前只被记成"5001 没起"，等于没测。它不是常驻端口：
`gui/flask_server.py:1706` 的 `POST /api/start-genagents` 才把它拉起来，
`genagents/genagents_flask.py` 里 `:5001` 上的 `GET /` 渲染 `templates/decision_interview.html`，
`POST :5001/api/shutdown` 立一个 flag、由那条路由自己起的 monitor 线程等干净后 release 端口。
run #24 起探针把这条起落做成三条判据，三个实例上都是 PASS（fork 组示例）：

| 判据 | 实测 |
|---|---|
| 启动路由 | `POST :5000/api/start-genagents` → HTTP 200，回报 `'http://127.0.0.1:5001/'`，`:5001` 在 0.0s 内 bind |
| 页面渲染 | `GET http://<容器名>:5001/` → HTTP 200、30094 字节，且页面里带着探针传进去那句指令与 `instructionText` |
| 收尾 | `POST :5001/api/shutdown` → monitor 线程在 1.5s 内 release，`:5001` 关回不监听 |

三处口径值得单写，因为它们是读代码读出来、不是猜的：

1. **这条 POST 必须是 JSON 体**。`flask_server.py:1721` 读的是 `request.get_json()`，
   探针里那个给 `/api/send` 用的 `http_post()` 发表单，套到这条上只会得到
   400「缺少克隆要求参数」——所以新加了 `_post_json()`，别顺手复用。
2. **回报的 URL 是 `127.0.0.1` 硬编码的**（`flask_server.py:1803` 拼的是
   `http://127.0.0.1:PORT/`）。探针在**另一个容器**里，照着这个 URL 去连必然连到探针自己，
   所以判据只把回报值当字面证据打印，真正拨号用的是容器名 + 5001。这不是容器的缺陷：
   在同机浏览器里那个 URL 本来就是对的。
3. **`check_genagents_interview` 排在预算测量之前、`check_tts` 之后**，因为它只验"镜像里那套
   模板与 `genagents` 包能不能在容器里拉起一个子服务"，与显存和 LLM 无关，不该被问答预算拖着。

另外记一个**不打补丁的上游静默空转**：`gui/flask_server.py:1753-1758` 那条"清除之前的记忆"
只有 `from llm.nlp_cognitive_stream import clear_agent_memory`（函数确实存在，fork 里在
`llm/nlp_cognitive_stream.py:3193`），**导入之后没有调用**，下一行就 `util.log` 出
"已清除之前的决策分析记忆"。run #24 的容器日志正好把这条坐实了 —— 探针只发了启动与关闭，
23:48:26 那一句"已清除"照样打了出来。`origin_fay` 同一段一字不差
（`gui/flask_server.py:1754-1758`，符号在 `llm/nlp_cognitive_stream.py:2402`）。
它是功能缺失而不是容器化缺陷，探针不去调一个没接线的函数，本栈也不为此改上游 ——
与本节其它"读到但不动手"的记录同规格。

### 优雅停止：一次正常的 `docker stop` 不该长得像崩溃

`./run.sh test` 收尾会 `compose stop fay-lite`。run #20 之后 `compose ps -a` 里那个实例
留着 **Exited (1)**，看起来像跑完测试跑挂了 —— 实际它是被 SIGTERM 正常停掉的。今天
（2026-09-20 22:03/22:04）拿健康的容器直接量了一次，两个实例都复现：

| | `docker stop` 墙钟 | 退出码 | 日志末行 |
|---|---|---|---|
| `fay-lite`（fork，未打 0005） | 5.27s | **1** | `清理超时，立即强制退出...` |
| `origin-fay`（上游，未打 0005） | 5.45s | **1** | 同上 |
| `fay-lite`（打 0005 后） | 3.84s | **0** | `所有线程已停止` → `程序即将强制退出...` |
| `origin-fay`（打 0005 后） | 3.88s | **0** | 同上 |

退出码由应用自己决定，跟容器运行时没关系，所以这条只能改代码，而两边代码是同一份：
`fay/scheduler/thread_manager.py` 与 `origin_fay/` 那份**字节相同**（`diff` 为空），
上游 `main.py` 的 `signal_handler` 也在同样的行号上。所以这是上游缺陷，不是 fork 回归。

机制是两处叠加，缺一不可：

1. `main.py:155` 给清理线程的预算是 `join(timeout=5.0)`，而 `thread_manager.stopAll()`
   的第二个循环是**每个线程各排 2 秒**的 `join(timeout=2.0)`。向线程 `raise_exception()`
   之后只能等它自己回到解释器才收得到 `SystemExit`，卡在 C 层 `recv()` 上的连接线程收不到，
   于是每个都要满额 2 秒。两个这样的线程（`Thread-1/2 (__connect)`）就 4 秒，加上前面
   关服务约 0.5 秒和 `cleanup_and_exit` 里那句 `time.sleep(1.0)` —— 5 秒预算正好被吃光，
   而超时的分支是 `os._exit(1)`。
2. 走到那个分支并不代表停止失败：它只说明**有几个非守护线程不肯退**，而这正是 `os._exit`
   存在的理由（服务侧清理在此之前已经全部打完日志完成）。把 SIGTERM 的收尾报成非零，
   在 `restart: on-failure` 下会把一台本该停下的实例重新拉起来。

补丁做两件事：`stopAll()` 的等待改成**共享一个 2.0s 截止时刻**（`raise_exception()` 早已
全部发完，剩下的只是等，逐个等满没有意义），`signal_handler` 超时分支改 `os._exit(0)`。
为什么线程数才是关键：打补丁那次 lite 实际有 8 个线程没能在截止时刻前退干净
（`__connect`×2、`run`×2、`memory_scheduler_thread`、`__record`、
`accept_audio_device_output_connect`、`device_socket_keep_alive`），旧写法按 2 秒一个
摊开是 16 秒 —— 那已经越过 docker 默认的 `stop_grace_period=10s`，dockerd 会直接 SIGKILL。
**这一步是算术，不是实测**：上表 before 那一行只打出 2 行超时，是因为预算在第 3 个 join
刚开始时就到期了，"2" 是它来得及处理的个数，不是线程总数。

判据接在 `run.sh test` 的收尾：那里本来就要 `stop fay-lite`，顺手读 `State.ExitCode`
即可，零额外成本、也不碰显存。只在「停止前它确实在跑」时才判 —— 单独跑某一组
（`./run.sh test ue-audit`）时 lite 可能早就停着，那时 inspect 读到的是上次留下的旧码，
拿它判等于伪造证据，这种情况明确打一行「本轮没有证据可取（不算通过）」。这条判据的反证：
把 cid 指向一个 `ExitCode=1` 的容器，分支如实报红并把整轮 `rc` 置 1。

### 已清理的本地改动

按「bundle + 文件备份后清理」执行，四个仓库现在都是 `main` == `origin/main`、
工作树干净、无任何未推送提交。当初清理掉的是**不属于上游的本地分支与提交**（用户指示），
备份当时的归档物包括：`fay` 全分支 bundle（含本地分支 `kb-embedding-test`）、移出的
`新知识库/` 语料、`chromadb_yueshen/` 向量库、`faymcp-data/` 改过的 MCP 配置。

> **归档已移除**：本次仓库整理时，那份 `_backup/` 归档（约 155MB，含 127MB 的 bundle）经
> 确认无需回滚后已删除，工作区内不再有这些备份文件，因此上述"拷回/`git fetch <bundle>`"式
> 的恢复**在本 workspace 内已不可用**。四仓仍与 `origin/main` 一致，未受影响；`service`
> 仓库里那条 `stash@{0}: local uv.lock changes` 保留未动（不是提交，与清理无关）。
> 注：`kb-embedding-test` 等是本地专属分支、未推送上游；其唯一副本随本次删除而移除，
> 属用户在"确认无需回滚"前提下的知情决定。

### 密钥不给全，产物里连 ID 都会消失（Vite 的 DCE，不是构建坏了）

数字人形象是魔珐 Xmov SDK，三个 `VITE_XMOV_*`（APP_ID / APP_SECRET / GATEWAY）。
最初给它们留空，结果构建产物里**连那个 APP_ID 的字符串都搜不到**，一度以为
"Vite 不认 build arg"。逐条排掉三个假设（`docker run --entrypoint env` 证明 ARG/ENV
确实进去了；干净小项目里复现证明 vite 6.4.3 的 `resolveConfig().env` 会读 `process.env`；
不是 `.env.production` 抢占）之后，根因是 **rollup 的死代码消除**：
`src/api/xmov.js:79` 是

```js
if (!XMOV_APP_ID || !XMOV_APP_SECRET) { reject(…); return }
```

两个常量在打包时是字面量，只要有一个为空，这个 `if` 恒真、后面整段（包括那两个
字面量本身）被摇掉。实测对照：只给 ID → 产物 `index-C34WNEPg.js` **122.39kB、
`grep -c "nebula-agent\|XMOV\|xingyun3d"` = 0**；两个都给 → `index-CkSrAofW.js`
**126.77kB、哨兵字符串全在**。

所以本仓库默认构建的是**不含密钥**的那一份（`ARG` 留空）：两个 GitHub 仓库都是公开的，
把伙伴方的 SDK key 烤进镜像再推上去就是泄漏。要接数字人形象，得自己
`--build-arg VITE_XMOV_APP_ID=… --build-arg VITE_XMOV_APP_SECRET=…` ——
**并注意 BuildKit 会警告 `SecretsUsedInArgOrEnv`**：ARG 会留在客户端构建上下文和
`docker history` 里，这种镜像不能推公开仓库（要推就先去掉那层，或改成 BuildKit secret）。

### 13 条判据全绿的那晚，手机仍一个字都识别不出来：426

`ws-relay-test` 第一次跑就 13/13，可同一天从宿主经外壳打真链路是：

```
dev 直口    192.168.x.x:10095/ -> 101  final='飞。Ai.Ai.'  wall=1.5s
经外壳转发  192.168.x.x:5173/funasr-ws -> HTTP/1.1 426 Upgrade Required
```

外壳自己没回这个 426（它的 `ws_dispatch` 只会回 400/404，拨号失败回 502）—— 它回的是**上游**的
426：转发在建上游请求时把客户端的 `Upgrade` 透传了一份、又在末尾统一补了一份，上游收到两条
`Upgrade`。拿同一份握手对真 ASR 直接试三种头组合：单条 `Upgrade` → 101，两条 → 426，
两条 `Connection` → 仍然 101（`Server: Python/3.10 websockets/16.1`，实测 2026-09-21 22:44）。
所以这不是"哪个上游更挑剔"的运气问题：`open_tunnel` 的剔除名单漏了一个名字。

测试为什么没抓住更值得记：假上游用 `dict` 收请求头，同名头被折叠成一条，判据 5 于是永远看不见
重复 —— **假上游比真上游宽松一寸，转发侧的 bug 就只在生产里露头**。三处一起改：

- `frontend/carecho_web.py` 的剔除名单加 `"upgrade"`（客户端那条一律不进上游，由补回的那两条统一给）；
- `probes/wsutil.py` 的 `server_handshake` 改成"照真上游的严度"：`Connection`/`Upgrade`/
  `Sec-WebSocket-Key`/`Sec-WebSocket-Version` 任一重复就回 426 并记名（所以判据 4 会红，
  detail 直接写"上游因为重复的请求头拒了握手"），头对象换成带 `.duplicates` 的 `RequestHeaders`
  —— 观测能力没变窄，但重复这件事不再是不可见的；
- 新增**负面自检 C**：把剔除名单里的 `"upgrade"` 删掉（即精确还原这次的 bug），判据必须变红。
  改完 `ws-relay-test` 是 14/14，其中 `13 … 变红=True —— 上游因为重复的请求头拒了握手：['upgrade']`。

修完再跑那次真链路，两个方向数字一致（外壳转发 wall=1.2s、直口 1.5s，同一条 final），
这才是「生产里 H5 靠同源转发拿到识别」这句话的证据 —— 在此之前它只有假上游那一半。

## 这台机器的私有环境（上面所有数字的前提）

### AI 端点的落点（2026-09-23 实测）

对话与嵌入**是两台服务**，两台都在 compose 之外：

| 能力 | 落点 | 模型 | 这条端口的特点 |
|---|---|---|---|
| 对话（大小模型同档） | 栈外的本地 OpenAI 兼容服务，端口 `11432`（`ft serve`，`/v1/models` 的 `owned_by` 写着 FreeToken） | `gemma-4-26b-a4b-nvfp4`，`/v1/models` 只有这一条 | 上下文 262144；**没有 ollama 那个 `/api/ps`**，所以"读权重驻留比例"这条证据在它上面取不到 |
| 嵌入（仿生记忆 + 知识库检索） | 宿主 ollama `:11434` | `qwen3-embedding:0.6b` | 单条队列，换入实测 70s 起 |
| `fay-lite` 的对话 | 宿主 ollama `:11434` | `qwen2.5:1.5b` | 全驻显存时暖态 0.2s |

具体地址在 `containerd/.env` 的 `FAY_GPT_BASE_URL` / `YUESHEN_EMBED_BASE_URL` 里，本文不复述
（按机器变的事只进 `.env`）。**两组键是分开的，本机现在也确实分开。** 本仓库早先那句
「所有 AI 能力都指向宿主机 Ollama」只在对话还没搬走时成立，现在已经按两类落点改写；
读任何一条耗时判据之前先确认自己读的是哪一类。

显存前提：`nvidia-smi` 实测 16376 MiB 里占了 11049，其中那个对话服务的 worker 单个进程
就占 **10742 MiB**（2026-09-23）。它和 ollama 上那几个模型抢的是同一块卡 —— 本文那些
换入秒数、驻留百分比和「问答是分钟级」的结论都成立于这个前提。对话端点搬走之后，最重的
那一发不再压在 ollama 上，但**嵌入侧的排队没有消失**（见上面「测试件的顺序」那一段）。

这台服务不是本栈的一部分：compose 不起它、也不该由本栈去重启它。它挂了的表现是
`fay-probe` 那条「本地 LLM 主机就绪（直连，不经 Fay）」判据红，加每一问的 `fay_error` ——
那时候要查的是栈外，不是容器。

### 与既有宿主服务的隔离

宿主机已经跑着 `mysql`（占 0.0.0.0:3306）和 `redis` 两个容器。本栈新建的是**自己的**
`dh-mysql` / `dh-redis`，宿主端口用 13306 / 16379，内部走 compose 网络，互不影响；
数据在 `mysql-data` / `redis-data` 卷里，`./run.sh reset` 会连卷一起删。

`dh-redis` 是**跟着上游的依赖声明建的，不是被测出来的**：`service/app/core/config.py:28`
有 `redis_url` 字段、`pyproject.toml:17` 依赖 `redis>=8.0.1`，但全仓 grep 不到任何一处
使用 —— 上游自己的 `.env.example:13` 和 `README.md:20` 都写着「预留，当前核心流程未强依赖」。
所以「redis 健康」不是后端功能正常的证据，反过来说，后端把 Redis 停掉也不会有任何测试变红；
等它真被用上（缓存/任务队列）时，这条得补进测试件。

工程根目录里那份 `compose/docker-compose.yml`（2026-08-16）**不是本栈的前身或备用配置，
而是一张便条**：它的全部 `services:` 都被注释掉了，注释里写的是「当前环境已有
Docker MySQL + Redis 运行，数据库 `care_echo_rehab` 已创建」—— 也就是原作者当年直接
复用了宿主上别人的容器。本栈不复用、也不改它：依赖自建、端口另开，才谈得上可复现。
它旁边那份 `compose/.env`（2026-08-16）同理**只属于那张便条**：那份 yml 去掉注释行后
只剩三个空行（一个能跑的 service 都没有），自然也没有任何生效的 `env_file:` 指向它；
本栈的密钥由 `run.sh` 从 `.env.example` 复制并随机填充成 `containerd/.env`
（被 `containerd/.gitignore` 排除）。两份的键名有重叠
（`MYSQL_*` / `JWT_*` / `REDIS_*`）但值互不相同，且那份还带着 `WECHAT_APP_SECRET` 等第三方
凭据 —— 本栈既不读它、也不把它复制进任何镜像层或 `.env`；**没删它**，那属于上游作者的本地文件。
四个上游仓库的容器化覆盖情况：`fay/`、`service/`（+ 新增的 `adapter/`）、`frontend/`
进 compose 并被测试件覆盖，`ue/` 的边界与判据见 `README.md`「UE 仓库的容器侧处置」。

还有一处端口撞车值得记下来：dev 档位给 Fay 音频桥选的宿主端口是 **10199 而不是 9001**，
因为这台机器的 9001 已经被别人的 nextcloud 占着。本栈不复用、也不重启宿主上任何
既有容器 —— 需要端口时让 `FAY_BRIDGE_PORT` 可以让它落在别处。

#### ⚠️ 宿主机遗留：git 的 insteadOf 里躺着明文 token

这一条不属于本栈，但会影响每一个照本文操作的人，所以记在这里而不是塞进根 README 的门面：

本机 `~/.gitconfig` 里有一条全局改写
`url."https://<用户>:gho_…@github.com/".insteadOf = "https://github.com/"`，
把 `gh auth` 的 OAuth token 以明文写死，并让**所有** GitHub remote 在 `git remote -v`
里显示成带 token 的形式 —— 包括 `fay/` 里那个 `upstream` remote。实际上四个上游仓库各自的
`.git/config` 存的都是干净 URL，token 只在克隆/拉取时由这条改写注入。

**处理口径由本机所有者定过（2026-09-23）：不轮换、也不改那条全局配置。** 理由记录在这里，
免得下一个读到本节的人又把它当待办重提一遍：这个 token 只存在于本机，而本栈的推送一律走
显式 SSH URL（`git@github.com:greenhandzdl/…`），根本不经过那条 https 改写。
所以本节是**一行现象解释** —— 你在 `git remote -v` 里看到带 token 的 URL，那是宿主机的改写，
不是哪个仓库把它提交了进去。真要收敛也是宿主机侧的事（删掉 insteadOf、改用
`gh auth git-credential`），本仓库不代改宿主机全局配置，也不把它当成集成问题来"修"。

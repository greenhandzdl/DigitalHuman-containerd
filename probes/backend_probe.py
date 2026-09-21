#!/usr/bin/env python3
"""后端容器活体探针：只查「换到容器里才会坏」的那几件事。

为什么 pytest 那 39 条盖不住：`./run.sh test backend-test` 跑的是上游自带的
`tests/`，它用 TestClient 在**进程内**起 app、连的是独立的
`care_echo_rehab_test` 库。于是这几类故障它天生看不见：正在应答 HTTP 的那个进程
是不是当前镜像起起来的、**业务库**的 schema 有没有跟到 alembic head、compose 注入
的这套配置下服务到底起不起得来。而这三类恰恰是集成过程自己会弄坏的东西 —— 补丁改的是
导入点、seed 层补的是仓库里没有的参考语料、迁移是在干净的 `dh-mysql` 上重跑的。

十一条判据沿用 `probes/fay_probe.py` 的三态规矩：**没执行到的算 SKIP，不算绿**。

  1. 活体健康检查     GET {prefix}/health → 200（这个进程活着，不是 TestClient 活着）
  2. openapi 可取     文档取不到时下面几条各自 SKIP，而不是被第一条的红连带掩盖
  3. 路由挂载完整性   逐个 import `app/api/v1/*.py`，把它们声明的每条 path 拿去对
                      **活服务**的 openapi；少一条就是某个 router 没挂上 / 起的是旧镜像
  4. 迁移追平         实库 `alembic_version` 必须等于脚本目录的 head，且只有一个 head
  5. ORM ↔ 实库       每张 ORM 表在实库里存在、每个列都在（多出来的表/视图只报不判）
  6. GET 扫描         openapi 里所有「无路径参数」的 GET 全部 <500（401/403/422 都算过：
                      这条量的是「这个端点在容器里会不会炸」，不是权限模型）
  7. 带鉴权重跑       用应用自己的三个 dev-login（elder/volunteer/admin）各拿真 token 把
                      第 6 条再扫一遍 —— 匿名扫下来 31/34 是 401，业务读路径根本没走到 DB；
                      三个角色合起来的 2xx 并集没比匿名多即 FAIL
  8. 活体写路径       elder 排一次人工队列 → volunteer 从 SQL 视图 v_volunteer_queue_pending
                      把它读回来 → 取消 → 断言视图里没了。前面几条全是读，视图在 ORM 里
                      没有对应物，只有真写一次再读一次才知道 join/列名/过滤对不对
  9. 定时任务调度器   在这个镜像里真的 start_scheduler() 一遍：apscheduler 装没装、
                      reminder_timezone=Asia/Shanghai 解不解得开（没 tzdata 时 ZoneInfo
                      直接抛），四个作业是否都在排。起完立刻 shutdown
 10. 提醒扫描真跑一次  POST {prefix}/dev/run-reminder-scan?scan_type=all —— 吃药/漏服/
                      随访/训练四个定时任务的本体，平时只在 cron 里跑，容器里从没被执行过
                      也没人知道；DEBUG 关掉时 dev 路由不挂 → SKIP
 11. 排队超时扫描     POST {prefix}/dev/run-queue-timeout-scan —— 同上

10/11 会往业务库写提醒行（是应用自己的 Job 行为，不是探针造的脏数据），第 7 条会建三个
全新的 dev 用户（elder/volunteer/admin 各一个），第 8 条用两个固定 openid 各拿一次 token
并写完就把自己那条排队取消掉（不留悬空的 pending 行），要留干净库就把这几条注释掉；
探针不打 LLM、不碰显存，秒级跑完。

只用镜像里已有的依赖（sqlalchemy / alembic / fastapi / 标准库），所以它跑在
`dh-service:local` 自己身上，不需要额外的镜像。
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

# 脚本从 /probe 跑（compose 只读挂载），`python /probe/x.py` 会把 sys.path[0] 定成
# /probe，于是 import app 找不到 —— 被测的 app 包在镜像的 /app 里。
APP_DIR = os.environ.get("APP_DIR", "/app")
sys.path.insert(0, APP_DIR)

BACKEND = os.environ.get("BACKEND_URL", "http://backend:8000").rstrip("/")
PREFIX = os.environ.get("API_V1_PREFIX", "/api/v1")
TIMEOUT = float(os.environ.get("PROBE_TIMEOUT_SECONDS", "30"))

RESULTS: list[tuple[str, str, str]] = []  # (verdict, name, detail)
OPENAPI_PATHS: dict = {}
ANON_SWEEP: list[tuple[str, int]] = []  # 第 6 条的原始结果，第 7 条拿它当基线

# 三个角色各拿一个真 token：get_current_admin / get_current_volunteer 会把非本角色挡在
# 403，而统计类读路径（业务库里那 4 个视图）大多在管理员侧，只用 elder 扫还是碰不到 DB。
DEV_LOGINS = (
    ("elder", "/auth/dev-login"),
    ("volunteer", "/auth/dev-volunteer-login"),
    ("admin", "/auth/dev-admin-login"),
)


def verdict(kind: str, name: str, detail: str = "") -> None:
    RESULTS.append((kind, name, detail))
    mark = {"PASS": "PASS", "FAIL": "FAIL", "SKIP": "SKIP "}[kind]
    print(f"{mark}  {name}" + (f"  —— {detail}" if detail else ""), flush=True)


def ok(name: str, detail: str = "") -> None:
    verdict("PASS", name, detail)


def bad(name: str, detail: str = "") -> None:
    verdict("FAIL", name, detail)


def skipped(name: str, detail: str = "") -> None:
    verdict("SKIP", name, detail)


def http(
    method: str, path: str, body: bytes | None = None, headers: dict | None = None
) -> tuple[int, str]:
    req = urllib.request.Request(BACKEND + path, method=method, data=body, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001 - 连不上/超时都要成为判据而不是 traceback
        return -1, f"{type(e).__name__}: {e}"


def check_health() -> bool:
    """活体那一跳通不通。openapi 能不能取到不混在这里判，留给各自的判据。"""
    code, text = http("GET", f"{PREFIX}/health")
    if code != 200:
        bad("活体健康检查", f"GET {PREFIX}/health → {code} {text[:120]}")
        return False
    ok("活体健康检查", f"GET {PREFIX}/health → 200 {text[:80].strip()}")
    code, text = http("GET", "/openapi.json")
    if code != 200:
        bad("openapi 文档可取", f"GET /openapi.json → {code}")
        return True
    globals()["OPENAPI_PATHS"] = json.loads(text).get("paths", {})
    ok("openapi 文档可取", f"{len(OPENAPI_PATHS)} 条路径")
    return True


def check_routes_mounted() -> None:
    """每个 v1 模块声明的路由，都必须出现在活服务的 openapi 里。"""
    if not OPENAPI_PATHS:
        skipped("路由挂载完整性", "openapi 没取到，没有可比对象")
        return
    import importlib
    import pkgutil

    try:
        import app.api.v1 as v1
    except Exception as e:  # noqa: BLE001
        bad("路由挂载完整性", f"import app.api.v1 失败：{type(e).__name__}: {e}")
        return
    from fastapi import APIRouter

    live = list(OPENAPI_PATHS)
    missing: list[str] = []
    total = 0
    for m in pkgutil.iter_modules(v1.__path__):
        mod = importlib.import_module(f"{v1.__name__}.{m.name}")
        routers = [
            obj for obj in vars(mod).values()
            if isinstance(obj, APIRouter) and getattr(obj, "routes", None)
        ]
        for r in routers:
            for route in r.routes:
                path = getattr(route, "path", None)
                if not path:
                    continue
                total += 1
                if not any(l == path or l.endswith(path) for l in live):
                    missing.append(f"{m.name}:{path}")
    if missing:
        bad("路由挂载完整性", f"{total - len(missing)}/{total} 命中，缺 {missing[:6]}")
    elif total:
        ok("路由挂载完整性", f"{total} 条声明全部出现在活服务的 openapi 里")
    else:
        bad("路由挂载完整性", "一个 router 都没找到 —— 探针没读到 app.api.v1")


def check_migrations() -> None:
    from sqlalchemy import create_engine, inspect, text
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    from app.core.config import get_settings

    s = get_settings()
    engine = create_engine(s.database_url)
    with engine.connect() as c:
        try:
            current = [r[0] for r in c.execute(text("select version_num from alembic_version"))]
        except Exception as e:  # noqa: BLE001
            bad("迁移追平", f"实库读不到 alembic_version：{type(e).__name__}: {e}")
            return
    sd = ScriptDirectory.from_config(Config(os.path.join(APP_DIR, "alembic.ini")))
    heads = sd.get_heads()
    revisions = len(list(sd.walk_revisions()))
    url = engine.url.render_as_string(hide_password=True).split("@")[-1]
    if len(heads) != 1:
        bad("迁移追平", f"脚本目录有 {len(heads)} 个 head：{heads}（分叉了，探针不敢替你选）")
    elif current == heads:
        ok("迁移追平", f"{url} 在 {current[0]} == head，脚本共 {revisions} 个 revision")
    else:
        bad("迁移追平", f"{url} 实库 {current} ≠ 脚本 head {heads}（共 {revisions} 个 revision）")
    _inspect_cache["engine"] = inspect(engine)
    _inspect_cache["metadata"] = _load_metadata()


def _load_metadata():
    import app.models  # noqa: F401
    from app.db.base import Base

    return Base.metadata


_inspect_cache: dict = {}


def check_schema_parity() -> None:
    try:
        insp = _inspect_cache["engine"]
        metadata = _inspect_cache["metadata"]
    except KeyError:
        skipped("ORM ↔ 实库", "上一道迁移判据没跑成，这里没有可比的引擎")
        return
    live = set(insp.get_table_names())
    want = set(metadata.tables)
    missing = sorted(want - live)
    extra = sorted(live - want)
    cols_missing: list[str] = []
    for t in sorted(want & live):
        have = {c["name"] for c in insp.get_columns(t)}
        cols_missing += [f"{t}.{c}" for c in {c.name for c in metadata.tables[t].columns} - have]
    views = sorted(insp.get_view_names())
    detail = f"{len(want)} 张 ORM 表 / 实库 {len(live)} 表 + {len(views)} 视图"
    if missing or cols_missing:
        bad("ORM ↔ 实库", f"缺表 {missing[:5]} 缺列 {cols_missing[:5]}｜{detail}")
    else:
        ok("ORM ↔ 实库", detail + (f"｜实库多出 {extra}" if extra else ""))


def _get_paths() -> list[str]:
    return [p for p, ops in OPENAPI_PATHS.items() if "get" in ops and "{" not in p]


def _sweep(paths: list[str], headers: dict | None = None) -> list[tuple[str, int]]:
    return [(p, http("GET", p, headers=headers)[0]) for p in paths]


def _rejects(res: list[tuple[str, int]]) -> list[str]:
    return [f"{p}→{c}" for p, c in res if c == -1 or c >= 500]


def _stat(res: list[tuple[str, int]]) -> str:
    codes: dict[str, int] = {}
    for _, c in res:
        b = "2xx" if 200 <= c < 300 else ("4xx" if 400 <= c < 500 else str(c))
        codes[b] = codes.get(b, 0) + 1
    return " ".join(f"{k}:{v}" for k, v in sorted(codes.items()))


def check_get_sweep() -> None:
    if not OPENAPI_PATHS:
        skipped("GET 扫描", "openapi 没取到，没有可比对象")
        return
    paths = _get_paths()
    if not paths:
        skipped("GET 扫描", "openapi 里没有无路径参数的 GET 路由，这条没执行到")
        return
    res = _sweep(paths)
    ANON_SWEEP.extend(res)
    errs = _rejects(res)
    if errs:
        bad("GET 扫描", f"{len(paths)} 条里 {len(errs)} 条 5xx/连不上：{errs[:6]}")
    else:
        ok("GET 扫描", f"{len(paths)} 条无参 GET 全部 <500（{_stat(res)}）")


def check_authed_sweep() -> None:
    """带三个角色的真 token 把第 6 条重跑一遍，判据是「比匿名时多走到 2xx」。

    匿名扫描 34 条里 31 条是 401 —— 也就是说业务读路径根本没走到 service 层，
    而"能走到 DB 的读"恰恰是活体才有意义的部分（视图、JSON 列、真实建表结果）。
    登录用应用自己的 dev-login / dev-volunteer-login / dev-admin-login
    （`app/api/v1/auth.py:64/84/104`，DEBUG 关掉时它们自己 404），所以这条还顺带量了
    「这个容器签发的 token，同一个进程的 deps 认不认」：JWT_SECRET_KEY 注错、算法不匹配、
    user_auth 表没建，都会在这里现形。要三个角色是因为 get_current_admin /
    get_current_volunteer 会先把非本角色挡在 403，而统计类读路径（业务库那 4 个视图）
    多半在管理员侧 —— 只用 elder 扫，它们照样一条都没走到 DB。
    不写「全 401 才算红」是因为那永远不会红 —— 3 条公开路由（health 等）本来就 200；
    失效 token 的实测形态是"2xx 条数跟匿名一模一样"，所以拿匿名当基线比。
    登录出来的是全新的空档案用户，所以 403/404/422 都是预期 —— 量的不是权限模型。
    """
    name = "带鉴权重跑 GET 扫描"
    if not ANON_SWEEP:
        skipped(name, "匿名扫描没跑成，这里没有可比的基线")
        return
    paths = [p for p, _ in ANON_SWEEP]
    n2_anon = sum(1 for _, c in ANON_SWEEP if 200 <= c < 300)
    tokens: dict[str, str] = {}
    for role, suffix in DEV_LOGINS:
        code, text = http("POST", PREFIX + suffix, body=b"{}",
                          headers={"Content-Type": "application/json"})
        if code == 404:
            continue  # DEBUG 关掉时应用自己拒的，这个角色判不了
        if code != 200:
            bad(name, f"POST {PREFIX}{suffix}（{role}）→ {code} {text[:120]}")
            return
        try:
            tokens[role] = json.loads(text)["access_token"]
        except Exception as e:  # noqa: BLE001
            bad(name, f"{role} 登录 200 却读不出 access_token：{type(e).__name__}: {e}｜{text[:120]}")
            return
    if not tokens:
        skipped(name, "三个 dev-login 全 404（DEBUG 关掉时应用自己拒的），这台判不了")
        return
    errs: list[str] = []
    per_role: list[str] = []
    reached: set[str] = set()
    for role, token in tokens.items():
        res = _sweep(paths, {"Authorization": f"Bearer {token}"})
        errs += [f"{role} {p}→{c}" for p, c in res if c == -1 or c >= 500]
        hit = [p for p, c in res if 200 <= c < 300]
        reached |= set(hit)
        per_role.append(f"{role} {len(hit)}")
    if errs:
        bad(name, f"{len(paths)} 条里 {len(errs)} 条 5xx/连不上：{errs[:6]}")
    elif len(reached) <= n2_anon:
        bad(name, f"token 都签出来了却一条都没多走到 2xx（匿名 {n2_anon} → {'，'.join(per_role)}）"
                  f"—— 签发和校验对不上")
    else:
        ok(name, f"{len(tokens)} 个角色各扫 {len(paths)} 条全部 <500"
                 f"（各自 2xx：{'，'.join(per_role)}），合起来 {len(reached)} 条读到过 200 —— "
                 f"匿名只有 {n2_anon} 条，涨出来的是真走到 service 层和业务库的读")


def _dev_token(role: str, openid: str) -> tuple[str | None, str]:
    """走应用自己的 dev-login 拿一个能用的 token；拿不到就返回拒绝原因。

    openid 由调用方给定（不是 dev-login 默认那种随机值）：要写数据的判据必须固定身份，
    否则每轮多攒一对用户 + 一堆业务行 —— run #14 的 backend-test 就是这么红的。
    """
    suffix = dict(DEV_LOGINS)[role]
    body = json.dumps({"openid": openid}).encode()
    code, text = http("POST", PREFIX + suffix, body=body,
                      headers={"Content-Type": "application/json"})
    if code == 404:
        return None, f"POST {PREFIX}{suffix} 404（DEBUG 关掉时应用自己拒的）"
    if code != 200:
        return None, f"POST {PREFIX}{suffix}（{role}）→ {code} {text[:120]}"
    try:
        return json.loads(text)["access_token"], ""
    except Exception as e:  # noqa: BLE001
        return None, f"{role} 登录 200 却读不出 access_token：{type(e).__name__}: {e}｜{text[:120]}"


def _auth(token: str) -> dict:
    return {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}


def check_write_path() -> None:
    """写一条排队，用另一个角色从 SQL 视图读回来，再取消并断言它从视图里消失。

    为什么非要有一条写的：前面几条全是读。业务库 care_echo_rehab 里那 4 个视图在 ORM
    里没有对应物（第 5 条 ORM↔实库判不到它们的列），只有真读一次才知道对不对；而"读得到
    东西"又必须先"写得进去"。挑 human_service_queue 这条链是因为它一步就同时过三样东西：
    应用的 INSERT（`queue_service.enqueue_user`）、视图的 LEFT JOIN elder_profile、
    以及视图自己的 WHERE status='pending' 和 TIMESTAMPDIFF(MINUTE, enqueued_at, NOW())。
    取消那一步不是清理顺带 —— 它是这条判据能红的地方：视图要是把已取消的单也吐出来，
    读回这一步照样 PASS，只有"取消后应当消失"能抓到它。
    写路径挂了（500 / 422 / 视图列名对不上）在这条上都会红，不用等 pytest。
    上游 `tests/test_human_queue.py::test_human_queue_flow` 断的差不多是同一条链，所以这条
    不是"上游没测到的功能"，而是"同一条链换到真进程 + 业务库上再走一遍"：pytest 那侧
    跑在 TestClient + care_echo_rehab_test，run #14 已经证明它会受测试库累积影响 ——
    这边每次打的是刚起来的 backend 进程和 backend-probe 之外没人碰的 care_echo_rehab。

    两个角色各用一个固定 openid，跑完 cancel 掉自己那条，业务库里不留悬空的 pending 行。
    """
    name = "活体写路径：排队→视图读回→取消后消失"
    elder_tok, why = _dev_token("elder", "probe_write_elder")
    if not elder_tok:
        skipped(name, f"拿不到 elder 的 token：{why}")
        return
    vol_tok, why = _dev_token("volunteer", "probe_write_volunteer")
    if not vol_tok:
        skipped(name, f"拿不到 volunteer 的 token：{why}")
        return

    def pending_ids() -> tuple[int, list[int], str]:
        """志愿者侧读待接单视图；返回 (HTTP 码, 视图里的 queue_id 列表, 读不出来的原因)。"""
        code, text = http("GET", f"{PREFIX}/volunteer/home-summary", headers=_auth(vol_tok))
        if code != 200:
            return code, [], text[:160]
        try:
            return code, [q["queue_id"] for q in json.loads(text)["pending_queues"]], ""
        except Exception as e:  # noqa: BLE001
            return code, [], f"200 但响应读不出 pending_queues：{type(e).__name__}: {e}｜{text[:160]}"

    code, text = http("POST", f"{PREFIX}/elder/human-queue/join",
                      body=json.dumps({"risk_tier": "high"}).encode(), headers=_auth(elder_tok))
    if code not in (200, 201):
        bad(name, f"POST /elder/human-queue/join → {code} {text[:160]}")
        return
    try:
        row = json.loads(text)
        qid = row["id"]
    except Exception as e:  # noqa: BLE001
        bad(name, f"排队 2xx 但响应不成样子：{type(e).__name__}: {e}｜{text[:160]}")
        return
    try:
        code2, ids, note = pending_ids()
        if code2 != 200 or note:
            bad(name, f"写进去的是 queue_id={qid}，但 GET /volunteer/home-summary → {code2} {note}")
            return
        if qid not in ids:
            bad(name, f"queue_id={qid} 写成功了，视图 v_volunteer_queue_pending 里却没有它"
                      f"（读到 {len(ids)} 条：{ids[:8]}）—— 视图的列名/join 与实库对不上")
            return
        # 取消是这条判据的第二个断言：视图的 WHERE status='pending' 必须真的在过滤。
        code3, text3 = http("POST", f"{PREFIX}/elder/human-queue/{qid}/cancel",
                            body=b"{}", headers=_auth(elder_tok))
        if code3 not in (200, 204):
            bad(name, f"读回没问题，但 POST /elder/human-queue/{qid}/cancel → {code3} {text3[:120]}")
            return
        _, ids_after, _ = pending_ids()
        if qid in ids_after:
            bad(name, f"queue_id={qid} 已经 cancelled 了，视图还把它算作待接单（{len(ids_after)} 条）"
                      f"—— WHERE status='pending' 没生效")
            return
        ok(name, f"elder 写入 queue_id={qid}（priority={row.get('priority')}）→ 志愿者从视图读回它，"
                 f"wait_minutes={row.get('wait_minutes')} 由 TIMESTAMPDIFF 现算 → "
                 f"取消后视图里剩 {len(ids_after)} 条、不含它 —— 业务库的写、join 与视图过滤都对")
    finally:
        # 中途任何一步红掉，都别让这条单悬在 pending 里污染下一轮（取消是幂等的：
        # enqueue_user 会先返回该用户已有的 active 行，所以悬行还会让下一轮读到旧数据）。
        try:
            http("POST", f"{PREFIX}/elder/human-queue/{qid}/cancel", body=b"{}",
                 headers=_auth(elder_tok))
        except Exception:  # noqa: BLE001 - 清理失败不该盖掉真正的判据结果
            pass


def _dev_scan(path: str, name: str) -> None:
    if not OPENAPI_PATHS:
        skipped(name, "openapi 没取到，先确认这个端点在不在")
        return
    if not any(p.startswith(PREFIX + "/dev/") for p in OPENAPI_PATHS):
        skipped(name, "dev 路由没挂载（app/api/router.py 里 DEBUG 才挂），这台机器判不了这一条")
        return
    code, text = http("POST", path, body=b"")
    if code == 200:
        try:
            msg = json.loads(text).get("message", text[:80])
        except ValueError:
            msg = text[:80]
        ok(name, f"{code} {msg}")
    else:
        bad(name, f"{code} {text[:160]}")


def check_scheduler_jobs() -> None:
    """在这个镜像里真的把 APScheduler 起一遍。

    不是量「活进程里那个调度器现在还活着吗」（跨进程看不见，apscheduler 默认把作业
    放内存，也没有对外接口），而是量这条链路唯一会因容器化而坏的部分：镜像里装没装
    apscheduler、`reminder_timezone=Asia/Shanghai` 在这个镜像里解不解得开（没有 tzdata
    时 ZoneInfo 直接抛）。起不来就是 FAIL；起来了就立刻 shutdown，四个作业的下次触发
    时间最短也是一分钟后，不会在探针进程里真去发提醒。
    """
    want = {"medication_reminder_scan", "follow_up_reminder_scan",
            "training_reminder_scan", "queue_timeout_scan"}
    try:
        from app.core.scheduler import shutdown_scheduler, start_scheduler
        from app.core.config import get_settings

        sched = start_scheduler(get_settings())
    except Exception as e:  # noqa: BLE001
        bad("定时任务调度器起得来", f"{type(e).__name__}: {e}")
        return
    try:
        if sched is None:
            skipped("定时任务调度器起得来", "REMINDER_SCHEDULER_ENABLED=false，这台没开调度器")
            return
        jobs = {j.id: j for j in sched.get_jobs()}
        missing = sorted(want - set(jobs))
        nxt = ", ".join(f"{i}→{jobs[i].next_run_time:%H:%M}" for i in sorted(jobs))
        if missing:
            bad("定时任务调度器起得来", f"缺作业 {missing}｜实有 {sorted(jobs)}")
        else:
            ok("定时任务调度器起得来", f"4 个作业齐（时区 {sched.timezone}）：{nxt}")
    finally:
        shutdown_scheduler()


def main() -> int:
    # 十一条各自判各自的前置条件：后端死了不等于库也死了（第 4/5 条照样能量），
    # 所以不在这里 overall 短路，只让每条自己 SKIP，SKIP 也要留下名字和原因。
    check_health()
    check_routes_mounted()
    try:
        check_migrations()
    except Exception as e:  # noqa: BLE001 - 库连不上要成为判据，不是 traceback
        bad("迁移追平", f"{type(e).__name__}: {e}")
    check_schema_parity()
    check_get_sweep()
    check_authed_sweep()
    try:
        check_write_path()
    except Exception as e:  # noqa: BLE001 - 写路径炸在探针代码里也要成为判据
        bad("活体写路径：排队→视图读回→取消后消失", f"{type(e).__name__}: {e}")
    check_scheduler_jobs()
    _dev_scan(f"{PREFIX}/dev/run-reminder-scan?scan_type=all", "提醒扫描真跑一次")
    _dev_scan(f"{PREFIX}/dev/run-queue-timeout-scan", "排队超时扫描")

    n = {"PASS": 0, "FAIL": 0, "SKIP": 0}
    for kind, _, _ in RESULTS:
        n[kind] += 1
    print(f"\n[backend-probe] {n['PASS']} PASS / {n['SKIP']} SKIP / {n['FAIL']} FAIL"
          f"（共 {len(RESULTS)} 条，目标 {BACKEND}）", flush=True)
    for kind, name, detail in RESULTS:
        if kind == "FAIL":
            print(f"  ✗ {name}  {detail}", flush=True)
    return 1 if n["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main())

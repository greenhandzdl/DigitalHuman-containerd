#!/usr/bin/env python3
"""CareEcho H5 外壳（containerd/frontend/carecho_web.py）的契约测试。

跑在 dh-frontend:local 里，被测对象就是镜像里那份 /app/carecho_web.py 和镜像里那份
`npm run build` 的原样产物 /app/dist —— 也就是说这组同时判「构建产物真的挂上了」和
「外壳把它接对了后端」。外壳自己不发网络请求之外的功能，所以不需要真后端：假后端
起在同一容器里，按 `service/` 的真实响应形状（裸 pydantic、无信封）实现三个口。

判据分四类，每条都要能被改坏：

  A 静态托管（1-5）：产物在不在、Content-Type 对不对、SPA 回退、路径穿越、HTML 不缓存
  B 对话链路（6-11）：信封形状、设备 cookie、同 cookie 复用同一后端会话、不同 cookie
                      不同用户、多带字段不被吞成 422、空 content 不打后端
  C 故障语义（12-15）：后端不可达 / 后端没答 → 502 且 msg 可定位、未实现接口 → 501
                      点名是哪个口、token 过期会重登一次而不是永远 401
  D 并发与自证（16-19）：两设备并发不串线；17 证 `/funasr-ws` 不带升级头时**不会**被
                      静态回退当成 SPA 路径咽掉；18 把「后端 DEBUG=false 就没这个口」
                      那层耦合钉成断言（msg 必须点名 /auth/dev-login）；19 把外壳的
                      Set-Cookie 改坏重跑，判据 8 必须撑不住 —— 撑得住说明那条判据是摆设

只用标准库 —— 和外壳一样，为两个接口拉一套 http 框架不值。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
# 只为最后那条负面自检：把被测对象换成一份「故意改坏」的副本。正常路径用镜像里那份。
CARECHO = os.environ.get("CARECHO_PATH") or "/app/carecho_web.py"
STATIC = os.environ.get("CARECHO_STATIC_UNDER_TEST", "/app/dist")

SIDECAR_PORT = 19811
FAKE_PORT = 19812
DEAD_PORT = 19899                     # 没人听，用来量「后端不可达」那条
SIDECAR = f"http://127.0.0.1:{SIDECAR_PORT}"
# 判据 17 要的是「这条路径在表里，所以不带升级头该被拒」，与 compose 里那条 env 是同一份默认值。
# 显式钉住而不是继承：一旦谁改了 .env 或 docker-compose.yml 的 CARECHO_WS_RELAY，这里要当场变红，
# 不能悄悄退化成「表里没这条路径 → 走静态回退 → 200 index.html」那种看着像通过的假绿。
WS_TABLE = "/funasr-ws=funasr:10095/"

RESULTS: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, detail: str = "") -> None:
    RESULTS.append((bool(ok), name, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  —— {detail}" if detail else ""), flush=True)


# --------------------------------------------------------------------------- 假后端
class FakeBackend:
    """按 service/ 的真实形状实现外壳会打的三个口，并把每次调用记下来供判据核对。

    `settings` 是运行期可改的开关：
      reply=None        → assistant_message 为 null（后端 fay_forwarded=false 时的样子）
      expire_tokens=set  → 这些 token 一律回 401（模拟 JWT 过期）
    """

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.logins: list[str] = []          # 每次 dev-login 的 openid
        self.sessions: dict[int, dict] = {}  # session_id → {user_id, token}
        self.messages: list[tuple[int, str, str]] = []   # (session_id, content, token)
        self.next_user = 100
        self.next_session = 500
        self.reply: str | None = "ok"
        # 后端的 dev-login 只在 DEBUG=true 时挂载；判据 18 要演的就是它没挂的样子，
        # 所以这里得能按 FastAPI 的原样回 404（不是我们自己编的形状）。
        self.dev_login_status = 200
        self.expired: set[str] = set()
        self.lock = threading.Lock()
        self.srv: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *_a) -> None:
                pass

            def _json(self, code: int, obj) -> None:
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _bearer(self) -> str | None:
                raw = self.headers.get("Authorization") or ""
                return raw[7:] if raw.startswith("Bearer ") else None

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) or b"{}"
                with outer.lock:
                    outer.calls.append(self.path)
                if self.path == "/api/v1/auth/dev-login":
                    if outer.dev_login_status != 200:
                        self._json(outer.dev_login_status, {"detail": "Not Found"})
                        return
                    openid = json.loads(raw).get("openid") or ""
                    with outer.lock:
                        outer.next_user += 1
                        uid = outer.next_user
                        outer.logins.append(openid)
                    self._json(200, {"access_token": f"tok-{uid}", "token_type": "bearer",
                                     "expires_in": 720 * 60, "user_id": uid, "is_new_user": True})
                    return
                if self.path == "/api/v1/chat/sessions":
                    tok = outer._token_user(self._bearer())
                    if tok is None:
                        self._json(401, {"detail": "invalid token"})
                        return
                    with outer.lock:
                        outer.next_session += 1
                        sid = outer.next_session
                        outer.sessions[sid] = {"user_id": tok[1], "token": self._bearer()}
                    self._json(201, {"id": sid, "user_id": tok[1], "channel": "wechat_h5",
                                     "status": "active", "title": None,
                                     "created_at": "2026-09-21T00:00:00+08:00"})
                    return
                m = re.fullmatch(r"/api/v1/chat/sessions/(\d+)/messages", self.path)
                if m:
                    sid = int(m.group(1))
                    tok = outer._token_user(self._bearer())
                    if tok is None:
                        self._json(401, {"detail": "invalid token"})
                        return
                    content = json.loads(raw).get("content") or ""
                    with outer.lock:
                        outer.messages.append((sid, content, self._bearer() or ""))
                    reply = (None if outer.reply is None
                             else f"{outer.reply}:{sid}:" + str(len(content)))
                    self._json(201, {
                        "user_message": {"id": 1, "session_id": sid, "role": "user",
                                         "content": content, "content_type": "text"},
                        "assistant_message": (None if reply is None else
                                              {"id": 2, "session_id": sid, "role": "assistant",
                                               "content": reply, "content_type": "text"}),
                        "tier_classification": {"tier": "low", "reason": "假后端"},
                        "human_queue": False,
                        "fay_forwarded": reply is not None,
                        "fay_error": None if reply is not None else "adapter 502",
                    })
                    return
                self._json(404, {"detail": "no such route"})

        self.srv = ThreadingHTTPServer(("127.0.0.1", FAKE_PORT), H)
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()

    def _token_user(self, token: str | None):
        """token → (openid, user_id)；过期或不认识就 None（外壳那边表现成 401）。"""
        if not token or not token.startswith("tok-"):
            return None
        with self.lock:
            if token in self.expired:
                return None
        return (token, int(token[4:]))

    def messages_in(self, session_id: int) -> list[str]:
        with self.lock:
            return [c for sid, c, _ in self.messages if sid == session_id]

    def stop(self) -> None:
        if self.srv:
            self.srv.shutdown()
            # 只 shutdown 的话监听 socket 还占着端口：负面自检里第二个假后端要 bind 同一个口。
            self.srv.server_close()


# --------------------------------------------------------------------------- 被测外壳
class Shell:
    """起一份 carecho_web.py 子进程；close() 会停掉它。"""

    def __init__(self, backend: str, port: int = SIDECAR_PORT, path: str = CARECHO) -> None:
        env = {**os.environ, "CARECHO_BACKEND": backend, "CARECHO_PORT": str(port),
               "CARECHO_STATIC": STATIC, "CARECHO_LOG": "WARNING", "CARECHO_WS_RELAY": WS_TABLE}
        self.base = f"http://127.0.0.1:{port}"
        self.proc = subprocess.Popen([sys.executable, path], env=env,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                self.raw("GET", "/api/health")
                break
            except OSError:
                if self.proc.poll() is not None:
                    raise RuntimeError(f"外壳没起来（退出码 {self.proc.returncode}）")
                time.sleep(0.2)
        else:
            raise RuntimeError("外壳 20s 内没应答 /api/health")

    def close(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()

    def raw(self, method: str, path: str, payload=None, cookie: str | None = None):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Cookie": cookie} if cookie else {})
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                return resp.status, resp.read(), dict(resp.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), dict(exc.headers)

    def api(self, method: str, path: str, payload=None, cookie=None):
        status, body, hdrs = self.raw(method, path, payload, cookie)
        try:
            obj = json.loads(body) if body else {}
        except json.JSONDecodeError:
            obj = {"_unparsed": body[:120].decode("utf-8", "replace")}
        return status, obj, hdrs


def main() -> int:
    fake = FakeBackend()
    fake.start()
    shell = Shell(f"http://127.0.0.1:{FAKE_PORT}")
    try:
        # 1 镜像里那份 vite 产物真的挂着，而且是我们认得的那份
        status, body, hdrs = shell.raw("GET", "/")
        html = body.decode("utf-8", "replace")
        assets = re.findall(r'src="(/assets/[^"]+)"|href="(/assets/[^"]+)"', html)
        asset = next((a or b for a, b in assets), None)
        check(status == 200 and "CareEcho" in html and asset is not None,
              "1 GET / 发出的是构建产物（含 CareEcho 标题与 assets 引用）",
              f"HTTP {status}，引用资产 {asset}")

        # 2 那个哈希资产取得到且 MIME 正确（外壳自己映射表，不是 mimetypes 猜的）
        st2, _, h2 = shell.raw("GET", asset)
        check(st2 == 200 and "javascript" in (h2.get("Content-Type") or ""),
              "2 哈希 JS 资产 200 且 Content-Type 是 javascript",
              f"HTTP {st2} {h2.get('Content-Type')!r}")

        # 3 前端没有 router，但产物挂了就该回退到 index.html 而不是 404 空白页
        st3, body3, _ = shell.raw("GET", "/some/deep/route")
        check(st3 == 200 and b"CareEcho" in body3, "3 未知路径回退到 index.html", f"HTTP {st3}")

        # 4 路径穿越：静态根之外一个字节都不给
        st4, body4, _ = shell.raw("GET", "/..%2f..%2f..%2fetc%2fpasswd")
        check(b"root:" not in body4 and st4 != 200,
              "4 /../etc/passwd 这类穿越拿不到系统文件", f"HTTP {st4}")

        # 5 入口页不缓存：资产带内容哈希，唯一需要换新的是 index.html
        check("no-cache" in (hdrs.get("Cache-Control") or ""),
              "5 index.html 带 Cache-Control: no-cache", repr(hdrs.get("Cache-Control")))

        # 6 信封：前端 axios 拦截器只认 {code:200,data}，data.reply 直接进气泡
        st6, j6, h6 = shell.api("POST", "/api/chat/send",
                               {"content": "我头晕应该挂什么科？", "scene": "consultation"})
        cookie = parse_device_cookie(h6.get("Set-Cookie"))
        check(st6 == 200 and j6.get("code") == 200 and str(j6.get("data", {}).get("reply")).startswith("ok:"),
              "6 一问一答返回 {code:200,data:{session_id,reply}}",
              f"HTTP {st6} {json.dumps(j6, ensure_ascii=False)[:150]}")

        # 7 身份由这一侧发：首次请求必须带回设备 cookie，否则下一问就换个人
        check(bool(cookie) and "HttpOnly" in (h6.get("Set-Cookie") or "")
              and "SameSite=Lax" in (h6.get("Set-Cookie") or ""),
              "7 首次请求下发 HttpOnly 设备 cookie", repr(h6.get("Set-Cookie")))

        # 8 同 cookie 连续两问 → 后端只被建过一次会话（这就是「刷新页面接得上」的全部实现）
        before = fake.calls.count("/api/v1/chat/sessions")
        st8a, j8a, _ = shell.api("POST", "/api/chat/send", {"content": "第二问"}, cookie=cookie)
        st8b, j8b, _ = shell.api("POST", "/api/chat/send", {"content": "第三问"}, cookie=cookie)
        after = fake.calls.count("/api/v1/chat/sessions")
        same = (st8a == st8b == 200 and j8a["data"]["session_id"] == j8b["data"]["session_id"]
                and after == before)
        check(same, "8 同一 cookie 的多问复用同一个后端会话（不新建）",
              f"session {j8a['data'].get('session_id')} vs {j8b['data'].get('session_id')}，建会话次数 +{after - before}")

        # 9 后端收到的 content 与前端发的一致，且多带的 scene 不会把它打成 422
        sent = [c for _, c, _ in fake.messages if c == "第三问"]
        check(len(sent) == 1 and fake.calls.count("/api/v1/auth/dev-login") == 1,
              "9 content 原样到后端、多带的 scene 被忽略（不是 422）",
              f"后端收到 {sent}，dev-login 共 {fake.calls.count('/api/v1/auth/dev-login')} 次")

        # 10 不带 cookie 的另一个设备 = 另一个用户：两条 openid 必须不同
        n0 = len(fake.logins)
        _, j10, h10 = shell.api("POST", "/api/chat/send", {"content": "换个设备"})
        cookie_b = parse_device_cookie(h10.get("Set-Cookie"))
        check(len(fake.logins) == n0 + 1 and fake.logins[-1].startswith("h5_")
              and j10["data"]["session_id"] != j8a["data"]["session_id"] and bool(cookie_b),
              "10 没有 cookie 的请求自成新设备（新 openid、新会话、新 cookie）",
              f"openid {fake.logins[-1][:16]}…")

        # 11 空 content 在进后端之前就被挡下（前端本来也挡，但这道闸得在自己的边界上）
        n_before = len(fake.messages)
        st11, j11, _ = shell.api("POST", "/api/chat/send", {"content": "   "}, cookie=cookie)
        check(st11 == 400 and j11.get("code") == 400 and len(fake.messages) == n_before,
              "11 content 为空 → 400 且不打后端", f"HTTP {st11} msg={j11.get('msg')!r}")

        # 12 后端不可达：502 + msg 能定位（前端气泡就是「连接失败：<msg>」）。
        # 另起一个端口：主外壳还占着 SIDECAR_PORT。
        dead = Shell(f"http://127.0.0.1:{DEAD_PORT}", port=SIDECAR_PORT + 2)
        st12, j12, _ = dead.api("POST", "/api/chat/send", {"content": "喂"})
        check(st12 == 502 and "不可达" in str(j12.get("msg")),
              "12 后端不可达 → 502 且 msg 说清是连不上", f"HTTP {st12} msg={str(j12.get('msg'))[:90]!r}")
        dead.close()

        # 13 后端 201 但数字人没答（fay_forwarded=false）：不能翻译成空回复骗过前端
        fake.reply = None
        st13, j13, _ = shell.api("POST", "/api/chat/send", {"content": "它会答吗"}, cookie=cookie)
        fake.reply = "ok"
        check(st13 == 502 and "fay_error" in str(j13.get("msg")),
              "13 后端没答 → 502 并把 fay_error 带进 msg", f"HTTP {st13} msg={str(j13.get('msg'))[:90]!r}")

        # 14 未实现的接口点名是哪个口（前端 api/ 里那些方法一旦被启用，要能立刻看出来）
        st14, j14, _ = shell.api("GET", "/api/chat/history?session_id=1")
        check(st14 == 501 and "/api/chat/history" in str(j14.get("msg")),
              "14 未实现接口 → 501 且 msg 点名路径", f"HTTP {st14} msg={str(j14.get('msg'))[:90]!r}")

        # 15 JWT 过期会重登一次继续答，而不是从此每发都 401
        tok = session_token(fake, j8a["data"]["session_id"])
        with fake.lock:
            fake.expired.add(tok)
        st15, j15, _ = shell.api("POST", "/api/chat/send", {"content": "过期之后呢"}, cookie=cookie)
        new_sid = j15.get("data", {}).get("session_id")
        check(st15 == 200 and new_sid and new_sid != j8a["data"]["session_id"],
              "15 token 过期 → 自动重登换会话并答上（不永久 401）",
              f"HTTP {st15} session {j8a['data'].get('session_id')} → {new_sid}")

        # 16 两个设备并发各答各的：cookie→session 这张映射表不能串线
        out: dict[str, dict] = {}

        def ask(key: str, ck: str | None) -> None:
            _, body, _ = shell.api("POST", "/api/chat/send", {"content": f"并发-{key}"}, cookie=ck)
            out[key] = body

        threads = [threading.Thread(target=ask, args=a) for a in (("甲", cookie), ("乙", cookie_b))]
        for t in threads:
            t.start()
        for t in threads:
            t.join(90)
        sa = (out.get("甲") or {}).get("data", {}).get("session_id")
        sb = (out.get("乙") or {}).get("data", {}).get("session_id")
        check(sa is not None and sb is not None and sa != sb
              and fake.messages_in(sa)[-1:] == ["并发-甲"]
              and fake.messages_in(sb)[-1:] == ["并发-乙"],
              "16 两设备并发各进各的会话（cookie→session 不串线）",
              f"{sa}←{fake.messages_in(sa)[-1:]}，{sb}←{fake.messages_in(sb)[-1:]}")
        # 17 /funasr-ws 是转发表里的路径：不带升级头的普通 GET 必须当场被拒，不许往下掉。
        #    往下掉的结局是静态回退 —— 判据 3 已经证明任意未知路径都回 200 index.html，
        #    所以这条一旦失守，浏览器看到的是「握手失败 status=200」而日志里一个字节都没有。
        #    （顺带证了没去连上游：funasr 这个主机名在本容器里根本解析不到，真去连会是另一种失败。）
        st17, body17, _ = shell.raw("GET", "/funasr-ws")
        check(st17 == 400 and b"CareEcho" not in body17,
              "17 /funasr-ws 不带 Upgrade 头 → 400（不掉进 SPA 回退，也不碰上游）",
              f"HTTP {st17} 响应体 {body17[:60]!r}")

        # 18 后端 DEBUG=false 时 /api/v1/auth/dev-login 压根没挂载 → 404。这是我们与后端之间
        #    的一处真耦合，所以用断言钉住而不是写进 README 靠人记：气泡必须点名是哪个口，
        #    否则运维拿着「连接失败」三个字只能去查网络，而问题在档位上。
        n_login = len(fake.logins)
        fake.dev_login_status = 404
        st18, j18, _ = shell.api("POST", "/api/chat/send", {"content": "后端没挂这个口"})
        fake.dev_login_status = 200
        check(st18 == 502 and "/auth/dev-login" in str(j18.get("msg"))
              and "404" in str(j18.get("msg")) and len(fake.logins) == n_login,
              "18 后端 dev-login 404（=DEBUG=false 那条路）→ 502 且 msg 点名是哪个口",
              f"HTTP {st18} msg={str(j18.get('msg'))[:120]!r}，登录次数没变={len(fake.logins) == n_login}")
    finally:
        shell.close()
        fake.stop()

    # 19 负面自检：改坏 Set-Cookie 那行，判据 8 那条链路必须撑不住。还稳就说明它是摆设。
    red = run_negative_control()

    good = sum(1 for ok, _, _ in RESULTS if ok)
    total = len(RESULTS) + 1                     # 19 不进 RESULTS，单独算一格
    failed = total - good - (1 if red else 0)
    print(f"\n[frontend-test] {total - failed}/{total} 通过"
          + ("" if failed == 0 else f"，{failed} 项失败")
          + ("" if red else "（其中 19：负面自检没能把判据 8 弄红，这份测试是摆设）"))
    for ok, name, detail in RESULTS:
        if not ok:
            print(f"  ✗ {name}  {detail}")
    return 0 if (good == len(RESULTS) and red) else 1


def parse_device_cookie(set_cookie: str | None) -> str | None:
    if not set_cookie:
        return None
    m = re.search(r"(carecho_device=[0-9a-f]{32})", set_cookie)
    return m.group(1) if m else None


def session_token(fake: FakeBackend, session_id: int) -> str:
    with fake.lock:
        return (fake.sessions.get(session_id) or {}).get("token") or ""


def run_negative_control() -> bool:
    """把 cookie 名字改一个字母再起一遍外壳：判据 8（同 cookie 复用会话）必须失败。"""
    with open(CARECHO, encoding="utf-8") as fh:
        src = fh.read()
    broken = src.replace('cookie = f"carecho_device={device}; Path=/; SameSite=Lax; HttpOnly"',
                         'cookie = f"carecho_Xevice={device}; Path=/; SameSite=Lax; HttpOnly"')
    if broken == src:
        print("FAIL  19 负面自检没能改坏任何东西（替换锚点失效，这份自检已经不可信）")
        return False
    path = "/tmp/carecho_broken.py"
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(broken)
    fake = FakeBackend()
    fake.start()
    try:
        shell = Shell(f"http://127.0.0.1:{FAKE_PORT}", port=SIDECAR_PORT + 4, path=path)
        try:
            _, j, h = shell.api("POST", "/api/chat/send", {"content": "一"})
            cookie = parse_device_cookie(h.get("Set-Cookie"))
            n0 = fake.calls.count("/api/v1/chat/sessions")
            shell.api("POST", "/api/chat/send", {"content": "二"}, cookie=cookie or "x=1")
            mutated = fake.calls.count("/api/v1/chat/sessions") > n0
        finally:
            shell.close()
    finally:
        fake.stop()
    print(f"{'PASS' if mutated else 'FAIL'}  19 负面自检：改坏 Set-Cookie 后判据 8 确实撑不住"
          f"  —— 新建了会话 = {mutated}", flush=True)
    return mutated


if __name__ == "__main__":
    sys.exit(main())

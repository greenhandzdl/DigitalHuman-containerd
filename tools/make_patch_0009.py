"""按字节生成 patches/fay/0009-llm-endpoint-env-overridable-CRLF-source.patch。

上游 config_util.py 是 CRLF，diff 必须在字节层做：文本模式读会把 \r 吃掉，
patch 贴上去就改了行尾 —— README「补丁为什么贴不上：源文件是 CRLF」那节说的就是这个。
留着这个脚本是为了上游一动就把补丁重做一遍，不必再手写这段。
"""
import pathlib
import subprocess
import sys

SRC = pathlib.Path("/mnt/data/DigitalHuman/fay/utils/config_util.py")
OUT = pathlib.Path(
    "/mnt/data/DigitalHuman/containerd/patches/fay/"
    "0009-llm-endpoint-env-overridable-CRLF-source.patch")


def block(text: str) -> list[str]:
    return text.strip("\n").split("\n")


LLM = block("""
    # containerd 补丁：LLM 的端点与密钥允许用环境变量覆盖，不设变量时逐字等于上游。
    # 开这一项的理由是「端点是按机器变的事」：小模型跑在本机 ollama 还是远端另一台
    # 带得动大模型的机器上，换一台机器就换一个答案，而 overlay/fay/system.conf 是被跟踪
    # 的配置文件 —— 它只能留「在这台机器上能跑起来」的示例值。上一轮把远端 26B 的地址
    # 直接写进它，于是那 6 行机器拓扑进了 git 历史（已撤回，改由 .env 驱动）。
    # 模型名那两行由 0006 负责（FAY_GPT_MODEL_ENGINE / FAY_BIG_MODEL_ENGINE）。
    # key_gpt_api_key 覆盖在 :560 之前是有意的：上游把 embedding 的 key 复用成了它
    # （embedding_api_key = key_gpt_api_key），先改 LLM 再让 embedding 继承，顺序与上游一致。
    key_gpt_api_key = os.environ.get('FAY_GPT_API_KEY') or key_gpt_api_key
    gpt_base_url = os.environ.get('FAY_GPT_BASE_URL') or gpt_base_url
    big_model_base_url = os.environ.get('FAY_BIG_MODEL_BASE_URL') or big_model_base_url
    big_model_api_key = os.environ.get('FAY_BIG_MODEL_API_KEY') or big_model_api_key
""")

EMBED = block("""
    # containerd 补丁：embedding 的模型/端点/密钥同样可被环境变量覆盖，不设时逐字等于上游。
    # 必须单独开出来、不能蹭上面那条：上游 embedding_api_base_url 在配置文件里
    # embedding_base_url 留空时**直接复用 gpt_base_url**，于是把 LLM 换到远端的那一刻，
    # 嵌入请求也跟着换到那台机器的 /v1/embeddings 上去了 —— 它未必装了那个嵌入模型。
    # 而写进向量库的坐标与查询用的坐标一旦分叉，症状是「检索永远命中不到刚灌进去的内容」，
    # 且不报错。compose 里这一组与知识库侧的 YUESHEN_EMBED_* 由同一对 .env 变量喂，
    # 就是为了不让两边漂。
    embedding_api_model = os.environ.get('FAY_EMBEDDING_MODEL') or embedding_api_model
    embedding_api_base_url = os.environ.get('FAY_EMBEDDING_BASE_URL') or embedding_api_base_url
    embedding_api_key = os.environ.get('FAY_EMBEDDING_API_KEY') or embedding_api_key
""")

raw = SRC.read_bytes()
lines = raw.split(b"\r\n")          # 末元素是文件尾之后的空串

# 按锚点定位而不是按行号：贴错位置比贴不上更难查。锚点带缩进，因为插入点就是它的下一行。
for idx, want in ((545, b"    gpt_base_url = system_config.get('key', 'gpt_base_url'"),
                   (559, b"    embedding_api_key = key_gpt_api_key")):
    if not lines[idx].startswith(want):
        sys.exit(f"锚点不对：第 {idx + 1} 行是 {lines[idx][:60]!r}，期望以 {want!r} 开头")

llm = [ln.encode("utf-8") for ln in LLM]
embed = [ln.encode("utf-8") for ln in EMBED]
# 分别插在 gpt_base_url(:546) 之后、embedding_api_key(:560) 之后 —— 这两处都躲开了
# 0006（插在 :549 之后）与 0007（插在 :563 之后）改动的区间，所以三份补丁能同时贴。
# 前后各留一个空行，形状与 0006 那段一致；:561 本来就是空行，所以 embedding 那段不补尾。
patched = (lines[:546] + [b""] + llm + [b""] + lines[546:560]
           + [b""] + embed + lines[560:])

tmp = pathlib.Path("/tmp/dh0009")
a = tmp / "a" / "utils"
b = tmp / "b" / "utils"
a.mkdir(parents=True, exist_ok=True)
b.mkdir(parents=True, exist_ok=True)
(a / "config_util.py").write_bytes(raw)
(b / "config_util.py").write_bytes(b"\r\n".join(patched))

# 不带 --label：diff 自己写出的 `--- a/<path>\t<mtime> +0800` 才是仓库里那几份补丁的形状。
diff = subprocess.run(
    ["diff", "-ruN", "a", "b"],
    cwd=tmp, capture_output=True)
if not diff.stdout:
    sys.exit("diff 为空，没生成任何东西")
# `-ruN` 对目录做比较时第一行是 `diff -ruN a/... b/...`，仓库里那几份补丁没有它，去掉对齐。
body = diff.stdout.split(b"\n", 1)[1] if diff.stdout.startswith(b"diff -") else diff.stdout
OUT.write_bytes(body)
print(f"写好 {OUT.name}：{len(body)} 字节，"
      f"{body.count(b'@@ -')} 个 hunk")

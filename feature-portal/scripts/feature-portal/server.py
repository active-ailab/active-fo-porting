#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
功能移植工作台 (Feature Porting Portal)
=====================================

一个本地 Web 服务，把「功能评估 -> 代码移植 -> 编译 -> 刷机 -> 自测」串成一条流水线。

启动:
    cd /home/hgw/monaco/tools/feature-portal
    python3 server.py            # 默认 http://127.0.0.1:8770
    python3 server.py --port 9000 --workspace /home/hgw/monaco

设计说明:
  * 只用标准库，不装任何依赖（WSL Ubuntu-20.04 自带 python3.8 直接跑）。
  * 149MB 的 .map 文件用逐行流式解析并缓存，避免每次评估都吃满内存。
  * 外部脚本（active-build / v3dl / tools/gerrit_dl.py）全部走子进程 + 增量日志，
    前端轮询拿日志，不会因为脚本输出长而卡死。
"""
import argparse
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import uuid
import zipfile
from html.parser import HTMLParser
import urllib.error
import urllib.request
from concurrent import futures

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

PORTAL_DIR = os.path.dirname(os.path.abspath(__file__))


def normalize_workspace_path(path):
    """把 Windows/WSL UNC 路径转换成当前 WSL 内可访问的 Linux 路径。"""
    p = str(path or "").strip().strip('"')
    # Windows Python 进程无法访问 /home/...，必须保留 UNC 路径。
    if os.name == "nt":
        return p
    m = re.match(r"^[\\/]{2}wsl(?:\.localhost)?[\\/]([^\\/]+)[\\/](.*)$",
                 p, re.I)
    if m:
        rest = m.group(2).replace("\\", "/")
        return "/" + rest.lstrip("/")
    return p.replace("\\", "/") if p.startswith("/") else p
WEB_DIR = os.path.join(PORTAL_DIR, "web")
DATA_DIR = os.path.join(PORTAL_DIR, "data")
CACHE_DIR = os.path.join(DATA_DIR, "cache")
STATE_FILE = os.path.join(DATA_DIR, "state.json")

for _d in (DATA_DIR, CACHE_DIR):
    try:
        os.makedirs(_d)
    except OSError:
        pass


# --------------------------------------------------------------------------
# 工作区探测
# --------------------------------------------------------------------------
def detect_workspace():
    """按优先级确定代码工作区根目录。"""
    cands = []
    env = os.environ.get("FEATURE_PORTAL_WORKSPACE")
    if env:
        cands.append(env)
    # 本文件位于 <ws>/tools/feature-portal/server.py
    cands.append(os.path.dirname(os.path.dirname(PORTAL_DIR)))
    cands.append("/home/hgw/monaco")
    cands.append("/home/hgw/workspace")
    for c in cands:
        if not c:
            continue
        c = os.path.abspath(c)
        if os.path.isdir(os.path.join(c, "build")) and os.path.isfile(
                os.path.join(c, "build", "Makefile")):
            return c
    return os.path.abspath(cands[1])


WORKSPACE = detect_workspace()


# --------------------------------------------------------------------------
# 通用小工具
# --------------------------------------------------------------------------
def now_str():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _runnable(p):
    """这个路径在当前系统上真的能被执行吗？

    Windows 上 PATH 里常见 \\\\wsl.localhost\\...\\python3 这种 Linux ELF，
    看着是文件、其实 CreateProcess 会报 WinError 1921；直接跳过。
    """
    if not (os.path.isfile(p) and os.access(p, os.X_OK)):
        return False
    if os.name == "nt":
        low = p.lower()
        if low.startswith("\\\\wsl") or ".wsl.localhost" in low:
            return False
        if not low.endswith((".exe", ".bat", ".cmd")):
            return False
    return True


def which(prog):
    """在常见位置找可执行文件（不依赖服务进程的 PATH）。"""
    if not prog:
        return None
    if os.path.isabs(prog):
        return prog if _runnable(prog) else None
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if not d:
            continue
        p = os.path.join(d, prog)
        if _runnable(p):
            return p
    home = os.path.expanduser("~")
    for d in (os.path.join(home, "bin"), os.path.join(home, ".local", "bin"), "/usr/local/bin"):
        p = os.path.join(d, prog)
        if _runnable(p):
            return p
    return None


def _safe_cwd(p):
    """子进程工作目录：给不存在/不可用的路径兜个底，别让 CreateProcess 直接炸。"""
    if p and os.path.isdir(p):
        return p
    for cand in (DATA_DIR, PORTAL_DIR, os.path.expanduser("~")):
        if cand and os.path.isdir(cand):
            return cand
    return None


def json_read(path, default=None):
    try:
        with io.open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def json_write(path, obj):
    tmp = path + ".tmp"
    try:
        with io.open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------
# Codex / OpenAI Responses API（可选，不影响无 API Key 时的本地评估）
# --------------------------------------------------------------------------
CODEX_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4.1-mini")
CODEX_FALLBACK_MODELS = [
    "gpt-4.1-mini",
    "gpt-4.1",
    "gpt-4o-mini",
]


def codex_cli_path():
    """查找本机 Codex CLI；网页服务启动时的 PATH 可能没有 VS Code 扩展目录。"""
    candidates = [
        shutil.which("codex"),
        "/home/hgw/.vscode-server/extensions/openai.chatgpt-26.1007.21434-linux-x64/bin/linux-x86_64/codex",
    ]
    for path in candidates:
        if path and os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return ""
SCREEN_EVENT_RULES = """统一 Screen 事件评审规则（目标：不漏事件、不误触发）
1. 按键：检查 confirm/背键/上下左右/长按/短按/重复触发；页面声明的按键必须有处理，返回、确认和取消路径不能互相覆盖。
2. 触摸：检查点击、滑动方向、长按、抬起/取消、边界坐标和控件消费事件；不可把背景点击误判为控件点击。
3. 旋钮：检查顺时针、逆时针、按下、长按（如硬件支持）和焦点切换；旋钮事件必须只作用于当前焦点/列表，不能穿透到后台页面。
4. 生命周期：进入、退出、返回、重复进入、异常中断后事件状态必须清理；禁止重复注册监听导致一次操作多次响应。
5. 适配：390×450 的触摸热区、文字、焦点和 style 必须独立核对，不能直接沿用 480 的坐标或 style。
6. 评审输出：每个事件给出 PASS / FAIL / UNKNOWN，附文件、函数或控件证据；缺证据必须报 UNKNOWN，不得默认通过。"""


class _DescriptionTableParser(HTMLParser):
    """从 PFS 页面表格中提取 Description 列，不把其它列当需求。"""
    def __init__(self):
        HTMLParser.__init__(self)
        self.rows = []
        self.row = None
        self.cell = None
        self.tag = ""

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag == "tr":
            self.row = []
        elif tag in ("th", "td") and self.row is not None:
            self.cell = []
            self.tag = tag
        elif tag == "br" and self.cell is not None:
            self.cell.append("\n")

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in ("th", "td") and self.cell is not None and self.row is not None:
            self.row.append(re.sub(r"[ \t]+", " ", "".join(self.cell)).strip())
            self.cell = None
        elif tag == "tr" and self.row is not None:
            if self.row:
                self.rows.append(self.row)
            self.row = None


def extract_pfs_description(content):
    parser = _DescriptionTableParser()
    try:
        parser.feed(content or "")
    except Exception:
        pass
    values = []
    for row in parser.rows:
        headers = [str(x).strip().lower() for x in row]
        if "description" in headers:
            i = headers.index("description")
            if i < len(row) and row[i].strip():
                values.append(row[i].strip())
    if values:
        return "\n\n".join(dict.fromkeys(values))
    # 兼容导出的 Markdown/纯文本 PFS：只截取 Description 标题之后的内容。
    m = re.search(r"(?im)^\s*description\s*[:：]?\s*$([\s\S]*)", content or "")
    return m.group(1).strip() if m else (content or "").strip()


def extract_requirement_section(content, section=""):
    """读取飞书页面指定标题下的内容，直到下一个同级/更高标题。"""
    section = (section or "").strip()
    if not section:
        return extract_pfs_description(content)
    raw = content or ""
    plain = re.sub(r"<br\s*/?>", "\n", raw, flags=re.I)
    plain = re.sub(r"</(p|div|li|tr|h[1-6])>", "\n", plain, flags=re.I)
    plain = re.sub(r"<[^>]+>", "", plain)
    plain = re.sub(r"[ \t]+", " ", plain)
    lines = [x.strip() for x in plain.splitlines() if x.strip()]
    wanted = section.casefold()
    start = next((i for i, line in enumerate(lines) if wanted in line.casefold()), -1)
    if start < 0:
        return ""
    stop = len(lines)
    for i in range(start + 1, len(lines)):
        if re.match(r"^(?:第\s*\d+\s*[章节部分]|\d+(?:\.\d+)*[、.：:]|[一二三四五六七八九十]+[、.])", lines[i]):
            stop = i
            break
    return "\n".join(lines[start + 1:stop]).strip()


def make_numbered_pfs(text):
    items = []
    for line in re.split(r"\n+", text or ""):
        line = re.sub(r"^\s*(?:[-*•]|\d+[、.])\s*", "", line).strip()
        if line:
            items.append(line)
    return "\n".join("%d、%s" % (i, item) for i, item in enumerate(items, 1))


def strip_document_comments(text):
    """只保留文档正文，去掉 Markdown 注释块和常见评论 sidecar。"""
    text = re.sub(r"<!--.*?-->", "", text or "", flags=re.S)
    text = re.split(r"(?im)^\s*(?:comments?|评论区|评论内容)\s*[:：]?\s*$", text)[0]
    return text.strip()


def analyze_requirement_with_ai(requirement, source=""):
    prompt = """你是 ZeppOS 功能需求分析专家。请只分析下面的需求正文，不能引用或臆测评论区内容。
请判断本次需求具体修改了什么，并输出严格 JSON：
{"summary":"一句话总结", "changes":["明确的修改项"], "scope":["涉及模块/页面/流程"], "acceptance":["可验证的验收条件"], "risks":["风险"]}
要求：changes 必须是具体动作，不能写空泛描述；正文没有提到的内容不要补充。

来源：""" + source + "\n需求正文：\n" + requirement[:120000]
    r = call_codex(prompt)
    if not r.get("ok"):
        return {"ok": False, "error": r.get("error") or "AI 需求分析失败"}
    parsed = parse_codex_json(r.get("text")) or {}
    return {"ok": True, "analysis": parsed, "model": r.get("model")}


def _lark_cli():
    return which("lark-cli") or "/home/hgw/.local/node_modules/.bin/lark-cli"


def _lark_json(args):
    cli = _lark_cli()
    if not cli or not os.path.isfile(cli):
        raise RuntimeError("未找到 lark-cli，请先完成飞书授权")
    p = subprocess.run([cli] + args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       text=True, encoding="utf-8", errors="replace", timeout=60)
    try:
        data = json.loads(p.stdout)
    except Exception:
        raise RuntimeError((p.stderr or p.stdout or "lark-cli 返回无效数据")[-1000:])
    if not data.get("ok"):
        raise RuntimeError(json.dumps(data.get("error") or data, ensure_ascii=False))
    return data.get("data") or {}


def fetch_feishu_requirement(url, section=""):
    """通过 lark-cli 读取 Wiki 真实对象；支持 Wiki -> docx/doc/sheet。"""
    m = re.search(r"/wiki/([^/?#]+)", url)
    if not m:
        raise RuntimeError("当前只支持飞书 Wiki 链接")
    node = _lark_json(["wiki", "spaces", "get_node", "--params", json.dumps({"token": m.group(1)})]).get("node") or {}
    obj = node.get("obj_token")
    if node.get("obj_type") in ("docx", "doc") and obj:
        data = _lark_json(["docs", "+fetch", "--doc", obj,
                           "--doc-format", "markdown", "--scope", "full"])
        content = data.get("content") or data.get("markdown") or data.get("text") or ""
        if not content:
            content = json.dumps(data, ensure_ascii=False)
        requirement = ""
        if section:
            # Markdown 文档标题可能带编号、空格或全角标点，按标题层级截取子树。
            norm = re.sub(r"[\s#：:、.]+", "", section).casefold()
            lines = [x.rstrip() for x in content.splitlines() if x.strip()]
            hit = next((i for i, line in enumerate(lines)
                        if norm in re.sub(r"[\s#：:、.]+", "", line).casefold()), -1)
            if hit >= 0:
                mh = re.match(r"^(#{1,6})\s+", lines[hit])
                level = len(mh.group(1)) if mh else 6
                end = len(lines)
                for j in range(hit + 1, len(lines)):
                    h = re.match(r"^(#{1,6})\s+", lines[j])
                    if h and len(h.group(1)) <= level:
                        end = j
                        break
                requirement = "\n".join(lines[hit + 1:end]).strip()
        if not requirement:
            requirement = extract_requirement_section(content, section)
        if not requirement:
            raise RuntimeError("没有找到指定标题下的需求内容：%s" % section if section else "飞书文档没有可读取内容")
        return strip_document_comments(requirement), node.get("title") or "飞书需求"
    if node.get("obj_type") != "sheet" or not obj:
        raise RuntimeError("该飞书链接不是可读取的 docx/doc/sheet 文档")
    info = _lark_json(["sheets", "+workbook-info", "--token", obj])
    sh = (info.get("sheets") or [])[0]
    if not sh:
        raise RuntimeError("飞书表格没有可读取的工作表")
    cols = int(sh.get("column_count") or 26)
    rows = int(sh.get("row_count") or 200)
    def col_name(n):
        out = ""
        while n:
            n, r = divmod(n - 1, 26)
            out = chr(65 + r) + out
        return out
    data = _lark_json(["sheets", "+cells-get", "--spreadsheet-token", obj,
                       "--sheet-id", str(sh.get("sheet_id")),
                       "--range", "A1:%s%d" % (col_name(cols), rows),
                       "--format", "json", "--max-chars", "2000000"])
    matrix = [[str(c.get("value") or "") for c in row]
              for row in ((data.get("ranges") or [{}])[0].get("cells") or [])]
    if not matrix:
        raise RuntimeError("飞书表格没有内容")
    header = matrix[0]
    desc_col = next((i for i, v in enumerate(header) if re.search(r"^\s*description\b", v, re.I)), None)
    if desc_col is None:
        desc_col = next((i for i, v in enumerate(header) if "description" in v.lower()), None)
    if desc_col is None:
        raise RuntimeError("飞书模板中没有找到 Description 列")
    values = [(i + 1, row[desc_col].strip() if desc_col < len(row) else "", row)
              for i, row in enumerate(matrix[1:])]
    wanted = (section or "").strip().casefold()
    if wanted:
        hit = next((i for i, value, row in values if wanted in " ".join(row).casefold()), None)
        if hit is None:
            candidates = [v for _, v, _ in values if v and len(v) < 120]
            # 飞书模板常把“需求 1：xxx”作为外部目录标题，表格内部只保留其下方的 Description 行。
            # 这种情况下以需求编号 1 作为起点，读取 Description 列的全部需求项。
            if re.match(r"^需求\s*1\s*[：:、.]", section):
                return "\n".join(v for _, v, _ in values if v), node.get("title") or "飞书需求"
            raise RuntimeError("没有找到指定标题：%s；可选标题：%s" % (section, "、".join(candidates[:20])))
        start = next(i for i, value, row in values if i == hit)
        picked = []
        for i, value, row in values:
            if i < start or not value:
                continue
            if i > start and re.match(r"^需求\s*\d+\s*[：:、.]", value) and picked:
                break
            picked.append(value)
        return "\n".join(picked), node.get("title") or "飞书需求"
    return "\n".join(v for _, v, _ in values if v), node.get("title") or "飞书需求"


def review_import_screens(workspace):
    """下载 topic 后扫描 application 文件，importScreen 即视为新增 Screen 线索。"""
    found = []
    root = normalize_workspace_path(workspace)
    if not os.path.isdir(root):
        return {"checked": False, "screens": [], "message": "工作区不存在"}
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]
        if os.path.basename(base).lower() != "application":
            continue
        for fn in files:
            path = os.path.join(base, fn)
            try:
                text = io.open(path, "r", encoding="utf-8", errors="replace").read()
            except Exception:
                continue
            for no, line in enumerate(text.splitlines(), 1):
                if "importScreen" not in line:
                    continue
                names = re.findall(r"importScreen\s*\(\s*[\"']([^\"']+)", line)
                found.append({"file": os.path.relpath(path, root).replace("\\", "/"),
                              "line": no, "imports": names or [line.strip()]})
    return {"checked": True, "new_screen": bool(found), "screens": found,
            "message": "发现 application 中的 importScreen，需按新增 Screen 评审事件" if found
                       else "未发现 application/importScreen"}


def codex_config():
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    cli = codex_cli_path()
    return {"configured": bool(key) or bool(cli), "provider": "codex-cli" if cli else "openai-api",
            "cli": bool(cli), "model": CODEX_MODEL,
            "endpoint": os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")}


def call_codex(prompt, max_output_tokens=3000):
    # 优先使用本机已登录的 Codex CLI。它走 ChatGPT/Codex 登录态，不消耗 OPENAI_API_KEY 的 API 额度。
    codex_bin = codex_cli_path()
    if codex_bin:
        try:
            proc = subprocess.run(
                [codex_bin, "exec", "--ephemeral", "--skip-git-repo-check",
                 "--sandbox", "read-only", "--json", prompt],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace", timeout=180)
            chunks = []
            for line in (proc.stdout or "").splitlines():
                try:
                    event = json.loads(line)
                except Exception:
                    continue
                item = event.get("item") or {}
                if item.get("type") == "agent_message" and item.get("text"):
                    chunks.append(item["text"])
            if chunks:
                return {"ok": True, "provider": "codex-cli", "model": "codex-cli",
                        "text": "\n".join(chunks)}
            detail = (proc.stderr or proc.stdout or "Codex CLI 没有返回结果")[-1200:]
            return {"ok": False, "provider": "codex-cli",
                    "error": "Codex CLI 调用失败：%s" % detail}
        except Exception as exc:
            return {"ok": False, "provider": "codex-cli",
                    "error": "Codex CLI 调用失败：%s" % exc}

    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        return {"ok": False, "error": "未配置 OPENAI_API_KEY。请在启动 Feature Portal 的终端中设置 API Key。",
                "configured": False, "model": CODEX_MODEL}
    body = json.dumps({
        "model": CODEX_MODEL,
        "input": [{"role": "user", "content": [{"type": "input_text", "text": prompt}]}],
        "max_output_tokens": max_output_tokens,
    }).encode("utf-8")
    req = urllib.request.Request(
        os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/") + "/responses",
        data=body, headers={"Authorization": "Bearer " + key,
                            "Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:1200]
        return {"ok": False, "error": "Codex API HTTP %s: %s" % (exc.code, detail),
                "configured": True, "model": CODEX_MODEL}
    except Exception as exc:
        return {"ok": False, "error": "Codex API 调用失败：%s" % exc,
                "configured": True, "model": CODEX_MODEL}
    chunks = []
    for item in data.get("output", []) or []:
        for part in item.get("content", []) or []:
            if part.get("type") in ("output_text", "text") and part.get("text"):
                chunks.append(part["text"])
    return {"ok": True, "model": CODEX_MODEL, "text": "\n".join(chunks), "raw": data}


def parse_codex_json(text):
    text = (text or "").strip()
    for candidate in (text, re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)):
        try:
            return json.loads(candidate)
        except Exception:
            continue
    return {"raw": text}


def human_bytes(n):
    if n is None:
        return "-"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024.0:
            return "%.2f%s" % (n, unit)
        n /= 1024.0
    return "%.2fTB" % n


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def safe_rel(path, base=None):
    base = base or WORKSPACE
    try:
        return os.path.relpath(path, base).replace("\\", "/")
    except Exception:
        return path


def rel_or_abs(path, base=None):
    """能相对 base 就相对，跑出 base 之外（例如换了工作区）就保留绝对路径。"""
    r = safe_rel(path, base)
    if not r or r.startswith(".."):
        return path.replace("\\", "/")
    return r


# --------------------------------------------------------------------------
# 状态（含「上次选择的路径」）
# --------------------------------------------------------------------------
DEFAULT_STATE = {
    "workspace": WORKSPACE,
    "feature_name": "",
    "eval": {
        "doc": {"done": False, "at": "", "score": 0},
        "code": {"done": False, "at": "", "score": 0},
        "pfs": False, "frr": False
    },
    "feishu": {"app_id": "", "authorized": False},
    "port": {
        "path": WORKSPACE,
        "topic": "",
        "conflicts": [],
        "done": False
    },
    "build": {
        "family": "",
        "product": "",
        "threads": "24",
        "ok": False,
        "at": ""
    },
    "flash": {"done": False, "at": "", "mode": "app"},
    "selftest": {"items": [], "done": False, "at": ""},
    "recent_paths": []
}

_state_lock = threading.Lock()


def load_state():
    st = json_read(STATE_FILE, None)
    if not isinstance(st, dict):
        st = json.loads(json.dumps(DEFAULT_STATE))
    merged = json.loads(json.dumps(DEFAULT_STATE))
    for k, v in st.items():
        if isinstance(v, dict) and isinstance(merged.get(k), dict):
            merged[k].update(v)
        else:
            merged[k] = v
    merged["workspace"] = WORKSPACE
    for section, key in (("port", "path"), ("baseline", "path")):
        if isinstance(merged.get(section), dict) and merged[section].get(key):
            merged[section][key] = normalize_workspace_path(merged[section][key])
    if not merged.get("port", {}).get("path"):
        merged["port"]["path"] = WORKSPACE
    elif not os.path.isdir(merged["port"]["path"]):
        # 旧状态可能来自另一份 checkout（例如 /home/hgw/pamir），
        # 不要继续把它传给 gerrit_dl.py，自动回到当前工作区。
        merged["port"]["path"] = WORKSPACE
    if isinstance(merged.get("baseline"), dict) and merged["baseline"].get("path") \
            and not os.path.isdir(merged["baseline"]["path"]):
        merged["baseline"]["path"] = WORKSPACE
    return merged


def save_state(st):
    return json_write(STATE_FILE, st)


def patch_state(patch):
    """只接受已知的顶层字段，避免页面乱塞的键被永久写进 state.json。"""
    allowed = {"workspace", "feature_name", "eval", "feishu", "port", "build", "flash",
               "selftest", "recent_paths", "baseline"}
    with _state_lock:
        st = load_state()
        for k, v in (patch or {}).items():
            if k not in allowed:
                continue
            if isinstance(v, dict) and isinstance(st.get(k), dict):
                st[k].update(v)
            else:
                st[k] = v
        save_state(st)
        return st


# --------------------------------------------------------------------------
# .config 解析
# --------------------------------------------------------------------------
def parse_dotconfig(path):
    """解析 Kconfig 生成的 .config -> {KEY: value}，未启用的记为 'n'。"""
    out = {}
    if not path or not os.path.isfile(path):
        return out
    try:
        with io.open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.rstrip("\n")
                s = line.strip()
                if not s:
                    continue
                if s.startswith("#"):
                    m = re.match(r"#\s*([A-Za-z0-9_]+)\s+is\s+not\s+set", s)
                    if m:
                        out[m.group(1)] = "n"
                    continue
                if "=" in s:
                    k, v = s.split("=", 1)
                    v = v.strip()
                    if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
                        v = v[1:-1]
                    out[k.strip()] = v
    except Exception:
        pass
    return out


def cfg_int(cfg, key, default=0):
    v = cfg.get(key)
    if v is None:
        return default
    try:
        return int(str(v), 0)
    except Exception:
        return default


def _cfg_enabled(cfg, key):
    return str(cfg.get(key, "n")).lower() in ("y", "m", "1", "yes", "true")


# --------------------------------------------------------------------------
# 存储评估：解析 .map + .config
# --------------------------------------------------------------------------
REGION_NAMES = ("flash", "cache_sram", "sram", "psram_dlm", "psram_text",
                "psram_ext", "ilm", "psram", "psram_heap2")

_REGION_RE = re.compile(
    r"^(%s)\s+0x([0-9a-fA-F]+)\s+0x([0-9a-fA-F]+)" % "|".join(REGION_NAMES))
_SEC_RE = re.compile(
    r"^(\.\w[\w.]*)\s+0x([0-9a-fA-F]+)\s+0x([0-9a-fA-F]+)"
    r"(?:\s+load address\s+0x([0-9a-fA-F]+))?")


def parse_map_stream(path):
    """流式解析 ld map：返回 (regions, sections)。

    只保留顶层 section（.text / .rodata / .data_xxx / .bss_xxx），
    子 section（.text.foo）有缩进，会被自动跳过。
    """
    regions = []
    sections = []
    in_mem = False
    with io.open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if not line:
                continue
            c = line[0]
            if c == "M":
                if line.startswith("Memory Configuration"):
                    in_mem = True
                continue
            if in_mem:
                if line.startswith("Linker script"):
                    in_mem = False
                    continue
                m = _REGION_RE.match(line)
                if m:
                    regions.append({
                        "name": m.group(1),
                        "origin": int(m.group(2), 16),
                        "length": int(m.group(3), 16),
                    })
                continue
            if c == ".":
                m = _SEC_RE.match(line)
                if not m:
                    continue
                name = m.group(1)
                if name.count(".") > 1:      # .text.foo 之类子段
                    continue
                if name.startswith(".debug") or name.startswith(".comment"):
                    continue
                sections.append({
                    "name": name,
                    "addr": int(m.group(2), 16),
                    "size": int(m.group(3), 16),
                    "load": int(m.group(4), 16) if m.group(4) else None,
                })
    return regions, sections


def _find_region(regions, addr, size):
    """找地址所属内存区。用左闭右开，避免相邻区域边界地址互相抢占。"""
    for r in regions:
        if r["origin"] <= addr < r["origin"] + r["length"]:
            return r
    return None


def _is_region_pool_section(name):
    """判断是不是「整块区域占位段」。

    ld 脚本里常用 .psram / .psram_ext / .psram_heap2 这种与内存区同名的
    section 把整片区域整块声明下来（真正的内容会在运行时按需分配）。
    统计时如果把它们算进去，占用率会恒等于 100%，没有参考价值。
    """
    stem = name.lstrip(".").lower()
    for r in REGION_NAMES:
        if stem == r or stem.startswith(r + "_"):
            return True
    return False


def _usage_by_region(regions, sections):
    used = dict((r["name"], 0) for r in regions)
    detail = dict((r["name"], []) for r in regions)
    pools = dict((r["name"], 0) for r in regions)
    for s in sections:
        if s["size"] <= 0:
            continue
        if _is_region_pool_section(s["name"]):
            rp = _find_region(regions, s["addr"], s["size"])
            if rp:
                pools[rp["name"]] += s["size"]
            continue
        # 运行地址
        r = _find_region(regions, s["addr"], s["size"])
        if r:
            used[r["name"]] = max(used[r["name"]], s["addr"] + s["size"] - r["origin"])
            detail[r["name"]].append({"section": s["name"], "size": s["size"],
                                      "role": "run"})
        # load 地址（flash 里的初始化镜像）
        if s["load"] and s["load"] != s["addr"]:
            r2 = _find_region(regions, s["load"], s["size"])
            if r2:
                used[r2["name"]] = max(used[r2["name"]],
                                       s["load"] + s["size"] - r2["origin"])
                detail[r2["name"]].append({"section": s["name"], "size": s["size"],
                                           "role": "load"})
    return used, detail, pools


def find_maps(root=None):
    """扫描 <root>/build/out*/<target>/binary/*.map。"""
    found = []
    build = os.path.join(root or WORKSPACE, "build")
    if not os.path.isdir(build):
        return found
    for root_name in ("out", "out_hub"):
        base = os.path.join(build, root_name)
        if not os.path.isdir(base):
            continue
        for target in sorted(os.listdir(base)):
            bdir = os.path.join(base, target, "binary")
            if not os.path.isdir(bdir):
                continue
            for fn in sorted(os.listdir(bdir)):
                if fn.endswith(".map"):
                    p = os.path.join(bdir, fn)
                    try:
                        stt = os.stat(p)
                    except OSError:
                        continue
                    found.append({
                        "path": p,
                        "target": target,
                        "kind": "app" if root_name == "out" else "sensorhub",
                        "mtime": stt.st_mtime,
                        "size": stt.st_size,
                    })
    found.sort(key=lambda x: x["mtime"], reverse=True)
    return found


CACHE_VERSION = "v4"   # 解析算法改动时递增，让旧缓存自然失效


def _cache_key(path):
    try:
        stt = os.stat(path)
        return "%s|%s|%d|%d" % (CACHE_VERSION,
                               os.path.basename(os.path.dirname(os.path.dirname(path))),
                               int(stt.st_mtime), stt.st_size)
    except OSError:
        return None


def analyze_storage(map_path, cfg_path, use_cache=True):
    """解析一个 map + .config，产出存储占用报告。"""
    cache_file = None
    key = _cache_key(map_path)
    if use_cache and key:
        h = str(abs(hash(key)) % (10 ** 12))
        cache_file = os.path.join(CACHE_DIR, "storage_%s.json" % h)
        cached = json_read(cache_file)
        if isinstance(cached, dict):
            return cached

    t0 = time.time()
    regions, sections = parse_map_stream(map_path)
    used, detail, pools = _usage_by_region(regions, sections)
    cfg = parse_dotconfig(cfg_path)

    region_map = dict((r["name"], r) for r in regions)
    items = []
    for name in ("flash", "psram", "sram", "cache_sram", "psram_text",
                 "psram_ext", "psram_dlm", "ilm", "psram_heap2"):
        if name not in region_map:
            continue
        r = region_map[name]
        u = used.get(name, 0)
        total = r["length"]
        pct = (u / total * 100.0) if total else 0.0
        note = ""
        pool = pools.get(name, 0)
        if pool and pool >= total * 0.9:
            note = "整块预留区（.psram/.heap 池），%.1fMB 交给运行时分配" % (pool / 1048576.0)
        elif pct >= 99 and name != "flash":
            note = "静态段已铺满该区域（含帧缓冲 / 预留缓冲）"
        items.append({
            "name": name,
            "used": u,
            "total": total,
            "free": max(total - u, 0),
            "pct": round(pct, 2),
            "used_h": human_bytes(u),
            "total_h": human_bytes(total),
            "free_h": human_bytes(max(total - u, 0)),
            "note": note,
            "sections": detail.get(name, [])[:12],
        })

    # exbin（PSRAM 扩展固件）
    exbin = None
    bdir = os.path.dirname(map_path)
    target = os.path.basename(os.path.dirname(bdir))
    for cand in os.listdir(bdir) if os.path.isdir(bdir) else []:
        if cand.endswith("_exbin_sign.bin") or cand.endswith("_exbin.bin"):
            p = os.path.join(bdir, cand)
            try:
                sz = os.path.getsize(p)
            except OSError:
                continue
            cap = cfg_int(cfg, "HMI_PSRAM_EXBIN_MAX_SIZE", 0)
            exbin = {
                "file": cand, "size": sz, "size_h": human_bytes(sz),
                "limit": cap, "limit_h": human_bytes(cap),
                "pct": round(sz / cap * 100.0, 2) if cap else None,
            }
            break

    # 代码区上限（.config 里声明的）
    code_cap = cfg_int(cfg, "HMI_MAX_CODE_SIZE", 0)
    flash_item = next((i for i in items if i["name"] == "flash"), None)
    code_pct = None
    if flash_item and code_cap:
        code_pct = round(flash_item["used"] / code_cap * 100.0, 2)

    # 风险结论
    warns = []
    if flash_item:
        if flash_item["pct"] >= 97:
            warns.append({"level": "danger",
                          "text": "FLASH 已用 %.1f%%，仅剩 %s，新功能极可能爆仓"
                                  % (flash_item["pct"], flash_item["free_h"])})
        elif flash_item["pct"] >= 90:
            warns.append({"level": "warn",
                          "text": "FLASH 已用 %.1f%%，剩余 %s，需要关注增量"
                                  % (flash_item["pct"], flash_item["free_h"])})
        else:
            warns.append({"level": "ok",
                          "text": "FLASH 余量健康，剩余 %s（%.1f%%）"
                                  % (flash_item["free_h"], 100 - flash_item["pct"])})
    if exbin and exbin.get("pct") is not None:
        if exbin["pct"] >= 95:
            warns.append({"level": "danger",
                          "text": "EXBIN 已用 %.1f%%，剩余 %s"
                                  % (exbin["pct"], human_bytes(exbin["limit"] - exbin["size"]))})
        elif exbin["pct"] >= 85:
            warns.append({"level": "warn",
                          "text": "EXBIN 已用 %.1f%%，接近上限" % exbin["pct"]})
    if code_cap and flash_item and flash_item["used"] > code_cap:
        warns.append({"level": "danger", "text": "已超过 HMI_MAX_CODE_SIZE 上限"})

    report = {
        "ok": True,
        "map": map_path,
        "map_h": human_bytes(os.path.getsize(map_path)) if os.path.isfile(map_path) else "-",
        "target": target,
        "config": cfg_path,
        "regions": items,
        "sections": [{"name": s["name"], "size": s["size"],
                      "size_h": human_bytes(s["size"]),
                      "addr": "0x%08x" % s["addr"]} for s in sections],
        "exbin": exbin,
        "code_cap": code_cap,
        "code_cap_h": human_bytes(code_cap) if code_cap else "-",
        "code_pct": code_pct,
        "warnings": warns,
        "parse_ms": int((time.time() - t0) * 1000),
        "at": now_str(),
    }
    if cache_file:
        json_write(cache_file, report)
        _prune_storage_cache(cache_file)
    return report


def _prune_storage_cache(keep):
    """map 更新后会生成新的缓存文件，旧的直接清掉，别攒垃圾。"""
    try:
        for fn in os.listdir(CACHE_DIR):
            p = os.path.join(CACHE_DIR, fn)
            if fn.startswith("storage_") and p != keep:
                os.remove(p)
    except Exception:
        pass


# --------------------------------------------------------------------------
# 头文件索引（依赖分析用，带缓存）
# --------------------------------------------------------------------------
_HEADER_INDEX = {"key": None, "index": {}}
_HEADER_LOCK = threading.Lock()
_SKIP_DIRS = {".git", ".repo", "out", "out_hub", "build", "node_modules",
              "__pycache__", ".ccache", "3rdparty", "deprecated"}


def _walk_headers(limit=40000, budget_s=25.0, root=None):
    """扫描工作区头文件名索引。带时间预算，避免超大工程把接口卡死。"""
    root_dir = root or WORKSPACE
    idx = {}
    count = 0
    t0 = time.time()
    for root, dirs, files in os.walk(root_dir):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]
        for fn in files:
            if fn.endswith((".h", ".hpp")):
                idx.setdefault(fn, []).append(os.path.join(root, fn))
                count += 1
                if count >= limit:
                    return idx
        if time.time() - t0 > budget_s:
            break
    return idx


HEADER_INDEX_FILE = os.path.join(CACHE_DIR, "header_index.json")


def header_index(root=None):
    """头文件名 -> 路径列表。落盘缓存，1 小时内不重复扫描。

    root：要索引哪棵树。代码评估必须索引「本地基准目录」（可能是用户选的
    移植目录），不能固定用服务启动的 WORKSPACE，否则 include 解析会指向别的树。
    """
    root_dir = os.path.abspath(root or WORKSPACE)
    with _HEADER_LOCK:
        key = "%d|%s" % (int(time.time() / 3600), root_dir)
        if _HEADER_INDEX["key"] == key and _HEADER_INDEX["index"]:
            return _HEADER_INDEX["index"]
        cached = json_read(HEADER_INDEX_FILE)
        if isinstance(cached, dict) and cached.get("key") == key \
                and cached.get("workspace") == root_dir \
                and isinstance(cached.get("index"), dict) and cached["index"]:
            _HEADER_INDEX["key"] = key
            _HEADER_INDEX["index"] = cached["index"]
            return cached["index"]
        idx = _walk_headers(root=root_dir)
        _HEADER_INDEX["key"] = key
        _HEADER_INDEX["index"] = idx
        json_write(HEADER_INDEX_FILE, {"key": key, "workspace": root_dir,
                                       "index": idx})
        return idx


_INCLUDE_RE = re.compile(r'^\s*#\s*include\s+"([^"]+)"', re.M)
_EXTERN_RE = re.compile(
    r'^\s*extern\s+(?:[A-Za-z_][\w\s\*]*?\s+)?([A-Za-z_]\w*)\s*\(', re.M)
_PROTO_RE = re.compile(
    r'^(?:[A-Za-z_][\w]*[\s\*]+)+([A-Za-z_]\w*)\s*\([^;{]*\)\s*;', re.M)
# 华米代码里 H_Bool / H_Int32 这类是类型别名，容易被当成函数名
_TYPE_ALIAS_RE = re.compile(r'^[HU]_(Bool|Int\d+|UInt\d+|Float|Double|Char|Void|Bool\d*)$')
_NOT_FUNC = {"if", "for", "while", "switch", "return", "sizeof", "defined",
             "typedef", "struct", "union", "enum"}


def _is_type_alias(name):
    return bool(_TYPE_ALIAS_RE.match(name)) or name in _NOT_FUNC


def _read_text(path, max_bytes=400000):
    try:
        with io.open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read(max_bytes)
    except Exception:
        return ""


def _resolve_include(inc, cur_file, repo_root, hidx):
    """把 #include "x.h" 解析成本地绝对路径。"""
    cands = [os.path.normpath(os.path.join(os.path.dirname(cur_file), inc))]
    if repo_root:
        cands.append(os.path.normpath(os.path.join(repo_root, inc)))
    for c in cands:
        if os.path.isfile(c):
            return c
    base = os.path.basename(inc)
    hits = hidx.get(base) or []
    if hits:
        # 优先同仓库/同目录树的
        if repo_root:
            for h in hits:
                if h.startswith(repo_root):
                    return h
        return hits[0]
    return None


def analyze_dependencies(files, max_depth=3, max_nodes=250, root=None):
    """从改动文件出发做 #include 递归，产出依赖代码 / 依赖接口。

    接口只保留「真的被改动文件引用过」的那些，否则会把整个头文件库的
    函数原型全导出来，噪声大到没法看。
    """
    hidx = header_index(root)
    files_set = set(os.path.abspath(f) for f in files)
    corpus = set()
    seen = set()
    queue = []
    for f in files:
        if os.path.isfile(f):
            queue.append((os.path.abspath(f), 0, None))
    dep_files = []
    interfaces = {}
    while queue and len(seen) < max_nodes:
        path, depth, parent = queue.pop(0)
        if path in seen:
            continue
        seen.add(path)
        repo_root = _repo_root_of(path)
        text = _read_text(path)
        if path in files_set:
            corpus.update(re.findall(r"[A-Za-z_]\w*", text))
        incs = _INCLUDE_RE.findall(text)
        if depth < max_depth:
            for inc in incs:
                rp = _resolve_include(inc, path, repo_root, hidx)
                if rp and rp not in seen:
                    queue.append((rp, depth + 1, path))
        if path not in files_set:
            dep_files.append({
                "path": path,
                "rel": safe_rel(path),
                "depth": depth,
                "via": safe_rel(parent) if parent else "",
                "module": module_of(path),
            })
        # 接口：头文件里对外声明的函数
        if path.endswith((".h", ".hpp")):
            decls = set()
            for regex in (_EXTERN_RE, _PROTO_RE):
                for m in regex.finditer(text):
                    name = m.group(1)
                    if regex is _PROTO_RE and "typedef" in m.group(0):
                        continue
                    decls.add(name)
            for name in decls:
                if len(name) < 3 or _is_type_alias(name):
                    continue
                interfaces.setdefault(name, {
                    "name": name, "header": safe_rel(path),
                    "module": module_of(path),
                })
    used = [v for k, v in interfaces.items() if (not corpus) or (k in corpus)]
    used.sort(key=lambda x: x["name"])
    return {
        "nodes": len(seen),
        "files": dep_files,
        "interfaces": used,
        "declared_total": len(interfaces),
    }


def _repo_root_of(path):
    p = os.path.dirname(path)
    for _ in range(8):
        if os.path.exists(os.path.join(p, ".git")):
            return p
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    return WORKSPACE


# --------------------------------------------------------------------------
# manifest：project -> path，用于模块归属
# --------------------------------------------------------------------------
_MANIFEST = {"mtime": 0, "map": {}}


def manifest_map():
    mf = os.path.join(WORKSPACE, ".repo", "manifest.xml")
    try:
        mt = os.path.getmtime(mf)
    except OSError:
        return {}
    if _MANIFEST["mtime"] == mt and _MANIFEST["map"]:
        return _MANIFEST["map"]
    txt = _read_text(mf, 4000000)
    proj = {}
    for m in re.finditer(r'<project\s+([^>]+?)\s*/>', txt):
        attrs = dict(re.findall(r'(\w+)="([^"]*)"', m.group(1)))
        if attrs.get("path") and attrs.get("name"):
            proj[attrs["path"]] = attrs["name"]
    # include 的其它 manifest
    for inc in re.findall(r'<include\s+name="([^"]+)"', txt):
        sub = os.path.join(WORKSPACE, ".repo", "manifests", inc)
        stxt = _read_text(sub, 4000000)
        for m in re.finditer(r'<project\s+([^>]+?)\s*/>', stxt):
            attrs = dict(re.findall(r'(\w+)="([^"]*)"', m.group(1)))
            if attrs.get("path") and attrs.get("name"):
                proj.setdefault(attrs["path"], attrs["name"])
    _MANIFEST["mtime"] = mt
    _MANIFEST["map"] = proj
    return proj


def module_of(path):
    rel = safe_rel(path).replace("\\", "/")
    pm = manifest_map()
    best = ""
    for p in pm:
        if rel == p or rel.startswith(p + "/"):
            if len(p) > len(best):
                best = p
    if best:
        return best
    parts = rel.split("/")
    return "/".join(parts[:2]) if len(parts) >= 2 else parts[0]


# --------------------------------------------------------------------------
# 硬件支持评估
# --------------------------------------------------------------------------
HW_KEYWORDS = {
    "心率/PPG": ["hr", "heart", "ppg", "hrm"],
    "血氧": ["spo2", "oximeter"],
    "ECG": ["ecg"],
    "加速度/IMU": ["imu", "gsensor", "accel", "gyro"],
    "气压/海拔": ["baro", "altimeter", "pressure"],
    "GNSS/GPS": ["gnss", "gps"],
    "NFC": ["nfc"],
    "蓝牙/BLE": ["ble", "bt", "bluetooth"],
    "音频/扬声器": ["audio", "speaker", "mic", "asr", "vad"],
    "马达/振动": ["motor", "vibrat", "haptic"],
    "屏幕/显示": ["display", "lcd", "oled", "screen", "touch", "tp"],
    "充电/电源": ["charger", "charge", "battery", "power", "pmu"],
    "存储/NAND": ["nand", "emmc", "nor", "fatfs", "flash"],
    "温度": ["temp", "thermo"],
}


def hardware_support(cfg, tokens, cfg_path):
    """按功能关键词匹配 .config 里的能力开关。"""
    tokens = set(t.lower() for t in tokens if len(t) >= 3)
    matched_groups = []
    for label, kws in HW_KEYWORDS.items():
        if not (tokens & set(kws)) and not any(
                any(k in t for k in kws) for t in tokens):
            continue
        hits = []
        for k in cfg:
            lk = k.lower()
            if not any(kw in lk for kw in kws):
                continue
            if len(hits) >= 8:
                break
            hits.append({"key": k, "value": cfg[k],
                         "enabled": _cfg_enabled(cfg, k)})
        if not hits:
            continue
        enabled = [h for h in hits if h["enabled"]]
        matched_groups.append({
            "label": label,
            "supported": len(enabled) > 0,
            "total": len(hits),
            "enabled_count": len(enabled),
            "items": hits,
        })

    board = {
        "chip": cfg.get("HMI_CHIP") or cfg.get("CONFIG_ARCH") or "",
        "board": cfg.get("HMI_BOARD") or cfg.get("CONFIG_BOARD") or "",
        "code_cap": human_bytes(cfg_int(cfg, "HMI_MAX_CODE_SIZE", 0)),
        "psram": human_bytes(cfg_int(cfg, "HMI_BOARD_PSRAM2_SIZE", 0)
                             or cfg_int(cfg, "HMI_BOARD_PSRAM1_SIZE", 0)),
        "exbin_cap": human_bytes(cfg_int(cfg, "HMI_PSRAM_EXBIN_MAX_SIZE", 0)),
        "sram_heap": human_bytes(cfg_int(cfg, "HMI_SRAM_HEAP_SIZE", 0)),
        "main_heap": human_bytes(cfg_int(cfg, "HMI_MHS003_MAIN_HEAP_SIZE", 0)),
        "config": cfg_path,
    }
    # 板级关键项原始值
    key_items = []
    for k in ("HMI_BOARD_PSRAM2_SIZE", "HMI_PSRAM_EXBIN_MAX_SIZE",
              "HMI_MAX_CODE_SIZE", "HMI_PSRAM_USER_SECTION_SIZE",
              "HMI_SRAM_HEAP_SIZE", "HMI_MHS003_MAIN_HEAP_SIZE"):
        if k in cfg:
            key_items.append({"key": k, "value": cfg[k],
                              "human": human_bytes(cfg_int(cfg, k, 0))})

    unsupported = [g["label"] for g in matched_groups if not g["supported"]]
    return {
        "groups": matched_groups,
        "board": board,
        "key_items": key_items,
        "unsupported": unsupported,
        "ok": len(unsupported) == 0,
        "note": ("未在 .config 中找到这些能力开关，需确认硬件是否具备：%s"
                 % "、".join(unsupported)) if unsupported else "本次功能涉及的硬件能力均已在配置中启用",
    }


# --------------------------------------------------------------------------
# 工期预测
# --------------------------------------------------------------------------
def estimate_effort(metrics):
    """工期估算。

    数量类因子做上限折算：用户如果圈了整个模块目录（几百个文件），
    线性外推会得到荒谬的天数，这里按经验上限收敛，并标记低置信度。
    """
    files = metrics.get("files", 0)
    modules = metrics.get("modules", 0)
    ifaces = metrics.get("interfaces", 0)
    conflicts = metrics.get("conflicts", 0)
    loc = metrics.get("loc", 0)
    new_api = metrics.get("new_api", 0)

    files_eff = min(files, 50)
    ifaces_eff = min(ifaces, 100)
    loc_eff = min(loc, 6000)
    low_conf = (files > 80) or (ifaces > 150)

    score = (8 + files_eff * 1.6 + modules * 6.5 + ifaces_eff * 0.30
             + conflicts * 9 + loc_eff / 110.0 + new_api * 1.2)
    diff = clamp(score, 5, 100)
    if diff < 30:
        level, color = "简单", "ok"
    elif diff < 55:
        level, color = "中等", "info"
    elif diff < 78:
        level, color = "复杂", "warn"
    else:
        level, color = "高难", "danger"

    base = 0.4 + files_eff * 0.14 + modules * 0.55 + ifaces_eff * 0.040 \
        + conflicts * 0.45 + loc_eff / 500.0 + new_api * 0.12
    low = max(0.3, base * 0.8)
    high = base * 1.4

    breakdown = [
        {"label": "改动文件", "value": files, "weight": round(files_eff * 0.14, 2),
         "unit": "个"},
        {"label": "涉及模块", "value": modules, "weight": round(modules * 0.55, 2),
         "unit": "个"},
        {"label": "外部接口", "value": ifaces, "weight": round(ifaces_eff * 0.040, 2),
         "unit": "个"},
        {"label": "冲突仓库", "value": conflicts, "weight": round(conflicts * 0.45, 2),
         "unit": "个"},
        {"label": "新增代码", "value": loc, "weight": round(loc_eff / 500.0, 2),
         "unit": "行"},
        {"label": "新增 API", "value": new_api, "weight": round(new_api * 0.12, 2),
         "unit": "个"},
    ]
    assume = "基准效率 1 人日 = 有效编码 6 小时，含自测与联调；不含等待评审/送测时间"
    if low_conf:
        assume += ("。当前统计到 %d 个文件 / %d 个接口，规模超出单功能量级，"
                   "已按上限折算；建议填 gerrit topic 用真实 change 精确评估"
                   % (files, ifaces))
    return {
        "difficulty": round(diff, 1),
        "level": level,
        "tone": color,
        "days": round(base, 2),
        "days_low": round(low, 2),
        "days_high": round(high, 2),
        "low_confidence": low_conf,
        "breakdown": breakdown,
        "assume": assume,
    }


# --------------------------------------------------------------------------
# 任务（子进程）管理
# --------------------------------------------------------------------------
JOBS = {}
_JOB_LOCK = threading.Lock()
_ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")


class Job(object):
    def __init__(self, name, cmd, cwd, env=None, shell=False):
        self.id = uuid.uuid4().hex[:12]
        self.name = name
        self.cmd = cmd
        self.cwd = cwd
        self.lines = []
        self.status = "running"
        self.rc = None
        self.started = now_str()
        self.ended = ""
        self.meta = {}
        self._proc = None
        self._env = env
        self._shell = shell

    def cmd_str(self):
        if isinstance(self.cmd, (list, tuple)):
            return " ".join(self.cmd)
        return str(self.cmd)

    def append(self, text):
        text = _ANSI_RE.sub("", text or "")
        for ln in text.rstrip("\n").split("\n"):
            self.lines.append(repair_mojibake(ln))

    def snapshot(self, since=0):
        since = max(0, int(since or 0))
        return {
            "id": self.id, "name": self.name, "cmd": self.cmd_str(),
            "cwd": self.cwd, "status": self.status, "rc": self.rc,
            "started": self.started, "ended": self.ended,
            "lines": self.lines[since:], "cursor": len(self.lines),
            "meta": self.meta,
        }


def start_job(name, cmd, cwd, env=None, shell=False, input_text=None):
    job = Job(name, cmd, cwd, env, shell)
    with _JOB_LOCK:
        JOBS[job.id] = job

    def worker():
        try:
            e = dict(os.environ)
            e["PYTHONUNBUFFERED"] = "1"
            e["TERM"] = "dumb"
            if env:
                e.update(env)
            job._proc = subprocess.Popen(
                cmd, cwd=_safe_cwd(cwd), stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, stdin=(subprocess.PIPE if input_text is not None else None),
                env=e, shell=shell,
                universal_newlines=True, bufsize=1,
                encoding="utf-8", errors="replace")
            if input_text is not None and job._proc.stdin:
                job._proc.stdin.write(input_text)
                job._proc.stdin.close()
            for line in job._proc.stdout:
                job.append(line)
            job.rc = job._proc.wait()
            job.status = "ok" if job.rc == 0 else "fail"
        except Exception as exc:
            job.append("!! 启动失败: %s" % exc)
            job.rc = -1
            job.status = "fail"
        finally:
            job.ended = now_str()

    t = threading.Thread(target=worker)
    t.daemon = True
    t.start()
    return job


# --------------------------------------------------------------------------
# 项目列表（family / product）
# --------------------------------------------------------------------------
VARIANT_SUFFIX = ("boot", "recovery", "ht", "jlinkbin", "sensorhub", "fct",
                  "coretest", "ota", "debug")


def list_projects():
    cfg_root = os.path.join(WORKSPACE, "configs")
    families = []
    products = {}
    if os.path.isdir(cfg_root):
        for fam in sorted(os.listdir(cfg_root)):
            fdir = os.path.join(cfg_root, fam)
            if not os.path.isdir(fdir):
                continue
            defs = [f for f in os.listdir(fdir) if f.endswith("_defconfig")]
            if not defs:
                continue
            prods = set()
            for d in defs:
                stem = d[:-len("_defconfig")]
                if not stem.startswith(fam + "_"):
                    continue
                rest = stem[len(fam) + 1:]
                parts = rest.split("_")
                # 去掉尾部变体后缀
                while len(parts) > 1 and parts[-1] in VARIANT_SUFFIX:
                    parts.pop()
                if len(parts) >= 2 and parts[0] in VARIANT_SUFFIX:
                    parts.pop(0)
                prod = "_".join(parts)
                if prod and prod not in VARIANT_SUFFIX:
                    prods.add(prod)
            if prods:
                families.append(fam)
                products[fam] = sorted(prods)
    state = json_read(os.path.join(WORKSPACE, "build", ".active-build-state.json"), {}) or {}
    return {
        "families": families,
        "products": products,
        "active": {
            "family": state.get("family", ""),
            "product": state.get("project", ""),
            "threads": state.get("threads", "24"),
        },
    }


# --------------------------------------------------------------------------
# Gerrit
# --------------------------------------------------------------------------
def gerrit_script():
    """优先用工作台自带的 gerrit_dl.py。
    工作区里的 tools/gerrit_dl.py 可能是旧副本（不同工作区版本不一致），
    会导致 precheck / download 行为对不上，所以自带脚本优先。"""
    here = os.path.dirname(os.path.abspath(__file__))
    cands = [
        os.path.join(here, "gerrit_dl.py"),
        os.path.normpath(os.path.join(here, "..", "gerrit_dl.py")),
        os.path.join(WORKSPACE, "tools", "gerrit_dl.py"),
    ]
    for p in cands:
        if os.path.isfile(p):
            return p
    return None


def run_gerrit(subcmd, url, timeout=300, extra=None):
    script = gerrit_script()
    if not script:
        return {"status": "error", "message": "找不到 tools/gerrit_dl.py"}
    py = which("python3") or sys.executable
    cmd = [py, script, subcmd, "--url", url]
    if extra:
        cmd.extend(extra)
    try:
        r = subprocess.run(cmd, cwd=_safe_cwd(WORKSPACE), stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=timeout,
                           universal_newlines=True, errors="replace")
    except subprocess.TimeoutExpired:
        return {"status": "error", "message": "gerrit_dl.py 超时（%ss）" % timeout}
    except Exception as exc:
        return {"status": "error",
                "message": "跑不起 gerrit_dl.py（%s）：%s" % (py, exc),
                "cmd": cmd}
    out = (r.stdout or "").strip()
    err = (r.stderr or "").strip()
    # 脚本可能打印多段 JSON，取最后一个能解析的
    for blob in reversed(_split_json_blobs(out)):
        try:
            return json.loads(blob)
        except Exception:
            continue
    return {"status": "error",
            "message": "无法解析 gerrit_dl.py 输出",
            "stdout": out[-4000:], "stderr": err[-2000:]}


def _split_json_blobs(text):
    """把可能混在一起的多个 JSON 对象切开。"""
    if not text:
        return []
    blobs, depth, start = [], 0, None
    in_str = False
    esc = False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                blobs.append(text[start:i + 1])
                start = None
    return blobs


# --------------------------------------------------------------------------
# 冲突分析
# --------------------------------------------------------------------------
_CONFLICT_MARK = re.compile(r"^<{7}|^={7}$|^>{7}", re.M)


def analyze_conflicts(changes):
    """对 gerrit_dl.py 报出的冲突做上下文分析，给出解决建议。"""
    reports = []
    for ch in changes or []:
        if ch.get("state") != "conflict":
            continue
        repo = ch.get("repo")
        files = ch.get("conflicted_files") or []
        entry = {
            "repo": repo,
            "repo_rel": safe_rel(repo) if repo else "",
            "change": "#%s %s" % (ch.get("number"), ch.get("subject")),
            "project": ch.get("project"),
            "files": [],
            "advice": [],
        }
        for f in files:
            fp = os.path.join(repo, f) if repo else f
            info = {"file": f, "abs": fp, "regions": 0,
                    "theirs": [], "ours": [], "exists": os.path.isfile(fp)}
            if info["exists"]:
                txt = _read_text(fp, 800000)
                info["regions"] = len(re.findall(r"^<{7}", txt, re.M))
                # 提取冲突区块两侧的符号
                for m in re.finditer(
                        r"^<{7}[^\n]*\n(.*?)^={7}\n(.*?)^>{7}", txt, re.M | re.S):
                    for side, key in ((m.group(1), "ours"), (m.group(2), "theirs")):
                        for sym in re.findall(
                                r"\b([A-Za-z_]\w{3,})\s*\(", side):
                            if sym not in info[key]:
                                info[key].append(sym)
                info["ours"] = info["ours"][:8]
                info["theirs"] = info["theirs"][:8]
                # 冲突文件的同名 symbol 在仓库其它位置的引用 -> 影响面
                info["hotspots"] = _grep_symbols(repo, info["ours"] + info["theirs"])
            entry["files"].append(info)
        entry["advice"] = _conflict_advice(entry)
        reports.append(entry)
    return reports


def analyze_gapp_390_adaptation(changes, workspace):
    """下载完成后检查 480 gapp/style 是否有对应的 390 适配改动。

    只检查本次 topic 返回的文件清单；不把工作区里历史文件误算成这次修改。
    390 的目标规范固定为 390x450，style 与 gapp 都必须有对应改动。
    """
    files = []
    for change in changes or []:
        for item in (change.get("files") or []):
            if isinstance(item, dict):
                item = item.get("rel") or item.get("path") or ""
            if item:
                files.append(str(item).replace("\\", "/").lstrip("./"))
        for item in (change.get("conflicted_files") or []):
            if item:
                files.append(str(item).replace("\\", "/").lstrip("./"))
    files = sorted(set(files))
    low = [(p, p.lower()) for p in files]
    source = [p for p, x in low if ("480" in x or "480x480" in x)
              and (x.endswith(".gapp") or x.endswith(".style"))]
    changed = set(files)

    def counterpart(path):
        p = re.sub(r"480x480", "390x450", path, flags=re.I)
        p = re.sub(r"480", "390", p, flags=re.I)
        return p

    items = []
    for p in source:
        target = counterpart(p)
        kind = "gapp" if p.lower().endswith(".gapp") else "style"
        exists_in_topic = target in changed
        local_exists = bool(workspace and os.path.isfile(os.path.join(workspace, target)))
        items.append({
            "source_480": p, "target_390": target, "kind": kind,
            "changed_390": exists_in_topic, "local_390": local_exists,
            "status": "ok" if exists_in_topic else "adapt_required",
            "rule": "390×450 + 390 style" if not exists_in_topic else "已包含 390×450 适配",
        })
    return {
        "source_files": source,
        "items": items,
        "checked": bool(source),
        "adapt_required": sum(1 for x in items if x["status"] == "adapt_required"),
        "target_resolution": "390×450",
        "style_rule": "使用 390×450-mdpi 对应 style，不直接复用 480 style",
    }


def _grep_symbols(repo, symbols, limit=6):
    if not repo or not symbols:
        return []
    hits = []
    pat = re.compile(r"\b(%s)\b" % "|".join(re.escape(s) for s in symbols[:6]))
    for root, dirs, files in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]
        for fn in files:
            if not fn.endswith((".c", ".cc", ".cpp", ".h", ".hpp")):
                continue
            p = os.path.join(root, fn)
            try:
                with io.open(p, "r", encoding="utf-8", errors="replace") as f:
                    for i, line in enumerate(f, 1):
                        if pat.search(line):
                            hits.append({"file": safe_rel(p, repo), "line": i,
                                         "text": line.strip()[:120]})
                            break
            except Exception:
                continue
            if len(hits) >= limit:
                return hits
    return hits


def _conflict_advice(entry):
    advice = []
    files = entry["files"]
    if not files:
        return ["没有冲突文件，可能冲突已被清理"]
    advice.append("在下述仓库里解决冲突，然后 `git add` + `git cherry-pick --continue`：")
    advice.append("  cd %s && git status" % (entry["repo_rel"] or entry["repo"]))
    for f in files:
        if f["regions"]:
            both = []
            for sym in (f["ours"] + f["theirs"]):
                if sym not in both:
                    both.append(sym)
            advice.append("• %s 有 %d 处冲突块，双方都改到：%s"
                          % (f["file"], f["regions"],
                             "、".join(both[:5]) or "（非符号级改动）"))
        else:
            advice.append("• %s 冲突（文件可能已被删除/改名，检查 git status）" % f["file"])
    if any(f.get("hotspots") for f in files):
        advice.append("以下位置引用了冲突符号，改完要一起回归：")
        for f in files:
            for h in (f.get("hotspots") or [])[:4]:
                advice.append("  - %s:%s  %s" % (h["file"], h["line"], h["text"]))
    advice.append("冲突策略建议：以当前分支（ours）为基线，把 change 里新增的逻辑"
                  "以最小改动叠加；接口签名冲突优先保留本分支版本，再适配调用点。")
    return advice


# --------------------------------------------------------------------------
# 代码评估主流程
# --------------------------------------------------------------------------
_STOPWORDS = set("""the and for with from this that function feature support
add add new update fix use using when then than into out not are was were
will can may should must have has had been being also only such more most
some any all each other same very just about above after again""".split())


def tokenize_feature(text):
    toks = set()
    for t in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", text or ""):
        lt = t.lower()
        if lt in _STOPWORDS:
            continue
        toks.add(lt)
    return toks


def _git_has_object(repo, sha):
    if not (repo and sha):
        return False
    try:
        r = subprocess.run(["git", "-C", repo, "cat-file", "-e", sha + "^{commit}"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=15)
        return r.returncode == 0
    except Exception:
        return False


def _git_remote(repo):
    try:
        r = subprocess.run(["git", "-C", repo, "remote"],
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           universal_newlines=True, timeout=15)
        names = [x.strip() for x in (r.stdout or "").splitlines() if x.strip()]
        if "origin" in names:
            return "origin"
        return names[0] if names else "origin"
    except Exception:
        return "origin"


def ensure_change_objects(changes, timeout=180):
    """把 topic 各 change 的 commit 拉进本地对象库。

    `gerrit_dl.py plan` 不做任何 git 操作，所以补丁一开始并不在本地；
    不 fetch 就只能 review 本地旧版本，比对结论会失真。
    只 fetch 对象、不 checkout、不 cherry-pick，工作区不受影响。
    """
    groups = {}
    for c in changes:
        repo = c.get("repo") or ""
        sha = c.get("sha") or ""
        ref = c.get("ref") or ""
        if not (repo and sha and ref and os.path.isdir(repo)):
            continue
        if _git_has_object(repo, sha):
            continue
        groups.setdefault(repo, []).append(c)

    fetched, failed = [], []
    for repo, cs in groups.items():
        remote = _git_remote(repo)
        args = ["git", "-C", repo, "fetch", "--no-tags", remote] + [c["ref"] for c in cs]
        err = ""
        try:
            r = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               universal_newlines=True, errors="replace",
                               timeout=timeout)
            err = (r.stderr or "").strip()
        except Exception as exc:
            err = str(exc)
        for c in cs:
            if _git_has_object(repo, c["sha"]):
                fetched.append(c.get("number"))
            else:
                failed.append({"number": c.get("number"),
                               "repo": safe_rel(repo),
                               "error": (err or "fetch 后仍找不到该 commit")[:220]})
    return {"fetched": fetched, "failed": failed, "repos": list(groups.keys())}


def local_eval_root(explicit=None):
    """代码评估的「本地基准」目录。

    优先级：显式传入（移植目录 / 前端选择的目录）→ 代码基线目录 →
    上次移植目录 → 服务启动的工作区。评估必须对着这棵本地代码比，
    不能退回到 gerrit_dl 的默认扫描根（那是另一份 checkout，等于拿别的树比）。
    """
    st = load_state()
    cands = [explicit,
             (st.get("baseline") or {}).get("path"),
             (st.get("port") or {}).get("path"),
             WORKSPACE]
    for c in cands:
        if not c:
            continue
        p = os.path.abspath(c)
        if os.path.isdir(p):
            return p
    return WORKSPACE


def resolve_change_files(topic_url, workspace_hint=None):
    """用 gerrit plan 拿 change 清单；先补 fetch，再 git diff 拿改动文件。

    workspace_hint 决定「本地」是哪棵树 —— 必须和评估的本地基准一致，
    否则 plan 会在别的 checkout 里定位仓库，比对结论就串树了。
    """
    extra = []
    if workspace_hint and os.path.isdir(workspace_hint):
        extra = ["--workspace", workspace_hint]
    plan = run_gerrit("plan", topic_url, timeout=180, extra=extra)
    if plan.get("status") != "ok":
        return {"ok": False, "plan": plan, "changes": [], "fetch": None}
    changes = plan.get("changes") or []
    fetch = ensure_change_objects(changes)
    out = []
    for c in changes:
        repo = c.get("repo")
        item = dict(c)
        item["files"] = []
        item["loc"] = 0
        if repo and os.path.isdir(repo) and c.get("sha"):
            files, loc, err = _diff_files(repo, c["sha"])
            item["files"] = files
            item["loc"] = loc
            if err:
                item["note"] = (item.get("note") or "") + " " + err
        out.append(item)
    return {"ok": True, "plan": plan, "changes": out, "fetch": fetch}


def _diff_files(repo, sha):
    """git diff --stat <sha>^ <sha>，拿文件列表与增删行数。"""
    def g(*args):
        try:
            r = subprocess.run(["git", "-C", repo] + list(args),
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               universal_newlines=True, errors="replace",
                               timeout=60)
            return r.returncode, r.stdout, r.stderr
        except Exception as e:
            return 1, "", str(e)

    rc, out, err = g("show", "--numstat", "--format=", sha)
    if rc != 0:
        for args in (("diff-tree", "--no-commit-id", "--numstat", "-r", sha),
                     ("log", "-1", "--numstat", "--format=", sha)):
            rc, out, err = g(*args)
            if rc == 0:
                break
    if rc != 0:
        return [], 0, "读取改动文件失败: %s" % (err.strip()[:160])
    files, loc = [], 0
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        add, dele, path = parts[0], parts[1], parts[2]
        try:
            loc += int(add) + int(dele)
        except ValueError:
            pass
        files.append({"path": os.path.join(repo, path),
                      "rel": path,
                      "module": module_of(os.path.join(repo, path)),
                      "add": add, "del": dele})
    return files, loc, ""


# ==========================================================================
# review：topic 代码 vs 本地代码，只输出「本地没有的接口」
# ==========================================================================
_C_NOISE = set("""
if for while switch return sizeof do else goto break continue case default
defined offsetof typeof static_assert assert va_arg va_start va_end va_list
memset memcpy memmove memcmp strlen strcmp strncmp strcpy strncpy strstr strchr
strrchr snprintf sprintf printf fprintf vprintf puts putchar malloc free calloc
realloc abort exit getchar
""".split())

_CALL_NAME_RE = re.compile(r'\b([A-Za-z_]\w{2,})\s*\(')
_MACRO_USE_RE = re.compile(r'\b([A-Z][A-Z0-9_]{2,})\b')
_TYPE_USE_RE = re.compile(r'\b((?:[A-Z][A-Za-z0-9]*_t)|(?:H_[A-Z]\w*))\b')
_DEFINE_LINE_RE = re.compile(r'^\s*#\s*define\s+([A-Za-z_]\w*)')
_TOP_DEF_RE = re.compile(
    r'^[A-Za-z_][\w\s\*]*?([A-Za-z_]\w{2,})\s*\([^;]*\)\s*(?:const\s*)?\{?\s*$')
_TYPEDEF_NAME_RE = re.compile(r'^typedef\b[^;{}]*?\b([A-Za-z_]\w*)\s*;')


def _git_show_patch(repo, sha, timeout=60):
    """拿一个 change 的补丁文本。返回 (patch, error)。"""
    if not (repo and sha and os.path.isdir(repo)):
        return "", "缺少仓库路径或 sha（gerrit 未定位到本地仓库）"

    def run(*args):
        try:
            r = subprocess.run(["git", "-C", repo] + list(args),
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               universal_newlines=True, errors="replace",
                               timeout=timeout)
            return r.returncode, r.stdout or "", r.stderr or ""
        except Exception as exc:
            return 1, "", str(exc)

    last = ""
    for args in (("show", "--format=", "--unified=0", sha),
                 ("diff", "--unified=0", sha + "^", sha),
                 ("show", "--format=", sha)):
        rc, out, err = run(*args)
        if rc == 0 and out.strip():
            return out, ""
        last = (err or "").strip()
    return "", (last[:180] or "读取补丁失败")


def _patch_added_by_file(patch):
    """按文件切开补丁，只留新增行（+ 行）。"""
    cur = ""
    out = {}
    for ln in patch.splitlines():
        if ln.startswith("+++ "):
            p = ln[4:].strip()
            if p.startswith("b/"):
                p = p[2:]
            cur = p
            out.setdefault(cur, [])
            continue
        if ln.startswith("--- ") or ln.startswith("diff --git") or ln.startswith("index "):
            continue
        if ln.startswith("+") and cur:
            out[cur].append(ln[1:])
    return out


def _self_defined_syms(lines):
    """新增行里「自己定义」的函数 / 宏 / 类型，这些不算缺失依赖。"""
    out = set()
    for ln in lines:
        m = _DEFINE_LINE_RE.match(ln)
        if m:
            out.add(m.group(1))
            continue
        if not ln or ln[:1] in (" ", "\t", "#", "/", "*", "}"):
            continue
        m = _TOP_DEF_RE.match(ln)
        if m:
            out.add(m.group(1))
            continue
        m = _TYPEDEF_NAME_RE.match(ln)
        if m:
            out.add(m.group(1))
    return out


_SRC_EXTS = (".c", ".cc", ".cpp", ".cxx", ".h", ".hpp", ".hh", ".hxx",
             ".inc", ".ipp", ".inl", ".m", ".mm", ".S", ".s", ".asm")
_SRC_PATHSPEC = ["*" + e for e in _SRC_EXTS]

_REPO_FILES_CACHE = {}


def _repo_has_files(repo, timeout=30):
    """工作区里真的有源码文件吗？

    repo 半 sync（对象在 .repo/projects、工作区没 checkout）时，`git grep`
    会**静默返回无命中**，于是整库符号都被判成「本地没有」——
    这是「接口明明存在却报缺失」的根源之一，必须单独识别。
    """
    if repo in _REPO_FILES_CACHE:
        return _REPO_FILES_CACHE[repo]
    ok = False
    entry = False
    try:
        for name in os.listdir(repo):
            if name in (".git", ".repo"):
                continue
            entry = True
            break
    except Exception:
        entry = False
    if entry:
        try:
            r = subprocess.run(["git", "-C", repo, "ls-files", "-z"],
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               timeout=timeout)
            ok = bool((r.stdout or b"").strip(b"\0"))
        except Exception:
            ok = False
    _REPO_FILES_CACHE[repo] = ok
    return ok


_HIT_LINE_RE = re.compile(r"^(.*?):(\d+):(.+)$")


def _hit_kind(text):
    """命中行的形态：宏定义 / 声明 / 定义 / 引用。用于给「本地已有」出证据。"""
    t = (text or "").strip()
    if not t:
        return "引用"
    if re.match(r"^#\s*define\b", t):
        return "宏定义"
    if re.search(r"\b(?:struct|union|enum)\b", t) or re.search(r"\btypedef\b", t):
        return "类型定义"
    if t.endswith(";") and "(" in t:
        return "声明"
    if "(" in t and "{" in t:
        return "定义"
    if t.endswith(";") and "=" in t:
        return "定义"
    return "引用"


def _add_hit(out, name, path, line, text=""):
    path = path.replace("\\", "/")
    cur = out.get(name)
    # 头文件里的声明/定义比别处的引用更有说服力，优先留它
    rank_new = 0 if path.endswith((".h", ".hpp", ".hh", ".hxx")) else 1
    # 对宏来说，普通引用不能证明宏已经定义；后续若扫描到真正的
    # #define，必须用定义证据覆盖之前的引用证据。
    is_define = bool(re.match(r"^\s*#\s*define\b", text or ""))
    old_is_define = bool(cur and cur.get("kind") == "宏定义")
    if cur is None or (is_define and not old_is_define) or \
            (not old_is_define and rank_new < cur["_rank"]):
        out[name] = {"file": path, "line": line, "text": (text or "").strip()[:160],
                     "kind": _hit_kind(text), "_rank": rank_new}


def _git_grep_hits(repo, names, timeout=90, batch=200):
    """在**本地工作区**里查这些符号，定义 / 声明 / 调用都算命中。

    比对基准永远是本地代码，不是线上：
      * 命令里不带任何 revision（不查 origin/xxx、不查 change 的 sha），
        `git grep` 默认读的就是工作区文件，本地改到哪就算到哪；
      * 源码全类型都扫（.c/.cc/.h/…），不再只看头文件 ——
        头文件里有定义/声明，或者本地有人调用过这个名字，都说明接口在本地是存在的；
      * --untracked：本地新加、还没 git add 的文件同样算本地代码。

    返回 {符号: {file, line, kind}}；None 表示这个仓库不可用
    （没 checkout / 不是 git 仓库 / git 在 9p 上不可用）。
    """
    if not names:
        return {}
    if not _repo_has_files(repo):
        return None
    out = {}
    usable = False
    for i in range(0, len(names), batch):
        chunk = names[i:i + batch]
        chunk_re = re.compile(r"\b(?:%s)\b" % "|".join(re.escape(n) for n in chunk))
        args = ["git", "-C", repo, "grep", "-n", "-w", "-I",
                "--no-color", "--untracked", "--no-recurse-submodules",
                "--full-name"]
        for n in chunk:
            args += ["-e", n]
        args += ["--"] + _SRC_PATHSPEC
        try:
            r = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               universal_newlines=True, errors="replace",
                               timeout=timeout)
        except Exception:
            return None if not usable else out
        if r.returncode not in (0, 1):
            return None if not usable else out
        usable = True
        for ln in (r.stdout or "").splitlines():
            m = _HIT_LINE_RE.match(ln.strip())
            if not m:
                continue
            path, line_no, text = m.group(1), int(m.group(2)), m.group(3)
            if not os.path.isabs(path):
                path = os.path.join(repo, path)
            for mm in chunk_re.finditer(text):
                name = mm.group(0)
                if name not in out:
                    _add_hit(out, name, path, line_no, text)
    return out if usable else None


def _walk_words(roots, names, max_files=80000, budget=20.0):
    """grep 系列都不可用时的兜底：直接按源码文件逐行扫词。

    返回 ({符号: {file, line, kind}}, 是否扫完)。
    """
    if not names:
        return {}, True
    name_re = re.compile(r"\b(?:%s)\b" % "|".join(re.escape(n) for n in names))
    hit = {}
    t0 = time.time()
    scanned = 0
    for root_dir in roots:
        if not os.path.isdir(root_dir):
            continue
        for root, dirs, files in os.walk(root_dir):
            dirs[:] = [d for d in dirs
                       if d not in _SKIP_DIRS and not d.startswith(".")]
            for fn in files:
                if not fn.endswith(_SRC_EXTS):
                    continue
                scanned += 1
                if scanned > max_files or time.time() - t0 > budget:
                    return hit, False
                p = os.path.join(root, fn)
                try:
                    txt = io.open(p, "r", encoding="utf-8",
                                  errors="ignore").read(400000)
                except Exception:
                    continue
                if not txt:
                    continue
                for i, ln in enumerate(txt.splitlines(), 1):
                    if not name_re.search(ln):
                        continue
                    for m in name_re.finditer(ln):
                        n = m.group(0)
                        if n not in hit:
                            _add_hit(hit, n, p, i, ln)
    return hit, True


def _rg_hits(roots, names, budget=25.0):
    """用 ripgrep 把整棵本地工作区兜底扫一遍（比 python 遍历快一个数量级）。

    返回 ({符号: {file, line, kind}}, 是否扫完)；None 表示 rg 不可用 / 出错。
    """
    rg = which("rg") or which("ripgrep")
    if not rg or not names or not roots:
        return None
    pat = r"\b(?:%s)\b" % "|".join(re.escape(n) for n in names)
    args = [rg, "--no-messages", "--with-filename", "-n", "-w", "-I",
            "--no-ignore-vcs", "-e", pat]
    for e in _SRC_EXTS:
        args += ["-g", "*" + e]
    for e in _SKIP_DIRS:
        args += ["-g", "!" + e + "/**"]
    args += list(roots)
    complete = True
    try:
        r = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           universal_newlines=True, errors="replace",
                           timeout=budget)
        if r.returncode not in (0, 1):
            return None
        text = r.stdout or ""
    except subprocess.TimeoutExpired as exc:
        complete = False
        text = exc.stdout or ""
        if isinstance(text, bytes):
            text = text.decode("utf-8", "replace")
    except Exception:
        return None
    hit = {}
    name_re = re.compile(pat)
    for ln in text.splitlines():
        m = _HIT_LINE_RE.match(ln.strip())
        if not m:
            continue
        p, line_no, body = m.group(1), int(m.group(2)), m.group(3)
        for mm in name_re.finditer(body):
            n = mm.group(0)
            if n not in hit:
                _add_hit(hit, n, p, line_no, body)
    return hit, complete


def _repos_under(root, max_repos=400):
    """本地工作区里有哪些 git 仓库（优先读 .repo/project.list，其次找 .git）。"""
    out = []
    pl = os.path.join(root, ".repo", "project.list")
    if os.path.isfile(pl):
        try:
            for ln in io.open(pl, "r", encoding="utf-8", errors="replace"):
                p = (ln or "").strip()
                if not p:
                    continue
                d = os.path.join(root, p)
                if os.path.isdir(d):
                    out.append(d)
                if len(out) >= max_repos:
                    break
        except Exception:
            out = []
        if out:
            return out
    try:
        for dirpath, dirs, files in os.walk(root):
            has_git = (".git" in dirs) or (".git" in files)
            dirs[:] = [d for d in dirs
                       if d not in _SKIP_DIRS and not d.startswith(".")]
            if has_git:
                out.append(dirpath)
                if len(out) >= max_repos:
                    break
    except Exception:
        pass
    return out


def _deep_hits(root, names, budget=25.0):
    """在整棵**本地**工作区里找这些符号（跨仓库兜底）。

    顺序：rg 单遍（最快）→ 各仓库并行 git grep → 实在不行再文件扫描。
    返回 (命中表, 方式说明, 是否扫完)。
    """
    if not names:
        return {}, "", True
    t0 = time.time()
    got = _rg_hits([root], names, budget=budget)
    if got is not None:
        how = "本地整库 rg" if got[1] else "本地整库 rg(超时截断)"
        return got[0], how, got[1]

    hit = {}
    pending = set(names)
    repos = _repos_under(root)
    dead = []
    complete = True
    if repos:
        pool = futures.ThreadPoolExecutor(max_workers=8)
        try:
            futs = {pool.submit(_git_grep_hits, r, sorted(pending), 20): r
                    for r in repos}
            for fu in futures.as_completed(futs):
                if time.time() - t0 > budget:
                    complete = False
                    break
                try:
                    r = fu.result()
                except Exception:
                    r = None
                repo = futs[fu]
                if r is None:
                    dead.append(repo)
                    continue
                for k, v in r.items():
                    hit.setdefault(k, v)
                pending -= set(r)
                if not pending:
                    break
        finally:
            # Python 3.8 的 ThreadPoolExecutor 没有 cancel_futures 参数。
            # 不等待已提交任务，兼容旧版 WSL Python；任务完成后线程自行退出。
            pool.shutdown(wait=False)
    # git 用不了的仓库（半 sync / 9p）以及仓库外的文件：整棵树扫一遍
    if pending and (dead or not repos) and time.time() - t0 < budget:
        h, done = _walk_words([root], sorted(pending),
                              budget=max(3.0, budget - (time.time() - t0)))
        complete = complete and done
        for k, v in h.items():
            hit.setdefault(k, v)
        pending -= set(h)
    how = ("本地各仓库 git grep" if repos else "本地文件扫描") + \
        ("" if complete else "(超时截断)")
    return hit, how, complete


def _local_declared(repos, names, deep=True, root=None, budget=25.0):
    """这些符号在本地代码里出现过吗？出现 = 存在，不输出。

    比对基准是**本地工作区**（root，默认当前 WORKSPACE），绝不查远端 revision：
      1. 先在被改动仓库里 `git grep`（读工作区文件，本地改到哪算到哪）；
         仓库用不了（半 sync / 9p / 非 git）就退化成直接扫目录文件；
      2. 还没命中的，在整棵本地工作区兜底扫一遍 ——
         头文件里有定义/声明、或被别的模块调用过，都证明接口在本地存在。
    返回 ({符号: 证据}, 判定方式, 检索范围信息)。
    """
    if not names:
        return {}, "", {"repos": [], "deep": "", "complete": True}
    hit = {}
    pending = set(names)
    used = []
    searched = []
    complete = True
    # 大型 repo 工程通常有数百个子仓库。逐仓库启动 git grep 会产生数百次
    # 进程启动和索引扫描，代码评估的主要耗时就在这里；整棵树用 rg 一次扫描
    # 更快，而且能覆盖跨仓库依赖。只有 rg 不可用/超时后才走旧的逐仓库兜底。
    if deep and root and os.path.isdir(root):
        h, deep_how, done = _deep_hits(root, sorted(pending), budget=budget)
        for k, v in h.items():
            hit.setdefault(k, v)
        pending -= set(h)
        complete = complete and done
        # rg/git grep 可能因工程文件类型、挂载目录或索引状态漏掉少量结果。
        # 对剩余符号再做一次直接源码扫描，宁可多花一点时间，也不要把
        # 本地已经出现过的接口报成缺失。
        if pending and done:
            extra, extra_done = _walk_words([root], sorted(pending), budget=min(12.0, budget))
            for k, v in extra.items():
                hit.setdefault(k, v)
            pending -= set(extra)
            complete = complete and extra_done
        return hit, deep_how or "本地整库扫描", {
            "repos": [], "deep": deep_how, "complete": complete,
            "root": root or WORKSPACE,
        }

    for repo in repos:
        if not os.path.isdir(repo):
            continue
        got = _git_grep_hits(repo, sorted(pending))
        if got is None:
            # 这个仓库没法用 git grep（半 sync / 9p / 非 git）→ 直接扫文件
            got, done = _walk_words([repo], sorted(pending), budget=budget)
            used.append("文件扫描")
            complete = complete and done
        else:
            used.append("git grep")
        searched.append(repo)
        for k, v in got.items():
            if k not in hit:
                hit[k] = v
        pending -= set(got)
        if not pending:
            break
    deep_how = ""
    if pending and deep:
        base = root if (root and os.path.isdir(root)) else WORKSPACE
        h, deep_how, done = _deep_hits(base, sorted(pending), budget=budget)
        for k, v in h.items():
            hit.setdefault(k, v)
        pending -= set(h)
        complete = complete and done
    how = " + ".join(list(dict.fromkeys(used + ([deep_how] if deep_how else [])))) or "未检索"
    return hit, how, {"repos": searched, "deep": deep_how, "complete": complete,
                      "root": root or WORKSPACE}


def _git_has_header(repo, base, timeout=20):
    """仓库里有没有这个 basename 的头文件（兜底 header_index 未覆盖的情况）。"""
    try:
        r = subprocess.run(["git", "-C", repo, "ls-files", "--", "*/" + base, base],
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           universal_newlines=True, timeout=timeout)
        return bool((r.stdout or "").strip())
    except Exception:
        return False


# ==========================================================================
# topic 代码快照：先把 topic 的代码整棵树导出到备份目录，再拿它和本地比
# ==========================================================================
SNAP_ROOT = os.path.join(DATA_DIR, "topic-src")


def _snap_slug(topic_url):
    """topic 链接 -> 稳定的目录名（同一 topic 反复评估复用同一份快照）。"""
    raw = (topic_url or "manual").strip()
    s = raw
    m = re.search(r"topic[:=]\s*(.+)$", raw)
    if m:
        # /q/topic:%22hyrox%E6%A0%A1...%22 这种：先去掉引号再 URL 解码
        s = m.group(1).replace("%22", "").replace('"', "").replace("&", " ")
        try:
            s = unquote(s)
        except Exception:
            pass
    else:
        m = re.search(r"/\+/(\d+)", raw)
        if m:
            s = "change-" + m.group(1)
    s = re.sub(r"[^A-Za-z0-9_.@-]+", "-", s).strip("-._")
    s = re.sub(r"-{2,}", "-", s)
    if not s:
        s = "topic"
    tag = hashlib.md5(raw.encode("utf-8")).hexdigest()[:6]
    if len(s) > 40:
        s = s[:40].rstrip("-._")
    # 纯中文 / 冲突风险高的名字补个短哈希，避免两个 topic 撞到同一目录
    if not re.search(r"[A-Za-z]", s) or len(s) < 6:
        s = s + "-" + tag
    return s


def _snap_rel(repo, root):
    """仓库在快照里的相对目录名。"""
    for base in (root, WORKSPACE):
        if not base:
            continue
        try:
            rel = os.path.relpath(repo, base)
        except Exception:
            continue
        if not rel.startswith("..") and not os.path.isabs(rel):
            return rel.replace("\\", "/")
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", os.path.basename(repo.rstrip("/\\")) or "repo")


def _export_tree(repo, sha, dest):
    """把 commit 的完整文件树解到 dest（git archive，不碰工作区）。返回 (ok, files, err)。"""
    if not os.path.isdir(dest):
        os.makedirs(dest)
    tmp = dest + ".zip"
    try:
        r = subprocess.run(["git", "-C", repo, "archive", "--format=zip",
                            "-o", tmp, sha],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           universal_newlines=True, errors="replace", timeout=600)
        if r.returncode != 0:
            return False, 0, ((r.stderr or r.stdout or "").strip())[:300]
        n = 0
        with zipfile.ZipFile(tmp) as zf:
            zf.extractall(dest)
            n = len(zf.namelist())
        return True, n, ""
    except Exception as exc:
        return False, 0, str(exc)[:300]
    finally:
        try:
            os.remove(tmp)
        except Exception:
            pass


def _prune_snapshots(keep=3):
    try:
        items = [os.path.join(SNAP_ROOT, d) for d in os.listdir(SNAP_ROOT)]
        items = [p for p in items if os.path.isdir(p)]
        items.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        for p in items[keep:]:
            shutil.rmtree(p, ignore_errors=True)
    except Exception:
        pass


def build_topic_snapshot(changes, root, topic_url="", force=False):
    """把 topic 各 change 的代码整棵导出到 <DATA>/topic-src/<topic>/。

    这是「备份目录」：不动本地工作区，只从本地对象库里 `git archive` 出来。
    评估就对着这份副本读 topic 代码，比逐行拼补丁准（能看到完整的文件上下文）。
    同一 topic + 同一批 sha 会复用上次的结果，重跑评估很快。
    """
    dest = os.path.join(SNAP_ROOT, _snap_slug(topic_url))
    usable = [c for c in changes if c.get("repo") and c.get("sha")
              and os.path.isdir(c["repo"])]
    shas = sorted({c["sha"] for c in usable})
    man_path = os.path.join(dest, "_snapshot.json")
    old = json_read(man_path, None)
    if not force and isinstance(old, dict) and old.get("shas") == shas and old.get("ok"):
        if all(os.path.isdir(r.get("dest") or "") for r in (old.get("repos") or [])):
            try:
                os.utime(dest, None)      # 刷新时间，避免正在用的快照被 prune 掉
            except OSError:
                pass
            old["reused"] = True
            old["dir"] = dest
            return old

    if os.path.isdir(dest):
        shutil.rmtree(dest, ignore_errors=True)
    try:
        os.makedirs(dest)
    except OSError:
        pass

    groups = {}
    order = []
    failed = []
    for c in changes:
        repo = c.get("repo") or ""
        if not (repo and c.get("sha")):
            failed.append({"number": c.get("number"),
                           "project": c.get("project"),
                           "error": "本地没定位到仓库，无法导出"})
            continue
        if repo not in groups:
            groups[repo] = []
            order.append(repo)
        groups[repo].append(c)

    out_repos, total = [], 0
    for repo in order:
        rel = _snap_rel(repo, root)
        item = {"repo": repo, "rel": rel, "dest": os.path.join(dest, rel),
                "changes": [], "files": [], "exported": 0, "ok": False, "error": ""}
        for c in groups[repo]:
            ok, n, err = _export_tree(repo, c["sha"], item["dest"])
            if not ok:
                item["error"] = (item["error"] + " #%s 导出失败: %s"
                                 % (c.get("number"), err)).strip()
                failed.append({"number": c.get("number"), "project": c.get("project"),
                               "error": err})
                continue
            item["changes"].append(c.get("number"))
            item["exported"] += n
            for f in (c.get("files") or []):
                item["files"].append({"file": f.get("rel") or "", "path": f.get("path") or "",
                                      "add": f.get("add"), "del": f.get("del"),
                                      "number": c.get("number")})
        item["ok"] = item["exported"] > 0
        total += len(item["files"])
        out_repos.append(item)

    manifest = {
        "ok": bool([r for r in out_repos if r["ok"]]),
        "status": "ok" if out_repos and not failed else ("partial" if out_repos else "error"),
        "dir": dest, "shas": shas, "at": now_str(), "topic": topic_url,
        "repos": out_repos, "failed": failed, "files": total,
        "reused": False,
    }
    try:
        with open(man_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(manifest, ensure_ascii=False, indent=2, default=str))
    except Exception:
        pass
    _prune_snapshots(3)
    return manifest


def _snapshot_index(snap):
    """change 号 -> (快照里该仓库的目录, [改动文件相对路径])"""
    idx = {}
    if not isinstance(snap, dict):
        return idx
    for r in snap.get("repos") or []:
        base = r.get("dest") or ""
        for f in r.get("files") or []:
            n = f.get("number")
            if n is None or not f.get("file"):
                continue
            idx.setdefault(n, (base, []))[1].append(f["file"])
    return idx


def review_topic_vs_local(changes, ws_root=None, topic_root=None, snap=None):
    """对比 topic 代码与**本地工作区**代码，只挑「本地没有的接口」。

    本地基准 = ws_root（移植目录 / 代码基线目录），不传才退回服务的工作区。
    topic 一侧只读每个 change 的 diff 新增行；绝不把 topic 快照里的完整文件
    当成修改内容。本地一侧永远读工作区文件，不查任何远端分支。
    """
    root = os.path.abspath(ws_root) if (ws_root and os.path.isdir(ws_root)) else WORKSPACE
    review = {
        "changes": 0, "files": 0, "patch_ok": 0, "patch_failed": 0,
        "symbols": 0, "local_hit": 0, "missing": 0, "headers_missing": 0,
        "new_api": 0, "source": "",
    }
    errors = []
    by_file = {}
    snapshot_used = 0
    repo_of = {}
    failed = []
    # 兜底：调用方可能没先 fetch（单独调用本函数时），补一次
    ensure_change_objects(changes)

    for c in changes:
        repo = c.get("repo") or ""
        sha = c.get("sha") or ""
        review["changes"] += 1

        # 优先使用 commit 快照中的完整改动文件，保留宏和文件级上下文。
        snap_base = next((r.get("dest") for r in (snap or {}).get("repos", [])
                          if r.get("repo") == repo), "")
        has_snapshot = False
        if snap_base:
            for f in c.get("files") or []:
                rel = f.get("rel") or f.get("path") or ""
                p = os.path.join(snap_base, rel)
                if not os.path.isfile(p):
                    continue
                try:
                    with io.open(p, "r", encoding="utf-8", errors="replace") as fh:
                        lines = fh.read().splitlines()
                except OSError:
                    continue
                if lines:
                    by_file.setdefault(rel, []).extend(lines)
                    repo_of[rel] = repo
                    snapshot_used += 1
                    has_snapshot = True

        if has_snapshot:
            review["patch_ok"] += 1
            continue

        # 快照不可用时退回 diff 新增行。
        patch, err = _git_show_patch(repo, sha)
        if not patch:
            failed.append({"number": c.get("number"),
                           "repo": c.get("repo_rel") or c.get("project") or "",
                           "error": err})
            continue
        review["patch_ok"] += 1
        for rel, lines in _patch_added_by_file(patch).items():
            if not lines:
                continue
            if rel in by_file:
                continue
            by_file.setdefault(rel, []).extend(lines)
            repo_of[rel] = repo or _repo_root_of(os.path.join(repo, rel) if repo else rel)

    review["review_source"] = ("topic 快照完整改动文件" if snapshot_used
                                else "topic diff 新增行")
    review["source"] = "snapshot" if snapshot_used else "patch"
    review["snapshot_files"] = snapshot_used

    review["files"] = len(by_file)
    review["patch_failed"] = len(failed)
    # 补丁里新增（自己带来）的头文件，不能算「本地缺」
    patch_added = {os.path.basename(rel) for rel in by_file}
    if not by_file:
        return {
            "review": review, "items": [], "missing_headers": [],
            "errors": ["没能读取任何 topic 代码（快照和补丁都读不到），无法做 topic ↔ 本地对比"] + errors,
            "failed": failed, "degraded": True, "note": "",
            "baseline": {"mode": "local-worktree", "root": root,
                         "workspace": rel_or_abs(root, root),
                         "repos": [], "deep": "", "complete": True},
            "local_root": root, "hit_sample": [], "repos_scanned": 0,
            "topic_root": (snap or {}).get("dir") or "", "snap_used": snapshot_used,
        }

    call_where, macro_where, type_where, inc_where = {}, {}, {}, {}
    self_syms = set()
    for rel, lines in by_file.items():
        self_syms |= _self_defined_syms(lines)
        for idx, ln in enumerate(lines, 1):
            m = _INCLUDE_RE.match(ln)
            if m:
                inc_where.setdefault(m.group(1), rel)
                continue
            code = ln.split("//")[0].split("/*")[0]
            if not code.strip():
                continue
            for m in _CALL_NAME_RE.finditer(code):
                n = m.group(1)
                if n in _NOT_FUNC or n in _C_NOISE:
                    continue
                call_where.setdefault(n, (rel, idx))
            for m in _MACRO_USE_RE.finditer(code):
                macro_where.setdefault(m.group(1), (rel, idx))
            for m in _TYPE_USE_RE.finditer(code):
                type_where.setdefault(m.group(1), (rel, idx))

    review["new_api"] = len([s for s in self_syms if not s.isupper()])

    # 优先级：函数 > 宏 > 类型
    cand = {}
    for bucket, kind in ((call_where, "函数"), (macro_where, "宏"), (type_where, "类型")):
        for n in bucket:
            if n in self_syms or n in cand:
                continue
            if len(n) < 3:
                continue
            cand[n] = kind

    names = sorted(cand)
    review["symbols"] = len(names)
    repos = sorted({r for r in repo_of.values() if r})
    hidx = header_index(root)
    # 把 topic 代码 include 到的本地头文件所在仓库也纳入搜索范围，
    # 否则跨仓库依赖（sport 调 framework/base）会被误判成「本地没有」
    extra = set()
    for inc, rel in inc_where.items():
        rp = repo_of.get(rel) or ""
        if not rp:
            continue
        hit = _resolve_include(inc, os.path.join(rp, rel), rp, hidx)
        if hit:
            rroot = _repo_root_of(hit)
            if os.path.isdir(os.path.join(rroot, ".git")):
                extra.add(rroot)
    repos = sorted(set(repos) | extra)
    if not repos:
        repos = [root]
    local_hit, how, scan = _local_declared(repos, names, root=root) if names else (
        {}, "", {"repos": [], "deep": "", "complete": True, "root": root})
    # topic 使用的宏必须在本地找到真正的 #define 才算已存在。
    # 普通源码引用同名字符串不能作为宏存在的证明，否则会漏报未定义宏。
    local_hit = {
        n: ev for n, ev in local_hit.items()
        if cand.get(n) != "宏" or ev.get("kind") == "宏定义"
    }
    review["local_hit"] = len([n for n in names if n in local_hit])

    # 同族前缀：提示「是不是改名了」
    def _family_hint(name):
        for n in local_hit:
            if n == name:
                continue
            k = min(12, len(name), len(n))
            if k >= 8 and n[:k] == name[:k]:
                return "本地有同族接口 %s()，可能已改名/合并" % n
        return ""

    items = []
    for n in names:
        if n in local_hit:
            continue
        rel, line = call_where.get(n) or macro_where.get(n) or type_where.get(n) or ("", 0)
        items.append({
            "name": n,
            "kind": cand[n],
            "used_in": rel,
            "line": line,
            "module": module_of(os.path.join(root, rel)) if rel else "",
            "hint": _family_hint(n),
        })
    # 扫描未完整结束时无法证明剩余符号真的不存在；不把不确定项上报为缺失。
    # 这样即使大型工作区扫描超时，也不会再次出现“本地已有却误报”的结果。
    if names and not scan.get("complete", True):
        items = []
    review["missing"] = len(items)

    # 「本地已有」的抽样证据：让人一眼看到比对的是哪棵本地代码，而不是线上
    hit_sample = []
    for n in sorted(local_hit):
        ev = local_hit[n]
        f = ev.get("file") or ""
        hit_sample.append({
            "name": n,
            "kind": ev.get("kind") or "引用",
            "file": rel_or_abs(f, root) if f else "",
            "full": f,
            "line": ev.get("line") or 0,
            "text": ev.get("text") or "",
        })
    hit_sample.sort(key=lambda x: (x["kind"] != "宏定义", x["file"]))
    review["headers_hit"] = len([h for h in hit_sample
                                 if h["file"].endswith((".h", ".hpp", ".hh", ".hxx"))])

    # topic 里 include 了、本地却找不到的头文件
    missing_headers = []
    for inc, rel in sorted(inc_where.items()):
        if os.path.basename(inc) in patch_added:
            continue          # topic 自己新增的头文件，不是本地缺口
        rp = repo_of.get(rel) or root
        if _resolve_include(inc, os.path.join(rp, rel), rp, hidx):
            continue
        if os.path.isdir(os.path.join(rp, ".git")) and _git_has_header(rp, os.path.basename(inc)):
            continue
        missing_headers.append({"name": inc, "used_in": rel})
    review["headers_missing"] = len(missing_headers)

    note = ""
    if failed:
        note = ("%d 个 change 的代码读不到（快照没导出 / 仓库未定位），"
                "这部分退回用本地当前文件估算" % len(failed))
    if names and not scan.get("complete", True):
        note = (note + " " if note else "") + \
            "本地整库兜底扫描超时截断，个别接口可能被误判为缺失，再点一次「开始评估」可重跑"
    baseline = {
        "mode": "local-worktree",
        "root": root,
        "workspace": rel_or_abs(root, root),
        "repos": [rel_or_abs(r, root) for r in (scan.get("repos") or [])][:20],
        "deep": scan.get("deep") or "",
        "complete": bool(scan.get("complete", True)),
    }
    return {
        "review": review, "items": items, "missing_headers": missing_headers,
        "errors": errors, "failed": failed, "degraded": bool(failed),
        "note": note, "how": how, "baseline": baseline,
        "local_root": root, "hit_sample": hit_sample[:12],
        "repos_scanned": len(scan.get("repos") or []),
        "topic_root": (snap or {}).get("dir") or "", "snap_used": snapshot_used,
    }


def code_eval(feature_name, topic_url, manual_paths, map_path=None, ws_root=None):
    """功能评估 - 代码评估总入口。

    ws_root：本地基准目录（前端选的移植目录 / 代码基线目录）。
    topic 代码只跟这棵本地树比，不跟任何远端分支比。
    """
    t0 = time.time()
    root = local_eval_root(ws_root)
    result = {
        "ok": True,
        "feature": feature_name,
        "at": now_str(),
        "local_root": root,
        "changes": [],
        "missing": None,
        "hardware": None,
        "storage": None,
        "effort": None,
        "errors": [],
        "elapsed_ms": 0,
    }
    cfg_path = os.path.join(root, "build", ".config")
    if not os.path.isfile(cfg_path):
        cfg_path = os.path.join(WORKSPACE, "build", ".config")

    # 1) 解析改动文件
    all_files, modules, loc = [], set(), 0
    plan_changes = []
    # 已知冲突数（上一轮移植留下的），会影响工期
    conflicts = len((load_state().get("port") or {}).get("conflicts") or [])
    if topic_url:
        r = resolve_change_files(topic_url, root)
        if not r["ok"]:
            result["errors"].append("Gerrit 查询失败：%s"
                                    % (r["plan"].get("message") or "未知错误"))
        # 选定的本地目录里一个仓库都定位不到（半 sync / 没 checkout）时，
        # 退一步用服务自己的工作区，但必须在结果里说清楚用的是哪棵树
        if r["ok"] and r["changes"] and not any(c.get("repo") for c in r["changes"]) \
                and os.path.abspath(WORKSPACE) != os.path.abspath(root):
            r2 = resolve_change_files(topic_url, WORKSPACE)
            if r2["ok"] and any(c.get("repo") for c in r2["changes"]):
                base_err = ("本地 %s 里没定位到 change 对应的仓库（没 sync / 没 checkout），"
                            "已降级改用 %s 比对 —— 结论不代表你选的那棵树"
                            % (root, WORKSPACE))
                result["errors"].append(base_err)
                r = r2
                root = os.path.abspath(WORKSPACE)
                result["local_root"] = root
                result["local_root_fallback"] = True
        plan_changes = r["changes"]
        for c in r["changes"]:
            result["changes"].append({
                "number": c.get("number"), "subject": c.get("subject"),
                "project": c.get("project"), "repo": c.get("repo") or "",
                "repo_rel": rel_or_abs(c.get("repo") or "", root) if c.get("repo") else "",
                "note": c.get("note") or "", "loc": c.get("loc", 0),
                "files": [f["rel"] for f in c.get("files", [])],
            })
            for f in c.get("files", []):
                all_files.append(f)
            loc += c.get("loc", 0)
            if c.get("repo"):
                modules.add(module_of(c["repo"]))
        if plan_changes and not any(c.get("repo") for c in plan_changes):
            result["errors"].append(
                "本地 %s 里定位不到任何 change 对应的仓库（可能没 sync / 没 checkout），"
                "先用「0. 代码基线」更新代码再评估" % root)

    # 1.5) 先把 topic commit 导出到本地备份目录。
    # 评估从备份中的完整改动文件读取上下文，工作区本身不被 checkout 改动。
    snap = None
    topic_root = ""
    if topic_url and plan_changes:
        snap = build_topic_snapshot(plan_changes, root, topic_url)
        topic_root = (snap or {}).get("dir") or ""
    if manual_paths:
        MAX_SCOPE = 300          # 手动圈目录时的文件上限，防止整模块扫描拖死接口
        for p in manual_paths:
            if not p:
                continue
            ap = p if os.path.isabs(p) else os.path.join(root, p)
            if not os.path.isdir(ap) and not os.path.isfile(ap):
                ap2 = p if os.path.isabs(p) else os.path.join(WORKSPACE, p)
                if os.path.isdir(ap2) or os.path.isfile(ap2):
                    ap = ap2
            if os.path.isdir(ap):
                for dpath, dirs, files in os.walk(ap):
                    dirs[:] = [d for d in dirs if not d.startswith(".")]
                    for fn in files:
                        if len(all_files) >= MAX_SCOPE:
                            break
                        if fn.endswith((".c", ".cc", ".cpp", ".h", ".hpp", ".py", ".js")):
                            fp = os.path.join(dpath, fn)
                            all_files.append({"path": fp, "rel": rel_or_abs(fp, root),
                                              "module": module_of(fp),
                                              "add": "-", "del": "-"})
                            modules.add(module_of(fp))
            elif os.path.isfile(ap):
                all_files.append({"path": ap, "rel": rel_or_abs(ap, root),
                                  "module": module_of(ap), "add": "-", "del": "-"})
                modules.add(module_of(ap))

    file_paths = [f["path"] for f in all_files]

    # 2) 依赖：只输出「本地没有的接口」
    #    topic 代码（补丁新增行）里用到、但本地头文件里找不到声明的，才是移植时要补的。
    #    本地已有的接口、改动文件清单都不输出。
    dep = analyze_dependencies(file_paths, root=root) if file_paths else {
        "nodes": 0, "files": [], "interfaces": [], "declared_total": 0}
    rv = review_topic_vs_local(plan_changes, root, topic_root, snap) if topic_url else {
        "review": {"changes": 0, "files": 0, "patch_ok": 0, "patch_failed": 0,
                   "symbols": 0, "local_hit": 0, "missing": 0,
                   "headers_missing": 0, "headers_hit": 0, "new_api": 0,
                   "review_source": ""},
        "items": [], "missing_headers": [], "failed": [], "errors": [],
        "note": "", "how": "", "degraded": False,
        "local_root": root, "hit_sample": [], "repos_scanned": 0,
        "topic_root": "", "snap_used": 0,
    }
    result["missing"] = {
        "review": rv["review"],
        "items": rv["items"],
        "missing_headers": rv["missing_headers"],
        "failed": rv.get("failed") or [],
        "note": rv.get("note") or "",
        "how": rv.get("how") or "",
        "degraded": bool(rv.get("degraded")),
        "topic": bool(topic_url),
        "local_root": rv.get("local_root") or root,
        "topic_root": rv.get("topic_root") or "",
        "snap_used": rv.get("snap_used") or 0,
        "hit_sample": rv.get("hit_sample") or [],
        "repos_scanned": rv.get("repos_scanned") or 0,
        "baseline": rv.get("baseline") or {},
    }
    result["dep_summary"] = {"nodes": dep["nodes"]}
    if rv.get("errors"):
        result["errors"].extend(rv["errors"])

    # 3) 新增 API（有 topic 时直接从补丁里数，比逐文件 git diff 准且快）
    new_api = rv["review"].get("new_api") or 0
    if not topic_url:
        new_api = _count_new_api(file_paths)
    # 工期只按 topic 相对本地真正缺失的依赖计算；不能再使用
    # dep["interfaces"]，它统计的是改动文件中出现过的全部接口，
    # 会把本地已经存在的宏/函数/类型重复算进移植工作量。
    if topic_url:
        ifaces = len(rv.get("items") or []) + len(rv.get("missing_headers") or [])
    else:
        ifaces = dep["interfaces"]

    # 4) 硬件支持
    tokens = tokenize_feature(feature_name) | tokenize_feature(" ".join(
        os.path.basename(f["rel"]) for f in all_files))
    result["hardware"] = hardware_support(parse_dotconfig(cfg_path), tokens, cfg_path)

    # 5) 存储状态
    if not map_path:
        maps = find_maps(root) or find_maps()
        map_path = maps[0]["path"] if maps else None
    if map_path and os.path.isfile(map_path):
        try:
            result["storage"] = analyze_storage(map_path, cfg_path)
        except Exception as exc:
            result["errors"].append("存储评估失败: %s" % exc)
    else:
        result["errors"].append("未找到 build/out/*/binary/*.map，先编译一次再评估存储")

    # 6) 工期
    metrics = {
        "files": len(all_files),
        "modules": len(modules),
        "interfaces": ifaces,
        "conflicts": conflicts,
        "loc": loc,
        "new_api": min(new_api, 60),
        "scope": bool(manual_paths),
    }
    result["metrics"] = metrics
    result["effort"] = estimate_effort(metrics)

    # 7) 评分（用于首页勾选态提示）
    score = 100
    if result["errors"]:
        score -= 25 * min(len(result["errors"]), 2)
    hw = result["hardware"]
    if hw and not hw["ok"]:
        score -= 15
    st = result["storage"]
    if st and st.get("regions"):
        fl = next((x for x in st["regions"] if x["name"] == "flash"), None)
        if fl and fl["pct"] >= 97:
            score -= 20
        elif fl and fl["pct"] >= 90:
            score -= 10
    result["score"] = max(0, min(100, score))
    result["elapsed_ms"] = int((time.time() - t0) * 1000)
    return result


def _count_by_module(items):
    agg = {}
    for it in items:
        m = it.get("module") or "-"
        agg[m] = agg.get(m, 0) + 1
    return [{"module": k, "count": v}
            for k, v in sorted(agg.items(), key=lambda x: -x[1])[:15]]


_NEW_API_RE = re.compile(
    r"^\+\s*(?:extern\s+)?(?:[A-Za-z_][\w\s\*]*?\s+)?([A-Za-z_]\w+)\s*\([^;]*$", re.M)


def _count_new_api(paths, max_files=15):
    """从 git diff 里数新增的函数定义/声明。

    每个文件一次 git 调用，文件多时会很慢，所以只抽样前 N 个。
    """
    total = 0
    for p in paths[:max_files]:
        if not os.path.isfile(p):
            continue
        try:
            r = subprocess.run(
                ["git", "-C", os.path.dirname(p), "diff", "-U0", "--", p],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                universal_newlines=True, errors="replace", timeout=30)
            if r.returncode == 0 and r.stdout:
                total += len(_NEW_API_RE.findall(r.stdout))
        except Exception:
            continue
    return total


# --------------------------------------------------------------------------
# 文档评估
# --------------------------------------------------------------------------
DOC_PATTERNS = [
    ("需求 / PRD", [r"prd", r"需求", r"requirement", r"spec"]),
    ("交互 / UI 稿", [r"交互", r"ui", r"ux", r"design", r"prototype"]),
    ("协议 / 接口文档", [r"协议", r"protocol", r"接口", r"api"]),
    ("测试用例", [r"test", r"测试", r"case"]),
]


def doc_eval(feature_name):
    tokens = tokenize_feature(feature_name)
    roots = [os.path.join(WORKSPACE, "docs"),
             os.path.join(WORKSPACE, "documentation")]
    found_files = []
    for root in roots:
        if not os.path.isdir(root):
            continue
        for dirpath, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for fn in files:
                if not fn.lower().endswith((".md", ".txt", ".doc", ".docx", ".pdf")):
                    continue
                low = fn.lower()
                hit = [t for t in tokens if t in low]
                p = os.path.join(dirpath, fn)
                found_files.append({
                    "name": fn, "path": safe_rel(p),
                    "match": hit,
                    "mtime": time.strftime("%Y-%m-%d",
                                           time.localtime(os.path.getmtime(p))),
                })
    matched = [f for f in found_files if f["match"]]

    checks = []
    for label, pats in DOC_PATTERNS:
        hit = [f for f in found_files
               if any(re.search(p, f["name"].lower()) for p in pats)]
        matched_hit = [f for f in matched
                       if any(re.search(p, f["name"].lower()) for p in pats)]
        checks.append({
            "label": label,
            "found": len(hit),
            "matched": len(matched_hit),
            "samples": [f["path"] for f in (matched_hit or hit)[:4]],
        })
    score = 0
    for c in checks:
        if c["matched"]:
            score += 25
        elif c["found"]:
            score += 12
    if not tokens:
        score = min(score, 50)
    return {
        "ok": True,
        "feature": feature_name,
        "checks": checks,
        "matched_files": matched[:20],
        "total_docs": len(found_files),
        "score": score,
        "advice": ("找到 %d 份与「%s」直接相关的文档" % (len(matched), feature_name))
        if matched else
        ("没有找到与「%s」直接相关的文档，建议先补齐需求/协议文档" % (feature_name or "本功能")),
        "at": now_str(),
    }


# --------------------------------------------------------------------------
# 刷机 / 自测
# --------------------------------------------------------------------------
def probe_env():
    return {
        "workspace": WORKSPACE,
        "python": sys.version.split()[0],
        "active_build": which("active-build") or "",
        "v3dl": which("v3dl") or "",
        "codegraph": which("codegraph") or "",
        "gerrit_script": gerrit_script() or "",
        "git": which("git") or "",
        "sudo_ready": _sudo_ready(),
        "build_dir": os.path.join(WORKSPACE, "build"),
        "map_count": len(find_maps()),
    }


def _sudo_ready():
    try:
        r = subprocess.run(["sudo", "-n", "true"], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=6)
        return r.returncode == 0
    except Exception:
        return False


# --------------------------------------------------------------------------
# 路由
# --------------------------------------------------------------------------
ROUTES = {}


def route(path, methods=("GET",)):
    def deco(fn):
        for m in methods:
            ROUTES[(m, path)] = fn
        return fn
    return deco


@route("/api/bootstrap")
def api_bootstrap(q, body):
    st = load_state()
    projs = list_projects()
    return {
        "ok": True,
        "state": st,
        "env": probe_env(),
        "projects": projs,
        "maps": [{"path": m["path"], "target": m["target"],
                  "kind": m["kind"],
                  "mtime": time.strftime("%Y-%m-%d %H:%M",
                                         time.localtime(m["mtime"]))}
                 for m in find_maps()[:8]],
        "server_time": now_str(),
    }


@route("/api/state", ("POST",))
def api_state_set(q, body):
    st = patch_state(body or {})
    return {"ok": True, "state": st}


@route("/api/fs/list")
def api_fs_list(q, body):
    path = unquote((q.get("path") or [WORKSPACE])[0])
    if not path:
        path = WORKSPACE
    if not os.path.isabs(path):
        path = os.path.join(WORKSPACE, path)
    path = os.path.normpath(path)
    if not os.path.isdir(path):
        path = WORKSPACE
    dirs, files = [], []
    try:
        for name in sorted(os.listdir(path)):
            full = os.path.join(path, name)
            if name.startswith("."):
                continue
            try:
                is_dir = os.path.isdir(full)
            except OSError:
                continue
            if is_dir:
                dirs.append({"name": name, "path": full})
            elif len(files) < 40:
                files.append({"name": name, "path": full})
    except OSError as exc:
        return {"ok": False, "error": str(exc), "path": path}
    shortcuts = [
        {"name": "工作区", "path": WORKSPACE},
        {"name": "build", "path": os.path.join(WORKSPACE, "build")},
        {"name": "ui", "path": os.path.join(WORKSPACE, "ui")},
        {"name": "framework", "path": os.path.join(WORKSPACE, "framework")},
        {"name": "packages", "path": os.path.join(WORKSPACE, "packages")},
        {"name": "家目录", "path": os.path.expanduser("~")},
    ]
    return {"ok": True, "path": path, "parent": os.path.dirname(path),
            "dirs": dirs, "files": files, "shortcuts": shortcuts}


# --------------------------------------------------------------------------
# 代码基线：manifest(xml) / 分支 / 强制切换（repo_force_checkout.sh）
# --------------------------------------------------------------------------
_MAN_CACHE = {}
_MAN_LOCK = threading.Lock()


def parse_manifest_xml(path):
    """解析一个 manifest：默认 revision（分支）、project 数量、review 地址。
    按 mtime+size 缓存，避免每次刷新页面都重读十来个 xml。"""
    try:
        stt = os.stat(path)
        key = (int(stt.st_mtime), stt.st_size)
    except OSError:
        key = None
    if key:
        with _MAN_LOCK:
            hit = _MAN_CACHE.get(path)
        if hit and hit[0] == key:
            return dict(hit[1])
    info = _parse_manifest_raw(path)
    if key:
        with _MAN_LOCK:
            _MAN_CACHE[path] = (key, info)
    return dict(info)


def _parse_manifest_raw(path):
    txt = _read_text(path, 4000000) or ""
    rev = ""
    m = re.search(r"<default\b([^>]*?)/?>", txt)
    if m:
        rev = dict(re.findall(r'(\w+)="([^"]*)"', m.group(1))).get("revision", "")
    if not rev:
        revs = re.findall(r'<project\b[^>]*\brevision="([^"]+)"', txt)
        if revs:
            rev = max(set(revs), key=revs.count)
    names = re.findall(r'<project\b[^>]*\bname="([^"]+)"', txt)
    review = ""
    mr = re.search(r"<remote\b([^>]*?)/?>", txt)
    if mr:
        review = dict(re.findall(r'(\w+)="([^"]*)"', mr.group(1))).get("review", "")
    return {"revision": rev, "projects": len(names), "review": review,
            "sample": names[:8]}


def repo_baseline(workspace=None):
    workspace_root = normalize_workspace_path(workspace or WORKSPACE)
    root = os.path.join(workspace_root, ".repo")
    mdir = os.path.join(root, "manifests")
    current = ""
    mtxt = _read_text(os.path.join(root, "manifest.xml"), 400000) or ""
    inc = re.search(r'<include\s+name="([^"]+)"', mtxt)
    if inc:
        current = inc.group(1)
    elif mtxt.strip():
        current = "manifest.xml"

    items, branches = [], []
    if os.path.isdir(mdir):
        try:
            names = sorted(n for n in os.listdir(mdir) if n.endswith(".xml"))
        except OSError:
            names = []
        for n in names:
            full = os.path.join(mdir, n)
            info = {"name": n, "current": (n == current)}
            try:
                info.update(parse_manifest_xml(full))
                st = os.stat(full)
                info["mtime"] = time.strftime("%Y-%m-%d %H:%M",
                                              time.localtime(st.st_mtime))
                info["size_h"] = human_bytes(st.st_size)
            except Exception as exc:
                info["error"] = str(exc)
            rev = info.get("revision")
            if rev and rev not in branches and not rev.startswith("refs/"):
                branches.append(rev)
            items.append(info)

    head, head_repo = "", ""
    try:
        # 只试前几个仓库：repo 同步后基本都是 detached，全遍历会跑几十次 git
        for rel_path in list(manifest_map().keys())[:3]:
            rp = os.path.join(WORKSPACE, rel_path)
            if not os.path.exists(os.path.join(rp, ".git")):
                continue
            out = subprocess.run(
                ["git", "-C", rp, "rev-parse", "--abbrev-ref", "HEAD"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                universal_newlines=True, timeout=5, errors="replace")
            head = (out.stdout or "").strip()
            head_repo = rel_path
            break
    except Exception:
        pass

    script = os.path.join(PORTAL_DIR, "repo_force_checkout.sh")
    active_build = json_read(os.path.join(workspace_root, "build",
                                          ".active-build-state.json"), {}) or {}
    return {
        "ok": True,
        "workspace": workspace_root,
        "has_repo": os.path.isdir(root),
        "checkout_script": script,
        "checkout_script_ok": os.path.isfile(script),
        "current_manifest": current,
        "head": head or "(detached)",
        "head_repo": head_repo,
        "manifests": items,
        "branches": branches,
        "active_build": active_build,
    }


def _repo_root_dir(workspace=None):
    return os.path.join(normalize_workspace_path(workspace or WORKSPACE), ".repo")


@route("/api/workspace/switch", ("POST",))
def api_workspace_switch(q, body):
    """切换当前工作区：之后所有评估/编译/基线都以新目录为根。"""
    global WORKSPACE
    p = ((body or {}).get("path") or "").strip()
    if not p:
        return {"ok": False, "error": "路径为空"}
    p = os.path.abspath(normalize_workspace_path(p))
    if not os.path.isdir(p):
        return {"ok": False, "error": "目录不存在: %s" % p}
    if not os.path.isdir(os.path.join(p, ".repo")):
        return {"ok": False, "error": "不是 repo 工作区（缺 .repo 目录）: %s" % p}
    WORKSPACE = p
    # 与路径强绑定的缓存全部作废
    _MANIFEST["mtime"] = 0
    _MANIFEST["map"] = {}
    with _HEADER_LOCK:
        _HEADER_INDEX["key"] = None
        _HEADER_INDEX["index"] = {}
    try:
        os.remove(HEADER_INDEX_FILE)
    except OSError:
        pass
    st = load_state()
    st["workspace"] = p
    save_state(st)
    out = repo_baseline()
    out["switched"] = True
    return out


@route("/api/repo/baseline", ("GET", "POST"))
def api_repo_baseline(q, body):
    try:
        return repo_baseline(selected_workspace(body))
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def selected_workspace(body=None):
    """统一使用代码评估阶段选定的代码目录。"""
    b = body or {}
    st = load_state() or {}
    baseline = st.get("baseline") or {}
    port = st.get("port") or {}
    raw = ((b.get("path") or "").strip() or baseline.get("path") or
           port.get("path") or st.get("workspace") or WORKSPACE)
    return normalize_workspace_path(raw)


@route("/api/repo/checkout", ("POST",))
def api_repo_checkout(q, body):
    """按 manifest + 分支强制同步代码（会丢弃未提交改动）。"""
    b = body or {}
    xml = (b.get("xml") or "").strip()
    branch = (b.get("branch") or "").strip()
    jobs = str(b.get("jobs") or "8")
    target = normalize_workspace_path((b.get("path") or "").strip() or WORKSPACE)
    target = os.path.abspath(target)
    if not os.path.isdir(target):
        return {"ok": False, "error": "目录不存在: %s" % target}
    script = os.path.join(PORTAL_DIR, "repo_force_checkout.sh")
    if not os.path.isfile(script):
        return {"ok": False, "error": "找不到 %s" % script}
    if xml and not re.match(r"^[\w.\-]+\.xml$", xml):
        return {"ok": False, "error": "manifest 名称不合法：%s" % xml}
    if branch and not re.match(r"^[\w][\w./\-]*$", branch):
        return {"ok": False, "error": "分支名不合法：%s" % branch}
    if not re.match(r"^\d{1,2}$", jobs):
        jobs = "8"

    # 脚本集中在 feature-portal，使用绝对路径调用，cwd 仍保持为目标工作区。
    cmd = ["bash", script]
    if xml:
        cmd.append(xml)
    if branch:
        cmd.append(branch)
    cmd.append("-j" + jobs)
    job = start_job("repo-checkout", cmd, cwd=target)
    st = load_state()
    st["baseline"] = {"path": target, "xml": xml or "(当前 manifest)",
                      "branch": branch or "(按 manifest)", "at": now_str()}
    save_state(st)
    return {"ok": True, "job": job.snapshot(), "cmd": " ".join(cmd),
            "cwd": target}


@route("/api/repo/codegraph-refresh", ("POST",))
def api_repo_codegraph_refresh(q, body):
    """刷新当前代码基线的 CodeGraph 索引，不修改工作区代码。"""
    target = ((body or {}).get("path") or "").strip() or WORKSPACE
    target = os.path.abspath(target)
    if not os.path.isdir(target):
        return {"ok": False, "error": "目录不存在: %s" % target}
    cg = which("codegraph")
    if not cg:
        return {"ok": False, "error": "未找到 codegraph 命令，请先安装 CodeGraph"}
    graph_dir = os.path.join(target, ".codegraph")
    action = "sync" if os.path.isdir(graph_dir) else "init"
    cmd = [cg, action, target]
    job = start_job("codegraph-%s" % action, cmd, cwd=target)
    return {"ok": True, "job": job.snapshot(), "cmd": " ".join(cmd), "cwd": target,
            "action": action}


@route("/api/storage")
def api_storage(q, body):
    maps = find_maps()
    want = (q.get("map") or [None])[0]
    if not want:
        if not maps:
            return {"ok": False, "error": "未找到 .map 文件，请先编译"}
        want = maps[0]["path"]
    cfg = os.path.join(WORKSPACE, "build", ".config")
    try:
        return analyze_storage(want, cfg)
    except Exception as exc:
        traceback.print_exc()
        return {"ok": False, "error": str(exc)}


@route("/api/eval/doc", ("POST",))
def api_eval_doc(q, body):
    name = (body or {}).get("feature") or load_state().get("feature_name") or ""
    res = doc_eval(name)
    st = load_state()
    st.setdefault("eval", {})
    st["eval"]["doc"] = {"done": True, "at": now_str(), "score": res["score"]}
    if name:
        st["feature_name"] = name
    save_state(st)
    return res


@route("/api/codex/status")
def api_codex_status(q, body):
    return {"ok": True, **codex_config()}


@route("/api/codex/cli-models")
def api_codex_cli_models(q, body):
    codex_state = load_state().get("codex") or {}
    current = str(codex_state.get("implementation_model") or "").strip()
    used = codex_state.get("models_used") or []
    cache_path = os.path.expanduser(os.environ.get("CODEX_MODELS_CACHE", "~/.codex/models_cache.json"))
    models = []
    cache_time = ""
    try:
        with io.open(cache_path, "r", encoding="utf-8") as fh:
            cache = json.load(fh)
        cache_time = str(cache.get("fetched_at") or "")
        for item in cache.get("models") or []:
            slug = str(item.get("slug") or "").strip()
            if slug and str(item.get("visibility") or "list") == "list":
                models.append(slug)
    except Exception as exc:
        return {"ok": False, "models": [], "selected": current,
                "message": "无法读取本机 Codex 模型清单：%s" % exc}
    models = list(dict.fromkeys(models + [str(x).strip() for x in used if str(x).strip()]))
    if current and current not in models:
        current = ""
    return {"ok": True, "models": models, "selected": current,
            "fetched_at": cache_time,
            "message": "已读取本机 Codex 模型清单；不会使用默认模型，请先选择并保存。"}


@route("/api/codex/implementation-model", ("POST",))
def api_codex_implementation_model(q, body):
    model = str((body or {}).get("model") or "").strip()
    if not model or not re.match(r"^[A-Za-z0-9_.:-]+$", model):
        return {"ok": False, "error": "模型名称无效"}
    if model not in (api_codex_cli_models(q, body).get("models") or []):
        return {"ok": False, "error": "该模型不在本机 Codex 可用模型清单中，请先刷新模型列表"}
    st = load_state()
    st.setdefault("codex", {})["implementation_model"] = model
    save_state(st)
    return {"ok": True, "model": model}


@route("/api/codex/models", ("POST",))
def api_codex_models(q, body):
    """返回当前 API Key 能访问的模型；无 Key 或查询失败时返回可选候选。"""
    b = body or {}
    key = str(b.get("api_key") or os.environ.get("OPENAI_API_KEY", "")).strip()
    if not key:
        return {"ok": True, "configured": False, "models": [],
                "message": "请先填写自己的 OpenAI API Key，保存后刷新可用模型。"}
    req = urllib.request.Request(
        os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/") + "/models",
        headers={"Authorization": "Bearer " + key}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        ids = [str(x.get("id")) for x in (data.get("data") or [])
               if x.get("id") and ("codex" in str(x.get("id")).lower()
                                    or str(x.get("id")).startswith(("gpt-", "o")))]
        models = sorted(set(ids), key=lambda x: (0 if "codex" in x else 1, x))
        return {"ok": True, "configured": True, "models": models,
                "message": "已从 OpenAI 查询当前 API Key 可访问模型。" if models else "该 Key 没有返回可用的 Codex/GPT 模型。"}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        return {"ok": False, "configured": True, "models": [],
                "message": "模型查询失败（HTTP %s）：%s" % (exc.code, detail)}
    except Exception as exc:
        return {"ok": False, "configured": True, "models": [],
                "message": "模型查询失败：%s" % exc}


@route("/api/codex/config", ("POST",))
def api_codex_config(q, body):
    global CODEX_MODEL
    b = body or {}
    key = str(b.get("api_key") or "").strip()
    model = str(b.get("model") or CODEX_MODEL).strip()
    if key and len(key) < 20:
        return {"ok": False, "error": "API Key 格式看起来不完整"}
    if key:
        os.environ["OPENAI_API_KEY"] = key
    if model:
        os.environ["OPENAI_MODEL"] = model
        CODEX_MODEL = model
    env_path = os.path.join(PORTAL_DIR, "codex.env")
    try:
        if key or model:
            with io.open(env_path, "w", encoding="utf-8") as fh:
                if key:
                    fh.write("OPENAI_API_KEY=" + key.replace("\\n", "") + "\n")
                fh.write("OPENAI_MODEL=" + model.replace("\n", "") + "\n")
            os.chmod(env_path, 0o600)
    except Exception as exc:
        return {"ok": False, "error": "保存 Codex 配置失败：%s" % exc}
    return {"ok": True, **codex_config()}


def lark_cli_path():
    for p in (which("lark-cli"), "/home/hgw/.local/node_modules/.bin/lark-cli"):
        if p and os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return ""


@route("/api/feishu/status")
def api_feishu_status(q, body):
    st = load_state().get("feishu") or {}
    cli = lark_cli_path()
    return {"ok": True, "cli": bool(cli), "cli_path": cli,
            "app_id": st.get("app_id") or "", "authorized": bool(st.get("authorized")),
            "message": ("已找到 lark-cli，请完成飞书授权" if cli else "未找到 lark-cli")}


@route("/api/eval/pfs", ("POST",))
def api_eval_pfs(q, body):
    b = body or {}
    content = str(b.get("content") or "")
    name = str(b.get("name") or "需求.pfs")
    requirement = extract_pfs_description(content)
    if not requirement.strip():
        return {"ok": False, "error": "PFS 文件内容为空"}
    if len(requirement) > 120000:
        requirement = requirement[:120000] + "\n[Description 内容超过 120KB，已截断]"
    prompt = """你是 ZeppOS/智能手表功能需求评审专家。输入内容只来自 PFS 的 Description 列，请把它作为本次修改需求和后续代码评审依据。

不要解析或推断 Screen，不评审按键、触摸、旋钮事件；Screen 和事件只在下载 topic 代码后，根据 application 文件中的 importScreen 再评审。

输出严格 JSON，字段为：
{"requirements":[], "acceptance_criteria":[], "risks":[], "summary":""}

PFS 文件名：""" + name + "\nDescription 列内容：\n" + requirement
    r = call_codex(prompt)
    if not r.get("ok"):
        return r
    analysis = parse_codex_json(r.get("text"))
    return {"ok": True, "model": r.get("model"), "analysis": analysis,
            "rules": SCREEN_EVENT_RULES, "source": name}


@route("/api/eval/pfs-link", ("POST",))
def api_eval_pfs_link(q, body):
    b = body or {}
    url = str(b.get("url") or "").strip()
    section = str(b.get("section") or "").strip()
    if not re.match(r"^https?://", url, re.I):
        return {"ok": False, "error": "飞书需求链接必须是 http/https URL"}
    try:
        if "/wiki/" in url:
            requirement, source_title = fetch_feishu_requirement(url, section)
        else:
            req = urllib.request.Request(url, headers={"User-Agent": "feature-portal/1.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                content = resp.read(120000).decode("utf-8", "replace")
            requirement = strip_document_comments(extract_requirement_section(content, section))
            source_title = section or "Description"
    except Exception as exc:
        return {"ok": False, "error": "读取飞书需求失败：%s" % exc}
    if not requirement.strip():
        return {"ok": False, "error": "没有找到指定标题下的需求内容：%s" % section if section else "没有找到 Description 列内容"}
    pfs = make_numbered_pfs(requirement)
    if not pfs:
        return {"ok": False, "error": "指定区域没有可生成的需求条目"}
    skill_dir = os.path.join(DATA_DIR, "pfs-skills")
    os.makedirs(skill_dir, exist_ok=True)
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", section or "requirement")[:60] or "requirement"
    skill_path = os.path.join(skill_dir, slug + ".md")
    ai = analyze_requirement_with_ai(requirement, source_title)
    if not ai.get("ok"):
        return {"ok": False, "error": ai.get("error") or "AI 需求分析失败"}
    analysis = ai.get("analysis") or {}
    ai_items = analysis.get("changes") or []
    if ai_items:
        pfs = make_numbered_pfs("\n".join(str(x) for x in ai_items))
    md = "# PFS 需求\n\n来源：%s\n模板：%s\n需求目录/标题：%s\n\n%s\n" % (
        url, source_title, section or "正文", pfs)
    json_write(skill_path, md)
    st = load_state()
    st["pfs"] = {"content": pfs, "path": skill_path, "items": [
        {"id": "pfs-%d" % i, "label": line, "hint": "按本条 PFS 需求执行并确认"
         } for i, line in enumerate(pfs.splitlines(), 1) if line.strip()]}
    st["selftest"] = {"items": [], "done": False, "at": ""}
    st.setdefault("eval", {})["pfs"] = True
    save_state(st)
    return {"ok": True, "url": url, "skill_path": skill_path,
            "source": section or source_title or "正文", "pfs": pfs,
            "analysis": analysis, "model": ai.get("model")}


@route("/api/pfs/save", ("POST",))
def api_pfs_save(q, body):
    content = str((body or {}).get("content") or "").strip()
    if not content:
        return {"ok": False, "error": "PFS 内容不能为空"}
    normalized = make_numbered_pfs(content)
    if not normalized:
        return {"ok": False, "error": "没有可保存的 PFS 条目"}
    st = load_state()
    old = st.get("pfs") or {}
    path = old.get("path") or os.path.join(DATA_DIR, "pfs-skills", "pfs-1.md")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with io.open(path, "w", encoding="utf-8") as fh:
        fh.write("# PFS 需求\n\n## 需求正文\n" + normalized + "\n")
    items = [{"id": "pfs-%d" % i, "label": line,
              "hint": "按本条 PFS 需求执行并确认"}
             for i, line in enumerate(normalized.splitlines(), 1) if line.strip()]
    st["pfs"] = {"content": normalized, "items": items, "path": path}
    st["selftest"] = {"items": [], "done": False, "at": ""}
    save_state(st)
    return {"ok": True, "path": path, "content": normalized, "items": items}


IMPLEMENTATION_RULES = """功能实现规范（仅按本次 PFS 实现，不得发散）
1. 先使用本地 codegraph summary/module/path/file/impact 查询现有模块、入口、调用链和归属，再决定修改位置；优先复用已有能力，不新建平行框架。
2. 只实现 PFS 明确提出的需求；不顺手重构、不改无关模块、不改变已有公共接口行为。
3. 若需要新增 Screen，必须在正确的 application 文件中 importScreen，并同步检查 gapp 注册/配置；根据需求决定是否新增 style。
4. 方屏/圆屏、390×450 目标分辨率、按键、触摸右滑、旋钮事件必须遵循仓库已有规范；不能直接复制 480 坐标或 style。
5. 处理进入、退出、返回、重复进入、异常中断后的界面恢复和事件监听清理，禁止重复注册造成重复响应。
6. 修改完成后只运行与本次改动相关的静态检查或轻量验证；绝不执行固件编译、烧录或完整构建。输出修改目录、关键文件、测试重点和未解决风险。"""
IMPLEMENTATION_RULES_FILE = os.path.join(PORTAL_DIR, "implementation-skills",
                                         "feature-implementation-rules.txt")
IMPLEMENTATION_MODEL = ""


def selected_codex_model():
    return str((load_state().get("codex") or {}).get("implementation_model") or "").strip()


def implementation_snapshot(root, limit=30000):
    """记录源码文件的 size/mtime，兼容未纳入 Git 的准备工作目录。"""
    exts = (".c", ".cc", ".cpp", ".h", ".hpp", ".cxx", ".mk", ".in",
            ".json", ".xml", ".js", ".lua", ".py", ".yaml", ".yml", ".md")
    skip = {".git", ".codegraph", ".codex", ".cache", "build", "out", "dist"}
    result = {}
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in skip and not d.startswith(".")]
        for fn in files:
            if not fn.lower().endswith(exts):
                continue
            path = os.path.join(base, fn)
            try:
                st = os.stat(path)
                result[os.path.relpath(path, root).replace("\\", "/")] = [st.st_size, st.st_mtime_ns]
            except OSError:
                continue
            if len(result) >= limit:
                return result
    return result


def implementation_rules():
    try:
        with io.open(IMPLEMENTATION_RULES_FILE, "r", encoding="utf-8") as fh:
            custom = fh.read().strip()
        return IMPLEMENTATION_RULES + "\n\n用户维护的功能实现规范：\n" + custom
    except Exception:
        return IMPLEMENTATION_RULES


@route("/api/implementation/start", ("POST",))
def api_implementation_start(q, body):
    b = body or {}
    baseline_path = str((load_state().get("baseline") or {}).get("path") or "").strip()
    requested_path = str(b.get("path") or baseline_path).strip()
    if not requested_path:
        return {"ok": False, "error": "请先在准备工作中选择代码基线目录"}
    workspace = normalize_workspace_path(requested_path)
    if not os.path.isdir(workspace):
        return {"ok": False, "error": "代码工作区不存在：%s" % workspace}
    pfs = str((load_state().get("pfs") or {}).get("content") or "").strip()
    if not pfs:
        return {"ok": False, "error": "请先生成 PFS 需求"}
    codex = codex_cli_path()
    if not codex:
        return {"ok": False, "error": "未找到本机 Codex CLI"}
    graph = ""
    cg = which("codegraph")
    if cg:
        try:
            gp = subprocess.run([cg, "explore", "给出当前 workspace 的模块、application 入口、Screen、gapp/style 归属和相关调用链"],
                                cwd=workspace, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, errors="replace", timeout=90)
            graph = (gp.stdout or "")[-30000:]
        except Exception as exc:
            graph = "CodeGraph 查询失败：%s" % exc
    else:
        graph = "未找到 codegraph 命令，需由 Codex 在本地继续检查。"
    before = subprocess.run(["git", "-C", workspace, "status", "--porcelain"],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, errors="replace").stdout
    before_snapshot = implementation_snapshot(workspace)
    skill_dir = os.path.join(DATA_DIR, "implementation-skills")
    os.makedirs(skill_dir, exist_ok=True)
    skill_path = os.path.join(skill_dir, "feature-implementation-rules.md")
    with io.open(skill_path, "w", encoding="utf-8") as fh:
        fh.write("# feature-implementation-rules\n\n")
        fh.write("## 使用范围\n仅用于实现当前 PFS，不得扩展需求范围。\n\n")
        fh.write("## 实现规范\n%s\n\n" % implementation_rules())
        fh.write("## 当前 PFS\n%s\n" % pfs)
    implementation_model = selected_codex_model()
    if not implementation_model:
        return {"ok": False, "error": "请先在前期工作中选择并保存 Codex 模型"}
    available_models = api_codex_cli_models(q, body).get("models") or []
    if implementation_model not in available_models:
        return {"ok": False, "error": "已保存的 Codex 模型当前不可用，请刷新模型列表后重新选择"}
    prompt = """请使用模型 %s，在当前代码工作区直接实现下面 PFS 需求。
先运行本地 codegraph 查询代码结构，再阅读真实源码。实现范围必须严格受 PFS 限制。
不要只给建议，必须实际修改文件。完成后只执行静态检查或轻量验证，禁止执行固件编译、烧录和完整构建。
只能修改与本次 PFS 直接相关的候选目录和文件；禁止扫描后批量改动整个 workspace，禁止修改无关目录、第三方目录、生成目录和历史文件。
最后输出严格 JSON：{"summary":"", "changed_dirs":[], "changed_files":[], "tests":[], "risks":[], "skill":"feature-implementation-rules"}

CodeGraph 初步结果（仅作导航，必须打开真实源码核对）：
""" % implementation_model + graph + "\n\n规范：\n""" + implementation_rules() + "\n\nPFS：\n" + pfs[:120000]
    # 当前 Codex CLI 版本中 --approve-for-me 不能与 --sandbox 同时使用；
    # 这里使用非交互执行模式，并把工作目录限定为用户选择的代码工作区。
    cmd = [codex, "exec", "--ephemeral", "--skip-git-repo-check", "-m", implementation_model,
           "--dangerously-bypass-approvals-and-sandbox", "--json", "-C", workspace, prompt]
    job = start_job("feature-implementation", cmd, cwd=workspace)
    st = load_state()
    st["implementation"] = {"status": "running", "workspace": workspace,
                             "before": before, "before_snapshot": before_snapshot,
                             "skill": skill_path, "job": job.id}
    save_state(st)
    return {"ok": True, "job": job.snapshot(), "skill_path": skill_path,
            "workspace": workspace}


@route("/api/implementation/result", ("POST",))
def api_implementation_result(q, body):
    b = body or {}
    output = str(b.get("output") or "")
    job_status = str(b.get("job_status") or "unknown")
    st = load_state()
    rec = st.get("implementation") or {}
    workspace = normalize_workspace_path(rec.get("workspace") or selected_workspace(b))
    changed = subprocess.run(["git", "-C", workspace, "status", "--porcelain"],
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, errors="replace").stdout.splitlines()
    after_status = "\n".join(changed)
    before_snapshot = rec.get("before_snapshot") or {}
    after_snapshot = implementation_snapshot(workspace)
    snapshot_files = sorted(path for path, value in after_snapshot.items()
                            if path not in before_snapshot or before_snapshot[path] != value)
    before_lines = str(rec.get("before") or "").splitlines()
    before_map = {x[3:].strip(): x[:2] for x in before_lines if len(x) > 3}
    after_map = {x[3:].strip(): x[:2] for x in changed if len(x) > 3}
    files = sorted(set(path for path, status in after_map.items()
                       if path not in before_map or before_map[path] != status)
                   | set(snapshot_files))
    dirs = sorted(set(os.path.dirname(x) or "." for x in files))
    result = None
    for blob in reversed(_split_json_blobs(output)):
        try:
            obj = json.loads(blob)
            if isinstance(obj, dict) and ("summary" in obj or "tests" in obj):
                result = obj
                break
        except Exception:
            pass
    result = result or {}
    reported_dirs = result.get("changed_dirs") or []
    if not isinstance(reported_dirs, list):
        reported_dirs = [str(reported_dirs)]
    reported_dirs = [str(x).strip() for x in reported_dirs if str(x).strip()]
    result["detected_dirs"] = dirs
    result["changed_dirs"] = sorted(set(dirs + reported_dirs))
    result["changed_files"] = files
    result["reported_dirs"] = reported_dirs
    result["skill_path"] = rec.get("skill") or ""
    result["model"] = selected_codex_model()
    codex_state = st.setdefault("codex", {})
    models_used = codex_state.setdefault("models_used", [])
    if result["model"] and result["model"] not in models_used:
        models_used.append(result["model"])
    result["implementation_status"] = "completed" if job_status == "ok" else "failed"
    result["job_status"] = job_status
    errors = [line for line in output.splitlines()
              if re.search(r"error|failed|failure|exception|未找到|失败|错误", line, re.I)]
    if job_status != "ok":
        reason = "Codex 进程未正常结束，已产生的修改可能不完整。"
    elif not files:
        reason = ("Codex 进程执行完成，但 Git 状态和源码文件快照都没有新增变化；"
                  "当前确认没有检测到本次代码写入。")
    else:
        reason = "已检测到本次新增或变化的文件。"
    result["error_reason"] = reason if job_status != "ok" or not files else ""
    result["diagnostics"] = {
        "workspace": workspace,
        "before_git_status": str(rec.get("before") or "")[-12000:],
        "after_git_status": after_status[-12000:],
        "changed_by_snapshot": snapshot_files,
        "error_lines": errors[-30:],
        "output_tail": output[-12000:],
    }
    st["implementation"]["status"] = "done"
    st["implementation"]["result"] = result
    save_state(st)
    return {"ok": True, **result}


@route("/api/eval/frr", ("POST",))
def api_eval_frr(q, body):
    root = selected_workspace(body)
    found = []
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]
        for fn in files:
            if fn.lower().endswith(".frr"):
                found.append(os.path.relpath(os.path.join(base, fn), root).replace("\\", "/"))
    return {"ok": True, "exists": bool(found), "files": found[:100], "root": root}


@route("/api/eval/code", ("POST",))
def api_eval_code(q, body):
    b = body or {}
    name = b.get("feature") or load_state().get("feature_name") or ""
    topic = (b.get("topic") or "").strip()
    paths = b.get("paths") or []
    root = selected_workspace(b)
    if not os.path.isdir(os.path.join(root, ".repo")):
        return {"ok": False, "error": "当前代码目录不是 repo 工作区（缺少 .repo）：%s" % root,
                "local_root": root}
    try:
        # 本地基准：前端传的目录 > 代码基线目录 > 上次移植目录
        res = code_eval(name, topic, paths, b.get("map") or None,
                        root)
    except Exception as exc:
        traceback.print_exc()
        return {"ok": False, "error": "代码评估异常：%s" % exc}
    st = load_state()
    st.setdefault("eval", {})
    st["eval"]["code"] = {"done": True, "at": now_str(),
                          "score": res.get("score", 0),
                          "local_root": res.get("local_root", ""),
                          "topic_root": (res.get("snapshot") or {}).get("dir", "")}
    if name:
        st["feature_name"] = name
    # 评估时填过的 topic 直接记下来，移植步骤不用再填一次
    if topic:
        st.setdefault("port", {})
        st["port"]["topic"] = topic
        st["port"]["topic_from"] = "eval"
    save_state(st)
    return res


@route("/api/port/plan", ("POST",))
def api_port_plan(q, body):
    b = body or {}
    url = (b.get("topic") or "").strip()
    if not url:
        return {"ok": False, "error": "请填写 gerrit topic / change 链接"}
    # plan 必须和 download 扫同一棵目录，否则预检显示的仓库和实际落地的仓库对不上
    path = selected_workspace(b)
    res = run_gerrit("plan", url, timeout=180, extra=["--workspace", path])
    return {"ok": res.get("status") == "ok", "plan": res, "workspace": path}


@route("/api/port/precheck", ("POST",))
def api_port_precheck(q, body):
    """只读预检：每个 change 落到哪个仓库、当前分支/dirty/cherry-pick 状态，
    提前告诉用户 download 会不会失败。"""
    b = body or {}
    url = (b.get("topic") or "").strip()
    if not url:
        return {"ok": False, "error": "请填写 gerrit topic / change 链接"}
    path = selected_workspace(b)
    res = run_gerrit("precheck", url, timeout=300, extra=["--workspace", path])
    for change in (res.get("changes") or res.get("precheck", {}).get("changes") or []):
        if isinstance(change, dict) and change.get("subject"):
            change["subject"] = repair_mojibake(change["subject"])
    return {"ok": res.get("status") == "ok", "precheck": res, "workspace": path}


@route("/api/topic/snapshot", ("POST",))
def api_topic_snapshot(q, body):
    """把 topic 代码整棵导出到备份目录（不动本地工作区），返回快照信息。

    评估会自动做这一步；这个接口用于「只想先拿到代码看看」或强制重导。
    """
    b = body or {}
    url = (b.get("topic") or "").strip()
    if not url:
        return {"ok": False, "error": "请填写 gerrit topic / change 链接"}
    root = local_eval_root((b.get("path") or "").strip() or None)
    r = resolve_change_files(url, root)
    if not r.get("ok") or not r.get("changes"):
        return {"ok": False, "error": (r.get("plan") or {}).get("message") or "查不到 change 清单",
                "root": root}
    snap = build_topic_snapshot(r["changes"], root, url, force=bool(b.get("force")))
    return {"ok": bool(snap.get("ok")), "root": root, "snapshot": {
        "dir": snap.get("dir"), "status": snap.get("status"),
        "repos": [{"rel": x.get("rel"), "repo": x.get("repo"), "ok": x.get("ok"),
                   "files": len(x.get("files") or []),
                   "changes": x.get("changes"), "error": x.get("error")}
                  for x in (snap.get("repos") or [])],
        "failed": snap.get("failed") or [],
        "files": snap.get("files") or 0,
        "reused": bool(snap.get("reused")),
        "at": snap.get("at") or "",
    }}


@route("/api/port/download", ("POST",))
def api_port_download(q, body):
    b = body or {}
    url = (b.get("topic") or "").strip()
    if not url:
        return {"ok": False, "error": "请填写 gerrit topic / change 链接"}
    script = gerrit_script()
    if not script:
        return {"ok": False, "error": "找不到 tools/gerrit_dl.py"}
    path = selected_workspace(b)
    if not os.path.isdir(path):
        return {"ok": False, "error": "目标目录不存在：%s" % path}
    # 始终显式指定扫描根：否则 gerrit_dl.py 会退回默认的 workspace/monaco/master，
    # 把代码合到别的目录里去
    extra = ["--workspace", path]
    py = which("python3") or sys.executable
    job = start_job("gerrit-download", [py, script, "download", "--url", url] + extra,
                    cwd=path if os.path.isdir(path) else WORKSPACE)
    st = load_state()
    st.setdefault("port", {})
    st["port"]["topic"] = url
    st["port"]["path"] = path
    st["port"]["last_cmd"] = " ".join([os.path.basename(py), script, "download",
                                       "--url", url] + extra)
    rec = st.get("recent_paths") or []
    if path in rec:
        rec.remove(path)
    rec.insert(0, path)
    st["recent_paths"] = rec[:8]
    save_state(st)
    return {"ok": True, "job": job.snapshot(), "cmd": job.cmd_str(),
            "workspace": path, "script": script}


@route("/api/port/download-status")
def api_port_download_status(q, body):
    """判断当前 topic 是否已经在目标目录成功下载过。"""
    topic = ((q.get("topic") or [""])[0] or "").strip()
    path = os.path.abspath(normalize_workspace_path(
        ((q.get("path") or [""])[0] or WORKSPACE).strip()))
    rec = json_read(os.path.join(DATA_DIR, "last-download.json"), {}) or {}
    payload = rec.get("payload") or {}
    st_port = (load_state().get("port") or {})
    saved_topic = str(st_port.get("topic") or payload.get("topic") or "").strip()
    saved_path_raw = st_port.get("path") or payload.get("workspace") or payload.get("path") or ""
    saved_path = os.path.abspath(str(saved_path_raw).strip()) if saved_path_raw else ""
    matched = bool(topic and saved_topic and topic == saved_topic and
                   (not saved_path or saved_path == path) and
                   rec.get("status") == "ok")
    return {"ok": True, "downloaded": matched, "at": rec.get("at") or "",
            "status": rec.get("status") or "", "topic": saved_topic}


@route("/api/port/result", ("POST",))
def api_port_result(q, body):
    """download 结束后，解析 stdout 里的 JSON，抽出冲突并分析。"""
    b = body or {}
    text = b.get("output") or ""
    payload = None
    for blob in reversed(_split_json_blobs(text)):
        try:
            payload = json.loads(blob)
            break
        except Exception:
            continue
    if not payload:
        # 把原始输出存下来，否则界面一刷新就查不出失败原因了
        _save_last_download({"status": "unparsed", "at": now_str(),
                             "raw_tail": text[-8000:]})
        return {"ok": False, "error": "没能从输出里解析出 gerrit 结果 JSON",
                "raw_tail": text[-1500:]}
    status = payload.get("status")
    changes = payload.get("changes") or []
    has_conflict = any((c.get("state") or "") in ("conflict", "conflict_aborted")
                       for c in changes)
    conflicts = analyze_conflicts(changes) if has_conflict else []
    workspace = normalize_workspace_path(
        (load_state().get("port") or {}).get("path") or WORKSPACE)
    adaptation = analyze_gapp_390_adaptation(changes, workspace)
    screen_review = review_import_screens(workspace)
    # 每条 change 的 state/message 就是失败原因，必须完整给到前端
    brief = [{
        "number": c.get("number"),
        "subject": repair_mojibake(c.get("subject")),
        "project": c.get("project"),
        "branch": c.get("branch"),
        "repo": c.get("repo"),
        "state": c.get("state"),
        "message": (c.get("message") or "").strip(),
        "files": c.get("conflicted_files") or [],
        "auto_resolved": c.get("auto_resolved") or [],
        "tried": c.get("tried") or [],
    } for c in changes]
    st = load_state()
    st.setdefault("port", {})
    st["port"]["conflicts"] = [
        {"repo": c["repo_rel"], "files": [f["file"] for f in c["files"]]}
        for c in conflicts]
    st["port"]["done"] = (status == "ok")
    st["port"]["last_status"] = status
    save_state(st)
    payload["gapp_390_adaptation"] = adaptation
    payload["screen_review"] = screen_review
    _save_last_download({"status": status, "at": now_str(), "payload": payload,
                         "raw_tail": text[-8000:]})
    return {"ok": status == "ok", "status": status, "payload": payload,
            "changes": brief, "conflicts": conflicts,
            "gapp_390_adaptation": adaptation,
            "screen_review": screen_review,
            "summary": payload.get("summary") or [],
            "message": payload.get("message") or ""}


def _save_last_download(obj):
    try:
        out = os.path.join(DATA_DIR, "last-download.json")
        with io.open(out, "w", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False, indent=2, default=str))
        return out
    except Exception:
        return ""


def repair_mojibake(value):
    """修复少数 Gerrit/终端把 UTF-8 中文误按 GBK 解码后的文本。"""
    if not isinstance(value, str) or not value:
        return value
    # 典型特征：中文乱码中出现大量 GBK 解码产生的扩展汉字。
    if not any(x in value for x in ("瀹", "鏃", "锛", "浠", "绔", "鍏", "闂")):
        return value
    try:
        # 某些 Gerrit subject 中的符号在 GBK 不可表示，容忍并丢弃这些
        # 少量坏字节，优先保证中文主体恢复可读。
        fixed = value.encode("gbk", "ignore").decode("utf-8", "ignore")
        # 只有反解后确实减少乱码特征才采用，避免误伤正常中文。
        old_score = sum(value.count(x) for x in ("瀹", "鏃", "锛", "浠", "绔", "鍏", "闂"))
        new_score = sum(fixed.count(x) for x in ("瀹", "鏃", "锛", "浠", "绔", "鍏", "闂"))
        return fixed if fixed and new_score < old_score else value
    except (UnicodeEncodeError, UnicodeDecodeError):
        return value


@route("/api/port/conflict-analysis", ("POST",))
def api_conflict_analysis(q, body):
    """对当前仓库里未解决的冲突做一次现场分析（不需要 gerrit 输出）。"""
    b = body or {}
    repo = b.get("repo") or WORKSPACE
    if not os.path.isdir(repo):
        return {"ok": False, "error": "目录不存在: %s" % repo}
    # repo 工程的根目录本身不是 git 仓库（.git 在每个子项目里），
    # 直接在根上跑 git diff 会拿到空结果 —— 会被误判成「没有冲突」，所以要往下找。
    targets, changes = [], []
    if _is_git_worktree(repo):
        targets = [repo]
    else:
        targets = _find_git_repos(repo)
        if not targets:
            return {"ok": False,
                    "error": "%s 下没有找到 git 仓库；如果这是 repo 工程根，"
                             "请确认已 repo sync 或用「目标目录」指向具体仓库" % repo}
    for t in targets:
        files = _unmerged_files(t)
        if files:
            changes.append({"state": "conflict", "repo": t, "number": "-",
                            "subject": "工作区当前未解决冲突", "project": "",
                            "conflicted_files": files})
    reports = analyze_conflicts(changes)
    # 附带一份 markdown，方便丢给 codegraph / AI 继续分析
    md = _conflict_markdown(reports)
    out = os.path.join(DATA_DIR, "conflict-report.md")
    try:
        with io.open(out, "w", encoding="utf-8") as f:
            f.write(md)
    except Exception:
        out = ""
    return {"ok": True, "repo": repo, "scanned": targets,
            "files": [f for c in changes for f in c["conflicted_files"]],
            "reports": reports, "markdown": md, "report_path": out}


def _is_git_worktree(path):
    try:
        r = subprocess.run(["git", "-C", path, "rev-parse", "--is-inside-work-tree"],
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           universal_newlines=True, timeout=20)
        return (r.stdout or "").strip() == "true"
    except Exception:
        return False


def _find_git_repos(root, limit=400):
    """往下找 git 仓库（兼容 .git 目录 / .git 文件 / repo 工程的符号链接）。"""
    out = []
    root = os.path.abspath(root)
    skip = {".git", ".repo", "node_modules", "__pycache__", "out", "build_out"}
    for dirpath, dirnames, filenames in os.walk(root):
        if ".git" in dirnames or ".git" in filenames:
            out.append(dirpath)
            dirnames[:] = []
            if len(out) >= limit:
                break
            continue
        dirnames[:] = [d for d in dirnames if d not in skip]
    return out


def _unmerged_files(repo):
    try:
        r = subprocess.run(["git", "-C", repo, "diff", "--name-only",
                            "--diff-filter=U"],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           universal_newlines=True, errors="replace", timeout=60)
    except Exception:
        return []
    return [x.strip() for x in (r.stdout or "").splitlines() if x.strip()]


def _conflict_markdown(reports):
    lines = ["# 冲突分析报告", "", "生成时间：%s" % now_str(), ""]
    for r in reports:
        lines.append("## %s  (%s)" % (r.get("change"), r.get("repo_rel") or r.get("repo")))
        lines.append("仓库：`%s`" % r.get("repo"))
        lines.append("")
        for f in r.get("files", []):
            lines.append("### `%s`" % f["file"])
            lines.append("- 冲突块：%d" % f["regions"])
            if f.get("ours"):
                lines.append("- 本分支侧符号：%s" % "、".join(f["ours"]))
            if f.get("theirs"):
                lines.append("- change 侧符号：%s" % "、".join(f["theirs"]))
            if f.get("hotspots"):
                lines.append("- 引用点：")
                for h in f["hotspots"]:
                    lines.append("  - `%s:%s` %s" % (h["file"], h["line"], h["text"]))
            lines.append("")
        lines.append("### 处理建议")
        for a in r.get("advice", []):
            lines.append("- %s" % a)
        lines.append("")
    return "\n".join(lines)


@route("/api/build/start", ("POST",))
def api_build_start(q, body):
    b = body or {}
    exe = which("active-build")
    if not exe:
        return {"ok": False, "error": "找不到 active-build 命令（检查 ~/.local/bin 是否在 PATH）"}
    family = b.get("family") or ""
    product = b.get("product") or ""
    threads = str(b.get("threads") or "24")
    mode = str(b.get("mode") or "debug").strip().lower()
    if mode not in ("debug", "release"):
        return {"ok": False, "error": "编译类型只能是 debug 或 release"}
    if not family or not product:
        return {"ok": False, "error": "完整构建必须选择 family 和 project"}
    if not re.match(r"^\d+$", threads) or int(threads) < 1:
        return {"ok": False, "error": "线程数必须是正整数"}
    extra = (b.get("extra") or "").strip()
    build_dir = os.path.join(WORKSPACE, "build")

    # 写入 active-build 的激活状态，让无参执行时用这套配置
    if family and product:
        json_write(os.path.join(build_dir, ".active-build-state.json"), {
            "family": family, "project": product, "threads": threads,
            "mode": mode,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        })

    # 完整构建必须显式传入目标和构建类型，不能只依赖 build 状态文件。
    cmd = [exe, "-f", family, "-p", product, "-m", mode, "-j", threads]
    if extra:
        cmd.extend(_shlex(extra))
    # active-build 会在 manifest 与目标 XML 不一致时询问是否继续。
    # 网页任务没有交互终端，预先确认继续，并拒绝编译成功后的烧录询问，
    # 烧录仍由页面上的独立按钮负责。
    job = start_job("active-build", cmd, cwd=build_dir, input_text="y\nn\n")
    st = load_state()
    st["build"] = {"family": family, "product": product, "threads": threads,
                   "mode": mode,
                   "ok": False, "at": now_str()}
    save_state(st)
    return {"ok": True, "job": job.snapshot(), "cmd": job.cmd_str(),
            "cwd": build_dir}


def _shlex(s):
    import shlex as _s
    try:
        return _s.split(s)
    except Exception:
        return s.split()


@route("/api/build/result", ("POST",))
def api_build_result(q, body):
    b = body or {}
    ok = bool(b.get("ok"))
    st = load_state()
    st["build"]["ok"] = ok
    st["build"]["at"] = now_str()
    save_state(st)
    art = {}
    maps = find_maps()
    if maps:
        art = {"map": maps[0]["path"], "target": maps[0]["target"],
               "mtime": time.strftime("%Y-%m-%d %H:%M",
                                      time.localtime(maps[0]["mtime"]))}
        bdir = os.path.dirname(maps[0]["path"])
        for fn in os.listdir(bdir):
            if fn.endswith(".zip") and "fw_only" in fn:
                art["package"] = os.path.join(bdir, fn)
                art["package_size"] = human_bytes(os.path.getsize(
                    os.path.join(bdir, fn)))
                break
    return {"ok": True, "state": st, "artifacts": art}


@route("/api/flash/start", ("POST",))
def api_flash_start(q, body):
    b = body or {}
    exe = which("v3dl")
    if not exe:
        return {"ok": False, "error": "找不到 v3dl 命令（检查 ~/bin 是否在 PATH）"}
    mode = b.get("mode") or "app"
    args = [exe, mode]
    if b.get("file"):
        args.extend(["-f", b["file"]])
    if b.get("dirextory"):
        args.extend(["-d", b["dirextory"]])
    if not _sudo_ready():
        return {"ok": False,
                "error": "sudo 未授权，刷机可能卡在密码输入。请先在启动服务的终端里执行 `sudo -v`，再点重试。",
                "need_sudo": True}
    job = start_job("v3dl-%s" % mode, args, cwd=WORKSPACE)
    st = load_state()
    st["flash"] = {"done": False, "at": now_str(), "mode": mode}
    save_state(st)
    return {"ok": True, "job": job.snapshot(), "cmd": " ".join(args)}


@route("/api/flash/result", ("POST",))
def api_flash_result(q, body):
    ok = bool((body or {}).get("ok"))
    st = load_state()
    st["flash"] = {"done": ok, "at": now_str(),
                   "mode": (body or {}).get("mode", "app")}
    save_state(st)
    return {"ok": True, "state": st}


SELFTEST_ITEMS = [
    {"id": "boot", "label": "开机启动正常", "hint": "上电后能进表盘，无重启循环"},
    {"id": "display", "label": "屏幕显示 / 触摸", "hint": "无花屏、无残影，滑动跟手"},
    {"id": "feature", "label": "本次功能主流程", "hint": "按需求文档走一遍主路径"},
    {"id": "edge", "label": "边界 / 异常场景", "hint": "中断、超时、断连、低电"},
    {"id": "ble", "label": "蓝牙连接 App", "hint": "配对、同步数据、断连重连"},
    {"id": "sensor", "label": "相关传感器数据", "hint": "心率/IMU 等数值合理"},
    {"id": "power", "label": "功耗无异常", "hint": "静止待机电流在基线范围"},
]


@route("/api/selftest/items")
def api_selftest_items(q, body):
    st = load_state()
    saved = dict((i.get("id"), i) for i in (st.get("selftest", {}).get("items") or []))
    items = []
    pfs_items = (st.get("pfs") or {}).get("items") or []
    test_items = pfs_items or SELFTEST_ITEMS
    for base in test_items:
        it = dict(base)
        it["state"] = saved.get(base["id"], {}).get("state", "pending")
        it["note"] = saved.get(base["id"], {}).get("note", "")
        items.append(it)
    # 自动探测设备
    probe = {"device": None, "ports": []}
    exe = which("v3dl")
    if exe:
        try:
            r = subprocess.run([exe, "com"], stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, universal_newlines=True,
                               errors="replace", timeout=25)
            txt = r.stdout or ""
            ports = re.findall(r"(/dev/tty[A-Za-z0-9]+|COM\d+)", txt)
            probe["ports"] = sorted(set(ports))
            probe["device"] = bool(probe["ports"])
            probe["raw"] = txt.strip()[-600:]
        except Exception as exc:
            probe["error"] = str(exc)
    return {"ok": True, "items": items, "probe": probe}


@route("/api/selftest/save", ("POST",))
def api_selftest_save(q, body):
    b = body or {}
    items = b.get("items") or []
    st = load_state()
    done = bool(items) and all(i.get("state") == "ok" for i in items)
    st["selftest"] = {"items": items, "done": done, "at": now_str()}
    save_state(st)
    return {"ok": True, "done": done, "state": st}


@route("/api/job")
def api_job(q, body):
    jid = (q.get("id") or [""])[0]
    since = (q.get("since") or ["0"])[0]
    job = JOBS.get(jid)
    if not job:
        return {"ok": False, "error": "job 不存在或已过期"}
    return {"ok": True, "job": job.snapshot(since)}


# ---------------- 生命周期：页面登记 / 关闭即退出 ----------------
# 浏览器无法直接结束进程，所以让每个打开的页面用唯一 id 定期 ping 报到。
# 所有页面都 pagehide 后（或长期没心跳），看门狗让服务自己退出。
_LIFE = {
    "pages": {},        # page_id -> 最后一次心跳时间
    "registered": False,  # 至少有过一个页面，避免刚启动就退
    "empty_since": 0.0,   # 页面全部消失的起始时间
    "exit_after": 0,      # 0 表示从不自动退出
    "started": time.time(),
    "stale": 300,         # 超过这么久没心跳就当这个页面没了（后台标签页会被节流）
}
_LIFE_LOCK = threading.Lock()


def _page_id(q):
    """取页面 id。没有 id 说明只是探活（端口探测），不应该登记成页面。"""
    try:
        return (q.get("id") or [""])[0] or ""
    except Exception:
        return ""


@route("/api/ping")
def api_ping(q, body):
    """探活 + 页面报到。只有带 id 的请求才算“这个页面还开着”。"""
    pid = _page_id(q)
    now = time.time()
    with _LIFE_LOCK:
        if pid:
            _LIFE["pages"][pid] = now
            _LIFE["registered"] = True
            _LIFE["empty_since"] = 0.0
        alive = len(_LIFE["pages"])
    return {"ok": True, "pong": True, "time": now_str(),
            "workspace": WORKSPACE, "pid": os.getpid(),
            "auto_exit": _LIFE["exit_after"], "pages": alive}


@route("/api/bye", ("POST",))
def api_bye(q, body):
    """页面关闭时用 sendBeacon 注销自己。全部注销完才退出。"""
    pid = _page_id(q)
    with _LIFE_LOCK:
        if pid:
            _LIFE["pages"].pop(pid, None)
            if not _LIFE["pages"]:
                _LIFE["empty_since"] = time.time()
        left = len(_LIFE["pages"])
    return {"ok": True, "pages": left}


def start_lifecycle_watchdog():
    """所有页面关闭后延时退出。只在启动器带 --idle-exit 时启用。"""
    if _LIFE["exit_after"] <= 0:
        return
    grace = _LIFE["exit_after"]

    def loop():
        while True:
            time.sleep(2.0)
            now = time.time()
            with _LIFE_LOCK:
                # 后台标签页的定时器会被浏览器节流，超时就要放得很宽
                for k in [k for k, v in _LIFE["pages"].items()
                          if now - v > _LIFE["stale"]]:
                    _LIFE["pages"].pop(k, None)
                if _LIFE["registered"] and not _LIFE["pages"] \
                        and not _LIFE["empty_since"]:
                    _LIFE["empty_since"] = now
                empty_since = _LIFE["empty_since"]
                registered = _LIFE["registered"]
                started = _LIFE["started"]

            if empty_since and now - empty_since > grace:
                print("\n[lifecycle] 页面已全部关闭，服务退出")
                os._exit(0)
            if not registered and now - started > 600:
                print("\n[lifecycle] 启动后 10 分钟无人连接，服务退出")
                os._exit(0)

    t = threading.Thread(target=loop, name="lifecycle")
    t.daemon = True
    t.start()


@route("/api/reset", ("POST",))
def api_reset(q, body):
    save_state(json.loads(json.dumps(DEFAULT_STATE)))
    return {"ok": True, "state": load_state()}


# -- 只读命令执行（给 CodeGraph / git 排查冲突用） -------------------------
_ALLOWED_CMDS = {"codegraph", "git", "grep", "rg", "ls", "cat", "head", "tail"}
_GIT_READONLY = {"status", "log", "diff", "show", "blame", "grep", "rev-parse",
                 "branch", "remote", "ls-files", "cat-file", "diff-tree",
                 "describe"}


@route("/api/tool/run", ("POST",))
def api_tool_run(q, body):
    """执行只读排查命令。白名单 + git 只读子命令限制。"""
    b = body or {}
    cmd = (b.get("cmd") or "").strip()
    cwd = b.get("cwd") or WORKSPACE
    if not cmd:
        return {"ok": False, "error": "命令为空"}
    if not os.path.isdir(cwd):
        return {"ok": False, "error": "目录不存在: %s" % cwd}
    parts = cmd.split()
    first = parts[0]
    if first not in _ALLOWED_CMDS:
        return {"ok": False,
                "error": "只允许 %s 等只读排查命令" % "、".join(sorted(_ALLOWED_CMDS))}
    if which(first) is None:
        return {"ok": False, "error": "系统里没有 %s 命令" % first}
    if first == "git":
        # 支持 git -C <path> <sub>
        idx = 1
        while idx < len(parts) and parts[idx].startswith("-"):
            if parts[idx] == "-C":
                idx += 2
            else:
                idx += 1
        sub = parts[idx] if idx < len(parts) else ""
        if sub not in _GIT_READONLY:
            return {"ok": False, "error": "git 只允许只读子命令：%s"
                    % "、".join(sorted(_GIT_READONLY))}
    job = start_job(first, cmd, cwd=os.path.abspath(cwd), shell=True)
    return {"ok": True, "job": job.snapshot(), "cmd": cmd}


# --------------------------------------------------------------------------
# HTTP 服务
# --------------------------------------------------------------------------
MIME = {".html": "text/html; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".js": "application/javascript; charset=utf-8",
        ".json": "application/json; charset=utf-8",
        ".svg": "image/svg+xml",
        ".ico": "image/x-icon",
        ".png": "image/png",
        ".woff2": "font/woff2"}


_BENIGN_NET_ERRORS = ("ConnectionResetError", "ConnectionAbortedError",
                      "BrokenPipeError", "TimeoutError")


class SilentServer(ThreadingHTTPServer):
    """浏览器关页面 / 切端口探测会主动断连，这类异常不打印堆栈。"""

    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        name = type(exc).__name__
        if name in _BENIGN_NET_ERRORS:
            return
        # Windows 上是 ConnectionResetError(10054)，Linux 上是 104/32
        if isinstance(exc, OSError) and getattr(exc, "errno", None) in (32, 104, 10054, 10053):
            return
        super().handle_error(request, client_address)


class Handler(BaseHTTPRequestHandler):
    server_version = "FeaturePortal/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if os.environ.get("FEATURE_PORTAL_VERBOSE"):
            sys.stderr.write("[%s] %s\n" % (now_str(), fmt % args))

    def handle_one_request(self):
        """请求行读取阶段的断连统一吞掉，避免刷一屏 traceback。"""
        try:
            super().handle_one_request()
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            self.close_connection = True
        except OSError as exc:
            if getattr(exc, "errno", None) in (32, 104, 10053, 10054):
                self.close_connection = True
            else:
                raise

    # ---- helpers ----
    def _send(self, code, body_bytes, ctype="application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body_bytes)))
        self.send_header("Cache-Control", "no-store")
        # 允许从 file:// 或其它端口直接打开页面调试（本地工具，不涉及跨站风险）
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()
        try:
            self.wfile.write(body_bytes)
        except Exception:
            pass

    def _json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self._send(code, data)

    def _static(self, path):
        if path in ("/", ""):
            path = "/index.html"
        rel = path.lstrip("/")
        full = os.path.normpath(os.path.join(WEB_DIR, rel))
        if not full.startswith(WEB_DIR) or not os.path.isfile(full):
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        ext = os.path.splitext(full)[1].lower()
        try:
            with open(full, "rb") as f:
                data = f.read()
        except OSError:
            self._send(500, b"read error", "text/plain; charset=utf-8")
            return
        self._send(200, data, MIME.get(ext, "application/octet-stream"))

    def do_OPTIONS(self):
        self._send(204, b"")

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path.startswith("/api/"):
            fn = ROUTES.get(("GET", path))
            if not fn:
                self._json({"ok": False, "error": "no route %s" % path}, 404)
                return
            try:
                self._json(fn(parse_qs(parsed.query), None))
            except Exception as exc:
                traceback.print_exc()
                self._json({"ok": False, "error": str(exc),
                            "trace": traceback.format_exc()[-1200:]}, 500)
            return
        self._static(path)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        fn = ROUTES.get(("POST", path))
        if not fn:
            self._json({"ok": False, "error": "no route %s" % path}, 404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        body = {}
        if raw:
            try:
                body = json.loads(raw.decode("utf-8"))
            except Exception:
                body = {}
        try:
            self._json(fn(parse_qs(parsed.query), body))
        except Exception as exc:
            traceback.print_exc()
            self._json({"ok": False, "error": str(exc),
                        "trace": traceback.format_exc()[-1200:]}, 500)


def main():
    ap = argparse.ArgumentParser(description="功能移植工作台")
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--host", default="0.0.0.0",
                    help="监听地址，默认 0.0.0.0（WSL 里必须这样才能被 Windows 浏览器访问）")
    ap.add_argument("--workspace", help="代码工作区根目录")
    ap.add_argument("--idle-exit", type=int, default=0, metavar="SEC",
                    help="所有页面关闭后延时多少秒退出（启动器传 8；手动启动不要传）")
    ap.add_argument("--open", action="store_true",
                    help="启动后尝试自动打开浏览器（WSL 里走 interop）")
    args = ap.parse_args()

    _LIFE["exit_after"] = max(0, args.idle_exit or 0)

    global WORKSPACE
    if args.workspace:
                WORKSPACE = os.path.abspath(normalize_workspace_path(args.workspace))
    else:
        # 记住上次在页面上切换过的工作区
        try:
            sw = (load_state() or {}).get("workspace")
            if sw and os.path.isdir(sw):
                WORKSPACE = os.path.abspath(sw)
        except Exception:
            pass
    if not os.path.isdir(WEB_DIR):
        sys.stderr.write("缺少 web/ 目录：%s\n" % WEB_DIR)
        sys.exit(1)

    srv = None
    port = args.port
    for _ in range(20):
        try:
            srv = SilentServer((args.host, port), Handler)
            break
        except OSError as exc:
            if getattr(exc, "errno", None) in (48, 98, 10048, 10013):
                port += 1
                continue
            sys.stderr.write("启动失败：%s\n" % exc)
            sys.exit(1)
    if srv is None:
        sys.stderr.write("%d~%d 端口都被占用，请用 --port 指定其它端口\n"
                         % (args.port, port))
        sys.exit(1)
    srv.daemon_threads = True

    import socket as _sock
    try:
        local_ip = _sock.gethostbyname(_sock.gethostname())
    except Exception:
        local_ip = "127.0.0.1"
    if local_ip.startswith("127."):
        try:
            ss = _sock.socket(_sock.AF_INET, _sock.SOCK_DGRAM)
            ss.connect(("1.1.1.1", 80))
            local_ip = ss.getsockname()[0]
            ss.close()
        except Exception:
            pass

    print("=" * 68)
    print(" 功能移植工作台已启动")
    print("   浏览器打开 : http://127.0.0.1:%d" % port)
    if not local_ip.startswith("127.0.0.1"):
        print("   WSL IP     : http://%s:%d" % (local_ip, port))
    print("   监听       : %s:%d" % (args.host, port))
    print("   工作区     : %s" % WORKSPACE)
    print("   python     : %s" % sys.version.split()[0])
    print("   active-build : %s" % (which("active-build") or "未找到"))
    print("   v3dl         : %s" % (which("v3dl") or "未找到"))
    print("   gerrit_dl.py : %s" % (gerrit_script() or "未找到"))
    print("   repo 基线    : %s" % (
        "可用" if os.path.isfile(os.path.join(WORKSPACE, "repo_force_checkout.sh"))
        else "未找到 repo_force_checkout.sh"))
    print("=" * 68)
    print(" 提示：请在系统浏览器直接打开上面的地址；")
    print("       在 IDE 预览面板里以 file:// 打开会连不上后端。")
    if _LIFE["exit_after"]:
        print("       关闭所有页面后约 %d 秒服务自动退出" % _LIFE["exit_after"])
        print("       反复打开会自增端口，请从 http://127.0.0.1:%d 进入" % port)
    else:
        print("       Ctrl+C 停止")
    print("=" * 68)

    start_lifecycle_watchdog()
    if args.open:
        open_browser(port)

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


def open_browser(port):
    """WSL / Linux / Windows 上都尽力打开一次浏览器。"""
    url = "http://127.0.0.1:%d" % port
    for cmd in (["cmd.exe", "/c", "start", "", url],
                ["explorer.exe", url],
                ["xdg-open", url]):
        exe = which(cmd[0])
        if not exe:
            continue
        try:
            subprocess.Popen([exe] + cmd[1:],
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
            print("[open] 已请求系统浏览器打开 %s" % url)
            return True
        except Exception:
            continue
    return False


if __name__ == "__main__":
    main()

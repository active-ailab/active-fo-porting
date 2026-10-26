#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gerrit_download.py -- 按 Gerrit 链接（change / topic / merge）把代码 download 到本地仓库。

流程:
  解析链接 -> Gerrit REST 查 change 清单 -> 按依赖拓扑排序 -> 按 remote URL 定位本地仓库
  -> git fetch refs/changes/... -> cherry-pick 到当前分支 -> 冲突时输出现场信息交由 agent 解决。

子命令:
  plan     --url <链接|change号|Change-Id|topic:xxx> [--config C]
           只查询并列出 change 清单 + 定位到的本地仓库，不做任何 git 操作。
  precheck --url <...> [--workspace WS,WS2] [--repo <仓库路径>] [--config C]
           只读预检：每个 change 落到哪个仓库、该仓库当前分支 / 有没有未提交改动 /
           有没有未完成的 cherry-pick / change 是否已合入，提前说明 download 会不会失败。
  download --url <...> [--workspace WS,WS2] [--repo <仓库路径>] [--config C]
           执行 fetch + cherry-pick。单条失败不会中断后面的 change；
           冲突先尝试自动消解（单侧为空取另一侧 -> -X theirs），仍不行才报冲突。
  snapshot --url <...> --dest <备份目录> [--workspace WS,WS2] [--config C]
           把 topic 各 change 的完整文件树导出到备份目录（git archive，不动工作区），
           供代码评估拿这份副本和本地代码做比较。

配置来源(按序): --config / $GERRIT_CONFIG /
  /home/hgw/shaolu/aitools/mcp-servers/config/gerrit_config.json
  //wsl.localhost/Ubuntu-20.04/home/hgw/shaolu/aitools/mcp-servers/config/gerrit_config.json
  <skill目录>/gerrit.config
支持键: GERRIT_BASE_URL|gerrit_url, USERNAME|gerrit_user, HTTP_PASSWORD|gerrit_http_pass, VERIFY_TLS
环境变量覆盖: GERRIT_URL / GERRIT_USER / GERRIT_HTTP_PASS

输出: stdout JSON, status: ok / conflict / error
设计兼容 Python 3.8 (WSL Ubuntu-20.04 自带)。
"""
import argparse
import base64
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_CONFIGS = [
    "/home/hgw/shaolu/aitools/mcp-servers/config/gerrit_config.json",
    "//wsl.localhost/Ubuntu-20.04/home/hgw/shaolu/aitools/mcp-servers/config/gerrit_config.json",
    os.path.normpath(os.path.join(_SCRIPT_DIR, "..", "gerrit.config")),
]
_DEFAULT_WORKSPACE = ["/home/hgw/workspace", "/home/hgw/monaco", "/home/hgw/master"]
_SKIP_DIRS = {".git", ".repo", "node_modules", "__pycache__", ".gradle", ".ccache", ".svn", "out"}
_URL_RE = re.compile(r"^\s*url\s*=\s*(\S+)", re.M)
_REST_OPTS = "o=CURRENT_REVISION&o=CURRENT_COMMIT"
_FETCH_TIMEOUT = 600


def emit(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def die(msg, **extra):
    out = {"status": "error", "message": msg}
    out.update(extra)
    emit(out)
    sys.exit(1)


# ----------------------------- config -----------------------------
def load_config(explicit=None):
    candidates = []
    if explicit:
        candidates.append(explicit)
    env_cfg = os.environ.get("GERRIT_CONFIG")
    if env_cfg:
        candidates.append(env_cfg)
    candidates.extend(_DEFAULT_CONFIGS)
    raw = {}
    used = None
    for p in candidates:
        if p and os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                used = p
                break
            except Exception as e:
                die("解析配置 %s 失败: %s" % (p, e))
    if not raw:
        die("未找到 gerrit 配置文件，用 --config 指定。尝试过: %s" % ", ".join(candidates))
    base = (os.environ.get("GERRIT_URL") or raw.get("GERRIT_BASE_URL")
            or raw.get("gerrit_url") or "").rstrip("/")
    user = (os.environ.get("GERRIT_USER") or raw.get("USERNAME") or raw.get("gerrit_user") or "")
    pw = (os.environ.get("GERRIT_HTTP_PASS") or raw.get("HTTP_PASSWORD")
          or raw.get("gerrit_http_pass") or "")
    verify = raw.get("VERIFY_TLS", True)
    if not base or not user or not pw:
        die("配置不完整(base_url/username/http_password)，检查 %s" % used)
    return {"base_url": base, "user": user, "pw": pw, "verify_tls": bool(verify), "config_path": used}


# ----------------------------- Gerrit REST -----------------------------
class Gerrit(object):
    def __init__(self, cfg):
        self.cfg = cfg
        token = base64.b64encode(("%s:%s" % (cfg["user"], cfg["pw"])).encode()).decode()
        self.auth = "Basic " + token
        if not cfg["verify_tls"]:
            import ssl
            self.ctx = ssl._create_unverified_context()
        else:
            self.ctx = None

    def get(self, path, timeout=30):
        if not path.startswith("/a/"):
            path = "/a" + path
        url = self.cfg["base_url"] + path
        req = urllib.request.Request(url)
        req.add_header("Authorization", self.auth)
        req.add_header("Accept", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=self.ctx) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError("Gerrit REST HTTP %s for %s: %s" % (e.code, path, body[:400]))
        except urllib.error.URLError as e:
            raise RuntimeError("Gerrit REST 网络错误 for %s: %s" % (path, e))

    def rest_json(self, path):
        raw = self.get(path)
        if raw.startswith(")]}'"):
            raw = raw[4:]
        raw = raw.strip()
        if not raw:
            return []
        return json.loads(raw)


# ----------------------------- 链接解析 -----------------------------
def parse_link(s):
    s = s.strip()
    m = re.search(r"/c/(.+?)/\+/(\d+)(?:/(\d+))?", s)
    if m:
        return {"type": "change", "number": int(m.group(2)),
                "patchset": int(m.group(3)) if m.group(3) else None,
                "project_hint": m.group(1)}
    m = re.search(r"#/c/(\d+)(?:/(\d+))?", s)
    if m:
        return {"type": "change", "number": int(m.group(1)),
                "patchset": int(m.group(2)) if m.group(2) else None, "project_hint": None}
    m = re.search(r"topic[:=]([^&\s]+)", s)
    if m:
        return {"type": "topic", "topic": m.group(1)}
    m = re.search(r"\b(I[0-9a-f]{40})\b", s)
    if m:
        return {"type": "change-id", "change_id": m.group(1)}
    if re.fullmatch(r"\d{3,}", s):
        return {"type": "change", "number": int(s), "patchset": None, "project_hint": None}
    die("无法识别链接/参数: %s（支持 change 链接、topic 链接、纯 change 号、Change-Id、topic:名字）" % s)


# ----------------------------- change 查询与排序 -----------------------------
def _norm_change(info, patchset_hint):
    rev = info.get("current_revision")
    revisions = info.get("revisions", {})
    rev_info = revisions.get(rev, {})
    ps = rev_info.get("_number")
    if patchset_hint and patchset_hint != ps:
        for k, v in revisions.items():
            if v.get("_number") == patchset_hint:
                rev, ps, rev_info = k, patchset_hint, v
                break
    commit = rev_info.get("commit", {})
    return {
        "number": info.get("_number"),
        "project": info.get("project"),
        "branch": info.get("branch"),
        "subject": info.get("subject", ""),
        "change_id": info.get("change_id", ""),
        "topic": info.get("topic"),
        "status": info.get("status"),
        "revision": rev,
        "patchset": ps,
        "parents": [p.get("commit") for p in commit.get("parents", [])],
    }


def fetch_changes(g, link):
    if link["type"] == "topic":
        # 链接里的 topic 可能带 URL 编码(%E8...)和引号包裹(%22...%22)，先解码再剥引号
        t = urllib.parse.unquote(link["topic"]).strip("\"'")
        tq = urllib.parse.quote(t, safe="")
        seen, changes = {}, []
        for q in ("topic:%s" % tq, "topic:%s+status:merged" % tq):
            try:
                lst = g.rest_json("/changes/?q=%s&%s" % (q, _REST_OPTS))
            except RuntimeError as e:
                if "HTTP 404" in str(e):
                    continue
                raise
            for info in lst:
                n = info.get("_number")
                if n not in seen:
                    seen[n] = True
                    changes.append(_norm_change(info, None))
        if not changes:
            die("topic '%s' 下没有查到任何 change" % t)
        return changes
    if link["type"] == "change":
        q = "change:%d" % link["number"]
    else:
        q = "change:%s" % link["change_id"]
    lst = g.rest_json("/changes/?q=%s&%s" % (q, _REST_OPTS))
    if not lst:
        die("没有查到 change: %s" % q)
    return [_norm_change(lst[0], link.get("patchset"))]


def order_changes(changes):
    """按父子依赖拓扑排序（同 topic 内父 change 先合入），无依赖时按 change 号稳定排序。"""
    by_rev = {}
    for c in changes:
        by_rev[c["revision"]] = c
    ordered, remaining = [], sorted(changes, key=lambda c: c["number"])
    while remaining:
        moved = False
        for c in list(remaining):
            deps = [by_rev[p] for p in c["parents"] if p in by_rev and by_rev[p] is not c]
            if all(d in ordered for d in deps):
                ordered.append(c)
                remaining.remove(c)
                moved = True
        if not moved:
            ordered.extend(remaining)
            break
    return ordered


# ----------------------------- 本地仓库定位 -----------------------------
def _strip_tail(s, tail):
    if s.endswith(tail):
        return s[: -len(tail)]
    return s


def norm_url(u):
    return _strip_tail(u.strip().rstrip("/"), ".git").rstrip("/")


def iter_git_repos(root):
    """遍历 root 下所有 git 仓库，yield (repo_path, [remote_urls])。支持 .git 目录与 .git 文件(gitdir:)。"""
    root = os.path.abspath(root)
    if not os.path.isdir(root):
        return
    for dirpath, dirnames, filenames in os.walk(root):
        # 注意: 必须先记录 .git 形态再裁剪 dirnames（.git 在 _SKIP_DIRS 里）
        has_git_dir = ".git" in dirnames      # .git 目录或指向目录的符号链接(repo 工程)
        has_git_file = ".git" in filenames    # .git 文件(gitdir: 指针)
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        cfgp = None
        if has_git_dir:
            cfgp = os.path.join(dirpath, ".git", "config")
        elif has_git_file:
            gp = os.path.join(dirpath, ".git")
            try:
                with open(gp, "r", encoding="utf-8", errors="replace") as f:
                    first = f.readline().strip()
                if first.lower().startswith("gitdir:"):
                    gd = first.split(":", 1)[1].strip()
                    cfgp = os.path.normpath(os.path.join(dirpath, gd, "config"))
            except Exception:
                pass
        if cfgp and os.path.exists(cfgp):
            try:
                with open(cfgp, "r", encoding="utf-8", errors="replace") as f:
                    text = f.read()
                urls = _URL_RE.findall(text)
            except Exception:
                urls = []
            if urls:
                yield dirpath, [norm_url(u) for u in urls]
                # 有 remote 才是可用于 download 的项目仓库；repo 工程根目录
                # 可能自带一个无 remote 的 .git，不能因此阻断子仓库扫描。
                dirnames[:] = []  # 不再深入该仓库内部


def scan_roots(roots):
    repos = []
    for root in roots:
        repos.extend(iter_git_repos(root))
    return repos


def _project_aliases(project):
    """返回用于本地匹配的项目路径别名。

    Gerrit 上的 project 经常带有产品树前缀，例如
    ``firmware/huamiOS/packages/services/sport``，而 repo 工程的本地目录
    是 ``packages/services/sport``。本地下载应以 huamiOS 后面的目录为准。
    """
    raw = project.strip("/").replace("\\", "/").lower()
    aliases = [raw]
    parts = raw.split("/")
    for i, part in enumerate(parts):
        if part == "huamios" and i + 1 < len(parts):
            aliases.append("/".join(parts[i + 1:]))
            break
    # 兼容其它 manifest/产品前缀；至少保留两级，避免只用 sport 造成误匹配。
    for i in range(1, max(1, len(parts) - 1)):
        aliases.append("/".join(parts[i:]))
    return list(dict.fromkeys(a for a in aliases if a))


def match_repos(scanned, project, roots=None):
    """匹配优先级: 1) remote URL(去 .git) 与 project 完全一致(不区分大小写)
    2) 去掉 project 若干前缀段后的后缀匹配(容错 Gerrit/manifest 前缀差异, 如 firmware/)
    3) 末段目录名弱匹配。每级都要求唯一命中才返回。"""
    aliases = _project_aliases(project)
    exact, suffix, weak = [], [], []
    for path, urls in scanned:
        hit = None
        # 先比较 remote URL。别名匹配使 firmware/huamiOS 前缀不会挡住下载。
        for u in urls:
            un = u.lower()
            for rank, alias in enumerate(aliases):
                if un == alias or un.endswith("/" + alias):
                    cand = (path, 0 if rank == 0 else 1)
                    if hit is None or cand[1] < hit[1]:
                        hit = cand
                    break
        # repo 工程可能没有 remote，或 remote 命名与 Gerrit project 不同。
        # 此时按工作区内的目录（尤其是 huamiOS 后面的目录）定位。
        if hit is None and roots:
            ap = os.path.abspath(path).replace("\\", "/").lower().rstrip("/")
            for root in roots:
                ar = os.path.abspath(root).replace("\\", "/").lower().rstrip("/")
                if ap == ar or not ap.startswith(ar + "/"):
                    continue
                rel = ap[len(ar) + 1:]
                for rank, alias in enumerate(aliases):
                    if rel == alias or rel.endswith("/" + alias):
                        hit = (path, 0 if rank == 0 else 1)
                        break
                if hit is not None:
                    break
        if hit is None:
            for u in urls:
                if u.lower().rsplit("/", 1)[-1] == aliases[0].rsplit("/", 1)[-1]:
                    hit = (path, 99)
                    break
        if hit is None:
            continue
        if hit[1] == 0:
            exact.append(path)
        elif hit[1] < 99:
            suffix.append(path)
        else:
            weak.append(path)
    return exact, suffix, weak


def _project_suffixes(project):
    segs = project.strip("/").split("/")
    return ["/".join(segs[i:]) for i in range(len(segs))]


def repo_project_hint(roots, project):
    """项目在工作区里没落地时，看看 .repo 里是不是其实已经有它。
    repo 工程常见状态：.repo/projects/<path>.git 有对象，但工作区那份目录没有 .git 链接，
    于是 git 命令全部失效、定位不到仓库 —— 这种要把话说清楚，否则看起来像"仓库不存在"。"""
    for root in roots or []:
        rp = os.path.join(root, ".repo")
        if not os.path.isdir(rp):
            continue
        for suf in _project_suffixes(project):
            gd = os.path.join(rp, "projects", suf + ".git")
            if not os.path.isdir(gd):
                continue
            wt = os.path.join(root, suf)
            if os.path.exists(os.path.join(wt, ".git")):
                continue
            return ("%s/.repo 里已有该项目(%s)的 git 对象，但工作区目录 %s 没有 .git 链接，"
                    "git 命令在它上面等于无效，所以定位不到。"
                    "先在该工作区执行 `repo sync %s` 重建链接（或重新 repo sync 整个工程），"
                    "再执行 download。" % (root, suf, wt, suf))
    return None


def locate_repo(scanned_by_root, c, roots=None):
    """按 root 顺序(workspace, monaco, master 兜底)定位仓库。返回 (repo_path_or_None, note)"""
    for scanned in scanned_by_root:
        exact, suffix, weak = match_repos(scanned, c["project"], roots)
        if len(exact) == 1:
            return exact[0], None
        if len(exact) > 1:
            return None, "project %s 在本地命中多个仓库，请用 --repo 指定: %s" % (c["project"], exact)
        if len(suffix) == 1:
            return suffix[0], "按路径后缀匹配到 %s (project=%s)，请核对" % (suffix[0], c["project"])
        if len(suffix) > 1:
            return None, "project %s 按路径后缀匹配到多个仓库: %s" % (c["project"], suffix)
        if len(weak) == 1:
            return weak[0], "按末段目录名弱匹配到 %s (project=%s)，请核对" % (weak[0], c["project"])
        if len(weak) > 1:
            return None, "project %s 按末段名弱匹配到多个仓库: %s" % (c["project"], weak)
    return None, "本地没有找到 project=%s 对应的仓库（扫描了 %s），请确认仓库已 checkout 或用 --repo 指定" % (
        c["project"], ", ".join(roots or _DEFAULT_WORKSPACE))


# ----------------------------- git 操作 -----------------------------
def git(repo, *args):
    p = subprocess.run(["git", "-C", repo] + list(args),
                       capture_output=True, text=True, timeout=_FETCH_TIMEOUT)
    return p


# ----------------------------- 冲突自动消解 -----------------------------
_C_START = re.compile(r"^<{7}(?:\s|$)")
_C_MID = re.compile(r"^={7}\s*$")
_C_END = re.compile(r"^>{7}(?:\s|$)")


def resolve_trivial_conflicts(path):
    """把「一侧为空」的冲突块自动取非空那侧。

    只处理能明确判断的情况（比如本分支删了这段、change 侧新增），
    两侧都有内容的原样保留给人工/后续策略。返回 (解决块数, 剩余块数)。
    """
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except Exception:
        return 0, 0
    text = raw.decode("utf-8", "surrogateescape")
    lines = text.splitlines(True)
    out, i, n = [], 0, len(lines)
    solved = left = 0
    while i < n:
        if not _C_START.match(lines[i]):
            out.append(lines[i])
            i += 1
            continue
        j = i + 1
        ours, theirs, cur = [], [], None
        closed = False
        while j < n:
            if _C_MID.match(lines[j]):
                cur = theirs
            elif _C_END.match(lines[j]):
                closed = True
                j += 1
                break
            elif _C_START.match(lines[j]):
                break                       # 畸形嵌套，不碰
            elif cur is None:
                ours.append(lines[j])
            else:
                theirs.append(lines[j])
            j += 1
        if not closed:
            out.extend(lines[i:j])
            i = j
            continue
        has_ours = any(x.strip() for x in ours)
        has_theirs = any(x.strip() for x in theirs)
        if not has_ours and has_theirs:
            out.append("".join(theirs))
            solved += 1
        elif has_ours and not has_theirs:
            out.append("".join(ours))
            solved += 1
        else:
            left += 1
            out.extend(lines[i:j])
        i = j
    if solved:
        with open(path, "wb") as f:
            f.write("".join(out).encode("utf-8", "surrogateescape"))
    return solved, left


def unmerged_files(repo):
    r = git(repo, "diff", "--name-only", "--diff-filter=U")
    return [x.strip() for x in (r.stdout or "").splitlines() if x.strip()]


def finish_pick(repo):
    """把已经解决完的冲突提交掉。返回 (ok, err)。"""
    git(repo, "add", "-A")
    r = git(repo, "-c", "core.editor=true", "cherry-pick", "--continue")
    if r.returncode == 0:
        return True, ""
    return False, ((r.stderr or "") + (r.stdout or "")).strip()[:300]


def abort_pick(repo):
    git(repo, "cherry-pick", "--abort")


def try_trivial(repo, files):
    """对一批冲突文件逐个做「单侧为空」消解，返回 (已完全解决的文件, 仍有冲突的文件)。"""
    clean, dirty, touched = [], [], []
    for rel in files:
        p = os.path.join(repo, rel)
        solved, left = resolve_trivial_conflicts(p)
        if solved:
            touched.append(rel)
        if left:
            dirty.append(rel)
        elif solved:
            clean.append(rel)
    return clean, dirty, touched


def _pick(repo, sha, strategy=None):
    """执行 cherry-pick（可按策略 -X）。返回 (CompletedProcess, 是否 merge commit)。"""
    parents = git(repo, "rev-list", "--parents", "-n", "1", sha).stdout.split()
    np = max(len(parents) - 1, 0)
    args = ["cherry-pick"]
    if strategy:
        args += ["-X", strategy]
    if np > 1:
        args += ["-m", "1"]                # merge commit: 沿主线父合并
    args.append(sha)
    return git(repo, *args), np


def download_one(repo, c, remote, cfg, leave_on_conflict=True):
    """对单个 change 在单个仓库执行 fetch + cherry-pick（含自动解冲突）。

    leave_on_conflict=False 时，冲突最终没解决会 abort 掉，让同仓库后面的 change
    也能继续跑 —— 除了记录一条冲突，不会把整条流水线卡死。
    """
    n = c["number"]
    ps = c["patchset"] or "current"
    ref = "refs/changes/%02d/%d/%s" % (n % 100, n, ps)
    entry = {"number": n, "project": c["project"], "branch": c["branch"],
             "subject": c["subject"], "patchset": c["patchset"], "sha": c["revision"],
             "ref": ref, "repo": repo, "state": "error", "conflicted_files": [],
             "auto_resolved": [], "tried": [],
             "head_before": None, "head_after": None, "message": ""}

    if git(repo, "rev-parse", "--is-inside-work-tree").returncode != 0:
        entry["message"] = "%s 不是有效的 git 工作区" % repo
        return entry

    st = git(repo, "status", "--porcelain")
    if st.returncode != 0:
        entry["message"] = "git status 失败: %s" % (st.stderr or st.stdout).strip()[:300]
        return entry
    if st.stdout.strip():
        entry["message"] = ("工作区有未提交改动 %d 项，本条跳过（不影响其它 change）："
                            "先 commit 或 git stash\n%s"
                            % (len(st.stdout.strip().splitlines()),
                               "\n".join(st.stdout.strip().splitlines()[:10])))
        return entry

    if git(repo, "rev-parse", "-q", "--verify", "CHERRY_PICK_HEAD").returncode == 0:
        entry["message"] = ("上次 cherry-pick 未完成（CHERRY_PICK_HEAD 存在），本条跳过；"
                            "解决后 git cherry-pick --continue，或 git cherry-pick --abort 后重跑")
        return entry

    hb = git(repo, "rev-parse", "HEAD")
    if hb.returncode != 0:
        entry["message"] = "git rev-parse HEAD 失败: %s" % (hb.stderr or "").strip()[:300]
        return entry
    entry["head_before"] = hb.stdout.strip()

    fr = git(repo, "fetch", remote, ref)
    if fr.returncode != 0:
        # 回退: 用配置里的 HTTP 地址 + 认证直接 fetch
        auth_url = "%s/a/%s" % (cfg["base_url"], c["project"])
        auth_url = auth_url.replace("://", "://%s:%s@" % (cfg["user"], urllib.parse.quote(cfg["pw"], safe="")), 1)
        fr2 = git(repo, "fetch", auth_url, ref)
        if fr2.returncode != 0:
            entry["message"] = "git fetch %s 失败(经 remote=%s 与认证 HTTP 均失败): %s" % \
                (ref, remote, ((fr.stderr or fr2.stderr or "").strip())[:400])
            return entry
    sha = git(repo, "rev-parse", "FETCH_HEAD").stdout.strip()
    entry["sha"] = sha

    anc = git(repo, "merge-base", "--is-ancestor", sha, "HEAD")
    if anc.returncode == 0:
        entry["state"] = "already_applied"
        entry["message"] = "该 commit 已在当前分支历史中，跳过"
        return entry

    res, np = _pick(repo, sha)
    entry["tried"].append("cherry-pick")
    if res.returncode == 0:
        entry["state"] = "applied"
        entry["head_after"] = git(repo, "rev-parse", "HEAD").stdout.strip()
        entry["message"] = ("cherry-pick 成功(merge commit, -m 1)" if np > 1 else "cherry-pick 成功")
        return entry

    out = (res.stdout or "") + (res.stderr or "")
    if re.search(r"cherry-pick is now empty|was already applied", out, re.I):
        sk = git(repo, "cherry-pick", "--skip")
        if sk.returncode == 0:
            entry["state"] = "already_applied"
            entry["message"] = "cherry-pick 结果为空(内容已在当前分支)，已 --skip"
            return entry
        entry["message"] = "cherry-pick 为空且 --skip 失败: %s" % ((sk.stderr or "").strip()[:300])
        return entry

    entry["conflicted_files"] = unmerged_files(repo)
    if not entry["conflicted_files"]:
        entry["message"] = "cherry-pick 失败（没有冲突文件）：%s" % out.strip()[:300]
        return entry

    # ---- 策略 1：冲突块只有一侧有内容 -> 直接取那一侧 ----
    clean, dirty, touched = try_trivial(repo, entry["conflicted_files"])
    entry["tried"].append("单侧为空自动取另一侧")
    if not dirty:
        ok, err = finish_pick(repo)
        if ok:
            entry["state"] = "applied_auto"
            entry["auto_resolved"] = touched
            entry["head_after"] = git(repo, "rev-parse", "HEAD").stdout.strip()
            entry["message"] = ("冲突已自动解决（冲突块单侧为空，取另一侧）：%s。"
                                "请复核这些文件" % ", ".join(touched))
            return entry
        entry["message"] = "冲突消解后提交失败：%s" % err

    # ---- 策略 2：整体按 change 侧重来一遍（-X theirs） ----
    abort_pick(repo)
    res2, _ = _pick(repo, sha, "theirs")
    entry["tried"].append("cherry-pick -X theirs")
    if res2.returncode == 0:
        entry["state"] = "applied_auto"
        entry["auto_resolved"] = entry["conflicted_files"]
        entry["head_after"] = git(repo, "rev-parse", "HEAD").stdout.strip()
        entry["message"] = ("冲突按 change 侧整体解决（-X theirs）：%s。"
                            "本地侧的改动被让掉了，务必逐个 review"
                            % ", ".join(entry["conflicted_files"]))
        return entry

    left = unmerged_files(repo)
    out2 = (res2.stdout or "") + (res2.stderr or "")
    if not left and re.search(r"cherry-pick is now empty|was already applied", out2, re.I):
        git(repo, "cherry-pick", "--skip")
        entry["state"] = "already_applied"
        entry["message"] = "冲突侧重来后结果为空（内容已在当前分支），已 --skip"
        return entry
    if not left:
        abort_pick(repo)
        entry["state"] = "error"
        entry["message"] = "cherry-pick 失败但没有留下冲突文件：%s" % out2.strip()[:300]
        return entry
    if left:
        clean2, dirty2, touched2 = try_trivial(repo, left)
        if not dirty2:
            ok, err = finish_pick(repo)
            if ok:
                entry["state"] = "applied_auto"
                entry["auto_resolved"] = entry["conflicted_files"] + touched2
                entry["head_after"] = git(repo, "rev-parse", "HEAD").stdout.strip()
                entry["message"] = "冲突已自动解决（-X theirs + 单侧为空）：%s" % ", ".join(touched2)
                return entry
        left = dirty2 or left

    entry["state"] = "conflict"
    entry["conflicted_files"] = left or entry["conflicted_files"]
    if leave_on_conflict:
        entry["message"] = ("冲突无法自动解决，现场已保留（cherry-pick 未完成）：%s。"
                            "解决后 git add + git cherry-pick --continue，"
                            "或 git cherry-pick --abort 后重跑（已合入的会跳过）"
                            % ", ".join(entry["conflicted_files"]))
    else:
        abort_pick(repo)
        entry["state"] = "conflict_aborted"
        entry["message"] = ("冲突无法自动解决，已撤销本次 pick（git cherry-pick --abort），"
                            "好让同仓库后面的 change 继续跑。冲突文件：%s"
                            % ", ".join(entry["conflicted_files"]))
    return entry


# ----------------------------- 子命令 -----------------------------
def inspect_repo(repo, c):
    """只读预检：不做 fetch/cherry-pick，只报告仓库会不会挡住 download。
    返回 blockers(一定会失败) / warnings(可能影响结果) / facts。"""
    facts = {"repo": repo, "branch": "", "detached": False, "head": "",
             "dirty": [], "cherry_pick_head": False, "has_commit": False}
    blockers, warnings = [], []
    if not repo:
        return facts, ["没有定位到仓库"], warnings
    if not os.path.isdir(repo):
        return facts, ["仓库目录不存在: %s" % repo], warnings
    if git(repo, "rev-parse", "--is-inside-work-tree").returncode != 0:
        return facts, ["%s 不是有效的 git 工作区" % repo], warnings

    br = git(repo, "branch", "--show-current")
    facts["branch"] = (br.stdout or "").strip()
    if not facts["branch"]:
        facts["detached"] = True
        warnings.append("HEAD 处于 detached 状态（repo sync 常见），cherry-pick 会落在游离 HEAD 上，"
                        "切分支后再执行更安全")
    hb = git(repo, "rev-parse", "HEAD")
    facts["head"] = (hb.stdout or "").strip()

    st = git(repo, "status", "--porcelain")
    dirty = [l.strip() for l in (st.stdout or "").splitlines() if l.strip()]
    facts["dirty"] = dirty
    if dirty:
        blockers.append("工作区有未提交改动 %d 项，download 会直接拒绝；先 commit/stash" % len(dirty))

    if git(repo, "rev-parse", "-q", "--verify", "CHERRY_PICK_HEAD").returncode == 0:
        facts["cherry_pick_head"] = True
        blockers.append("上次 cherry-pick 未完成（CHERRY_PICK_HEAD 存在），"
                        "先解决冲突或 git cherry-pick --abort")

    # 该 change 的 commit 是否已在当前分支历史里（已合入则 download 会跳过）
    sha = c.get("revision")
    if sha and git(repo, "cat-file", "-e", sha + "^{commit}").returncode == 0:
        facts["has_commit"] = True
        if git(repo, "merge-base", "--is-ancestor", sha, "HEAD").returncode == 0:
            warnings.append("该 change 已在当前分支历史中，download 会跳过（already_applied）")

    if facts["branch"] and c.get("branch") and facts["branch"] != c["branch"]:
        warnings.append("change 目标是 %s，本地当前在 %s —— 跨分支 cherry-pick，冲突概率高"
                        % (c["branch"], facts["branch"]))
    return facts, blockers, warnings


def cmd_precheck(args, cfg, link):
    """只读预检：定位仓库 + 报告每个仓库会不会挡住 download。不产生任何 git 变更。"""
    g = Gerrit(cfg)
    changes = order_changes(fetch_changes(g, link))
    roots = [w.strip() for w in (args.workspace or ",".join(_DEFAULT_WORKSPACE)).split(",") if w.strip()]
    scanned_by_root = [scan_roots([r]) for r in roots]

    out, blocked = [], 0
    for c in changes:
        repo, note = locate_repo(scanned_by_root, c, roots)
        facts, blockers, warnings = inspect_repo(repo, c)
        if repo is None:
            hint = repo_project_hint(roots, c["project"])
            if hint:
                blockers = list(blockers) + [hint]
        if blockers:
            blocked += 1
        out.append({
            "number": c["number"], "project": c["project"], "subject": c["subject"],
            "branch": c["branch"], "patchset": c["patchset"],
            "repo": repo, "note": note or "",
            "facts": facts, "blockers": blockers, "warnings": warnings,
        })
    emit({"status": "ok", "mode": "precheck", "gerrit": cfg["base_url"],
          "workspace_roots": roots, "link_type": link["type"], "topic": link.get("topic"),
          "count": len(out), "blocked": blocked, "changes": out,
          "message": "预检完成：%d 个 change，%d 个仓库会挡住 download" % (len(out), blocked)
                     if blocked else "预检完成：%d 个 change，没有发现阻塞项" % len(out)})
    sys.exit(0)


def cmd_plan(args, cfg, link):
    g = Gerrit(cfg)
    changes = order_changes(fetch_changes(g, link))
    roots = [w.strip() for w in (args.workspace or ",".join(_DEFAULT_WORKSPACE)).split(",") if w.strip()]
    scanned_by_root = []
    for r in roots:
        scanned_by_root.append(scan_roots([r]))
    out_changes = []
    for c in changes:
        repo, note = locate_repo(scanned_by_root, c, roots)
        out_changes.append({
            "number": c["number"], "project": c["project"], "branch": c["branch"],
            "subject": c["subject"], "patchset": c["patchset"], "sha": c["revision"],
            "ref": "refs/changes/%02d/%d/%s" % (c["number"] % 100, c["number"], c["patchset"] or "current"),
            "repo": repo, "note": note or "",
        })
    emit({"status": "ok", "mode": "plan", "gerrit": cfg["base_url"], "config": cfg["config_path"],
          "workspace_roots": roots, "link_type": link["type"],
          "topic": link.get("topic"), "count": len(out_changes), "changes": out_changes,
          "message": "plan 完成，未做任何 git 操作"})
    sys.exit(0)


def cmd_download(args, cfg, link):
    """把 topic 的 change 逐个 cherry-pick 到本地。

    原则：**一条失败不影响后面**。定位不到仓库、工作区脏、fetch 失败都只记在
    那一条上，接着处理下一条；同仓库最后一条如果冲突没解决会保留现场，
    中间的冲突则会 abort 掉，让后面的 change 还能继续。
    """
    g = Gerrit(cfg)
    changes = order_changes(fetch_changes(g, link))
    roots = [w.strip() for w in (args.workspace or ",".join(_DEFAULT_WORKSPACE)).split(",") if w.strip()]
    scanned_by_root = [scan_roots([r]) for r in roots]

    # 先把每个 change 定位到仓库（纯读，无 git 变更），用来算「同仓库还有几条」
    located = []
    for c in changes:
        if args.repo and len(changes) == 1:
            located.append((os.path.abspath(args.repo), None))
        else:
            located.append(locate_repo(scanned_by_root, c, roots))
    left_in_repo = {}
    for repo, _note in located:
        left_in_repo[repo] = left_in_repo.get(repo, 0) + 1

    result_changes = []
    repo_remote = {}

    for c, (repo, note) in zip(changes, located):
        entry = {"number": c["number"], "project": c["project"], "branch": c["branch"],
                 "subject": c["subject"], "patchset": c["patchset"], "sha": c["revision"],
                 "ref": "refs/changes/%02d/%d/%s" % (c["number"] % 100, c["number"], c["patchset"] or "current"),
                 "repo": repo, "state": "pending", "conflicted_files": [],
                 "auto_resolved": [], "tried": [],
                 "head_before": None, "head_after": None, "message": ""}
        if note:
            entry["message"] = note
        if repo is None:
            entry["state"] = "error"
            hint = repo_project_hint(roots, c["project"])
            if hint:
                entry["message"] = (entry["message"] + " " if entry["message"] else "") + hint
            result_changes.append(entry)
            continue                                  # 继续下一条，不再整批停住

        # 保留本次 change 的文件清单，供下载完成后的 480/390 gapp/style
        # 适配检查使用；这里只读 commit 对象，不修改工作区。
        try:
            entry["files"] = [f.get("file") for f in changed_files(repo, c["revision"])
                              if f.get("file")]
        except Exception as exc:
            entry["files"] = []
            entry["files_error"] = str(exc)

        # 选 remote（每仓库只需确定一次）
        if repo not in repo_remote:
            rl = git(repo, "remote")
            names = [x.strip() for x in (rl.stdout or "").split() if x.strip()]
            repo_remote[repo] = "origin" if "origin" in names else (names[0] if names else None)
        remote = repo_remote[repo]
        if remote is None:
            entry["state"] = "error"
            entry["message"] = (entry["message"] + " " if entry["message"] else "") + \
                "%s 没有配置任何 git remote" % repo
            result_changes.append(entry)
            continue

        left_in_repo[repo] = left_in_repo.get(repo, 1) - 1
        last_for_repo = (left_in_repo[repo] <= 0)
        entry.update(download_one(repo, c, remote, cfg,
                                  leave_on_conflict=last_for_repo))
        entry["repo"] = repo
        result_changes.append(entry)

    states = [e["state"] for e in result_changes]
    progressed = [s for s in states if s in ("applied", "applied_auto", "already_applied")]
    bad = [s for s in states if s in ("conflict", "conflict_aborted", "error", "pending")]
    if not bad:
        status = "ok"
    elif progressed:
        status = "partial"
    elif "conflict" in states or "conflict_aborted" in states:
        status = "conflict"
    else:
        status = "error"

    summary = []
    for e in result_changes:
        if e["state"] in ("applied", "applied_auto"):
            tag = "applied" if e["state"] == "applied" else "applied(自动解冲突)"
            summary.append("#%s %s -> %s (%s)" % (e["number"], e["subject"], e["repo"], tag))
        elif e["state"] in ("conflict", "conflict_aborted"):
            summary.append("#%s %s -> %s (%s: %s)" % (
                e["number"], e["subject"], e["repo"],
                "CONFLICT" if e["state"] == "conflict" else "CONFLICT(已撤销)",
                ", ".join(e["conflicted_files"])))
        else:
            summary.append("#%s %s -> %s (%s: %s)" % (
                e["number"], e["subject"], e["repo"], e["state"],
                e["message"].splitlines()[0] if e["message"] else ""))

    if status == "ok":
        msg = "全部完成（%d 条）" % len(result_changes)
    elif status == "partial":
        msg = ("部分完成：%d 条已落地，%d 条需要处理。"
               "按 changes[] 里的 state/message 逐条处理，处理完重跑 download，"
               "已合入的会自动跳过" % (len(progressed), len(bad)))
    else:
        msg = ("全部失败：%d 条。按 changes[] 里的 state/message 处理" % len(bad))

    emit({"status": status, "mode": "download", "gerrit": cfg["base_url"],
          "config": cfg["config_path"], "link_type": link["type"],
          "topic": link.get("topic"), "count": len(result_changes),
          "applied": len(progressed), "failed": len(bad),
          "changes": result_changes, "summary": summary, "message": msg})
    sys.exit(0 if status == "ok" else 1)


# ----------------------------- 导出 topic 代码快照 -----------------------------
def export_tree(repo, sha, dest, timeout=600):
    """把某个 commit 的完整文件树导出到 dest 目录（不动工作区）。返回 (ok, files, err)。"""
    if not os.path.isdir(dest):
        os.makedirs(dest)
    tmp = os.path.join(dest + ".zip")
    try:
        r = subprocess.run(["git", "-C", repo, "archive", "--format=zip",
                            "-o", tmp, sha],
                           capture_output=True, text=True, timeout=timeout)
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


def changed_files(repo, sha, timeout=120):
    """该 commit 相对父提交改了哪些文件（新加/修改/删除/改名）。"""
    r = subprocess.run(["git", "-C", repo, "diff", "--name-status",
                        sha + "^", sha],
                       capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        # 根提交没有父
        r = subprocess.run(["git", "-C", repo, "show", "--name-status",
                            "--pretty=format:", sha],
                           capture_output=True, text=True, timeout=timeout)
    out = []
    for ln in (r.stdout or "").splitlines():
        parts = ln.split("\t")
        if len(parts) < 2:
            continue
        st = parts[0][:1]
        path = parts[-1].strip()
        if path:
            out.append({"status": st, "file": path})
    return out


def rel_under(path, root):
    try:
        rel = os.path.relpath(path, root)
        if rel.startswith(".."):
            return None
        return rel.replace("\\", "/")
    except Exception:
        return None


def fetch_objects(repo, c, remote, cfg):
    """只 fetch 该 change 的对象，不做任何工作区操作。返回 (sha, err)。"""
    n, ps = c["number"], c["patchset"] or "current"
    ref = "refs/changes/%02d/%d/%s" % (n % 100, n, ps)
    fr = git(repo, "fetch", remote, ref)
    if fr.returncode != 0:
        auth_url = "%s/a/%s" % (cfg["base_url"], c["project"])
        auth_url = auth_url.replace(
            "://", "://%s:%s@" % (cfg["user"], urllib.parse.quote(cfg["pw"], safe="")), 1)
        fr = git(repo, "fetch", auth_url, ref)
        if fr.returncode != 0:
            return "", ((fr.stderr or fr.stdout or "").strip())[:400]
    sha = git(repo, "rev-parse", "FETCH_HEAD").stdout.strip()
    if not sha:
        return "", "fetch 后取不到 FETCH_HEAD"
    return sha, ""


def cmd_snapshot(args, cfg, link):
    """把 topic 代码导出到一个**备份目录**（默认不碰任何工作区）。

    每个 change 的完整文件树用 `git archive` 解开到 <dest>/<仓库相对路径>/，
    同仓库多条按顺序叠加，最后落一份 _snapshot.json 说明来源。
    用途：代码评估拿这份副本和本地工作区比，比逐行看补丁准；
    也顺便验证「这些 change 到底能不能拿到」。
    """
    g = Gerrit(cfg)
    changes = order_changes(fetch_changes(g, link))
    roots = [w.strip() for w in (args.workspace or ",".join(_DEFAULT_WORKSPACE)).split(",") if w.strip()]
    scanned_by_root = [scan_roots([r]) for r in roots]
    dest_root = os.path.abspath(args.dest)
    if not os.path.isdir(dest_root):
        os.makedirs(dest_root)

    groups = []       # [(repo, rel, [change...])]
    by_repo = {}
    failed = []
    for c in changes:
        repo, note = locate_repo(scanned_by_root, c, roots)
        if not repo:
            hint = repo_project_hint(roots, c["project"])
            failed.append({"number": c["number"], "project": c["project"],
                           "error": (note or "本地没定位到仓库") + ("；" + hint if hint else "")})
            continue
        rel = None
        for r in roots:
            rel = rel_under(repo, r)
            if rel is not None:
                break
        rel = rel or os.path.basename(repo.rstrip("/")) or c["project"].split("/")[-1]
        key = (repo, rel)
        if key not in by_repo:
            by_repo[key] = []
            groups.append((repo, rel, by_repo[key]))
        by_repo[key].append(c)

    out_repos, total_files = [], 0
    for repo, rel, cs in groups:
        item = {"repo": repo, "rel": rel, "dest": os.path.join(dest_root, rel),
                "changes": [c["number"] for c in cs], "files": [],
                "exported": 0, "ok": False, "error": ""}
        remote = None
        rl = git(repo, "remote")
        names = [x.strip() for x in (rl.stdout or "").split() if x.strip()]
        if names:
            remote = "origin" if "origin" in names else names[0]
        for c in cs:
            sha, err = fetch_objects(repo, c, remote or "origin", cfg)
            if err:
                item["error"] = (item["error"] + " " + err).strip()
                continue
            ok, n, err = export_tree(repo, sha, item["dest"])
            if not ok:
                item["error"] = (item["error"] + " #%s 导出失败: %s" % (c["number"], err)).strip()
                continue
            item["exported"] += n
            for f in changed_files(repo, sha):
                if f not in item["files"]:
                    item["files"].append(f)
        item["ok"] = item["exported"] > 0 and not item["error"]
        total_files += len(item["files"])
        out_repos.append(item)

    status = "ok" if out_repos and not failed else ("partial" if out_repos else "error")
    manifest = {"status": status, "mode": "snapshot", "gerrit": cfg["base_url"],
                "topic": link.get("topic"), "link": args.url, "dest": dest_root,
                "roots": roots, "count": len(changes),
                "repos": out_repos, "failed": failed,
                "files": total_files,
                "message": ("已导出 %d 个仓库 / %d 个变更文件到 %s"
                            % (len([r for r in out_repos if r["ok"]]), total_files, dest_root))
                           + ("；%d 个 change 没定位到仓库" % len(failed) if failed else "")}
    try:
        with open(os.path.join(dest_root, "_snapshot.json"), "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
    emit(manifest)
    sys.exit(0 if status == "ok" else 1)


def main():
    ap = argparse.ArgumentParser(description="Gerrit link -> local download (fetch + cherry-pick)")
    sub = ap.add_subparsers(dest="cmd")
    for name in ("plan", "precheck", "download", "snapshot"):
        sp = sub.add_parser(name)
        sp.add_argument("--url", required=True, help="Gerrit change/topic 链接、change 号、Change-Id 或 topic:名字")
        sp.add_argument("--workspace", help="逗号分隔的本地仓库扫描根目录，默认 /home/hgw/workspace,/home/hgw/master")
        sp.add_argument("--repo", help="直接指定目标仓库路径（跳过自动定位，仅单 change 时生效）")
        sp.add_argument("--config", help="gerrit 配置 JSON 路径")
        if name == "snapshot":
            sp.add_argument("--dest", required=True, help="导出目录（备份目录，不会被当成工作区）")
    argv = sys.argv[1:]
    # 便捷用法: 第一个参数是链接（change 号/Change-Id/topic:/http 链接）时，默认按 download 执行
    if argv and argv[0] not in ("plan", "precheck", "download", "snapshot",
                                "-h", "--help") and not argv[0].startswith("-"):
        argv = ["download", "--url"] + argv
    args = ap.parse_args(argv)
    if not args.cmd:
        ap.error("需要子命令 plan / precheck / download / snapshot，或直接把链接作为第一个参数\n"
                 "  例: gerrit_download.py https://gerrit.../c/project/+/12345   (等同 download --url ...)")

    cfg = load_config(args.config)
    try:
        link = parse_link(args.url)
        if args.cmd == "plan":
            cmd_plan(args, cfg, link)
        elif args.cmd == "precheck":
            cmd_precheck(args, cfg, link)
        elif args.cmd == "snapshot":
            cmd_snapshot(args, cfg, link)
        else:
            cmd_download(args, cfg, link)
    except RuntimeError as e:
        die(str(e))


if __name__ == "__main__":
    main()

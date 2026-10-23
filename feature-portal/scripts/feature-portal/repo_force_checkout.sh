#!/usr/bin/env bash
# 强制清理 repo 工作区并按 manifest 同步，一步到位。
#
# 处理流程：
#   1) 直接清除 .repo/projects 下残留的 rebase/merge 状态
#      （解决 error: prior sync failed; rebase still in progress）
#   2) 逐仓库 reset/clean（可指定统一切换的分支）
#   3) repo sync --force-sync --force-remove-dirty --prune
#   4) 失败则对失败仓库逐个强制恢复后重试
#   5) 仍失败则直接重建该仓库的 git 目录与工作区后再同步
#
# 用法: ./repo_force_checkout.sh [manifest.xml] [分支名] [repo sync 参数...]
#   ./repo_force_checkout.sh milan.xml       # 最常用：按 milan.xml 的 revision 强制同步
#   ./repo_force_checkout.sh                 # 用当前 manifest 强制同步
#   ./repo_force_checkout.sh milan.xml master -j16
#
# 默认行为即执行同步（-j8）；*.xml 参数自动当作 -m <manifest>。
# 已自动附加 --force-sync --force-remove-dirty --prune。
#
# 清理时保留的未跟踪文件可通过环境变量 REPO_FORCE_KEEP_PATHS 配置
# （空格分隔，相对各仓库根目录），默认 "build/sign.pem"；
# 显式 export REPO_FORCE_KEEP_PATHS="" 可恢复为全部清除。

set -u

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

# git clean 时需要保留的未跟踪文件（空格分隔，路径相对各仓库根目录）。
# 可通过环境变量 REPO_FORCE_KEEP_PATHS 覆盖；显式设为空字符串则不保留任何文件。
# 默认保留签名密钥 build/sign.pem，避免强制清理后丢失。
KEEP_PATHS="${REPO_FORCE_KEEP_PATHS-build/sign.pem}"

REPO_CMD=()
TARGET_BRANCH=""
MANIFEST=""
RUN_SYNC=1
SYNC_ARGS=()

usage() {
  cat <<'EOF'
用法: repo_force_checkout.sh [manifest.xml] [分支名] [repo sync 参数...]

  manifest.xml  可选；以 .xml 结尾的参数自动作为 -m 传给 repo sync，
                不指定则使用当前 manifest
  分支名        可选；给出时所有仓库统一切到该分支，否则按 manifest 的 revision 更新
  --no-sync     只清理工作区，不执行 repo sync
  其它参数      原样传给 repo sync（默认已带 -j8）

示例:
  ./repo_force_checkout.sh milan.xml
  ./repo_force_checkout.sh milan.xml master -j16
  ./repo_force_checkout.sh --no-sync
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help)
      usage
      exit 0
      ;;
    --sync)                      # 默认已开启，保留兼容
      ;;
    --no-sync)
      RUN_SYNC=0
      ;;
    -m|--manifest-name)
      [[ $# -ge 2 ]] || { echo "❌ $1 缺少参数" >&2; exit 1; }
      MANIFEST="$2"
      shift
      ;;
    -m*)
      MANIFEST="${1#-m}"
      ;;
    --manifest-name=*)
      MANIFEST="${1#*=}"
      ;;
    *.xml)
      MANIFEST="$1"
      ;;
    -*)
      SYNC_ARGS+=("$1")
      ;;
    *)
      TARGET_BRANCH="$1"
      ;;
  esac
  shift
done

if [[ -n "$TARGET_BRANCH" ]] && ! git check-ref-format --branch "$TARGET_BRANCH" >/dev/null 2>&1; then
  echo "❌ 非法 Git 分支名: $TARGET_BRANCH" >&2
  exit 1
fi

if [[ -n "$MANIFEST" ]]; then
  SYNC_ARGS=(-m "$MANIFEST" "${SYNC_ARGS[@]}")
  if [[ ! -f "$MANIFEST" && ! -f "$SCRIPT_DIR/.repo/manifests/$MANIFEST" ]]; then
    echo "⚠️ 未找到 manifest 文件: $MANIFEST（仍会原样传给 repo sync）" >&2
  fi
fi

if command -v repo >/dev/null 2>&1; then
  REPO_CMD=(repo)
elif [[ -x "$SCRIPT_DIR/.repo/repo/repo" ]]; then
  REPO_CMD=("$SCRIPT_DIR/.repo/repo/repo")
else
  echo "❌ 找不到 repo 命令，请在 repo 项目根目录下运行。" >&2
  exit 1
fi

if [[ ! -d "$SCRIPT_DIR/.repo" ]]; then
  echo "❌ $SCRIPT_DIR 不是 repo 项目根目录。" >&2
  exit 1
fi

# 并发度：默认 8；用户显式传了 -j/--jobs 时不再追加
JOBS=8
HAS_JOBS=0
for a in ${SYNC_ARGS[@]+"${SYNC_ARGS[@]}"}; do
  case "$a" in
    -j|-j[0-9]*|--jobs|--jobs=*) HAS_JOBS=1 ;;
  esac
done
if [[ "$HAS_JOBS" -eq 0 ]]; then
  SYNC_ARGS+=(-j "$JOBS")
fi

# ---------------------------------------------------------------------------
# 生成 per-project 辅助脚本。
# 不用 `export -f` + `bash -c`（repo 的某些版本下子进程拿不到导出的函数，
# 会返回 127），改为让 repo forall 直接执行一个绝对路径的脚本文件，
# 参数通过环境变量传递。
# ---------------------------------------------------------------------------
WORK_DIR=$(mktemp -d "${TMPDIR:-/tmp}/repo_force.XXXXXX") || {
  echo "❌ 创建临时目录失败" >&2
  exit 1
}
trap 'rm -rf "$WORK_DIR"' EXIT INT TERM
HELPER="$WORK_DIR/project.sh"

cat >"$HELPER" <<'HELPER_EOF'
#!/usr/bin/env bash
# 由 repo_force_checkout.sh 生成，在每个 project 工作目录中执行一次。
set -u

MODE="${REPO_FORCE_MODE:-clean}"
BRANCH="${REPO_FORCE_BRANCH:-}"
PNAME="${REPO_PATH:-$(basename "$PWD")}"
GIT_DIR_PATH=""

say() { printf '%s:%s: %s\n' "$1" "$PNAME" "$2"; }

get_git_dir() {
  local d
  d=$(git rev-parse --absolute-git-dir 2>/dev/null) || d=""
  [[ -n "$d" ]] || d=$(git rev-parse --git-dir 2>/dev/null) || d=""
  [[ -n "$d" ]] || return 1
  [[ "$d" == /* ]] || d="$PWD/$d"
  GIT_DIR_PATH="$d"
}

# 中止一切进行中的操作，并删除 rebase/merge/cherry-pick 的状态文件。
purge_state() {
  local gd="${GIT_DIR_PATH:-}"

  git rebase --quit      >/dev/null 2>&1 || true
  git rebase --abort     >/dev/null 2>&1 || true
  git am --abort         >/dev/null 2>&1 || true
  git merge --abort      >/dev/null 2>&1 || true
  git cherry-pick --abort >/dev/null 2>&1 || true
  git revert --abort     >/dev/null 2>&1 || true

  if [[ -n "$gd" && -d "$gd" ]]; then
    # 覆盖 gitdir 以及 worktrees/<name>/ 下的残留（repo 报 rebase still in progress 的根源）
    find "$gd" -maxdepth 3 \
      \( -name 'rebase-apply' -o -name 'rebase-merge' -o -name 'sequencer' \) \
      -prune -exec rm -rf {} + 2>/dev/null
    find "$gd" -maxdepth 2 -type f \
      \( -name 'index.lock' -o -name 'HEAD.lock' -o -name 'packed-refs.lock' \
         -o -name 'shallow.lock' -o -name 'MERGE_HEAD' -o -name 'MERGE_MODE' \
         -o -name 'MERGE_MSG' -o -name 'AUTO_MERGE' -o -name 'CHERRY_PICK_HEAD' \
         -o -name 'REVERT_HEAD' -o -name 'BISECT_LOG' -o -name 'BISECT_START' \) \
      -exec rm -f {} + 2>/dev/null
  fi
  return 0
}

reset_worktree() {
  local args=(-ffdx -e '.repo') p
  # 保留 REPO_FORCE_KEEP_PATHS 指定的未跟踪文件（如 build/sign.pem）
  for p in ${REPO_FORCE_KEEP_PATHS:-}; do
    args+=(-e "$p")
  done
  git reset --hard >/dev/null 2>&1 || true
  git clean "${args[@]}" >/dev/null 2>&1 || true
}

if ! get_git_dir; then
  say FAIL "不是有效的 Git 仓库（$PWD）"
  exit 1
fi

purge_state
reset_worktree
purge_state   # clean 可能重新产生状态文件，再清一次

case "$MODE" in
  clean)
    if [[ -z "$BRANCH" ]]; then
      say OK "工作区已清理（revision 由 manifest 决定）"
      exit 0
    fi
    git fetch --all --prune --force >/dev/null 2>&1 || say WARN "git fetch 失败，沿用已有远端引用"
    if git show-ref --verify --quiet "refs/remotes/origin/$BRANCH"; then
      if git checkout -B "$BRANCH" "refs/remotes/origin/$BRANCH" >/dev/null 2>&1; then
        say OK "已切换到 $BRANCH（origin/$BRANCH）"
        exit 0
      fi
      say FAIL "无法切换到 origin/$BRANCH"
      exit 1
    elif git show-ref --verify --quiet "refs/heads/$BRANCH"; then
      if git checkout -f "$BRANCH" >/dev/null 2>&1; then
        say OK "已切换到本地分支 $BRANCH"
        exit 0
      fi
      say FAIL "无法切换到本地分支 $BRANCH"
      exit 1
    fi
    say FAIL "origin 与本地均不存在分支 $BRANCH"
    exit 1
    ;;
  recover)
    git fetch --all --prune --force >/dev/null 2>&1 || say WARN "git fetch 失败"
    say OK "已强制清理 rebase/merge 状态"
    ;;
  *)
    say OK "工作区已清理"
    ;;
esac
exit 0
HELPER_EOF
chmod +x "$HELPER"

export REPO_FORCE_BRANCH="$TARGET_BRANCH"
export REPO_FORCE_KEEP_PATHS="$KEEP_PATHS"

# ---------------------------------------------------------------------------
# 直接清除 .repo 内部残留的 rebase 状态（不依赖 repo forall 能否执行成功）
# ---------------------------------------------------------------------------
purge_central_rebase_state() {
  local n=0 d
  for d in "$SCRIPT_DIR"/.repo/projects/*.git "$SCRIPT_DIR"/.repo/project-objects/*.git; do
    [[ -d "$d" ]] || continue
    if [[ -e "$d/rebase-merge" || -e "$d/rebase-apply" || -e "$d/sequencer" ]]; then
      rm -rf "$d/rebase-merge" "$d/rebase-apply" "$d/sequencer"
      n=$((n + 1))
    fi
  done
  if [[ -d "$SCRIPT_DIR/.repo/projects" ]]; then
    find "$SCRIPT_DIR/.repo/projects" -maxdepth 3 \
      \( -name 'rebase-merge' -o -name 'rebase-apply' -o -name 'sequencer' \) \
      -prune -exec rm -rf {} + 2>/dev/null || true
  fi
  echo "  已清除 $n 个仓库残留的 rebase 状态目录"
}

run_forall() {
  local mode="$1" out status
  export REPO_FORCE_MODE="$mode"
  out=$("${REPO_CMD[@]}" forall -j "$JOBS" -c "$HELPER" 2>&1)
  status=$?
  # 某些 repo 版本不支持 forall -j：无输出且失败时退化为串行执行
  if [[ $status -ne 0 && -z "${out//[$' \t\r\n']/}" ]]; then
    out=$("${REPO_CMD[@]}" forall -c "$HELPER" 2>&1)
    status=$?
  fi
  printf '%s\n' "$out"
  return "$status"
}

run_sync() {
  SYNC_OUTPUT=$("${REPO_CMD[@]}" sync --force-sync --force-remove-dirty --prune ${SYNC_ARGS[@]+"${SYNC_ARGS[@]}"} 2>&1)
  SYNC_STATUS=$?
  printf '%s\n' "$SYNC_OUTPUT"
  return "$SYNC_STATUS"
}

# 从 repo sync 的输出里解析 "Failing repos (checkout):" 之后的仓库路径
extract_failed_repos() {
  printf '%s\n' "$1" | awk '
    /^Failing repos/ { inlist = 1; next }
    /^Try re-running|^Try "repo/ { inlist = 0 }
    inlist && NF { print $1 }
  ' | sed 's#/$##' | sort -u
}

repair_one() {
  local p="$1" dir="$SCRIPT_DIR/$1"
  if [[ ! -d "$dir" ]]; then
    echo "WARN:$p: 工作目录不存在，跳过"
    return 0
  fi
  if ! (cd "$dir" && REPO_FORCE_MODE=recover REPO_FORCE_BRANCH="" REPO_FORCE_KEEP_PATHS="$KEEP_PATHS" REPO_PATH="$p" "$HELPER"); then
    echo "FAIL:$p: 强制恢复失败"
    return 1
  fi
  return 0
}

# 最后手段：删掉该仓库的 git 目录并清空工作区，让 repo sync 重新 clone

# 把字符串转义为可安全嵌入 ERE 的形式
escape_ere() {
  printf '%s' "$1" | sed -e 's/[][\.*^$()+?{}|]/\\&/g'
}

# 生成排除 REPO_FORCE_KEEP_PATHS 及其父目录的 find 正则（仅保留路径所在仓库内有效）
build_keep_regex() {
  local base="$1" p acc seg rx=""
  for p in ${REPO_FORCE_KEEP_PATHS:-}; do
    acc=""
    while :; do
      seg="${p%%/*}"
      if [[ "$p" == */* ]]; then
        # 中间父目录：只保护目录本身，不保护其下其他内容
        rx+="${rx:+|}$(escape_ere "$base/$acc$seg")"
      else
        # 叶子路径：保护自身及其下所有内容
        rx+="${rx:+|}$(escape_ere "$base/$acc$seg")(/.*)?"
      fi
      acc="$acc$seg/"
      [[ "$p" == */* ]] || break
      p="${p#*/}"
    done
  done
  [[ -n "$rx" ]] && printf '%s' "($rx)"
}

nuke_one() {
  local p="$1" dir="$SCRIPT_DIR/$1" gd=""
  if [[ -z "$p" || "$p" == "." ]]; then
    echo "WARN:$p: 根项目只清理不重建"
    repair_one "$p"
    return $?
  fi
  [[ -d "$dir" ]] && gd=$(cd "$dir" && git rev-parse --absolute-git-dir 2>/dev/null) || gd=""
  if [[ -n "$gd" ]]; then
    if [[ "$gd" == "$SCRIPT_DIR"/.repo/projects/*.git ]]; then
      rm -rf "$gd"
    else
      echo "WARN:$p: git 目录 $gd 不在 .repo/projects 下，跳过删除"
    fi
  fi
  if [[ -d "$dir" ]]; then
    local keep_rx
    keep_rx=$(build_keep_regex "$dir")
    if [[ -n "$keep_rx" ]]; then
      # 重建工作区时同样保留 REPO_FORCE_KEEP_PATHS 指定的文件（含其父目录）
      find "$dir" -regextype posix-extended -depth -mindepth 1 ! -name '.repo' ! -regex "$keep_rx" \
        -exec rm -rf {} + 2>/dev/null || true
    else
      find "$dir" -mindepth 1 -maxdepth 1 ! -name '.repo' -exec rm -rf {} + 2>/dev/null || true
    fi
  fi
  echo "NUKE:$p: 已清空本地仓库，等待 repo sync 重新拉取"
}

# 指定了 manifest 时先切换，保证后续 repo forall 遍历的就是该 manifest 的仓库
switch_manifest() {
  [[ -n "$MANIFEST" ]] || return 0
  local cur want
  cur=$(readlink -f "$SCRIPT_DIR/.repo/manifest.xml" 2>/dev/null) || cur=""
  want=$(readlink -f "$SCRIPT_DIR/.repo/manifests/$MANIFEST" 2>/dev/null) || want=""
  [[ -n "$want" ]] || want=$(readlink -f "$MANIFEST" 2>/dev/null) || want=""
  if [[ -n "$cur" && -n "$want" && "$cur" == "$want" ]]; then
    echo "  当前 manifest 已是 $MANIFEST，无需切换"
    return 0
  fi
  echo "  切换 manifest 到 $MANIFEST ..."
  if "${REPO_CMD[@]}" init -m "$MANIFEST" >/dev/null 2>&1; then
    echo "  manifest 已切换"
  else
    echo "⚠️ repo init -m $MANIFEST 失败，清理阶段仍按旧 manifest 进行，同步时会再次切换" >&2
  fi
}

# ---------------------------------------------------------------------------
cd "$SCRIPT_DIR" || exit 1
echo "============================================="
if [[ -n "$TARGET_BRANCH" ]]; then
  echo "  强制切换所有仓库到分支: $TARGET_BRANCH"
else
  echo "  按 manifest 中的 revision 更新"
fi
if [[ -n "$MANIFEST" ]]; then
  echo "  manifest: $MANIFEST"
else
  echo "  manifest: 使用当前 manifest"
fi
if [[ "$RUN_SYNC" -eq 1 ]]; then
  echo "  同步参数: --force-sync --force-remove-dirty --prune ${SYNC_ARGS[*]}"
else
  echo "  仅清理工作区，不执行 repo sync"
fi
echo "  将丢弃所有本地修改、未跟踪文件和进行中的 Git 操作"
echo "============================================="

echo ""
echo ">> 步骤 1/5: 切换 manifest 并清除 .repo 内部残留的 rebase 状态"
switch_manifest
purge_central_rebase_state

echo ""
echo ">> 步骤 2/5: 逐仓库强制清理工作区"
FORALL_OUT=$(run_forall clean)
FORALL_STATUS=$?
printf '%s\n' "$FORALL_OUT"

OK_COUNT=$(printf '%s\n' "$FORALL_OUT" | awk '/^OK:/{c++} END{print c+0}')
FAIL_COUNT=$(printf '%s\n' "$FORALL_OUT" | awk '/^FAIL:/{c++} END{print c+0}')
WARN_COUNT=$(printf '%s\n' "$FORALL_OUT" | awk '/^WARN:/{c++} END{print c+0}')
TOTAL=$((OK_COUNT + FAIL_COUNT))
EXPECTED=$("${REPO_CMD[@]}" list 2>/dev/null | wc -l)

echo ""
echo "============================================="
echo "  清理结果: 已处理 $TOTAL（manifest 共 $EXPECTED），成功 $OK_COUNT，失败 $FAIL_COUNT，警告 $WARN_COUNT"
echo "============================================="
if [[ "$FORALL_STATUS" -ne 0 && "$TOTAL" -eq 0 ]]; then
  echo "⚠️ repo forall 未产生任何结果（状态码 $FORALL_STATUS），继续依赖后续同步与恢复步骤。" >&2
fi
if [[ "$FAIL_COUNT" -gt 0 ]]; then
  printf '%s\n' "$FORALL_OUT" | grep '^FAIL:' >&2
  echo "⚠️ 部分仓库清理失败，后续同步阶段会继续尝试恢复。" >&2
fi

if [[ "$RUN_SYNC" -ne 1 ]]; then
  echo "完成（已按 --no-sync 跳过同步）。"
  exit 0
fi

echo ""
echo ">> 步骤 3/5: 首次强制同步"
if run_sync; then
  echo ""
  echo "✅ 同步完成。"
  exit 0
fi

echo ""
echo ">> 步骤 4/5: 对失败仓库强制恢复后重试"
FAILED_REPOS=$(extract_failed_repos "$SYNC_OUTPUT")
if [[ -n "$FAILED_REPOS" ]]; then
  while IFS= read -r p; do
    [[ -n "$p" ]] || continue
    repair_one "$p" || true
  done <<<"$FAILED_REPOS"
else
  echo "  未能从输出中解析失败仓库，改为全量恢复"
  RECOVER_OUT=$(run_forall recover)
  printf '%s\n' "$RECOVER_OUT"
fi

if run_sync; then
  echo ""
  echo "✅ 同步完成。"
  exit 0
fi

echo ""
echo ">> 步骤 5/5: 重建仍然失败的仓库后同步"
FAILED_REPOS=$(extract_failed_repos "$SYNC_OUTPUT")
if [[ -z "$FAILED_REPOS" ]]; then
  echo "❌ 无法解析失败仓库列表，同步终止。" >&2
  exit 1
fi
while IFS= read -r p; do
  [[ -n "$p" ]] || continue
  nuke_one "$p"
done <<<"$FAILED_REPOS"

if run_sync; then
  echo ""
  echo "✅ 同步完成。"
  exit 0
fi

echo ""
echo "❌ 已尝试清理、强制恢复、重建仓库，repo sync 仍然失败，请查看上面的错误输出。" >&2
exit 1

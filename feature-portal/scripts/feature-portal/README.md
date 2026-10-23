# 功能移植工作台 (Feature Porting Portal)

把「功能评估 → 代码移植 → 编译烧录 → 自测验证」串成一条流水线的本地网页工具。
纯 Python 标准库实现，不装任何第三方包。

```
评估 ──► 移植 ──► 编译 ──► 刷机 ──► 自测
 │        │        │         │        │
缺失接口  目录选择  active-build  v3dl   清单确认
（本地没有  gerrit   map/config   app      OK / NG
 的那些）  冲突分析 整屏变绿 → 询问刷机  自动进入
硬件支持
存储余量
工期预测
```

## 启动

### 最省事：双击 `launch.vbs`

在 Windows 资源管理器里进 `\\wsl.localhost\Ubuntu-20.04\home\hgw\monaco\tools\feature-portal\`，
双击 **`launch.vbs`** 就行。双击后**立刻弹一个「正在加载中…」的小窗**
（深色、置顶、带跑马灯进度条），不会让人对着空桌面等：

1. 探测 8770~8776，已有服务就直接开浏览器（不会起第二份）
2. 没有就在 WSL 里拉 `run.sh`（git / repo 只能在 WSL 里正常工作）
3. WSL 不可用时回退 Windows 的 `python`
4. 等到 `/api/ping` 通 → **关掉小窗、直接打开网页**，不会出现白屏转圈
5. 30 秒还没起来 → 小窗标题变「启动失败」，里面显示 `/tmp/feature-portal.log` 的最后几行，
   点「关闭」即可

**关掉所有页面约 8 秒后，服务自己退出**，不用手动停。
（多标签页是各自登记的——关一个不影响其它，全关才退。）

> 为什么不能"打开网页就自动跑脚本"：浏览器安全沙箱禁止网页启动本地进程，
> 没有例外。所以最小代价就是双击一次这个图标。
>
> 第一次双击如果 Windows 提示"来自网络位置"，点「运行」即可；
> 想彻底免提示，可以把 `launch.vbs` 复制到桌面再双击。

#### 装成桌面快捷方式（可选）

```powershell
$dst = "$env:USERPROFILE\Desktop\功能移植工作台.lnk"
$ws  = New-Object -ComObject WScript.Shell
$sc  = $ws.CreateShortcut($dst)
$sc.TargetPath   = "\\wsl.localhost\Ubuntu-20.04\home\hgw\monaco\tools\feature-portal\launch.vbs"
$sc.IconLocation = "$env:SystemRoot\System32\shell32.dll,13"
$sc.Save()
```

之后双击桌面的「功能移植工作台」即可，图标也换了。

### 手动启动（要自己看日志时）

```bash
cd /home/hgw/monaco/tools/feature-portal

./start.sh        # 前台常驻，终端打印 banner 和日志，Ctrl+C 停
bash run.sh       # 后台语义，关闭页面 8 秒后自动退出
```

然后**用系统浏览器**（Chrome / Edge）打开终端打印的地址，默认
<http://127.0.0.1:8770>。WSL2 会自动把 localhost 转发到 Windows。

⭐ **不要用 IDE 的预览面板打开 `index.html`**。预览面板是以 `file://` 方式渲染静态文件，
`/api/...` 请求会变成 `file:///api/...`，必然报 `Failed to fetch`。
如果确实要用预览面板，页面会自动去探测 `127.0.0.1:8770~8776`（已开 CORS），
探测不到就会显示一个带排查步骤的诊断页，而不是只甩一行报错。

### 连不上时

页面上的「后端未连接」诊断页会列出步骤。手工确认：

```bash
# 1. 服务活着吗？（返回 pong 就是活的）
curl -s http://127.0.0.1:8770/api/ping

# 2. 启动日志（run.sh / launch.vbs 启动的都写这里）
tail -50 /tmp/feature-portal.log

# 3. 端口对吗？启动 banner 里那行 "浏览器打开"（被占会自动顺延）
# 4. WSL2 里 localhost 桥接偶尔失效，改用 banner 打印的 "WSL IP" 地址
```

其他参数：

```bash
python3 server.py --port 9000                # 换端口
python3 server.py --workspace /home/hgw/master   # 换工作区（也会被页面记住）
python3 server.py --host 127.0.0.1           # 只监听本机
python3 server.py --idle-exit 8              # 页面全关后延时退出（启动器用）
python3 server.py --open                     # 起来后自己开浏览器
```

启动时终端会打印环境自检：

```
 浏览器打开 : http://127.0.0.1:8770
   工作区     : /home/hgw/monaco
   active-build : /home/hgw/.local/bin/active-build
   v3dl         : /home/hgw/bin/v3dl
   gerrit_dl.py : /home/hgw/monaco/tools/gerrit_dl.py
   repo 基线    : 可用
```

哪个是「未找到」，页面上那个环节就用不了，其余步骤不受影响。

## 各步骤做了什么

### 0. 代码基线（在评估页最上方）

先定「代码放在哪、按哪个 manifest、切到哪个分支」，再评估 —— 否则评估的是一份不知道版本的代码。

| 项 | 来源 |
|---|---|
| 代码目录 | 目录选择器，默认带出上次选择的路径（存 `data/state.json`） |
| Manifest | 扫 `<工作区>/.repo/manifests/*.xml`，逐个解析 `<default revision>` 和 project 数量，标出当前生效的那个（读 `.repo/manifest.xml` 的 `<include>`） |
| 分支 | 跟随所选 xml 的 `revision`，可直接改；候选来自所有 xml 里出现过的 revision |
| 当前 HEAD | 从 manifest 里第一个存在的仓库跑 `git rev-parse --abbrev-ref HEAD`（repo 同步后通常是 detached） |
| 编译配置 | 读 `build/.active-build-state.json`，显示 active-build 当前激活的 family/project |

点「更新代码到该基线」执行：

```bash
./repo_force_checkout.sh <manifest.xml> <分支> -j<并发>
```

脚本本身的行为（清 rebase 残留 → 逐仓库 reset/clean → `repo sync --force-sync
--force-remove-dirty --prune` → 失败重试 → 仍失败则重建）全部保留，页面只是套了层壳：
参数校验、日志实时滚动、退出码判定。**这会丢弃各仓库未提交的改动**（默认保留
`build/sign.pem`，可用 `REPO_FORCE_KEEP_PATHS` 调整），所以点之前有二次确认。

切换目录会调 `/api/workspace/switch`，之后所有评估、编译、基线都以新目录为根，
并记住到 `state.json`，重启后仍然生效。

基线更新成功后，之前的「代码评估」勾选会被自动取消 —— 代码变了，评估结论就作废了。

### 1. 功能评估

填功能名称（如 `HYROX 混合训练`），两个卡片分别评估，**都勾选后才能进下一步**。

**文档评估** — 扫 `docs/` 与 `documentation/`，按「需求 / 交互 / 协议 / 测试用例」四类
统计与功能名匹配的文档，给出评分和缺口。

**代码评估** — 点一下输出四块：

| 输出 | 数据来源 |
|---|---|
| 依赖代码情况 | **先把 topic 代码整棵导到备份目录**，再拿这份副本和**本地工作区**比，只列本地没有的接口 |
| 硬件是否支持 | `build/.config` 里与功能关键词相关的能力开关 |
| 项目存储状态 | `build/out/*/binary/*.map` + `.config` |
| 预测移植时间 | 文件数 / 模块数 / 接口数 / 冲突数 / 改动行数加权 |

依赖那块的判定逻辑（这是评估里最容易被误解的部分）：

0. **比对基准 = 本地这棵树**，不是线上。
   `gerrit_dl.py plan` 会带上 `--workspace <本地基准目录>`（代码基线目录 / 移植目录），
   所以 change 定位到的仓库、后面做的比对，全都落在你选的那棵工作区里；
   从前这里不传 `--workspace`，plan 会退回默认扫描根（`/home/hgw/workspace`、`/home/hgw/monaco`……），
   拿另一份 checkout 去比，结论自然对不上。
1. **先把 topic 代码下载到备份目录**（`data/topic-src/<topic>/`）。
   `gerrit_dl.py plan` 本身不做任何 git 操作，补丁一开始并不在本地，
   所以会先 `git fetch <remote> refs/changes/xx/N/ps` 把 commit 对象拉下来
   —— **只 fetch**，然后用 `git archive` 把每个 change 的**完整文件树**解到备份目录，
   **不 checkout、不 cherry-pick 到工作区**，本地代码一个字节都不动。
   同一 topic + 同一批 sha 会复用上次导出的副本（`_snapshot.json`），重跑很快。
2. 评估读的是**备份目录里的完整文件**（topic 改动过的那些），不是逐行拼补丁 ——
   这样宏定义、typedef、静态函数、文件级上下文都在，判定比只看 `+` 行准。
3. topic 自己定义的函数 / 宏 / typedef 不算依赖，直接排除。
4. 剩下每个符号到**本地工作区**里按词查，判定顺序：
   被改动仓库 `git grep`（读工作区文件，本地改到哪算到哪，含 `--untracked`）
   → 整棵本地工作区兜底（rg / 各仓库并行 `git grep` / 文件扫描）。
   **出现即存在**：头文件里有定义或声明、甚至只是被别人调用过，都算「本地已有」，不输出。
   只有本地哪都找不到的才进「本地缺失」列表。
   结果里会给出「比对基准」路径、「备份目录」路径，以及「本地已有」的抽样证据
   （文件:行 + 声明/定义/引用），可以直接核对是不是你期望的那份本地代码。
5. topic `#include` 了、本地却找不到的头文件单独列在最前面 —— 这类要先合入，
   否则它声明的接口也补不上。

所以清单里出现的东西，就是移植时必须一起带过来或自己实现的；本地已经有的接口、
改动文件明细都不会占地方。


存储那块读的是 149MB 的 map，采用逐行流式解析（约 5 秒），结果按 map 的
mtime+size 缓存到 `data/cache/`，不会每次重算。

解析时会把 ld 脚本里 `.psram_ext` / `.psram_heap2` 这类**整块区域占位段**排除掉，
否则占用率会恒等于 100%，没有参考价值。

### 2. 代码移植

- 目录选择器默认带出上次选的路径（存在 `data/state.json`，重启不丢）。
  **输入框里打的字随时落盘**（停止输入 700ms 后写 `data/state.json`，同时镜像到
  `localStorage`，服务挂了也还在），不用点任何按钮；用过的目录会进 `recent_paths`，
  输入框下拉即可选回。
  交互是「**点文件夹进入下一层，选好后点右下角『选定此目录』**」；
  也可以直接在顶部路径框粘贴完整路径、回车跳转；
  `#basePath` / `#targetPath` 两个输入框本身也可编辑，回车即生效。
- **topic 只在「代码评估」填一次**，评估完会写进 `data/state.json` 的 `port.topic`，
  进到移植步骤自动带过来（右上角标「沿用代码评估填写」）。想换再改这个输入框即可。
- 「预检」（`precheck`，纯只读）先告诉你**每个 change 会落到哪个仓库、该仓库当前分支、
  有没有未提交改动 / 未完成的 cherry-pick、change 是否已合入**，提前标出「会失败 / 有风险 / 可以」。
- 下载 = `tools/gerrit_dl.py download --workspace <目标目录>`（fetch + cherry-pick）。
  预检和下载**扫的是同一个目录**（之前预检不传 `--workspace`，会退回去扫
  `/home/hgw/workspace,/home/hgw/monaco,/home/hgw/master`，显示的仓库和实际落地的对不上）。
- **单条失败不再拖垮整批**：定位不到仓库、工作区脏、fetch 失败都只记在那一条上，
  接着处理下一条（以前第一条失败后全部 `pending: 前面的 change 失败，本条未执行`）。
- **冲突会先自动试着解**，按顺序：
  1. 冲突块只有一侧有内容（比如本分支删了、change 侧新增）→ 直接取有内容那侧；
  2. 还是不行 → `git cherry-pick -X theirs` 重来一遍，整体按 change 侧解决；
  3. 仍冲突 → 同仓库后面还有 change 就 `cherry-pick --abort` 让后面继续跑，
     没有后续则**保留现场**等人处理。
  自动解决的那条会标「已合入（自动解冲突）」并列出被解掉的文件，需要 review。
- 结束后解析脚本输出的 JSON，把**每个 change 的 state / message 逐条列出来**（这是失败原因），
  冲突再按**仓库 → 文件 → 冲突块**展开，并给出：
  - 每个文件有几处冲突块、双方分别改到了哪些符号
  - 这些符号在仓库其它位置的引用点（改完要回归的地方）
  - 处理策略建议
- 完整输出落盘 `data/last-download.json`（含 `status`、每条 `changes[]`、日志尾部），
  页面刷新后也能回查；生成 `data/conflict-report.md` 可以直接丢给 CodeGraph / AI 继续分析。
- 页面底部有「排查」输入框，可以直接跑 `codegraph impact <符号>`、
  `git -C <repo> log --oneline -5` 之类只读命令（有白名单，写操作会被拒）。

#### 下载失败的常见原因

| 现象（changes[].message） | 原因 | 处理 |
|---|---|---|
| `本地没有找到 project=... 对应的仓库（扫描了 <目标目录>）` | 目标目录里没有这个项目的 git 工作区。**repo 工程**下很常见：`.repo/projects/<path>.git` 有对象，但工作区那份目录缺 `.git` 链接 | 在该目录执行 `repo sync <project路径>` 重建链接；预检会直接点名是哪个项目 |
| `工作区有未提交改动…先 commit/stash` | 仓库 dirty | 先 commit / stash，或 `git checkout -- .` |
| `上次 cherry-pick 未完成（CHERRY_PICK_HEAD 存在）` | 上一次冲突没处理完 | 解决后 `cherry-pick --continue`，或 `cherry-pick --abort` |
| `git fetch … 失败` | 仓库 remote 没权限 / 走不通 | 检查 `.git/config` 的 remote，或让 `gerrit_config.json` 里的账号密码可用 |
| `cherry-pick 结果为空` | 补丁内容本地已有 | 自动 `--skip`，算已合入 |

有未解决冲突时「下一步」是锁住的，解决完点「重新分析工作区冲突」自动放行。
注意：**repo 工程根目录本身不是 git 仓库**，这个分析会自动往下找子仓库逐个查
（早期版本直接在根上跑 `git diff -U`，永远返回空 → 误报「没有冲突」）。

### 3. 编译烧录

选 Family / Project（从 `configs/*/​*_defconfig` 自动解析），写入
`build/.active-build-state.json` 后在 `build/` 下执行 `active-build`。

附加参数栏可以填 `-c fw` 之类。日志实时输出。

**编译成功 → 整个界面变绿**，弹出「是否立即刷机」，确认后调 `v3dl app`。

> 刷机需要 sudo。网页没法输密码，所以启动服务后先在同一个终端执行一次
> `sudo -v` 预热（`v3dl` 里配了 1080 分钟的 sudo 超时）。页面会检测 sudo 状态，
> 没授权会直接提示而不是卡住。

### 4. 自测

刷机成功后自动跳转。清单包含开机、显示触摸、本次功能主流程、边界场景、蓝牙、
传感器、功耗七项，逐项标 OK / NG。

「设备状态」会自动跑 `v3dl com` 探测 Watch-Link 串口。全部 OK 显示通过，
有 NG 则标红并提示记录现象。

## 目录结构

```
tools/feature-portal/
├── server.py          # 后端（HTTP + map 解析 + 依赖分析 + 子进程任务）
├── web/
│   ├── index.html
│   ├── styles.css
│   └── app.js
└── data/              # 运行时状态（自动生成）
    ├── state.json     # 上次选择的路径、评估状态、编译目标（输入框内容也在这）
    ├── cache/         # map / 头文件索引缓存
    ├── topic-src/     # topic 代码备份目录（每个 topic 一份，留最近 3 份）
    ├── last-download.json
    └── conflict-report.md
```

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/bootstrap` | 环境自检 + 项目列表 + 状态 |
| POST | `/api/state` | 合并保存状态 |
| GET | `/api/fs/list?path=` | 目录浏览 |
| GET | `/api/storage` | 单独跑存储评估 |
| POST | `/api/eval/doc` | 文档评估 |
| POST | `/api/eval/code` | 代码评估（`{feature, topic, paths, path}`，`path` = 本地基准目录） |
| POST | `/api/port/plan` | gerrit 清单（只读，落到哪个仓库） |
| POST | `/api/port/precheck` | 移植预检（只读，含各仓库分支/dirty/cherry-pick 状态） |
| POST | `/api/topic/snapshot` | 把 topic 代码导到备份目录 `data/topic-src/`（不动工作区） |
| POST | `/api/port/download` | 拉代码（后台任务，单条失败不影响其它条） |
| POST | `/api/port/result` | 解析下载结果 + 逐条 `changes[]` 原因 + 冲突分析 |
| POST | `/api/port/conflict-analysis` | 扫当前工作区未解决冲突 |
| POST | `/api/build/start` | 编译（后台任务） |
| POST | `/api/flash/start` | 刷机（后台任务） |
| GET | `/api/selftest/items` | 自测清单 + 设备探测 |
| POST | `/api/tool/run` | 只读排查命令（白名单） |
| GET | `/api/job?id=&since=` | 拉任务增量日志 |

## 需要知道的限制

- **工期是估算**：按经验权重算的，用于排期参考，不是承诺。如果圈的是整个模块目录
  （几百个文件），数量会按上限折算并标注「建议用 gerrit topic 精确评估」。
- **代码评估用 gerrit topic** 才能拿到真实改动文件（`git show --numstat`，只读）；
  不填的话只能按你手动圈的路径扫。
- **map 解析只在编译后有效**：没编译过就没有 `build/out/*/binary/*.map`，
  存储那栏会提示先编译。
- 服务只监听 `127.0.0.1`，不对外网开放。
- 页面里的所有分析都是静态推断，不构成技术决策依据，最终以真机验证为准。

# Feature Portal 操作参考

可分发项目目录：`/home/hgw/workspace/feature-portal/scripts/feature-portal`

可执行入口：`/home/hgw/workspace/feature-portal/scripts/feature-portal-start`

## 启动与健康检查

```bash
/home/hgw/workspace/feature-portal/scripts/feature-portal-start \
  --workspace /path/to/local/code
# 或后台启动
/home/hgw/workspace/feature-portal/scripts/feature-portal-start \
  --workspace /path/to/local/code --idle-exit 8
curl -s http://127.0.0.1:8770/api/ping
```

端口可能从 8770 顺延到 8776。服务日志通常在 `/tmp/feature-portal.log`。

## 评估重点

代码评估请求：

```text
POST /api/eval/code
{
  "feature": "功能名",
  "topic": "Gerrit topic/change 链接",
  "paths": [],
  "path": "/本地代码基线"
}
```

结果重点查看：

- `missing.review`: change、文件、符号、缺失数量和扫描完整性
- `missing.items`: 本地缺失的函数、宏、类型
- `missing.missing_headers`: topic include 但本地找不到的头文件
- `missing.baseline`: 实际使用的本地基线和扫描范围
- `missing.hit_sample`: 本地已有符号的证据
- `errors` / `missing.note`: 降级、快照失败或扫描不完整信息

宏的正确判定是：topic 使用宏 → 本地必须能找到 `#define MACRO`。只有普通引用不算已定义；缺失项应显示 `kind=宏`。

## 移植、构建、刷机、自测

- `/api/port/plan`：只读解析 topic 变更
- `/api/port/precheck`：检查目标仓库、分支、脏状态和未完成 cherry-pick
- `/api/topic/snapshot`：导出 topic 快照，不修改工作区
- `/api/port/download`：fetch + cherry-pick，属于状态变更
- `/api/port/conflict-analysis`：分析未解决冲突
- `/api/build/start`：调用 active-build 构建
- `/api/flash/start`：调用 v3dl 刷机，可能需要 sudo
- `/api/selftest/items`：获取设备状态和自测清单

执行下载前确认目标工作区和脏状态；执行基线重置/同步前必须再次确认，因为可能丢失未提交改动。

## 常见故障

- 直接打开 `web/index.html`：API 会变成 `file:///api/...`，应改用本地 HTTP 服务。
- 找不到 change 对应仓库：目标 workspace 可能未 sync 或缺少 checkout。
- 评估结果显示扫描不完整：先确认 workspace 可读，再重跑评估；不应把不完整扫描结果当成确定的“本地缺失”。
- 存储状态为空：通常需要先完成一次构建，生成 `.map` 文件。
- 刷机卡住：先在启动服务的终端执行 `sudo -v`，再重试。

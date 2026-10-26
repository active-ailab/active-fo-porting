# 功能移植工作台 · Windows 启动器（带「正在加载中」提示窗）
# 双击 launch.vbs 会隐藏调用本脚本。逻辑：
#   0. 立刻弹一个加载中的小窗（否则双击后什么都没有，容易以为没反应）
#   1. 端口 8770~8776 里已有服务 -> 直接开浏览器
#   2. 没有 -> 在 WSL 里拉起 run.sh（git/repo 只能在 WSL 里工作）
#   3. WSL 不可用 -> 明确提示，不使用 Windows Python（避免 repo 链接失效）
#   4. 等到服务端口可用 -> 关掉小窗、开浏览器
# 关闭所有页面后，服务靠心跳超时自动退出（约 6 秒），无需手动停。

$Ports   = @(8770)
$Distro  = ''
$WslDir  = '/home/hgw/workspace/github/active-fo-porting/feature-portal/scripts/feature-portal'
$Idle    = 8
$LogPath = '/home/hgw/workspace/github/active-fo-porting/feature-portal/scripts/feature-portal/feature-portal-server.log'
$TempLog = Join-Path $env:TEMP 'feature-portal-launch.log'
$WinLog  = $TempLog
Set-Location -LiteralPath $env:TEMP

function Write-LaunchLog([string]$msg) {
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss.fff') $msg"
    try {
        Add-Content -LiteralPath $TempLog -Value $line -Encoding UTF8
    } catch { }
    if ($WinLog -ne $TempLog) {
        try { Add-Content -LiteralPath $WinLog -Value $line -Encoding UTF8 }
        catch { }
    }
}

Write-LaunchLog "launcher start; script=$($MyInvocation.MyCommand.Path); distro=$Distro; wsl_dir=$WslDir"

# 自动选择 WSL 发行版：优先 Ubuntu，找不到时使用第一个已安装发行版。
try {
    # wsl.exe -l -q 在 Windows PowerShell 中常以 UTF-16 字节输出，先转成
    # 字符串再去 NUL，避免发行版名被拆成字符或带隐藏空字节。
    $distroRaw = (& wsl.exe -l -q 2>$null | Out-String) -replace "`0", ''
    $distros = @($distroRaw -split "`r?`n" |
        ForEach-Object { $_.Trim() } |
        Where-Object { $_ -and $_ -notmatch '^Windows Subsystem' })
    $Distro = ($distros | Where-Object { $_ -match 'Ubuntu' } | Select-Object -First 1)
    if (-not $Distro) { $Distro = $distros | Select-Object -First 1 }
} catch { $Distro = '' }
Write-LaunchLog "selected distro=$Distro; available=$($distros -join ',')"
if ($Distro) {
    # Keep diagnostics beside the launcher and follow the detected distro name.
    Write-LaunchLog "diagnostic log path=$TempLog (WSL UNC logging disabled during startup)"
}

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()

function Test-Tcp([int]$p) {
    $c = New-Object System.Net.Sockets.TcpClient
    try {
        $ar = $c.BeginConnect('127.0.0.1', $p, $null, $null)
        if (-not $ar.AsyncWaitHandle.WaitOne(500)) { return $false }
        return $c.Connected
    } catch {
        return $false
    } finally {
        try { $c.Close() } catch { }
    }
}

function Test-TcpHost([string]$hostName, [int]$p) {
    $c = New-Object System.Net.Sockets.TcpClient
    try {
        $ar = $c.BeginConnect($hostName, $p, $null, $null)
        if (-not $ar.AsyncWaitHandle.WaitOne(500)) { return $false }
        return $c.Connected
    } catch { return $false }
    finally { try { $c.Close() } catch { } }
}

# 直接走 socket，绕开系统代理（有 http_proxy 时 Invoke-WebRequest 会 502）
function Test-Portal([int]$p) {
    $c = New-Object Net.Sockets.TcpClient
    try {
        if (-not $c.ConnectAsync('127.0.0.1', $p).Wait(80)) { return $false }
        $s = $c.GetStream()
        $s.ReadTimeout = 250
        $req = [Text.Encoding]::ASCII.GetBytes("GET /api/ping HTTP/1.0`r`nHost: 127.0.0.1`r`n`r`n")
        $s.Write($req, 0, $req.Length)
        $s.Flush()
        $buf = New-Object byte[] 128
        $n = $s.Read($buf, 0, 128)
        if ($n -le 0) { return $false }
        $txt = [Text.Encoding]::ASCII.GetString($buf, 0, $n)
        return ($txt -match '200')
    } catch {
        return $false
    } finally {
        try { $c.Dispose() } catch { }
    }
}

function Find-Running {
    # 默认端口优先；只有找不到时才检查备用端口。
    foreach ($p in $Ports) {
        # 这些端口由 feature-portal 专用。Windows/WSL 转发场景下，
        # 二次读取 /api/ping 可能被防火墙或代理拦截，但浏览器仍可正常访问；
        # 端口监听存在即可直接打开，避免误报 30 秒超时。
        if (Test-Tcp $p) { Write-LaunchLog "port listening: $p"; return $p }
    }
    return 0
}

function Stop-Portal([int]$p) {
    # Windows Python 进程可直接按监听端口结束；WSL 服务则通过其脚本路径
    # 精确停止，避免误杀其他项目的 Python/WSL 任务。
    try {
        $conns = Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue
        foreach ($c in $conns) {
            if ($c.OwningProcess -and $c.OwningProcess -ne 0) {
                try { Stop-Process -Id $c.OwningProcess -Force -ErrorAction SilentlyContinue } catch { }
            }
        }
    } catch { }
    try {
        # 不同步等待 WSL：某些机器首次唤醒 WSL 会卡住数十秒，导致加载
        # 对话框一直不结束。后台发出精确停止命令，启动流程继续往下走。
        Start-Process -FilePath 'wsl.exe' `
            -ArgumentList "-d $Distro -- bash -lc `"pkill -f '[s]erver.py --port $p' || true`"" `
            -WindowStyle Hidden -ErrorAction SilentlyContinue | Out-Null
    } catch { }
    for ($i = 0; $i -lt 30; $i++) {
        if (-not (Test-Tcp $p)) { break }
        Start-Sleep -Milliseconds 100
    }
}

# ---------------- 加载中窗口 ----------------
$Form = New-Object System.Windows.Forms.Form
$Form.Text = '功能移植工作台'
$Form.ClientSize = New-Object System.Drawing.Size(430, 156)
$Form.StartPosition = 'CenterScreen'
$Form.FormBorderStyle = 'FixedDialog'
$Form.MaximizeBox = $false
$Form.MinimizeBox = $false
$Form.TopMost = $true
$Form.ShowInTaskbar = $true
$Form.ShowIcon = $false
$Form.BackColor = [System.Drawing.Color]::FromArgb(24, 26, 30)
$Form.ForeColor = [System.Drawing.Color]::FromArgb(232, 234, 238)

$Title = New-Object System.Windows.Forms.Label
$Title.Text = '正在加载中…'
$Title.Font = New-Object System.Drawing.Font('Microsoft YaHei UI', 13, [System.Drawing.FontStyle]::Bold)
$Title.ForeColor = [System.Drawing.Color]::FromArgb(126, 224, 166)
$Title.AutoSize = $true
$Title.Location = New-Object System.Drawing.Point(22, 20)
$Form.Controls.Add($Title)

$Note = New-Object System.Windows.Forms.Label
$Note.Text = '正在确认本地服务…'
$Note.Font = New-Object System.Drawing.Font('Microsoft YaHei UI', 9)
$Note.ForeColor = [System.Drawing.Color]::FromArgb(176, 180, 190)
$Note.AutoSize = $false
$Note.Size = New-Object System.Drawing.Size(386, 42)
$Note.Location = New-Object System.Drawing.Point(24, 54)
$Form.Controls.Add($Note)

$Bar = New-Object System.Windows.Forms.ProgressBar
$Bar.Style = 'Marquee'
$Bar.MarqueeAnimationSpeed = 28
$Bar.Size = New-Object System.Drawing.Size(386, 10)
$Bar.Location = New-Object System.Drawing.Point(24, 100)
$Form.Controls.Add($Bar)

$Close = New-Object System.Windows.Forms.Button
$Close.Text = '关闭'
$Close.Size = New-Object System.Drawing.Size(84, 26)
$Close.Location = New-Object System.Drawing.Point(326, 118)
$Close.Visible = $false
$Close.Add_Click({ $Form.Close() })
$Form.Controls.Add($Close)

$Form.Add_Shown({ $Form.Activate() })
$Form.Show()
[System.Windows.Forms.Application]::DoEvents()

function Pump([int]$ms) {
    $end = (Get-Date).AddMilliseconds($ms)
    while ((Get-Date) -lt $end) {
        [System.Windows.Forms.Application]::DoEvents()
        Start-Sleep -Milliseconds 60
    }
}

function Set-Loading([string]$msg) {
    $Note.Text = $msg
    [System.Windows.Forms.Application]::DoEvents()
}

function Show-Fail([string]$msg) {
    $Title.Text = '启动失败'
    $Title.ForeColor = [System.Drawing.Color]::FromArgb(255, 180, 182)
    $Note.Text = $msg
    $Bar.Visible = $false
    $Close.Visible = $true
    [System.Windows.Forms.Application]::DoEvents()
    while ($Form.Visible) {
        [System.Windows.Forms.Application]::DoEvents()
        Start-Sleep -Milliseconds 80
    }
}

function Open-PortalUrl([string]$url) {
    # Different Windows environments handle WSL URLs differently. Try the
    # shell's default browser through several independent mechanisms.
    $methods = @(
        { Start-Process -FilePath $url -ErrorAction Stop },
        { Start-Process -FilePath 'cmd.exe' -ArgumentList @('/c', 'start', '""', $url) -WindowStyle Hidden -ErrorAction Stop },
        { Start-Process -FilePath 'explorer.exe' -ArgumentList $url -ErrorAction Stop },
        { Start-Process -FilePath 'rundll32.exe' -ArgumentList @('url.dll,FileProtocolHandler', $url) -WindowStyle Hidden -ErrorAction Stop }
    )
    foreach ($method in $methods) {
        try { & $method; Write-LaunchLog "browser open requested: $url"; return $true }
        catch { Write-LaunchLog "browser open method failed: $($_.Exception.Message)" }
    }
    return $false
}

# ---------------- 1. 已在运行 ----------------
Set-Loading '检测本地服务是否已在运行…'
$port = Find-Running
Write-LaunchLog "initial port=$port"
if ($port -gt 0) {
    Set-Loading "发现旧服务（端口 $port），正在关闭并重新启动…"
    Stop-Portal $port
    Write-LaunchLog "old service stop requested on port=$port"
}

# ---------------- 2. WSL 启动 ----------------
# 发行版已经由 `wsl -l -q` 验证过，不再额外执行 test -f；该命令在部分
# Windows PowerShell/WSL 组合下会返回 -1，导致有效的 WSL 被误判不可用。
$hasWsl = [bool]$Distro
Write-LaunchLog "wsl_available=$hasWsl distro=$Distro"

$launched = $false
$serviceUrl = ''
if ($hasWsl) {
    Set-Loading '正在 WSL 里启动服务（git / repo 只能在 WSL 里工作）…'
    try {
        $startOutput = & "$env:WINDIR\System32\wsl.exe" -d $Distro --exec /bin/bash "$WslDir/start-service.sh" 8770 2>&1
        $startRc = $LASTEXITCODE
        Write-LaunchLog "start-service.sh rc=$startRc output=$($startOutput -join ' ')"
        if ($startRc -ne 0) { throw "start-service.sh failed: $startRc" }
        $launched = $true
        $readyText = ($startOutput -join ' ')
        $ipMatch = [regex]::Match($readyText, 'ip=(\d{1,3}(?:\.\d{1,3}){3})')
        if ($ipMatch.Success) {
            $serviceUrl = "http://$($ipMatch.Groups[1].Value):8770/"
            Write-LaunchLog "using WSL IP service URL=$serviceUrl"
        }
        Write-LaunchLog "start-service.sh confirmed HTTP ready"
    } catch {
        $launched = $false
        Write-LaunchLog "run.sh launch failed: $($_.Exception.Message)"
    }
}

if (-not $launched) {
    Write-LaunchLog "no launcher available"
    Show-Fail "WSL 启动失败，请查看项目目录 feature-portal-launch.log。"
    exit 1
}

# ---------------- 4. 等就绪 ----------------
$tips = @(
    '马上就好，首次启动要等 WSL 里的 python 起来…',
    '正在等 /api/ping 返回…',
    '服务启动中，再等几秒…'
)
$port = 0
$wslReady = $launched
# Check the HTTP endpoint inside WSL first. Windows TCP forwarding can lag or
# fail independently, which previously produced a false 30-second timeout.
for ($i = 0; $port -eq 0 -and $i -lt 12; $i++) {
    if ($i -gt 0 -and ($i % 8) -eq 0) {
        Set-Loading $tips[[int](($i / 8 - 1) % $tips.Count)]
    }
    Pump 250
    if (-not $Form.Visible) { exit 0 }
    $port = Find-Running
    if (($i % 4) -eq 0) { Write-LaunchLog "windows localhost probe $i port=$port" }
    if ($port -gt 0) { break }
}

if ($port -gt 0) {
    Write-LaunchLog "service ready on port=$port"
    if (-not $serviceUrl) { $serviceUrl = "http://127.0.0.1:$port/" }
}

if ($port -gt 0) {
    $Title.Text = '正在进入服务…'
    Set-Loading "服务已就绪（端口 $port），正在打开浏览器…"
    # Always prefer Windows localhost forwarding. The WSL internal IP may be
    # unreachable from the user's browser even though the service is healthy.
    $serviceUrl = "http://127.0.0.1:$port/"
    if (-not (Open-PortalUrl $serviceUrl)) {
        Show-Fail "服务已启动，但无法调用系统浏览器。请手动打开：$serviceUrl"
        exit 1
    }
    Pump 500
    $Form.Close()
    exit 0
}

$tail = ''
if ($hasWsl) {
    try { $tail = (& wsl.exe -d $Distro -- tail -n 12 $LogPath 2>&1) -join "`r`n" } catch { }
}
if ($wslReady) { $tail = "WSL API 正常，但 Windows 无法连接 localhost 或 WSL IP。`r`n" + $tail }
Show-Fail "服务启动失败。日志（$LogPath）：`r`n$tail"
Write-LaunchLog "startup failed; wsl_tail=$tail"
exit 1

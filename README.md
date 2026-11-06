# GCP VM 管理系统

一个轻量的 Google Cloud Compute Engine 实例管理面板，重点解决 **IP 被墙自动检测与更换**。

## 功能

- **多账号**：上传多个 GCP 服务账号密钥，自动发现各项目下所有区域的实例
- **新建 / 删除实例**：在面板里选区域、机型、系统、磁盘、Spot 直接开新实例，自动写入面板 SSH 公钥、开启被墙自动换 IP，可附带开机脚本；删除时一并释放本系统创建的静态 IP
- **手动操作**：开机、关机、重启、一键更换外网 IP、单次被墙检测
- **被墙自动检测**：通过部署在国内的探针（或本机在国内时直接检测）定时 TCP 探测实例端口
  - 国内不通 + 海外通 → 判定为被墙；连续 N 次（默认 3）后自动更换 IP
  - 国内外都不通 → 判定为实例/服务故障，**不会**换 IP
  - 探针自身异常 → 不计数，避免误判
- **先验证再接受**：换 IP 时每拿到一个新 IP，先绑定并用国内探针检测，国内不通（别人用过、早已被墙的 IP）就记入被墙名单、占住并继续换下一个，直到拿到验证可达的 IP（可在检测设置中关闭）
- **安全阀**：换 IP 冷却时间、每日最多自动更换次数、新 IP 自动避开近 30 天被墙过的 IP
- **临时 IP / 静态 IP** 两种模式
  - 临时 IP（默认，免费）：删除并重新分配外网 IP
  - 静态 IP：预留新地址 → 绑定 → 释放本系统创建的旧地址（用户自己预留的不会被删除）
  - 换 IP 失败时自动补回临时 IP，避免实例失联
- **自动开机**：Spot / 抢占式实例被回收或意外关机时自动拉起
- **Telegram 通知**：换 IP、自动开机、失败等事件推送
- **执行命令**：在面板里直接对实例执行安装/运维脚本
  - SSH 执行：以 root 执行 bash 脚本并实时显示输出，可停止；内置常用脚本（系统信息、更新系统、开启 BBR、安装 Docker），也可保存自己的
  - 启动脚本：写入实例元数据 `startup-script`，开机时自动执行（可选仅执行一次、写入后立即重启），可查看串口启动日志；SSH 连不上时也能用
- **防火墙**：查看、新建、启用/禁用、删除项目的 VPC 防火墙规则，一键放行全部端口 / 80、443
- **实时速率**：实例安装轻量流量上报后，面板显示每台实例的实时上下行速率；可按「分组」生成公开只读速率页，用 iframe 嵌入 NY 等转发面板的公告
- **自有服务器（非 GCP）**：香港等其他厂商的 VPS 也能接入：被墙检测 + TG 通知（无服务商 API，不能自动换 IP，提示手动处理）、实时速率与分组、执行命令、服务监控
- **服务监控**：已安装上报的实例 / 服务器，转发服务（默认 nyanpass）停止或机器失联超过设定时间就发 TG 通知，恢复时再通知（GCP 实例同样适用）
- **事件日志**：所有操作和检测结果可追溯

## 工作原理

```
                     ┌──────────────────────────┐
  浏览器 ──登录──▶   │  管理端（海外，本项目）  │──REST──▶ GCP Compute API
                     │  定时检测 / 换 IP / 开机 │
                     └───────┬─────────┬────────┘
                 ① 请求探测 │         │ ② 海外对照 TCP 探测
                             ▼         ▼
                  ┌────────────┐   ┌──────────────┐
                  │ 国内探针   │──▶│  GCP 实例    │
                  │ probe.py   │TCP│  ip:port     │
                  └────────────┘   └──────────────┘
```

管理端直接调用 Compute Engine v1 REST API（服务账号 JWT 换取访问令牌），不依赖庞大的 `google-cloud-compute` SDK。

## 快速开始

### 1. 准备 GCP 服务账号

GCP 控制台 → IAM 和管理 → 服务账号 → 创建服务账号 → 授予角色 → 创建 JSON 密钥。

| 场景 | 需要的角色 |
| --- | --- |
| 最简单 | `Compute Admin`（roles/compute.admin） |
| 最小权限（仅临时 IP） | `Compute Instance Admin (v1)` |
| 使用静态 IP 模式 | 再加 `Compute Network Admin`（用于预留/释放地址） |
| 使用「防火墙」页 | `Compute Admin` 已包含；最小权限时需 `Compute Security Admin` |
| 使用「执行命令」 | 再加 `Service Account User`（roles/iam.serviceAccountUser）。实例以服务账号身份运行时，修改其元数据（写 SSH 公钥、启动脚本）需要此角色 |

也可以只对实例使用的服务账号授权（权限更小），在 Cloud Shell 执行：

```bash
gcloud iam service-accounts add-iam-policy-binding 项目编号-compute@developer.gserviceaccount.com \
  --member="serviceAccount:面板服务账号邮箱" --role="roles/iam.serviceAccountUser"
```

### 2. 部署管理端（建议部署在海外服务器）

Docker Compose：

```bash
git clone https://github.com/thepunk01/gcp-vm.git && cd gcp-vm
cp .env.example .env        # 修改 GCPVM_ADMIN_PASSWORD
docker compose up -d --build
```

或直接运行（Python 3.10+）：

```bash
pip install -r requirements.txt
export GCPVM_ADMIN_PASSWORD='你的强密码'
python -m app               # 默认 http://0.0.0.0:8080
```

浏览器访问 `http://服务器IP:8080`，建议在前面加一层 Nginx/Caddy 配 HTTPS。

### 3. 部署国内探针（管理端在海外时必需）

探针仅依赖 Python 3 标准库，有两种模式：

**主动上报模式（推荐，探针设备不需要公网 IP）**

适合家里的路由器（OpenWrt / iStoreOS）、NAS、树莓派。探针每 20 秒左右主动连接面板，领取需要检测的 IP:端口，探测后上报结果。

1. 面板「检测设置」→「+ 添加主动上报探针」，填名称（如 家里电信），令牌自动生成，点「保存设置」
2. 点该探针的「安装命令」，按提示在设备上执行（探针脚本直接从面板 `/probe/probe.py` 下载，不依赖 GitHub）
3. 在 OpenWrt / iStoreOS 上大致是：

```bash
opkg update && opkg install python3
mkdir -p /root/gcpvm-probe && cd /root/gcpvm-probe
wget -O probe.py http://面板地址:8080/probe/probe.py
wget -O /etc/init.d/gcpvm-probe http://面板地址:8080/probe/gcpvm-probe.openwrt && chmod +x /etc/init.d/gcpvm-probe
# 编辑 /etc/init.d/gcpvm-probe 中的 PANEL 与 TOKEN
/etc/init.d/gcpvm-probe enable && /etc/init.d/gcpvm-probe start
logread -e gcpvm    # 看到「已连接面板」即成功
```

4. 回到面板刷新「检测设置」，探针显示「在线」即可

说明：面板缓存每个探针的最新结果（90 秒内有效）；新 IP（如手动「检测测试」或刚换的 IP）会登记为待测，等探针下一轮上报，通常 20 秒内出结果；探针超过 2 分钟未连接视为离线，不参与判断。

**监听模式（探针需要公网 IP）**

在一台有公网 IP 的 **中国大陆** 机器（国内 VPS 等）上运行：

```bash
# 只需要 probe/probe.py 这一个文件
PROBE_TOKEN=$(openssl rand -hex 16) && echo $PROBE_TOKEN
PROBE_TOKEN=上面生成的令牌 python3 probe.py      # 监听 0.0.0.0:9800
```

长期运行可用 systemd：把 `probe.py` 放到 `/opt/gcpvm-probe/`，修改 `probe/gcpvm-probe.service` 中的令牌后：

```bash
cp probe/gcpvm-probe.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now gcpvm-probe
```

探针安全措施：令牌鉴权；只接受 IP（不解析域名，避免 DNS 污染干扰）；默认禁止探测内网与保留地址。建议防火墙只放行管理端 IP 访问 9800 端口。

可以部署多个探针（如电信/联通/移动各一台），系统按国内可达率综合判断，更不容易误判。

> 如果管理端本身就部署在国内，在「检测设置」中选择「本机直接检测」即可，无需探针。

### 4. 在控制台中配置

1. **账号** 页：上传服务账号 JSON → 自动验证并同步实例
2. **检测设置** 页：填入国内探针地址与令牌，用「检测测试」确认探针工作正常
3. **实例** 页：点击实例的「设置」
   - 打开「被墙自动更换 IP」
   - **检测端口** 填实例上实际开放的 TCP 端口（如代理服务端口或 SSH 22），并确认 GCP 防火墙已放行

## 新建与删除实例

「实例」页点「+ 新建实例」：

- 区域列表把常用的亚洲区域（台湾、香港、东京、新加坡等）排在前面；机型只列常用的 x86 系列（E2、N2D、N2、T2D、N1、C2D、C3D、C3，32 vCPU 以内）
- 系统可选 Debian 12/13、Ubuntu 24.04/22.04、Rocky Linux 9、AlmaLinux 9
- 新实例使用临时外网 IP（可选高级 / 标准网络层级），不附加服务账号；创建时写入面板 SSH 公钥并设置 `enable-oslogin=FALSE`，建好即可用「命令」执行脚本
- 可勾选 Spot（便宜但可能被回收，会自动勾上「自动开机」），可填写开机后执行一次的脚本，可从常用脚本中选择
- 创建通常需要 30-60 秒；配额不足、可用区资源不足时会给出中文提示

删除：实例「设置」→「删除实例」，需要输入实例名称确认。启动盘随实例一起删除；如果使用的是本系统创建的静态 IP（`gcpvm-` 开头）会一并释放，用户自己预留的静态 IP 会保留。

## 自有服务器（非 GCP）

「账号」页 →「添加自有服务器」，填名称、IP、SSH 端口 / 用户、分组、检测端口：

- **填 root 密码**：面板用密码登录一次，写入面板 SSH 公钥并安装上报，**密码不保存**
- **不填密码**：添加后自动复制「接入命令」，到服务器上用 root 执行一次即可（`curl … | bash`，同样写入公钥 + 安装上报）

之后可以在面板里执行命令、看实时速率，并参与公开速率页的分组统计。被墙检测与 GCP 实例相同（国内探针 + 海外对照），达到连续被墙阈值时通知一次、恢复时再通知；因为没有服务商 API，**不会自动换 IP**。在服务商后台换了 IP 后，到服务器「设置」里更新 IP 即可。

## Azure

账号页切换到「Azure」添加。先在 Azure Cloud Shell 创建服务主体：

```bash
az ad sp create-for-rbac --name gcpvm --role Contributor --scopes /subscriptions/<订阅ID>
```

把输出的 JSON（appId / password / tenant）粘贴进去，再填订阅 ID。之后 Azure 虚拟机与 GCP 实例一样支持：同步、开关机（关机为「解除分配」，停止计费）、重启、被墙检测与自动换 IP（新建 Standard 静态公网 IP 换上，旧 IP 自动删除）、自动开机、SSH 执行命令（首次通过 Azure「运行命令」写入面板公钥到 root）、新建与删除（删除时一并清理网卡、公网 IP、系统盘）。
面板新建的 Azure 实例放在资源组 `gcpvm-<区域>`，自动创建虚拟网络和放行全部入站的网络安全组。Azure 不参与自动替补。

## 自动替补（GCP）

设置 →「自动替补」：开启后，勾选了「自动替补」的 GCP 实例在以下情况会按替补模板在**下一个有空位的 GCP 账号**新建一台：

- 账号被封 / 停用：连续两轮检测访问账号都返回 401/403、项目被暂停、结算停用等（网络错误、5xx 不算），账号标记为「被封」；
- 实例被删除，或被挂起（连续两轮）。
- 实例停机超过设定分钟数（默认 10，0 为不处理）：先自动开机；开不了且是结算停用、无权限等账号问题，判定账号停用并整号替补；其他原因（如无资源）则替补并删除旧实例。

每个账号最多开「每个账号最多实例数」台（默认 3），开满就用下一个账号；新实例默认沿用原实例的可用区和机型（新账号在该区开不出来时改用模板的），并沿用分组、检测端口、自动换 IP，模板里的开机脚本（安装 nyanpass、对接 NY 等）和面板上报会自动执行，完成后 TG 通知。「24 小时内最多替补台数」防止失控。**按分组替补**：可以给每个分组添加专属模板（可用区、机型、开机脚本各不相同），哪个分组的机器挂了就用哪个分组的模板，没有专属模板的分组用默认模板。**勾选开机命令**：实例「设置」里可勾选若干「常用脚本」，它被替补时新机首次开机按列表顺序执行这些脚本（在模板脚本之后），新机继承同样的勾选。设置页的「按当前模板新建一台」可用于首次开满或测试模板。账号恢复后在账号页点「解除标记」。

## 定时 Telegram 报告

设置 →「通知」→「定时报告」：按设定时间（北京时间，如 `09:00,21:00`）把本周期各分组的峰值带宽、更换 IP 次数（含自动次数）和自动替补次数推送到 Telegram 群组，推送后重新开始统计。先把机器人拉进群组，填群组 Chat ID（一般 -100 开头），留空则发到通知的 Chat ID。「立即发送一次（预览）」不影响定时统计。

## 附加 IP（GCP）

实例详情 →「概览」→「附加 IP」：给一台机器挂多个公网 IP（每台最多 8 个）。用 GCP 协议转发实现（静态地址 + 转发规则 → 目标实例，所有协议、所有端口），机器上无需配置，监听 0.0.0.0 的服务（如 nyanpass）在每个 IP 上都可用。
每个附加 IP 单独做被墙检测（跟随实例的「被墙自动换 IP」开关、冷却按 IP 单独计算），被墙只换那一个 IP，主 IP 和其他 IP 不受影响；新 IP 同样先规避已知被墙 IP 并用国内探针验证。删除实例时自动清理附加 IP；自动替补时新实例会补上同样数量的附加 IP。
费用：每个 IP 约 $3.6/月；同区域外部 IP 数量受 `IN_USE_ADDRESSES` / `STATIC_ADDRESSES` 配额限制。

## 服务监控

「检测设置」→「服务监控」：开启后，面板每 10 秒检查一次已安装上报的实例 / 服务器：

- **失联**：超过设定时间（默认 60 秒）没有收到上报（宕机、网络中断、上报服务停止）
- **服务停止**：监控的 systemd 服务（默认 `nyanpass`）不是运行状态并持续超过设定时间

每种异常只通知一次，恢复后再通知一次。修改监控的服务名后，需要在各机器上重新安装一次上报。适合多台机器共用同一个 NY 设备（NY 无法发现其中某一台掉线）的场景。

## 实时速率与公开速率页

1. **安装流量上报**：实例「命令」→「填入安装脚本」→「执行」。会在实例上安装 `gcpvm-traffic` systemd 服务（一个 bash 脚本，每 2 秒用 curl 上报默认出口网卡的累计收发字节数，几乎不占资源）。上报地址默认是你当前打开面板用的地址，实例需要能访问到它。卸载：「填入卸载脚本」→「执行」
2. **设置分组**：实例「设置」→「分组」，例如 `香港入口`。组名建议和转发面板里的设备组一致
3. **开启公开速率页**：「检测设置」→「公开速率页」，勾选开启（自动生成访问密钥），保存设置，复制嵌入代码
4. **嵌入 NY 面板**：把嵌入代码（`<iframe …>`）粘到 NY 后台「站点公告」并保存，用户登录后在主页即可看到每个组的实时速率

速率页只显示组名、在线节点数和合计上下行速率，不包含 IP 和实例名；没有设置分组的实例不会出现。速率按实例网卡计算（bit/s），实例超过 10 秒未上报视为离线。速率页通过 SSE 每 2 秒推送更新（被代理拦截时自动退回每 2 秒轮询）。

> NY 面板是 HTTPS 时，浏览器会拦截 HTTP 的 iframe，所以本面板也需要 HTTPS 访问（例如用 Caddy 反代：`gcpvm.你的域名 { reverse_proxy 127.0.0.1:8080 }`），并用 HTTPS 地址生成嵌入代码和安装上报脚本。

## 执行命令

实例卡片上点「命令」打开执行窗口。

**SSH 执行**

- 面板首次使用时在数据目录生成一把 ed25519 密钥（`data/ssh/id_ed25519`），执行前自动把公钥写入实例元数据 `ssh-keys`（保留实例上原有的其他公钥）
- GCP guest agent 会自动创建用户 `gcpvm` 并授予免密 sudo，脚本以 root 身份执行，`DEBIAN_FRONTEND=noninteractive`
- 首次写入公钥后需要 10-30 秒生效，面板会自动等待重试
- 要求：实例在运行、有外网 IP、防火墙放行 22 端口（默认网络自带 `default-allow-ssh` 规则）、未启用 OS Login（`enable-oslogin` 不为 TRUE）
- 管理端部署在海外时，IP 被墙不影响 SSH 执行
- 输出实时显示，最长保留 1 MB；可设置超时（2-60 分钟）或手动停止；同一实例同时只能有一个命令在执行
- 任务记录保存在内存中，面板重启后清空（执行结果会写入事件日志）

**启动脚本**

- 写入实例元数据 `startup-script`，由 GCP 在每次开机时执行，不依赖 SSH
- 勾选「仅执行一次」（默认）时，脚本成功执行后会在实例上留下标记 `/var/lib/gcpvm/startup-*.done`，之后开机不再执行；失败则下次开机重试
- 勾选「写入后立即重启执行」会强制重启运行中的实例（关机的实例则开机）
- 「启动日志」读取实例串口输出中与启动脚本相关的行，勾选「显示全部」可看完整串口日志

> 安全提示：能登录面板就能以 root 在所有实例上执行命令，请使用强密码并为面板配置 HTTPS。

## 检测设置说明

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| 检测间隔 | 300 秒 | 每轮检测所有开启自动功能的实例 |
| 连续被墙次数阈值 | 3 | 连续 N 轮判定被墙才换 IP，防止网络抖动误判 |
| TCP 超时 / 重试 | 5 秒 / 3 次 | 任意一次连通即视为可达 |
| 国内可达率阈值 | 0.5 | 多探针时，成功比例低于该值视为国内不可达 |
| 海外对照检测 | 开 | 海外也不通时视为服务故障，不换 IP |
| 换 IP 冷却 | 30 分钟 | 两次自动更换的最短间隔 |
| 每日上限 | 10 次 | 24 小时内每台实例最多自动更换次数 |

## API

所有接口（登录除外）需要 `Authorization: Bearer <token>`。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/login` | `{"password": "..."}` → `{"token": "..."}` |
| GET / POST | `/api/accounts` | 列出 / 添加账号 `{"name": "", "key": {...}}` |
| DELETE | `/api/accounts/{id}` | 删除账号 |
| GET | `/api/instances` | 实例列表 |
| POST | `/api/sync` | 从 GCP 同步实例 |
| PATCH | `/api/instances/{id}` | 修改实例配置 `auto_ip, auto_start, check_port, ip_mode, grp, auto_replace, boot_scripts, note` |
| POST | `/api/instances/{id}/action` | `{"action": "start|stop|reset|change_ip|check|refresh"}` |
| POST | `/api/instances/{id}/exec` | SSH 执行 `{"script": "...", "timeout": 600}` → 任务 |
| GET | `/api/jobs/{id}?offset=0` | 任务状态与从 offset 起的新输出 |
| POST | `/api/jobs/{id}/cancel` | 停止任务 |
| GET / PUT / DELETE | `/api/instances/{id}/startup-script` | 查看 / 写入 `{"script", "run_once", "reboot"}` / 清除启动脚本 |
| GET | `/api/instances/{id}/serial-log?all=1` | 串口启动日志 |
| GET | `/api/ssh-key` | 面板 SSH 公钥 |
| GET / POST | `/api/accounts/{id}/firewalls` | 列出防火墙规则与网络 / 新建 `{"name", "rules": "all 或 tcp:443 udp:443", "ranges", "direction", "action", "priority", "target_tags", "network"}` |
| GET | `/api/accounts/{id}/create-options` | 新建实例可选的可用区、网络、系统镜像 |
| GET | `/api/accounts/{id}/machine-types?zone=` | 可用区内的机型 |
| POST | `/api/accounts/{id}/instances` | 新建实例 `{"name", "zone", "machine_type", "image", "disk_size_gb", "disk_type", "network_tier", "spot", "tags", "startup_script", "auto_ip", "auto_start", "check_port", "note"}` |
| DELETE | `/api/instances/{id}` | 删除实例 `{"confirm_name": "实例名"}` |
| PATCH / DELETE | `/api/accounts/{id}/firewalls/{name}` | 启用/禁用 `{"disabled": true}` / 删除 |
| POST | `/api/instances/{id}/traffic-agent` | 生成流量上报安装脚本 `{"panel_url": "https://…"}`（`{"uninstall": true}` 为卸载脚本） |
| GET | `/api/traffic` | 各实例实时速率（含服务状态） |
| POST | `/api/servers` | 添加自有服务器 `{"name", "ip", "ssh_port", "ssh_user", "password"(可选), "grp", "check_port", "panel_url"}` |
| PATCH / DELETE | `/api/servers/{id}` | 修改名称 / IP / SSH 端口 / 用户；从面板移除 |
| POST | `/api/servers/{id}/bootstrap-command` | 生成在服务器上执行的接入命令 |
| POST | `/api/traffic-agent/report` | 实例上报流量（实例令牌鉴权） |
| GET | `/api/public/traffic?key=` | 公开速率数据（按组，需开启并带密钥） |
| GET | `/api/public/traffic/stream?key=` | 同上，SSE 每 2 秒推送 |
| GET | `/status?key=&theme=light\|dark\|auto` | 公开速率页（可 iframe 嵌入） |
| GET | `/api/push-probes` | 主动上报探针在线状态 |
| GET | `/api/probe-agent/tasks` | 探针领取任务（探针令牌鉴权） |
| POST | `/api/probe-agent/report` | 探针上报结果（探针令牌鉴权） |
| GET / PUT | `/api/settings` | 检测设置 |
| POST | `/api/probe-test` | `{"host": "1.2.3.4", "port": 22}` |
| POST | `/api/run-check` | 立即触发一轮检测 |
| GET | `/api/events?limit=200` | 事件日志 |

示例：用脚本手动开机

```bash
TOKEN=$(curl -s -X POST http://127.0.0.1:8080/api/login -d '{"password":"xxx"}' | jq -r .token)
curl -X POST http://127.0.0.1:8080/api/instances/1/action \
  -H "Authorization: Bearer $TOKEN" -d '{"action":"start"}'
```

## 注意事项

- 服务账号密钥以明文保存在 `data/gcpvm.db`，面板 SSH 私钥保存在 `data/ssh/`，请保护好数据目录与服务器
- 临时 IP 在实例 **关机** 时会被 GCP 回收，开机后 IP 会变化；需要固定 IP 请用静态 IP 模式（注意未绑定的静态 IP 会计费）
- GCP 新分配的临时 IP 可能也在被墙网段中，系统会在下一轮检测后继续更换，直到达到每日上限
- 更换 IP 会中断现有连接

## 开发

```bash
pip install -r requirements-dev.txt
pytest
```

目录结构：

```
app/
  gcp.py           Compute Engine REST 客户端（服务账号 JWT 鉴权）
  checker.py       被墙检测（国内探针 + 海外对照）
  manager.py       业务逻辑：同步、开关机、换 IP、自动检测循环、执行命令
  remote.py        SSH 密钥与远程执行、启动脚本封装
  firewall.py      防火墙规则解析与展示
  provision.py     新建实例：镜像、区域、机型筛选与请求体
  traffic.py       流量上报脚本、实时速率与分组汇总
  jobs.py          命令任务与输出缓存
  integrations.py  Telegram 通知
  db.py            SQLite 存储
  web.py           Web API
  static/          Web 控制台
  pushprobe.py     主动上报探针的任务分发与结果缓存
probe/probe.py     国内探针（纯标准库，监听 / 主动上报两种模式）
probe/gcpvm-probe.openwrt  OpenWrt / iStoreOS 开机自启脚本
tests/             单元测试（模拟 GCP，无需真实账号）
```

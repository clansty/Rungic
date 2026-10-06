# 116 Linux 的独立网络（作为 Rungic 应用联网）

2026-10-06，task #24。起因：一位用户的手机开着 Clash 全局 VPN，重启后 Linux 的解析器是空的，root 发出的 HTTPS 在 tun0 里 TLS 失败，而安卓应用都正常；Codex 和 Raft 离线。

## 根因

容器和安卓共用网络命名空间（`lxc.net.0.type = none`）。Linux 的连接带着 Linux 的 uid（root 0、用户 1000）进入安卓的网络栈。对安卓来说这是系统身份：netd 的按 uid 路由、VPN 应用的分应用规则、私人 DNS、按流量计费都按“系统”处理，和普通应用不同。各家 VPN 怎么对待 uid 0/1000 不一样，所以“开了 VPN，Linux 的网络是否正常”取决于那个 VPN 应用。

修 DNS（解析器永不留空，提交 4a75a91）只治症状。

## 做法

Linux 有自己的网络命名空间（`lxc.net.0.type = empty`）。安卓这边用 pasta（passt 项目，Podman 的默认用户态网络）替它联网：pasta 以 Rungic 应用的安卓 uid 运行，用普通 socket 发出 Linux 的连接。安卓和 VPN 把 Linux 当成 Rungic 应用，和别的应用一样处理：走 VPN、用 VPN 的 DNS、守分应用规则。

- **Linux 一侧**：网卡 eth0，地址 10.0.2.15/24，网关 10.0.2.2（用户态网络的惯例）。
  - DNS 仍由 network-manager 按安卓默认网络写。服务器地址经 pasta 发出，所以 VPN 的 DNS（比如 SwiftWire 的 198.18.0.1）照常可用。
  - 带接口的 IPv6 链路本地 DNS 在独立网络里没有对应接口，会被跳过；pasta 的网关不当作 DNS。
- **端口**：
  - 进：22（局域网 SSH 照旧）。
  - 出：Linux 连自己 127.0.0.1 上的端口，若安卓侧在这个端口监听（PulseAudio 的 20017 等），由 pasta 转到安卓的回环（`-T auto -U auto`）。
- **抽象 socket**：抽象 Unix socket 属于网络命名空间。Linux 访问安卓服务用的三个（`com.rungic.device.v1`、`com.rungic.calls.v1`、`com.rungic.clipboard.v1`）由 `rungic-own-network host` 中转：在 Linux 的命名空间里监听同名 socket，连到安卓的服务。
  - 只转发 Linux 的 root 和 1000（安卓服务原本接受的就是这几个 uid）。
  - 中转以 root 连接安卓的服务，服务端接受 root。
  - Linux 的客户端会检查服务端的 uid：设备和通话服务是 root（0），剪贴板服务是 shell（2000）。中转在 Linux 一侧的 socket 按各自的 uid 开始监听（SO_PEERCRED 取监听时的身份），客户端看到的和以前一样。第一次实机验收时剪贴板就是因为这一点失败的（中转以 root 监听，剪贴板客户端拒绝了它）。

## 怎么启动

`rungic-own-network host --app-uid UID` 由安卓侧的 `system/rungic-plasma` 在 `lxc-start` 之后启动：
- 进入容器的 mount 和 PID 命名空间（用容器里的 python 和 pasta，`/proc` 是容器的）；
- 留在安卓的网络命名空间，以 root 运行；
- 用 `setsid -f` 脱离，App 进程被结束后照常运行（mibook 的验收条件：强行停止 App 后转发、DNS、已有连接都不断）。

它做三件事：
1. 把容器 1 号进程的网络命名空间绑定挂载到 `/run/rungic-own-network/netns`。pasta 以应用 uid 运行，打不开别的用户的 `/proc/PID/ns`，而且会关掉继承的文件。
2. 建中转。
3. 启动 pasta 并看护：退出后按 1、2、4…30 秒退避重启（恢复优先）。

pasta 用 `setpriv` 切到应用 uid，带环境能力 sys_admin、net_admin、net_bind_service：进入命名空间、配置网卡、监听 22 端口。不用 pasta 的 `--runas`：它在进入命名空间之前就降了权限，之后会 EPERM。

状态在 `/run/rungic-own-network/status.json`，日志在 `/var/log/plasma/own-network.log`。

## 开关

设置 → 服务 → “独立网络”，默认打开，和 SSH 一样是一个服务组（`desktop/services/policy.json` 的 `own-network`）。

- 开关就是 `rungic-own-network.service` 是否启用。
- `rungic-network-mode.path` 监视启用状态的变化，由 `rungic-own-network mode` 写 `/var/lib/rungic-host/network-mode`（own / shared）。这个目录就是安卓侧的 `$LXC/state/host`。
- 安卓侧在下一次启动 Linux 时读这个文件。网络命名空间在 systemd 启动之前就定了，所以**切换在下次启动 Linux 时生效**；`rungic-own-network status` 显示当前实际是哪种。
- 默认值在 rungic-plasma-bridges 安装时设置一次（记在 `/var/lib/rungic/service-defaults`），用户关掉以后升级不会再打开。
- 建不起来时写 `network-mode.failed`：找不到容器进程或应用 uid、中转进程起不来、没装 pasta、pasta 一启动就退出（连续三次）。下一次启动用共享网络一次，然后再试独立网络。

## 原型（2026-10-06，G100 S，不重启容器）

在运行中的容器旁边另起一个网络命名空间验证同样的链路：
- 地址 10.0.2.15/24，默认路由经 10.0.2.2；
- OpenAI、GitHub、Raft、百度的 HTTPS 都通；
- DNS 走 SwiftWire 的 198.18.0.1；
- 安卓侧看到的连接属于 uid 10353（Rungic 应用）。

试过的坑：
- `pidof` 会找到用户的 systemd → 用 `/proc/*/status` 的 NSpid “<主机 pid> 1”，正式实现用 `lxc-info -pH`；
- 不加 `-p` 时 pasta 报 “Can't determine init namespace”；
- `--runas` 报 EPERM；
- 日志文件的属主要是应用 uid。

## 检查

- 离线：`tools/tests/test_own_network.py`（pasta 参数、中转的 uid 检查、路由解析、开关文件），`test_resolv_conf.py`（独立网络里的 DNS 选择）。
- 实机（G100 S，SwiftWire 开着），独立网络和共享网络各一遍，加重启：
  - 三类流量：root（系统服务）、用户（浏览器、Codex）、Raft；
  - 设备后端、剪贴板、通话 socket、声音（127.0.0.1:20017）；
  - 局域网 SSH 进来；
  - 吞吐和 CPU；
  - 强行停止 Rungic App 后网络照常，App 回来后正常。

## 实机验收（2026-10-06，G100 S，SwiftWire 开着）

Kevin 同意后换了安卓侧脚本（旧的备份为 `rungic-plasma.bak-own-network`），重启 Linux 三次：打开、关掉、再打开。

- 独立网络：
  - eth0 10.0.2.15/24，默认路由经 10.0.2.2，DNS 198.18.0.1（SwiftWire）；
  - pasta 以 u0_a353（10353）运行；Linux 的连接在安卓侧都属于这个 uid（Linux 不在安卓的网络命名空间里，只能经 pasta 出去）。
- 三类流量：root 和用户 1000 访问 GitHub、百度都是 200，OpenAI、Raft 的 API 有正常的 HTTP 应答（421、404）。Codex 和 Raft 桌面程序本身没有单独验。
- 安卓服务：
  - 网络桥（Connectivity 4）、调制解调器、蓝牙桥都在运行；
  - 设备、通话、剪贴板三个中转都在用，客户端看到的服务端 uid 分别是 0、0、2000；
  - 声音：向 android 输出（经 127.0.0.1:20017）播放成功。
- 从局域网 SSH 连 22 端口：拿到 OpenSSH 的应答。
- 强行停止 Rungic App：pasta 不受影响，一个限速 200 KB/s 的下载在这期间没有中断（25 秒收到 4.6 MB）；之后再打开 App，桌面照常。
- 吞吐：经 SwiftWire 下载 GitHub，独立网络 0.67 MB/s，共享网络 0.90 MB/s，受 VPN 线路限制，两者差别在波动范围内；pasta 约占一个核的 3%~8%。
- 关掉开关后重启：回到共享网络（Linux 看到安卓的全部接口），联网正常；再打开开关重启：回到独立网络。`start` 在 12 秒左右返回。

验收中改掉的问题：
- 剪贴板：中转以 root 监听，剪贴板客户端要求服务端是 shell（2000），拒绝连接。现在每个中转按客户端检查的 uid 监听。
- `start` 不返回：脱离出去的中转进程继承了调用方的输出管道，App 和 adb 一直等它关闭。现在不继承。
- pasta 第一次启动时 Linux 的 `/dev/net/tun` 还没出现，失败一次，2 秒后重试成功。现在先等它出现。

未验：手机整机重启后的自动启动（走同一个 `start`）；IPv6 外网；通话中的实际音频（通话服务的通道和身份已验）。

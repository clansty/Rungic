# APK 数据被清除或被强制停止后的桌面恢复

2026-09-30，承接 [95 篇](95-install-use-case-tests.md)。那一轮用 `pm clear` 和强制停止做真机验证时，发现两个既有问题，桌面都要人工处理才能恢复。用户要求修复。真机为 G100 S（ZY32MVJS25，`10.77.0.16:35577`，K8-Plus 通过 Wi-Fi/VPN 连接），证据在 `.work/verify/20260930-review-fixes/`（`t9`–`t16`）。

## A. 清除数据后，容器仍绑定已删除的 socket 目录

- **机制**：`rungic-plasma-enter` 在容器启动时，把 `/data/user/0/com.rungic.plasma/files/tmp` bind 到容器的 `/mnt/android-wayland`。bind 固定的是目录 inode。`pm clear` 或卸载重装会删除并重建这个目录，运行中的容器仍指向 `files/tmp//deleted`，KWin 连不上新的 Wayland 服务端，APK 显示“暂时无法进入”。
- **选型**：
  - 控制器已有同类处理：Android 重新挂载共享存储后 bind 失效，`start` 会重启容器（`shared-storage` 阶段）。
  - 也考虑过热重绑：用 `open_tree`/`move_mount` 把新目录挂进容器的 mount namespace。没有采用，原因有二：
    - 卸载重装可能改变 APK 的 uid 和 SELinux 标签，而 `android-shm-context` 只在 `start_container` 时生成；
    - 这个场景很少出现，重启容器的代价可以接受，而且实现更简单。
- **修复**：`start` 和 `restart-session` 在容器运行时，比较主机上 `files/tmp` 与容器内 `/mnt/android-wayland` 的 inode；两边都能读到且不一致时，停止容器，由后续步骤重新启动。任一侧读取失败时不重启，避免因一次 attach 失败误重启。
- **真机**：在容器运行时 `pm clear`，然后打开 APK。容器 init PID 从 28030 变为 12381，容器内 inode 与新目录一致（559170），15 秒内 KWin/plasmashell 恢复为 active，全程无人工干预（`t15-A-pmclear.log`）。
- **仍需注意**：在 APK 重新创建 `files/tmp` 之前，所有经过 `rungic-plasma-enter` 的控制命令都会报 `bind Android Wayland socket directory`。APK 在调用这些命令前会先创建该目录；95 篇新增的 `install-publish` 不经过 enter。

## B. APK 被强制停止后再打开，plasmashell 停在 failed 或 inactive

- **机制**（journal 时间线，`t9-reproduce-B2-journal.log`）：
  1. 新 APK 进程启动时，KWin 的 Wayland 连接断开，plasmashell 以 255 退出；因为 `Restart=on-failure`，systemd 立即重启它。
  2. APK 发起 `restart-session`。会话 unit 的 `ExecStop` 执行 `systemctl --user stop graphical-session.target`，target 停止后命令就返回了，而 plasmashell 还要 1–2 秒才退出。
  3. 新会话的 `plasma-core.target` 对 plasmashell 只是 `Wants=`。systemd 会丢弃与正在运行的 stop 作业冲突的弱依赖启动作业，结果 target 显示“已到达”，plasmashell 却从未启动。
- **复现**：
  - 强制停止后立刻重开：未复现；
  - 强制停止后隔 30 秒再重开：复现，plasmashell 停在 failed。
- **修复 1（预防）**：`desktop/session` 在已有的“等待上一个 startplasma-wayland 退出”之后，再等待用户 systemd 中的 stop 作业结束，最多 20 秒。等待过时会在 journal 写一行 `Waited Nx0.1 s ...`。
- **修复 2（恢复）**：APK 在 Wayland 已初始化时点“重新检查”，发送的是 `start`，而 `start` 只会等待 plasmashell，不会拉起它。现在控制器在 `start` 时发现 plasmashell unit 处于 failed，就改走 `restart-session`。
- **真机确定性对比**（`t14-*`）：临时给 plasmashell 加一个用户级 drop-in `ExecStopPost=/bin/sleep 3`，让它每次都停得慢，稳定制造竞争：
  - 未修复的会话脚本：`restart-session` 后 plasmashell 停在 inactive，控制器等到超时；
  - 修复后：连续 2 轮都记录了 `Waited 20x0.1 s`，plasmashell active，控制器报告就绪；
  - drop-in 测完已删除。
- **其他真机结果**：
  - 修复 2：在 plasmashell 已 failed 的状态下点“重新检查”，5 秒内 plasmashell、KWin 和语音浮层都恢复（`t11-retry-recovery.log`）。
  - 部署修复后，按“强制停止 → 30 秒 → 重开”共跑了 7 轮，全部正常（`t12`、`t13`）。但这 7 轮里等待循环都没有触发：部署同时带进了 d006ba6 对 `kconf_update` 的修改，会话启动时序变了。因此这 7 轮只是回归结果，不能作为修复 1 有效的证据；修复 1 的证据是上面的确定性对比。

## C. 宿主断开后桌面会话保留（2026-10-05，PR #20）

用户报告：夜间长时间待机后，Moto 的省电程序会杀掉 APK，第二天整个桌面重新开始，未保存的内容丢失（#15）。Linux 容器和其中的进程其实都还活着，丢失来自两处：

- KWin 在宿主连接断开时以 133 退出，交给 `kwin_wayland_wrapper` 重启（[57 篇](57-zero-copy-explicit-sync.md)“宿主重启时KWin中止”一节的做法）；
- APK 新宿主进程启动时发送 `restart-session`，整个会话重启。

**改动**：

- **KWin**（`android-host-reconnect.patch`，kwin `+rungic11`）：
  - 断开只报告一次，事件线程不再读取已断开的连接，不再 `qFatal`。
  - 手机输出 WL-0 保留，对客户端一直存在、尺寸不变。第一版曾把输出移除、让窗口留在工作区的占位输出上，结果 plasmashell 退出：日志先报 `There are no outputs`，随后占位屏幕上的面板得到 width=0 的 layer surface，触发协议错误（mibook 的 G100 日志）。
  - 宿主不在时输出不渲染：渲染循环暂停，丢掉还在等旧宿主的帧和从宿主借来的缓冲，销毁宿主窗口、各层的子表面和光标表面（`WaylandOutput::unbindHost`）。投屏输出、输入设备和导入的缓冲随宿主移除。
  - 每 500 ms 探测宿主 socket，能连通后重新连接（不再有首次启动的 60 秒等待）：同一个输出得到新的宿主窗口和子表面，等宿主配置完成（期间宿主尺寸变了就当作一次普通的宿主改尺寸），再整帧重画（`WaylandBackend::rebindOutputs`）；随后建立新的 seat、宿主全局对象、文字输入和防休眠。
  - 旧连接上的对象在断开时全部销毁，重连成功后旧连接释放；宿主键盘映射的文件描述符随即关闭（KWin 用自己的键盘映射）。第一轮实测每次重连多出约 4 个描述符：旧连接的 socket 和事件线程的退出管道共 3 个，键盘映射 1 个。
- **APK 2.35**：新宿主发送 `start`，不再发送 `restart-session`。
- **控制器**（`system/rungic-plasma`）：`start` 只在 plasmashell 或 KWin 失败、或 KWin 不在运行时才改走 `restart-session`。

**真机验收**（mibook，USB G100，kwin 开发覆盖，2026-10-05）：

- 强制停止 APK 再打开，20 轮：KWin、plasmashell、两个应用窗口和独立硬件探针的 PID 都不变，未保存文本保留；APK 不在时 KWin CPU 最高约 0.30%。
- 修复描述符泄漏后再跑 20 轮：每轮 pipe 都是 5，键盘映射临时文件 0，socket 在 40–46 之间波动，不再随轮数累积；末轮总描述符 139（修复前末轮 224）。
- 以已编辑、未保存的文本为基线再跑 20 轮（实装 95eaec6）：KWin（wrapper 11751／实际 11762）、plasmashell 12046、两个窗口 12367／12403 和独立后台探针的 PID 都不变，编辑器文本和“已修改”状态保留；宿主不在时 CPU 最高 0.299%；轮后 RSS 162.5–173.3 MiB；总描述符基线 160、末轮 143、轮间 137–159，socket 40–43，pipe 每轮 5，键盘映射临时文件 0，没有逐轮累积。
- 断开超过 35 秒、熄屏后强制停止：进程和文本保留。熄屏后首次打开约 2 秒时仍显示“等待桌面”，约 8 秒显示原编辑器，不需要再次强停，之后触摸和输入正常。旧版本在这一场景下重开后有约 5 秒、164 条 `Rendering a layer failed`；新版本复测为 0，日志中也没有 `There are no outputs`、协议错误或进程退出。
- 设备面板方向：WL-0 从 360×800 变为 800×360，再恢复原尺寸。Android 系统的自动旋转不转输出，这是预期行为（只有设备面板的方向设置转动手机，[50 篇](50-plasma-display-settings.md)）。
- 未覆盖：投屏输出（当时没有投屏显示）、整夜自然待机（在 USB G100 上进行中，当晚约 6 小时，不计为整夜通过）、Android 低内存真实杀死 APK。

## 离线测试

`tools/ci/test_session_recovery.py` 从真实的控制器和会话脚本中抽出这些片段，用桩命令运行，共 9 个用例：

- inode 不一致时重启容器、一致时不动、任一侧读取失败时不动；
- plasmashell 已 failed 时 `start` 改走 `restart-session`；
- 会话脚本等待 stop 作业（不等待 start 作业），并且等待有上限。

拿修复前的源码运行，9 个全部失败。`tools/run-tests.sh` 已包含这个文件。

## 部署状态与边界

- G100 S 上的 `rungic-plasma` 和容器内的 `/usr/libexec/rungic-plasma-session` 是为这次验证直接替换的。后者属于 `rungic-plasma-session` 软件包，直接替换后完整性检查会报告它被改过；要随下一次打包发布正式下发。部署前的原文件在 `device-backup/`。
- 最终 smoke 9/9 通过（`.work/acceptance/unreleased/20260930-202509/`）。OCR 模型和 prefs 已从备份恢复，标签按目录的完整 MLS 类别重新 `chcon`。
- 还没做的：Android 低内存时真实杀死 APK 的场景（这次用 `am force-stop` 近似），以及卸载后重装导致 uid 变化的场景。

## 发布 20260930.9（2026-09-30）

- 构建：在 Mac mini（ARM64，系统代理 Surge 127.0.0.1:6152）上重建了 17 个 stale 的项目包，版本 0.510，另外 2 个包本来就是最新，共 19 个。stale 是因为自上次构建以来打包路径的内容变了（e7f7f50 拆分目录等）。`rungic-plasma-session_0.510` 中的会话脚本与 `desktop/session` 的 SHA 一致。发布元包 `20260930.9` 固定 70 个包，对应提交 be86132；APK 升为 2.28（versionCode 76），单独发布。
- 部署到 G100 S：按用户选择，先对上次部署保留的 `.8` snapshot 执行 `rungic_release.py commit`，再部署。**主机侧部署进程在 sync 之后被我设的 590 秒客户端超时杀掉了**（snapshot、settled 和经 Wi-Fi 同步 37 个包已用掉约 9 分钟）。手机上的 apt 在 `systemd-run` 临时单元里独立跑完：dpkg 设置完全部包，`rungic-release (20260930.9)` 为最后一步，term.log 中没有 `Errors were encountered`，`dpkg --audit` 干净，70 个包的版本与发布一致；但该单元以 100 退出，最可能是写最后的输出时管道已断。
- 补完：用 `rungic_release.py` 自身的函数按 `deploy()` 的顺序补完其余步骤，写入同一部署记录 `.work/deploy/20260930-212112-20260930.9/`，结果标为 `ok (resumed after a client timeout)`。补完脚本在验收失败时不会自动回滚 snapshot，而是交给用户决定；实际验收通过，没有用到。
  - Android 侧：更新了 `rungic-plasma`、`rootfs.sepolicy.rule`、`rootfs-mount-hook`；
  - 服务：重启了 bridges 的 3 个服务和 suggestions 的定时器，并重启了会话；
  - 完整性：只剩部署前就有的 `/usr/lib/rungic-cua/rungic_cua/keyring.py` 不属于任何包；
  - smoke 9/9。
- 安装 APK 2.28 后重新打开，smoke 9/9（`.work/acceptance/20260930.9/20260930-213913/`）。`.9` 的 snapshot 仍保留，接受后再执行 `rungic_release.py commit`。
- **教训**：经 Wi-Fi 部署整套发布会超过 10 分钟，不要给 `rungic_release.py deploy` 套短超时，要放在后台运行。

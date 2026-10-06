# 117 麦克风、相机和手机外放移出 App：独立媒体后端

2026-10-06，task #26，拆分计划（#rungic:7a5023dd，mibook 的 linux-service-ownership-20261006）的第二项“音频与通话”。

## 问题

Linux 用的麦克风、相机、手机外放，以及和 Agent 通话的声音，都由 App 进程里的 CaptureBridge 提供，它直接持有 Activity。App 被冻结、被系统回收或强行停止，正在进行的 Agent 通话就断了，Linux 侧的采集也一起断。网络、蓝牙、短信早已移到独立的 root 后台（DeviceDaemon，docs/113），媒体还没有。

## 可行性（G100 S，2026-10-06）

用一个 root `app_process` 探针，在屏幕熄灭时验证（只输出电平和帧数，不存录音）：
- 录音：MIC 和 VOICE_COMMUNICATION 两种来源都有真实信号，没有被静音；
- 播放：AudioTrack 正常走完；
- 相机：后摄 1.5 秒拿到 31 帧。

所以 root 进程不需要 App 的前台服务就能用麦克风和相机。docs/101 记的限制（“Android 14 起后台进程不能开麦克风”）针对的是 App 的 uid，不适用于 uid 0。

坑：`CameraManager.openCamera` 会读一项桌面模式的开发者设置（`DesktopModeFlags.getToggleOverride`）。ActivityManager 不认识这个进程，读设置时抛 SecurityException（“Unable to find app for caller”）。框架把这项设置缓存在静态字段 `sCachedToggleOverride` 里，后台启动时先预置为 `OVERRIDE_UNSET`，就不再去读。

同类做法：scrcpy 以 shell 身份从 `app_process` 采集音频和相机（FakeContext），我们的 DeviceDaemon 与 CallDaemon 也是这种 root `app_process` 后台。

## 结构

- **MediaDaemon**（`com.rungic.plasma.MediaDaemon`）：root `app_process`，用系统 context。
  - 直接运行原来的 CaptureBridge：复用，不另写一份。CaptureBridge 只是不再依赖 Activity，App 才能做的事改由 `CaptureBridge.Host` 提供。
  - 对 Linux 的协议完全不变：麦克风、相机、手机外放、通话三路，帧格式也一样。新增一个 `info` 操作，返回和 `capture-info` 相同的内容，兼作就绪检查。
  - socket 改为宿主状态目录里的 `state/host/media/capture.sock`，Linux 里是 `/var/lib/rungic-host/media/capture.sock`。这是文件路径，不受独立网络（docs/116）影响。
- **监督**：`system/android-device` 以 `android-media` 的名字运行时监督 MediaDaemon。监督规则和设备后台相同：移出 App 的冻结组、有限退避、开关文件、生命周期记录。状态目录是 711，Linux 的桌面用户能连 socket，后台自己检查对端 uid（0、1000）。
- **App 一侧（MediaLink）**：经只在安卓侧的抽象 socket `com.rungic.media.v1` 连到后台，后台只接受 App 的 uid。App 负责：
  - 报告桌面是否在前台，以及用户拒绝过的权限；
  - 后台要权限时弹系统对话框，把结果回给后台；
  - 和 Agent 通话时登记系统通话（AgentCall：锁屏通话界面、挂断按钮、来电时保持），把保持、挂断回报给后台；
  - 显示采集通知（CaptureService）。
  - `capture-info` 仍由 App 的平台桥回答，内容取后台最近一次推送的状态。
- **Linux 一侧**：media-bridge、相机源、通话音频服务优先连后台的 socket，没有时连旧 App 的 `capture.sock`，兼容还没升级的 APK。

## 规则

- 桌面的麦克风和相机仍只在用户给了 Rungic 权限、并且桌面在前台时使用。权限按 Rungic 应用的授权判断（PackageManager），没有另开 root 的口子。App 不在时按“不在前台”处理。
- Agent 通话一旦开始，就不受前台限制，App 被杀也继续：
  - Telecom 的通话随 App 进程消失，后台自己把音频切到通话模式（原来已有的降级路径），声音照旧走通话路由和回声消除；
  - 唤醒锁由后台持有，4 小时上限不变。
- 开始一通 Agent 通话仍要求桌面在前台：通话从屏幕上发起。

## 检查

- 离线：
  - 监督脚本以两个名字分别运行，各有状态、开关和锁（`test_device_services.py`）；
  - Linux 侧对 socket 的选择（`test_contract_media.py`）；
  - 契约 audio、camera 的路径更新。
- 实机，隔离测试（G100 S，2026-10-06，不碰正在用的 App）：
  - 测试后台放在 `state/host/media-test/`，用一个以 App uid 运行的替身充当 App 一侧；
  - Linux 的桌面用户经它：`info` 正常（权限、两个相机）；
  - 不在前台时拒绝麦克风；替身报“在前台”后，麦克风 1 秒 48 kHz 单声道有真实信号，后摄 1280×720 帧格式正确；
  - 采集通知的开关按使用情况发给了 App 一侧；
  - 测完已删除。
- 待做的实机验收（装新 APK 和脚本之后）：
  - 桌面录音、相机、手机外放；
  - Agent 通话：锁屏、后台、强行停止 App 时通话不断，App 回来后重新连上；
  - 插拔耳机和蓝牙耳机的路由；
  - 后台被杀后的恢复；
  - 唤醒锁在通话结束后释放。

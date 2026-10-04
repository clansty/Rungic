# 零拷贝呈现与显式同步

2026-09-24。XT2537-4 / Android16 / APK1.10–1.25 / KWin 6.6.6+moto8–moto10。56篇的量化结论是：KWin改原生Vulkan只省CPU，更大的开销在宿主GLES合成和`glFinish`同步等待。本篇实施了其中两项共享层改动，并做同机A/B。

## 结论

| 3轮中位数，抽屉滚动，热状态0 | 原来 | 零拷贝 | **零拷贝 + fence** |
|---|---:|---:|---:|
| KWin Paint p50 / p95 | 6.74 / 12.00 ms | 7.16 / 13.86 ms | **2.05 / 3.87 ms**（复测2.03 / 3.91） |
| KWin `glFinish`等待 p50 | 4.27 ms | 4.39 ms | **无** |
| 宿主APK CPU（单核%） | 22.6 | 15.4 | **15.4–16.3** |
| 宿主GPU时间（8.5秒） | 481 ms | 0 | **0** |
| KWin / SurfaceFlinger CPU | 21.4 / 27.0 | 18.9 / 25.3 | 18.6–19.5 / 25.3–25.5 |
| KWin每轮绘制帧数 | 355 | 338 | 362–370 |
| SurfaceFlinger帧间隔 p95 | 16.7 ms | 16.8 ms | 16.8 ms |

- 零拷贝：宿主不再每帧用GLES全屏重绘一次（含BGR交换和它自己的`glFinish`），宿主GPU工作归零、APK CPU降约30%。SurfaceFlinger把这一层交给硬件合成（`dumpsys SurfaceFlinger`中`composition=DEVICE`），没有转成GPU合成。
- fence：KWin不再在`glFinish`上阻塞，每帧阻塞时间降约70%，缓冲约提前4.7ms交给Android。
- 两者必须同时开。只开fence、零拷贝关闭时，宿主要先在CPU上等fence再做GLES，SurfaceFlinger帧间隔p95劣化到25ms。
- 只开零拷贝时KWin Paint略变差（+0.4ms）：宿主GPU工作消失后，GPU停在低频档的时间从33–38%升到41–47%，平均频率约从630降到约550MHz，KWin自己的GPU工作变慢。GPU频率策略（见56篇）因此更重要。
- 显示节拍（SurfaceFlinger p95 16.7ms）不变，受宿主帧回调与呈现反馈影响，另行处理。

## 零拷贝

`native/plasma/src/android/surface_control.rs`：

- 从SurfaceView窗口创建子`ASurfaceControl`。帧内只有一个位于(0,0)、非光标、能在分配器中查到租约AHB的dma-buf（KWin输出）时，用`ASurfaceTransaction_setBufferWithRelease`（API36，dlsym）提交该AHB，设OPAQUE、SRGB、整幅damage；其余情况隐藏子层，回退原GLES合成。
- 释放：每个提交的缓冲持有Smithay `Buffer`（其最后一个引用释放时才发`wl_buffer.release`）和AHB引用。SurfaceFlinger的释放回调在binder线程发生，经通道回到合成线程；release fence signal后才放掉持有，KWin此时才能复用该缓冲。
- 分配usage由0x333改为0xB33，加`AHARDWAREBUFFER_USAGE_COMPOSER_OVERLAY`：NDK头文件说明直接用`setBuffer`的缓冲必须带这一位（框架只给自己分配的缓冲加）。
- 调研与坑点来自AOSP `surface_control.h`、Chromium `android_surface_control_compat.cc`/`gl_surface_egl_surface_control.cc`、Lindroid `ComposerImpl.cpp`（最接近的同类项目，未跟踪逐缓冲release，没有照搬）、高通参考gralloc/HWC。Termux:X11和Winlator仍是GLES绘制，不是零拷贝。
- 运行时开关：`setprop debug.moto.zerocopy 0`；`native-stats`显示零拷贝帧数、acquire fence数、回退次数与原因、在途缓冲数。
- 顺带修复：触屏模式下不再绘制回退光标（左上角箭头，55篇P1记录）；每帧的dma-buf info日志降为debug。

## 显式同步（sync_file）

KGSL不是DRM设备，linux-drm-syncobj-v1不可用。改用基于sync_file的`zwp_linux_explicit_synchronization_v1`（wayland-protocols unstable，仍在维护列表，Smithay与KWin客户端都未实现，本次两端各自实现）：

- KWin（moto8，`wayland_egl_backend.cpp`）：有该全局且处于Android flat-output模式时，用`EGL_ANDROID_native_fence_sync`导出本帧GPU完成fence，经`set_acquire_fence`随同一次commit发出，不再调用`glFinish`。`MOTO_KWIN_EXPLICIT_SYNC=-1`恢复旧路径。释放继续用`wl_buffer.release`（宿主在SurfaceFlinger释放后才发）。
- 宿主（`backend/wayland/explicit_sync.rs`）：acquire fence与release对象都是按commit双缓冲的表面状态。零拷贝把fence dup后交给SurfaceFlinger作为acquire fence；GLES回退路径在采样前等待（最多1秒）。`get_release`对象在该commit最后一个引用释放时发`immediate_release`，与`wl_buffer.release`同一时刻。
- 风险：同一App进程的所有SurfaceControl事务按FIFO排队，未signal的acquire fence会阻塞后续事务（AOSP `TransactionHandler`）。容器GPU挂死时宿主显示会一起停住；这与原来KWin在`glFinish`上挂住的后果相同。

## 事故：BGRA缓冲使SurfaceFlinger崩溃（已修复）

2026-09-24 01:08，APK1.11运行零拷贝时SurfaceFlinger在RenderEngine线程abort（tombstone_28）：`Unable to generate SkImage ... GrGLTextureInfo fTarget 36197 fFormat 32856 (GL_RGBA8) colorType 6 (kBGRA_8888)`。随后system_server、相机与指纹HAL相继退出，Android用户空间重启，无线调试与VPN（Swift wire）随之断开。

- 原因：KWin选择DRM `XR24`，分配器映射为HAL私有格式BGRA_8888（5）。该层平时由HWC overlay合成（`composition=DEVICE`）；一旦SurfaceFlinger必须用GPU合成它（转场、任务快照等），Skia对BGRA颜色类型与RGBA8纹理的组合无法创建图像而abort。截图路径恰好没有触发，所以此前的截屏验证没有发现。
- 修复（APK1.14起）：宿主dma-buf只通告`XBGR8888/ABGR8888`，分配器只接受这两种并映射为公开的`AHARDWAREBUFFER_FORMAT_R8G8B8A8`，拒绝BGRA。KWin随即改用`AB24`。compbench同步改为`XB24`。
- 验证：`service call SurfaceFlinger 1008 i32 1`关闭硬件overlay、强制该层走GPU合成（`composition=CLIENT`），滑动与截屏期间SurfaceFlinger与system_server进程号不变，画面颜色正确，随后恢复overlay。
- 教训：零拷贝层必须同时通过HWC与GPU（CLIENT）两条合成路径的验收；缓冲格式只用Android公开的AHB格式。

## 呈现反馈（第2项）：实测后默认关闭

APK1.12–1.18给零拷贝事务设`setOnComplete`，用present fence实际signal时间（`SYNC_IOC_FILE_INFO`）或latch时间报告`wp_presentation_feedback`。结果：

1. 冻结：一次无效时间（`getLatchTime()`返回负数被当作无符号数）使KWin把渲染安全余量算成极大值，桌面停止更新（时钟停住，KWin主线程在poll中空闲）。修复：只接受`[now-1s, now+5ms]`内的时间；任何反馈最迟60ms必定送出；零拷贝关闭或隐藏时立即送出全部挂起反馈。
2. 即使时间正确，KWin也变慢：KWin以“呈现时间−帧回调时间”作为安全余量提前开始渲染。真实时间包含1–2个vsync的显示流水线，余量增至约20ms以上，KWin每轮绘制帧数从约370降到106，SurfaceFlinger帧间隔p95升至50ms。
3. 结论：默认恢复“提交后立即报告”（APK1.19+），真实时间保留在`setprop debug.moto.present_feedback hw`下，且只有打开时才注册完成回调（每帧少一次binder往返）。要利用真实时间，需要同时修改KWin嵌套后端的余量逻辑，另行评估。

最终版本APK1.20（零拷贝+fence，立即反馈）3轮：KWin Paint p50/p95 2.03/3.81ms，每轮365帧，与1.11一致；SurfaceFlinger p95在16.7/25.0ms两档之间逐轮跳动（1.11同样出现过25ms），属于边界上的量化噪声。

## 按需vsync（第3项）

原来Java `DisplayPacer`每个vsync都重新注册Choreographer回调并调用`frameTick`，合成线程每个vsync跑一遍（排空命令、检查socket、dispatch、flush），桌面静止时也一样。APK1.22改为按需：

- 原生（`frame_clock.rs`、`compositor.rs`）：最近400ms内没有客户端commit、JNI命令（输入等）、按下的触点、未决的SurfaceFlinger释放fence/呈现反馈，就进入停泊状态。停泊时不再等vsync条件变量，而是`poll()` Wayland display fd、监听socket、XWayland事件循环和一个kick eventfd，最长1秒做一次例行检查；`send_command`和SurfaceFlinger的释放/完成回调都会kick。停泊时不再做500ms心跳重绘（它会重复提交同一缓冲）。唤醒后先立即处理本次事件，再回到vsync节拍。
- Java：`doFrame`中`NativeBridge.wantsVsync()`为false时不再重新注册回调；主Looper用`MessageQueue.addOnFileDescriptorEventListener`监听原生`vsyncWakeFd`（eventfd），原生离开停泊时写入它，Java排空后恢复回调。原生先置位再发信号，两边的先后顺序不会丢失恢复。停泊时补一次2.6秒后的刷新率投票复核，替代原来每秒采样的释放逻辑。
- 调研：Android官方做法就是按需`postFrameCallback`（Choreographer不会自行持续回调）；桌面合成器（KWin、wlroots、Weston）同样只在有损伤或frame callback时调度重绘，事件循环空闲时阻塞在Wayland fd上。本改动把宿主对齐到这一模型，没有引入新组件。
- 开关：`setprop debug.moto.ondemand_vsync 0`恢复每vsync唤醒；`native-stats`末尾显示`vsync_wanted`与累计停泊次数`parks`。

同一APK交替开关、主屏静止各10秒（热状态0，a11y按当时状态）：

| 单核% / 每秒运行次数 | 关 | 开 |
|---|---:|---:|
| APK合计 | 10.14 / 10.09 | **4.62 / 2.16**（首次测量2.67） |
| APK主线程 | 6.5% / 79–82次 | 1.4–2.6% / 16–30次 |
| APK合成线程 | 2.4% / 85–87次 | 0.1–0.7% / 1–21次 |
| SurfaceFlinger `app`（应用vsync分发） | 4.8–5.0% / 67–69次 | 0.7–1.3% / 14–20次 |

“开”的第一轮KWin有约10次/秒绘制（桌面上有零星更新），所以APK仍有4.6%。抽屉滚动3轮（`kwin_pipeline_run --zerocopy on`）：KWin Paint p50 2.07–2.24ms、p95 3.83–4.35ms，SurfaceFlinger帧间隔p95三轮均16.8ms，APK CPU 13.4–15.4%（1.20为14.7–17.7%），无退化。

按控件名启动/关闭检查：开关交替5次共10轮全部通过，停泊8秒后点击启动4/4、直接点击8/8。开发过程中另有两次“开”状态下的首轮失败：一次点击坐标y=127，后查明是检查脚本在抽屉列表被滚动后点到了搜索框（见第6项）；另一次点中了图标位置但应用未启动，未能复现也未归因。检查脚本现已在启动失败时自动截图留证。

另外观察到plasmashell在静止时持续约10%单核、120–150次/秒唤醒，与本开关无关，原因与修复见下节（第6项）。

## plasmashell静止开销（第6项）：回移上游修复

现象：主屏静止时plasmashell主线程约8.5–10.5%单核、110–150次/秒运行，但KWin和plasmashell渲染线程几乎不出帧。

定位过程（全部经agent接口完成，无需界面操作）：

1. perfetto `frame`预设的`sched_waking`：plasmashell主线程的唤醒约100次/秒来自`swapper`（即ppoll超时，定时器到期），另有约13次/秒来自pulseaudio。
2. gdb对`QCoreApplication::notifyInternal2`设条件断点（事件类型为Timer），经`metaObject()->className()`取接收者：8秒内302次全部发给`QSGThreadedRenderLoop`。Qt在有多个可见窗口时不能用vsync推进动画，改用主线程定时器按刷新周期推进，说明有动画在运行。
3. 对`QAbstractAnimationJob::setCurrentTime`断点：两个`QSequentialAnimationGroupJob`（各含Pause和属性动画）已连续运行约45分钟。对照源码是快捷设置面板里的`MarqueeLabel`跑马灯：`running: charactersOverflowing && visible`。面板为了打开速度一直不设`visible: false`，关闭后Item仍“可见”；中文“录屏”状态文字和“Caffeine”状态文字溢出，所以关着的面板一直在滚动文字。

修复：上游plasma-mobile在2026-09-18/19刚修复，原样回移到vendor 6.6.5：

- `ce647d80` quicksettings: Ensure MarqueeLabel is off when panel is hidden（`scrollingEnabled`由面板是否打开、当前页决定；另要求`Window.window.visible`；缓存TextMetrics宽度）。
- `0f35c4af` MarqueeLabel: Avoid masking/effects if text doesn't need marquee（不溢出时不做OpacityMask图层）。

与6.6.5的差异只在上下文（状态文字表达式），手工合入后`QuickSettings.qml`与上游提交后的blob一致。在手机上以`build_on_device.py plasma-mobile targets --target mobileshellplugin`构建，经已有的dpkg-divert替换，只重启plasmashell。

结果：静止主屏3次各10秒，plasmashell 0.57–0.61%单核、16–17次/秒（修复前8.5–10.5%、110–150次/秒），其中一次在打开并关闭快捷设置面板之后测得；面板打开时溢出文字仍正常滚动。抽屉滚动3轮×2次：plasmashell 28.6–33.8%（修复前36.8–43.9%），KWin Paint p50 1.97–2.36ms，SurfaceFlinger帧间隔p95在16.7/25.0ms两档间跳动，与之前相同。

顺带修正了`ui_launch_check.py`：抽屉会保留滚动位置（基准会滑动它），被滚到搜索框下方的图标仍报告有效坐标，点击会落在搜索框上。第3项中点击坐标y=127的那次失败即此原因；现在点击前若图标高于搜索框下沿，先把列表拖回顶部。

## GPU频率（第4项）：本轮不改

- 本机电源HAL（高通/Moto MDPF）配置中，GPU最低功率档`/sys/class/kgsl/kgsl-3d0/min_pwrlevel`只由`INTERACTION_SEVERE/MODERATE_SCROLL`、`INTERACTION_SEVERE/MODERATE_TOUCH`（厂商交互hint）和`EXPENSIVE_RENDERING`（SurfaceFlinger在昂贵GPU合成时设置）触发，普通应用没有入口。ADPF（`PerformanceHintManager`）会话的MDPF配置只有CPU uclamp的PID参数，没有GPU项；会话线程还必须属于调用方进程，宿主不能替容器内KWin/plasmashell报告。`SessionHint.GPU_LOAD_UP`等提示在这里没有对应的GPU动作。
- 其余可行手段都是全局改动：KGSL `devfreq`的`mod_percent`（需要root、影响全机）、`min_pwrlevel`或频率锁定。它们违反49、51篇“不锁频、不改温控”的边界，未采用。
- 必要性也下降了：显式同步后KWin不再在CPU上等待GPU，滚动时GPU只有11–13%忙碌，平均约560MHz（295–816MHz之间来回），SurfaceFlinger帧间隔p95已稳定在16.8ms。频率只影响缓冲就绪的时刻，当前数据中看不到由它造成的丢帧。
- 以后如果出现明确由GPU低频造成的卡顿（KGSL轨迹中提交排队、`adreno_cmdbatch_retired`延后），再评估用户可选的`mod_percent`开关；默认不启用。

## UBWC压缩输出缓冲（第5项）

KWin输出缓冲原为线性。Adreno的UBWC（带宽压缩）可减少GPU写入与显示控制器读取的内存流量。

调研与核对（实机）：

- gralloc（高通snapalloc，`vendor.gralloc.disable_ubwc=0`）：AHB usage加`1<<28`（高通`GRALLOC_USAGE_PRIVATE_ALLOC_UBWC`，即`AHARDWAREBUFFER_USAGE_VENDOR_0`）且不带CPU读写位时分配UBWC；带CPU位或不带该位一律线性。1080×2400 RGBA：线性10444800字节，UBWC 10522624字节，多出的77824字节正是msm_media_info `RGBA8888_UBWC`的元数据平面（16×4像素块，宽按64、高按16对齐，4K对齐，位于像素数据之前），像素stride 1088像素。
- Mesa gallium（fd6 layout）：显式布局下元数据平面同样在前、同样的对齐公式，pitch要求256字节对齐，与gralloc结果逐项一致。gallium不自行编程UBWC模式寄存器（highest bank bit、swizzle、macrotile由内核在GPU初始化时按平台配置写入；本机KGSL报告HBB=14、UBWC 4.0），因此Mesa写出的UBWC与显示控制器、高通GLES使用同一套配置。gallium对FD710写死的`highest_bank_bit=16`只用于CPU端tiled拷贝，不影响此路径。
- 阻碍只在上游分支的KGSL dma-buf“窄契约”（[lfdevs/mesa-for-android-container PR #85](https://github.com/lfdevs/mesa-for-android-container/pull/85)，为XWayland定的保守约定：共享缓冲一律LINEAR，拒绝导入非线性修饰符），不是已知硬件限制。

实现（三处，默认开启只影响KWin）：

- Mesa（vendor，19行）：`FD_KGSL_DMABUF_UBWC=1`时允许在KGSL dma-buf路径**导入**`DRM_FORMAT_MOD_QCOM_COMPRESSED`；导出与修饰符查询仍只有LINEAR，客户端（plasmashell等）的分配行为不变。以`build_on_device.py mesa targets`（新增meson支持，选项与`build-mesa.sh`共用`plasma/mesa-meson-options`）在手机上构建libgallium并dpkg-divert替换。
- 宿主（APK1.23）：分配协议v2带修饰符；UBWC请求用`0xB00|1<<28`分配，并按dma-buf大小核对确为UBWC布局才回报QCOM_COMPRESSED，否则回报LINEAR。dma-buf全局对XB24/AB24增加QCOM_COMPRESSED。零拷贝照常提交AHB；GLES回退对租约缓冲本来就经AHB导入，由高通驱动解码。
- KWin（moto9）：`MOTO_KWIN_UBWC=1`且宿主提供该修饰符时按v2请求。`plasma/kwin`只为KWin导出这两个变量。

验证：日志中输出缓冲均为`modifier=0x500000000000001`；HWC以`composition=DEVICE`直接扫描；以root执行`service call SurfaceFlinger 1008 i32 1`强制GPU合成（`composition=CLIENT`）并滑动、截屏，画面正确，SurfaceFlinger与system_server进程号不变，之后恢复。横竖屏各两次、启动检查3/3与搜索2/2通过。

同机A/B（抽屉滚动，每组3轮，开/关交替两次，热状态0）：

| 中位数 | UBWC关 | UBWC开 |
|---|---:|---:|
| KWin GPU时间（8.5秒） | 1129–1139 ms | **627–655 ms**（约−43%） |
| KWin Paint p50 / p95 | 2.27–2.39 / 4.03–4.25 ms | 2.24–2.27 / 3.72–3.84 ms |
| DDR实测带宽（bwmon） | 1883–2001 MB/s | 1738–1828 MB/s（约−8%） |
| GPU忙碌 | 11.0–11.5% | 10.9% |
| SurfaceFlinger帧间隔p95 | 16.8 ms | 16.8 ms |

DDR带宽取自内核`dcvs/bw_hwmon_meas`事件（bwmon每个采样窗口测得的MB/s），已加入`rungic_trace`的KGSL tracefs实例，报告与`kwin_pipeline_run`汇总给出时间加权平均`bw_mbps`。带宽含全机所有流量（plasmashell与客户端缓冲仍是线性），UBWC只作用于KWin输出与显示扫描。显示面板上的实际观感需人工确认；截屏只覆盖GPU合成路径。

### 零拷贝横屏被裁半屏（同时发现并修复）

验收旋转时发现：零拷贝打开时竖屏→横屏只显示左侧1080像素，其余全黑；关闭UBWC仍然如此，关闭零拷贝则正常。原因：零拷贝层是SurfaceView窗口层的子层，SurfaceFlinger按父层范围裁剪子层，而父层范围来自它最后一帧缓冲；零拷贝期间宿主不再向窗口提交缓冲，旋转后父层仍是旧方向的尺寸。此前零拷贝没有做过横屏验收。

修复（APK1.24）：记录最近一次GLES帧实际绘入的窗口缓冲尺寸，与当前表面尺寸不一致时先走GLES（回退原因`window resized: parent frame first`），一致后恢复零拷贝。尺寸必须在绘制前用`eglQuerySurface`读取：Android在`eglSwapBuffers`时就把EGL_WIDTH/HEIGHT更新为下一块缓冲的尺寸，swap后读取会误判（第一版即因此只修好了横屏→竖屏）。实测每次旋转走2帧GLES，竖→横→竖→横→竖五次截图均完整。

## KWin绘制以外的CPU（第7项）

方法：Android自带`simpleperf record -p <kwin_wayland> --call-graph fp`（容器与Android同一内核，root可直接采样容器进程；Ubuntu 24.04起默认保留帧指针，DWARF回溯越不过libc而fp可以）。记录时设备端读不到容器内文件，构建号全为0；把`libkwin`、`libgallium`、`libQt6Core`等从容器rootfs拷回K8，用`llvm-objcopy --remove-section .note.gnu.build-id`去掉构建号后，以NDK主机端`simpleperf report --symfs`即可符号化。

抽屉滚动12秒，KWin主线程（占KWin进程样本83%）包含子调用的分布：`Compositor::composite` 52.5%（场景绘制约30%；`endFrame`约15%，其中`GLVertexBuffer::endOfFrame`和本项目导出的`EGLNativeFence`各约10%，主要是Mesa线程化上下文在建fence时同步执行驱动批次，属于必要的绘制成本），客户端请求分发`Display::dispatchEvents` 12%，宿主事件`WaylandEventThread::dispatch` 9%，futex唤醒与Unix socket发送合计约10%（内核）。

发现的浪费：`WaylandOutput::applyConfigure` 5.5%，经`Workspace::updateOutputConfiguration → updateOutputs → WaylandCursorImage::updateCursorTheme`重新扫描光标主题目录（大量`statx`/`readlink`与SELinux路径检查）。原因在宿主：每次触摸按下都调用`apply_focus_candidate`，无条件`toplevel.send_configure()`，焦点与状态都没变也发configure；KWin每收到一次就重新应用输出配置。修复（APK1.25）：改用Smithay的`send_pending_configure()`，只在状态实际变化时发送。

结果：同样的滚动负载下`applyConfigure`与`updateCursorTheme`不再出现，KWin采样周期4.65G→4.46G（约−4%）；3轮滑动基准每帧KWin CPU约−5%，Paint p50 2.14–2.45ms、SurfaceFlinger帧间隔p95 16.7–16.8ms（一轮25.0ms，同前述两档跳动），启动检查3/3、搜索2/2通过。

仍可继续看的：`QCoreApplicationPrivate::sendThroughApplicationEventFilters`自身约3%（每个事件都遍历应用级事件过滤器），来源未查。

## 宿主重启时KWin中止与客户端崩溃潮（第8、9项）

现象：每次APK更新或宿主进程重启，`kwin_wayland`以SIGABRT退出并留下约250MB核心转储（`moto-coredump-collect`随即在手机上做zstd压缩和gdb回溯）；随后`moto-plasma-keyboard`、`plasma-settings`等Qt客户端在`QtWayland::wl_compositor::create_surface()`段错误。

原因：

- KWin嵌套Wayland后端`WaylandEventThread::dispatch()`在宿主连接断开时调用`qFatal("Wayland connection broke")`，这是上游设计。`kwin_wayland_wrapper`把非0、非133的退出都算作崩溃，立即重启KWin；此时宿主往往还没起来，新KWin连不上又退出。wrapper的崩溃计数只在退出码133时清零，超过10次就不再重启。
- 客户端带`QT_WAYLAND_RECONNECT=1`，在KWin重启的空窗重连到wrapper保留的socket，新连接上一个全局对象都没收到就重建surface，空的`wl_compositor`被调用。这是Qt未合并的[QTBUG-150287](https://qt-project.atlassian.net/browse/QTBUG-150287)；候选修复[Gerrit 770189](https://codereview.qt-project.org/c/qt/qtbase/+/770189)只把段错误改成`_exit(1)`，客户端仍不能存活。Ubuntu 26.04（6.10.2）和Debian sid（6.11.2）都没有修复。

修复（KWin moto10，`backends/wayland/wayland_display.cpp`，仅在`MOTO_GPU_ALLOCATOR`存在即Android宿主时生效）：

- 启动时最多等60秒宿主socket可连接，而不是一次失败即退出。
- 运行中宿主断开：记录“Host connection lost”，等宿主socket重新可连接（最多120秒）后以133退出。wrapper随即重启KWin且不计崩溃，新KWin一次连上宿主；不再产生核心转储。

> 2026-10-05 起，运行中宿主断开时KWin不再退出：输出保留、宿主回来后重新接回，APK也不再重启会话，见[96 篇C节](96-desktop-recovery-after-apk-restart.md)。下面是当时的记录。

验收：两次`am force-stop`后重新打开APK，0个新核心转储、0次段错误，桌面恢复。APK新宿主进程本来就会执行`restart-session`，客户端随会话重启，不再以崩溃方式退出。Qt重连缺陷本身未修改；它只在重连落到“没有可用KWin”的空窗时触发，本修复消除了这个空窗。

## 事故：打开Elisa后宿主被低内存杀手杀死（已移除Elisa）

2026-09-24 05:51与05:52，用户打开Elisa后桌面两次整体崩溃。

- 经过：Android lmkd在内存耗尽时从后台一路杀到前台，`dev.moto.plasma`（oom_adj 0，TOP）被杀；KWin随即“Host connection lost”，会话重启，Marble Maps、QmlKonsole、Elisa等Qt客户端在重连空窗里于`wl_compositor::create_surface`段错误（QTBUG-150287）。容器进程不受lmkd管理，它只能杀Android应用。
- 根因：KGSL `page_alloc_max`达4.27GB（常态约0.5GB）。`kgsl_mem_alloc`跟踪显示Elisa的QSGRenderThread单次分配730–980MB；gdb在`_mesa_TexImage2D`上抓到`QSGPlainTexture`上传13824×13824与6912×6912纹理（含mipmap约977MB）。移动端播放器的封面图`sourceSize`写成`512 * Screen.devicePixelRatio`，Qt 6再按DPR换算一次，默认封面经`image://icon`（KIconThemes的头文件实现`KQuickIconProvider`，调用`QIcon::pixmap(requestedSize)`又乘一次应用DPR）：本机DPR为3，512×27=13824。DPR为1的桌面上只有512，所以不易察觉。Elisa上游移动端代码与KIconThemes上游至今未改。
- 处理：按用户决定直接卸载Elisa（`apt-get remove elisa`，无其他包依赖它），并从`plasma/ubuntu-packages.txt`移除；`provenance/…/packages.tsv`是当时发布的历史记录，保留。
- 遗留风险：任何容器应用的GPU内存失控都会让lmkd杀掉前台宿主，进而连带整个会话。可选的共享层防护是在Android侧以root监视KGSL各进程内存，在可用内存过低时先结束占用最大的容器进程；尚未实施。

## 其他修复

- 开机左上角黑框光标：宿主seat在触屏模式下仍声明`wl_pointer`，KWin据此在(0,0)绘制自己的光标（透明区在不透明层中显示为黑框），直到有指针事件。宿主改为只在鼠标/触控板模式声明`wl_pointer`（触屏模式只走`wl_touch`，本来就不经过指针），切换模式时增删能力。开机截图已无光标；按控件名启动/关闭应用检查3/3及改变布局2/2通过。蓝牙鼠标模式尚未实机验证。
- 共享存储：Android用户空间重启后vold重新挂载外部存储，容器启动时绑定的`/storage/emulated/0/Plasma`失效，`moto-plasma-shared`每3秒失败重启。`shared-storage`启动时先卸掉失效的FUSE挂载；`moto-plasma start/restart-session`检测到共享目录不可读时先重启容器以重建绑定。

## 复现

```sh
source tools/work-env.sh
uv run --script tools/kwin_pipeline_run.py OUT --zerocopy on,off              # 同一APK交替开关零拷贝
uv run --script tools/kwin_pipeline_run.py OUT --zerocopy on --kwin-env MOTO_KWIN_EXPLICIT_SYNC=-1
```

数据：`benchmarks/zero-copy-20260924/`。原生宿主可在K8构建：`MOTO_PROXY= bash plasma/build-native-core.sh`（自动使用SDK中最新NDK，xkbcommon取自已安装APK）。

## 宿主帧节拍：活跃时事件驱动（2026-09-25，APK 1.43）

本篇当时留下的“显示节拍受宿主帧回调与呈现反馈影响”，在用户反馈 QML 应用滚动掉帧时（59 篇）处理。

- **测量方法**：`dumpsys SurfaceFlinger --latency moto-zero-copy#N` 读取宿主零拷贝图层最近 128 帧的实际显示时间（`.work/sf-latency.py`）。操作是在语音助手的长对话里做一次快速滑动加一次慢速拖动。另外用 Qt 渲染循环日志（`qt.scenegraph.time.renderloop`）看应用侧的帧间隔。
- **现象**：屏幕确实运行在 120 Hz（周期 8.33 ms），但滚动时约 29% 的相邻显示间隔是 17 ms（36/124），而且零散分布，不是固定的 60 Hz。Qt 侧，语音助手和 `plasma-settings` 都有约 30% 的帧间隔超过 11 ms，时间耗在 swap 上。
- **原因**：
  - 宿主合成循环在活跃状态下每一圈都停在 `frame_clock::wait_next`，等下一个 Choreographer tick，醒来才处理 KWin 的提交、触摸命令和 SurfaceFlinger 的回报。
  - KWin 画完一帧的时刻在 vsync 周期内是抖动的。落在 tick 刚过之后的一帧，要等将近一个周期才被取走；下一帧如果按时到了，两帧会在同一个 tick 里被处理，其中一帧被跳过，显示上就少了一个 vsync。
  - 呈现反馈也要到下一个 tick 才转给 KWin，拖慢了 KWin RenderLoop 的节奏（它按“显示时刻减帧回调时刻”估算安全余量，wayland 后端允许两帧在途）。
- **做法**（`native/plasma/src/android/frame_clock.rs`、`compositor.rs`）：
  - 活跃状态改用 `wait_active`：`frameTick` 每个 tick 同时写一个 eventfd，循环一起 poll 这个 tick fd、原有的 kick fd（JNI 命令和 SurfaceFlinger 回调已经会 kick）以及 Wayland 服务端的 fd，任何一个到了都立即处理。
  - KWin 的提交一到就转成 SurfaceControl 事务，由 SurfaceFlinger 按 vsync 锁存（Chromium 的 SurfaceControl 路径也是一帧准备好就提交）。
  - 休眠（parked）逻辑不变。
- **结果**（同一手势，APK 1.42 → 1.43）：

  | 指标 | 之前 | 之后 |
  |---|---|---|
  | 语音助手，SurfaceFlinger 显示间隔 ≥10 ms 的比例 | 29%（36/124） | 13.5%（17/126）、10.3%（13/126） |
  | `plasma-settings`，同上 | 未测 | 7.1%（9/126）、2.4%（3/126） |
  | 语音助手，Qt 帧间隔 ≥11 ms | 30% | 13% |
  | 语音助手，swap p90 | 12 ms | 8 ms |
  | `plasma-settings`，Qt 帧间隔 ≥11 ms | 31% | 26% |
  | 宿主 APK CPU（空闲 / 滚动，单核） | — | 1.3% / 12.2% |

- **剩余**：
  - 仍有约 10% 的漏帧；`plasma-settings` 在 Qt 侧只从 31% 降到 26%，这个指标还包括应用自己的帧调度。
  - 下一步可考虑按 vsync 预测提前发帧回调，以及量化滑动开始时 Android 从 30 Hz 切到 120 Hz 的延迟（空闲时 SurfaceFlinger 的当前模式是 30 Hz，系统默认优先级还有一条最高 90 Hz 的投票）。

## 按 vsync 预测调节奏：两次尝试，实测没有收益，已撤回（2026-09-26，APK 1.44 → 1.45，KWin moto18 → moto17）

用户要求在上一节的基础上继续做“按 vsync 预测提前发帧回调”。

- **先定位剩下的漏帧**（`dumpsys SurfaceFlinger --latency` 的三列：期望显示、实际显示、帧就绪）：
  - KWin 的帧在提交后 0.4 ms（p90 2.6 ms）内就绪，GPU 不是瓶颈。
  - SurfaceFlinger 从收到事务到显示最少 15.5 ms、中位数 20 ms，约 2–2.5 个周期，这是 Android 在 120 Hz 下的正常流水线深度。
  - 宿主的帧回调在处理提交时立刻发出，并不慢，所以“帧回调串行”已不是问题。
- **尝试 1：KWin 允许 3 帧在途**（moto18）：wayland 后端在 Android 主输出上 `setMaxPendingFrameCount(3)`，RenderLoop 的提前量上限从 2 个 vblank 改为“在途帧数 × vblank”（至少 2）。
  - 迟到帧数没有可区分的变化：moto18 为 8、4、4、19、31、8、9、21、21、37、11、6、11，moto17 为 17、13、8、8（这些数都受下面说的重开干扰）。
- **KWin 的逐帧记录**（`KWIN_LOG_PERFORMANCE_DATA=1`，经 `/etc/plasma/gpu-env` 临时打开）暴露了时间基准问题：
  - 宿主默认报给 KWin 的是“提交即显示”的软件时间，相邻两帧间隔 10–15 ms，不是 vblank 的整数倍；KWin 自己从未错过目标，但 vblank 相位是乱的。
  - 改报 SurfaceFlinger 的真实显示时间（`debug.moto.present_feedback=hw`）会形成正反馈：真实时间里含排队时间，KWin 的安全余量从 22 ms 涨到 35 ms，迟到比例升到 40–61%。
- **尝试 2：Android 帧时间线**（APK 1.44）：
  - Java 改用 `Choreographer.postVsyncCallback`（API 33）取得各时间线的 vsyncId、截止时间和预计显示时间；
  - 每帧按顺序分配一条截止时间还没过的时间线，用 `ASurfaceTransaction_setFrameTimeline`（API 31，dlsym）设到事务上；
  - 等到截止时间，再以截止时间作为显示时间回报给 KWin。
  - 结果：KWin 收到的时间全部落在 vblank 上，但迟到帧反而更多。
- **可靠的对照方法**（`.work/pacing-ab.py`）：App 只打开一次，先等 25 秒；在同一实例里交替切换运行时开关，每轮做同样的三次滑动；同时记录 KGSL 频率（都是 600 MHz）。
  - 此前每轮都用 `--conversation` 重开语音助手，服务每次都要在后台恢复 8 MB 的 Codex 线程（59 篇），CPU 负载和滚动测试叠在一起，结果在 1–25 之间乱跳，甚至得出过相反的结论。
  - 对照结果（每轮约 126 个显示间隔中的迟到数）：

    | 配置 | 6 轮 | 中位数 / 平均 |
    |---|---|---|
    | KWin moto18，软件回报 | 9、11、18、8、6、8 | 8.5 / 10.0 |
    | KWin moto18，帧时间线 | 26、21、25、21、19、17 | 21 / 21.5 |
    | KWin moto17，APK 1.45（即 1.43 的行为） | 5、4、14、7、20、13 | 10 / 10.5 |

- **结论**：
  - 帧时间线明显更差，3 帧在途没有可测收益，而且会增加一帧延迟。两者都已撤回：APK 1.45 的代码与 1.43 相同，KWin 装回 moto17。
  - 当前状态：滚动时约 8%（中位数 10/126）的显示间隔迟到一个 vsync。
  - 上一节 1.42 → 1.43 的 29% → 10–14%，当时也用了重开 App 的方法，绝对值受干扰，但方向和这里一致。
- **仍待查明**：剩下的漏帧可能来自客户端（Qt 应用在 KWin 帧回调之后才开始画），也可能来自 KWin 按“提交即显示”推算的 vblank 相位。下一步应当用对照方法，同时记录 Qt 渲染循环和 KWin 的逐帧数据来区分。

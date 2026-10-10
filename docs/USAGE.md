# Quest XR Bridge 0.3.0 使用文档

## 1. 安装

基础姿态 SDK 需要 Python 3.13+，支持手部/身体追踪的 Quest Browser 和互通局域网。
从仓库 v0.3.0 Release 下载 wheel、文档包和 SHA256SUMS。校验与安装：

```bash
sha256sum -c SHA256SUMS
python -m pip install quest_xr_bridge-0.3.0-py3-none-any.whl
quest-xr-bridge
```

校验时应将 SHA256SUMS 列出的产物放在同一目录。基础 SDK 不导入 GI、相机 SDK 或 Pillow。
开发证书自动生成在工作目录 `certs/`；首次进入 Quest 页面需要信任该证书。
生产或已有证书通过 `cert_file` 与 `key_file` 成对传入，不会被 SDK 覆盖。

CLI 支持 `POSE_HOST`、`POSE_PORT`、`POSE_LOG_ENABLED`、`POSE_CERT_FILE`、`POSE_KEY_FILE`。
默认HTTPS端口8000、录制关闭。CLI复用SDK；SDK本身不修改宿主信号或全局日志设置。

完整Python参数、返回值、异常及线程契约见 [SDK API参考](API.md)。

## 2. 生命周期与姿态

```python
from quest_xr_bridge import QuestServer

service = QuestServer(host="0.0.0.0", port=8000, record_poses=False)
try:
    service.start()  # 等待监听就绪；端口、证书等错误直接抛出
    print(service.running, service.url)
    run_your_application(service)
finally:
    service.stop()
```

也可使用上下文管理。只读 `running` / `url` 表示对象状态；停止后可重新启动。
控制方法由宿主串行调用；`submit_video` 可由采集线程调用。
`record_poses=True` 开启有界后台录制、轮转与配额；并行录制实例须使用不同 `data_dir`。

Quest 访问 `/`，点击 **Start prep**，三秒后发送姿态。身体锚点不可用时明确提示，
不输出错误人体坐标。PC Viewer 位于 `/viewer`，250ms无新帧或断开即隐藏旧姿态。

消费端连接 `wss://<PC-IP>:8000/ws`，仅在真实新姿态到达时收到 JSON，不存在固定输出时钟。
每手21点和腕部姿态、关节半径、肘、肩、头部角度与序号均保留；缺失值为null。
手点为腕部局部坐标，肩/肘/腕为人体坐标；默认人体轴X前、Y上、Z右，单位米，四元数XYZW。
消费者也应自行按接收时间判断过期。坐标变换通过 `/api/coordinate-transform` 配置。

## 3. 冻结的双目显示配置

安装可选的参考图像处理依赖：

```bash
python -m pip install Pillow
```

```python
from quest_xr_bridge import QuestServer
from quest_xr_bridge.examples.frozen_stereo import start_frozen_video, prepare_eye

with QuestServer() as service:
    start_frozen_video(service)  # 平面显示 + 每眼1278×360、60FPS上限
    for left, right, capture_ns in your_camera_frame_pairs:
        # left/right：每眼1280×720、同步校正、连续内存RGB888
        accepted = service.submit_video(
            prepare_eye(left), prepare_eye(right), timestamp_ns=capture_ns,
        )
```

`prepare_eye` 复现验收中的裁边、隔行取样与JPEG80往返，不调整对比度/曝光。
其输出是拥有独立内存的RGB字节。应由采集/处理线程调用，不要阻塞宿主UI线程。
SDK 不生成相机帧；宿主负责相机启停、同步、标定和立体校正，以及云台/机器人跟随头姿。
实际采集必须持续提供60帧；`fps=60` 不会把低帧率输入补成60帧。
不要重新加入0.03秒等待。若上游缓存可能累积，应只保留最新完整左右帧对。

冻结参数等价于：

```python
from quest_xr_bridge import VideoConfig, VideoDisplayConfig

service.set_video_display(VideoDisplayConfig(
    projection="plane", height_m=8, distance_m=7,
    aspect_ratio=1.66667, offset_x_m=0, offset_y_m=-1,
    swap_eyes=False, saturation=1, gamma=1,
))
service.start_video(VideoConfig(width=1278, height=360, mode="stereo", fps=60))
```

先设置平面投影，再启用视频。基础 API 默认仍为 `projection="camera"`；
冻结效果通过上述显式配置复现，避免改变其他调用方的投影语义。
Quest 页面勾选 Video return，再 Start prep；页面内可在线调整显示参数。

## 4. 通用 RGB 输入

`VideoConfig.width/height` 表示**每眼**尺寸，mode为mono或stereo。
普通bytes、bytearray、memoryview或连续uint8数组均可输入，必须恰好为width×height×3字节。
SDK不猜RGB/BGR，不接受非连续视图；宿主自行转换。
双目必须一次提交两张同尺寸图和共同时间戳；单目只提交左图。
提交调用只校验并拷贝到有界共享帧槽，不等待编码/网络；返回后输入缓冲可复用。

时间戳应使用同一递增单调采集时基的纳秒值。省略时SDK使用本机monotonic_ns。
尺寸、单/双目模式变更须 `stop_video()` 后重新 `start_video()`。
启用失败抛 `VideoUnavailableError`，姿态继续运行。进程故障后修复条件并显式重新start_video。

若选择camera投影，需要与校正图实际尺寸相符的 `CameraIntrinsics(fx,fy,cx,cy)`，
设置到VideoConfig.left_intrinsics/right_intrinsics；缩放或裁切后同步更新K。
plane投影不需要K；双目校正仍由外部完成。

| 展示参数 | 意义 |
|---|---|
| projection | camera标定射线，或plane头部跟随平面；显式选择，无自动回退 |
| height_m / distance_m | plane高度/距离，各[0.05,100]米 |
| aspect_ratio | None采用每眼输入比例，或显式[0.1,10] |
| offset_x_m / offset_y_m | plane水平/竖直偏移，各[-100,100]米 |
| swap_eyes | 交换输入眼序，不修改头显IPD |
| saturation / gamma | [0,2] / [0.5,2]，默认1保持原色，两眼共用 |

数值范围是输入校验，不表示任意取值都舒适。平面距离不是镜头对焦或真实物体距离。

## 5. 视频原生部署

视频支持Linux/NVIDIA硬编。wheel只含Python、网页和补丁，不包含GPU驱动、GStreamer或GI。
主SDK用Python3.13+；独立worker默认 `/usr/bin/python3`，需能import GI，亦可指定worker_python。

Ubuntu24.04的基础组件可安装为：

```bash
sudo apt install python3-gi gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0   gir1.2-gst-plugins-bad-1.0 gstreamer1.0-tools gstreamer1.0-plugins-base   gstreamer1.0-plugins-good gstreamer1.0-plugins-bad gstreamer1.0-nice
```

仍需单独提供 `nvh264enc` / `nvh265enc`、RTP payloaders/parsers、TWCC扩展和Rust插件 `rtpgccbwe`。
发行版包不保证包含全部功能；SDK在启用视频时检查插件并真实试编码，缺项明确报错，不回退软件编码。

部署的webrtcbin必须是GStreamer1.24.13或兼容更高版本，并应用
`quest_xr_bridge/patches/gstreamer-webrtc-dtls-owner.patch`。本次实际验证版本为1.24.13。
在对应gst-plugins-bad源码根目录应用补丁，并在Meson构建中设置：

```bash
patch -p1 < /path/to/gstreamer-webrtc-dtls-owner.patch
# 加入既有Meson配置；还需要系统GStreamer开发依赖及DTLS/SRTP/SCTP开发库
meson setup build --prefix=/your/native/prefix --libdir=lib/x86_64-linux-gnu   -Dauto_features=disabled -Dwebrtc=enabled -Ddtls=enabled -Dsrtp=enabled   -Dsctp=enabled -Dintrospection=disabled -Dtests=disabled -Dexamples=disabled   '-Dpackage-name=GStreamer Bad Plug-ins (quest-crt DTLS owner fix)'
meson compile -C build
meson install -C build
```

该步骤只准备WebRTC相关构建件；NVENC/GCC等仍须另行准备。
若使用自定义前缀，启动宿主前配置LD_LIBRARY_PATH、GST_PLUGIN_PATH_1_0与GI_TYPELIB_PATH，
确保webrtcbin及相关webrtc/webrtcnice/sctp库来自同一兼容构建。
SDK检查实际加载插件版本及package标记；标记是部署约定，不是密码学证明。
补丁修复DTLS对象浮动引用所有权，单纯安装原版1.24.13仍不能保证同样的关闭行为。
新版源码若已经修复，应先核对补丁及原生关闭回归，不能仅伪造package标记。

默认8Mbps起始、16Mbps上限；GCC按TWCC调节码率，NACK/RTX在对端支持时协商。
优先H.265 Main，其次满足规格的H.264；严格按真实接收能力协商，不改写浏览器能力。
仅降低码率或丢弃过期帧，不暗降输入尺寸/FPS。重入XR会回收旧视频worker并启动新进程，可能多等几秒。

## 6. 接口、状态与排障

| 地址 | 用途 |
|---|---|
| GET /、/viewer | Quest与PC页面 |
| POST /api/webrtc/offer | 姿态WebRTC协商 |
| POST /api/video/offer、/api/video/close | 独立视频协商/关闭，使用每连接UUID peer_id |
| GET/PUT /api/video/config | 视频状态及显示配置 |
| GET/PUT /api/coordinate-transform | 坐标配置 |
| WSS /ws | 仅新姿态的JSON输出 |
| GET /health | 姿态健康及独立video状态 |

视频图像不经HTTP。`video.running` 表示源生命周期就绪，worker_running表示子进程存活；
peer_connected、frame_age_ms和实际接收统计共同判断视频是否在更新，不能只看running。
250ms无新源帧或解码帧时隐藏视频；错误不结束姿态或XRSession。
退出XR/关闭视频会关闭对应peer并停止编码，宿主相机可继续采集；stop_video才停止整个视频源。

0.2迁移到0.3：`:8001/ws` 改为`:8000/ws`；移除WSS姿态上行、`/ws/stream`、QSTR/UDP与固定90Hz时钟。
旧StreamBus/StreamClock等Python API不再导出；旧短包、video_return及QCRT bit7均不再使用。
发送方必须使用完整QCRT v5 / 804字节。相机专用video-host/presence/stats接口与CloudXR启动器已删除。


## 7. 名称迁移

仓库/发行包/CLI：`quest-crt` → `quest-xr-bridge`；Python导入：`quest_crt` → `quest_xr_bridge`。
本版不保留旧导入别名。升级时先卸载旧包，避免混用两个CLI：

```bash
python -m pip uninstall quest-crt
python -m pip install quest_xr_bridge-0.3.0-py3-none-any.whl
```

网络上的QCRT v5名称与804字节布局不因项目更名而改变。
原生插件构建标记仍为 `quest-crt DTLS owner fix`，它是已验证构建的兼容契约，不是发行包名。

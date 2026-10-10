# Quest XR Bridge 工程手册（0.3）

## 职责与资源所有权

宿主创建 `QuestServer`，生命周期内管理一个 HTTPS 服务和可选视频子进程。
相机由宿主采集；同步、标定、去畸变和立体校正，以及机器人/云台跟随头部姿态，
均由外部程序负责。SDK 不安装相机驱动，不做机器人控制或深度重建。

| 模块 | 职责 |
|---|---|
| pose / binary_protocol | 严格 Pose v5 校验、固定 QCRT 编解码 |
| coordinates | 纯坐标与四元数计算 |
| runtime | 单活动源、latest-only worker、共享序列化输出、遥测 |
| pose_log | 有界后台录制、轮转、配额 |
| server | HTTP/WSS 路由、姿态连接与应用生命周期 |
| sdk | 就绪启动、异常返回、统一关闭 |
| video / video_worker | 有界 RGB 交接、独立原生编码与视频 WebRTC |
| static | 唯一 XRSession、姿态采集、独立视频显示与 Viewer |

姿态的处理线程与 HTTP 事件循环使用 thread-safe 通知。多订阅方共享同帧转换结果，
独立等待/发送，单次发送超过 250ms 关闭慢消费者。坐标配置改变不重复广播旧帧。

视频进程使用系统 Python + GI，主 SDK 不导入 GI。共享内存固定两槽，文件锁保护
完整帧发布/claim；处理槽不会被覆盖，未处理旧帧可被新帧替换。worker 通过 mmap
附加，不注册另一份 Python resource tracker；父进程负责回收。
控制与 SDP 走管道，RGB 不经过 JSON/HTTP。stdout 仅控制协议，stderr 为原生日志。

## SDK 生命周期

`QuestServer(host="0.0.0.0", port=8000, record_poses=False, cert_file=None,
key_file=None, data_dir=None)`。data_dir 默认为当前目录，开发证书在 certs，录制在 logs。
外部证书/密钥必须成对提供，永不覆盖。

- `start(timeout=10)`：预绑定端口，等待服务 ready；返回对象自身。
- `stop(timeout=10)`：关闭连接与 worker；超时抛异常，不能丢掉未退出线程句柄。
- 重复 start/stop 幂等；停止后重新启动使用干净姿态运行态。
- 控制方法由宿主串行调用；submit_video 可来自相机线程。
- 并发录制实例必须使用不同 data_dir，SDK以目录锁强制保护活动日志；默认不录制的实例不受此限制。
- 上下文退出包括业务异常；不依赖 `__del__`。
- SDK 不改宿主信号/日志配置；CLI 负责独立程序日志和 SIGTERM 退出。

`start_video(VideoConfig(...))` 检查真实编码能力后启用；无活动服务时拒绝。
`submit_video(left, right=None, timestamp_ns=None)` 成对提交 RGB888，返回是否接纳；
停用或失效时返回 False。mode=stereo 必须传右图；mono 不接受右图。
每张图必须连续、uint8、width×height×3 字节，调用方不得在 submit 返回前改写。
时间戳缺省使用本机 monotonic_ns；显式时间戳必须在同一单调采集时基上递增。
`set_video_display(VideoDisplayConfig(...))` 更新展示参数。
`stop_video()` 只关闭视频；进程故障后可重新 start_video，尺寸/模式更换也通过重启视频。

`VideoConfig.left_intrinsics` / `right_intrinsics` 接收 `CameraIntrinsics(fx, fy, cx, cy)`。
fx/fy 为正的有限像素焦距，cx/cy 为有限的主点像素坐标；对应校正图的实际 width/height，
以左上角像素中心为 (0,0)。双目提供左右各一份；单目仅提供左眼内参。
缩放或裁剪后，宿主必须更新内参。不得使用未经校正图的 K，或从画幅比例猜测 FOV。
标定值经配置传给前端；图像仍只经过 SDK 的 RGB 入口。

## Pose 与 QCRT

输入只支持 `type=pose`、`version=5`、UUID session_id、正 uint32 seq、有限非负时间。
reference_space 固定 spine-upper-scapula，units 固定 meters；肩和头必填。
手点/四元数/头角/时间不能是 NaN 或 Infinity；非法跟踪标志与数据可用性不一致时拒绝。
每手 radii 恒为 21 项，缺省 null；非空半径必须非负且有对应关节位置。

QCRT little-endian，magic QCRT、binary version 5，总长 804 B：

| 偏移 | 内容 |
|---:|---|
| 0 | magic，4 B |
| 4 | binary version，uint8 |
| 5 | 追踪 flags：左/右手、左/右肘、左/右肩、头；bit7=0 |
| 6 | uint16 reserved=0 |
| 8 | uint32 seq |
| 12 / 20 | float64 timestamp_ms / capture_epoch_ms |
| 28 | UUID 的 16 B |
| 44 | 左后右的两组 21×3 float32 手点 |
| 548 | 左右腕各四个 float32，XYZW |
| 580 / 604 | 左右肘 / 左右肩，各两个三维点 |
| 628 | head yaw、pitch，两个 float32 |
| 636 | 左后右的两组 21 float32 radii |

缺失向量必须整体 NaN，不能混合 NaN 与有限值；这是二进制的空值标记，解码成 null。
半径单项 NaN 解码成 null；Infinity、负半径、零范数四元数、旧包与保留位均拒绝。
Python encoder 总是输出 804 B，decoder 返回已校验的 JSON 字典。

输出为 HTS wrist-relative：肩/肘/腕遵循配置的人体轴映射；每手 landmarks 以腕为原点，
使用腕姿态旋转到局部，四元数采用 XYZW。head 仍以身体坐标计算，不随显示预设改变。
原始身体坐标 X 前、Y 上、Z 右；yaw 右正、pitch 上正。关节半径不随轴变换改变。

`server_received_epoch_ms - capture_epoch_ms` 受 PC/Quest 时钟偏差影响，不能直接当绝对延迟。
relative_transport_delay_ms 减去会话最小差值，用于观察额外排队变化；帧龄使用本机单调时钟。

## 视频传输与双目

视频使用独立 RTCPeerConnection，不与姿态 BUNDLE。视频编码/GLib loop 在子进程，
只有轻量信令经过 HTTPS 服务；视频错误不改变姿态连接或核心 healthy。

目标输入每眼 1280×720@60，内部 full-SBS 为2560×720。
GStreamer 使用 appsrc → RGB/YUV 转换 → NVENC → parse → RTP pay → webrtcbin。
接收能力满足时优先 H.265 Main，使用 nvh265enc/h265parse/rtph265pay；
H.264 使用 nvh264enc/h264parse/rtph264pay。选择以真实 SDP 为依据，不改写浏览器能力。
GCC rtpgccbwe 经 TWCC / transport-cc 估计带宽并更新 NVENC 码率，保留标准 PLI/关键帧链。
媒体 transceiver 在协商前显式启用 `do-nack`，使接收端支持时协商 NACK/RTX；
仅在 RTP caps 中声明反馈不足以启用重传。重传复用同一媒体连接和 GStreamer 的有界包历史。
低延迟配置不使用 B 帧和 lookahead；必须按配置协商足够的 codec profile/tier/level。
level-asymmetry-allowed 不能绕过接收端最高能力；不对浏览器 SDP 伪造能力。

GStreamer 1.24 系列与较新版本 NVENC 属性名字存在区别；使用 worker 中的能力检查与兼容配置。
必需 GI namespaces 为 Gst、GstApp、GstVideo、GstRtp、GstSdp、GstWebRTC，
插件包括 webrtcbin、nvh264enc、h264parse、rtph264pay、rtpgccbwe 与 ICE/DTLS/SCTP。
Rust RTP 插件需单独准备；不能把 nvidia-smi 或 factory 注册成功当作实际编码证明。
WebRTC 插件使用 1.24.13 或以上版本，并应用仓库
[DTLS transport 引用补丁](../../patches/gstreamer-webrtc-dtls-owner.patch)。
在 gst-plugins-bad 源码根目录运行 `patch -p1 < /绝对路径/quest-xr-bridge/patches/gstreamer-webrtc-dtls-owner.patch`，
构建时传入 Meson 参数 `-Dpackage-name='GStreamer Bad Plug-ins (quest-crt DTLS owner fix)'`。
安装同一 patch 版本的 webrtc 插件及其 webrtc/webrtcnice/sctp 库；用运行时库、插件搜索路径选择它们。
SDK 在视频启用时通过 factory.load().get_plugin() 检查实际加载的插件版本和 package 标记；
无标记或版本不符明确拒绝视频，姿态服务可独立运行。标记记录部署配方，正确性仍由原生回归验证。
本机 core/base/NVENC 保留1.24.2，与修复后的1.24.13 WebRTC库已通过ABI与实际编解码验证；
检查对象是webrtc插件版本，不能以Gst.version()代替。没有扩展到所有混合版本的兼容保证。
启动进行真实 NVENC 小帧编码 probe 后才 ready。无软件编码回退。

只保留最新 RGB 帧，source 停止时不循环重发旧图。GCC 可降低码率和丢过期帧，
不自动降分辨率/FPS；硬件不足或网络不足通过统计明确报告。
两个 peer 仍共享网卡/Wi-Fi、GPU、CPU 与内存带宽，不能承诺完全物理隔离。

Quest 使用唯一 `immersive-vr` XRSession；local-floor、hand-tracking、body-tracking
仍为必需能力，失败时显示实际原因。显示只复用现有 `XRWebGLLayer` 的逐眼 framebuffer，
不要求 WebXR Layers 扩展。camera 与 plane 为显式投影选择，共用 renderer；没有自动回退。

camera 投影中，每个 XRView 使用运行时实际投影矩阵还原眼局部射线，按实际眼到头的旋转换到头部方向，
再转换为相机约定的 X右、Y下、Z前，以对应相机的 K 投到校正图。
相机像素投影为 u=fx·X/Z+cx、v=fy·Y/Z+cy；采样时保留像素中心约定。
每眼按 `view.eye` 选对应图和内参，交换眼时两者一起交换。
相机视场未覆盖的方向显示不透明黑色，不用边缘拉伸或修改头显投影来补满。
不覆盖头显 IPD，不添加人工立体基线或眼平移。外部相机 baseline 与用户 IPD 的差异
以及头部平移的深度重投影不能靠 RGB-only 显示修复；SDK 不作这种保证。
机器人跟头由外部程序执行，显示不再额外按绝对头部姿态旋转视频，以免重复转动。

plane 投影在 viewer 参考空间定义一块共享显示面：中心为
`[offset_x_m, offset_y_m, -distance_m]`，高度 height_m，宽度为高度乘 aspect_ratio
（None 时采用每眼输入宽高比）。从实际眼位发出的射线与该面求交，然后分别采样对应眼图。
使用头显实际眼位，不覆盖IPD；显示面距离不表示画面内物体的真实距离。
该投影不消费相机内参、不执行深度重建，外部双目同步与校正责任不变。

每个新解码帧只上传一次 full-SBS 视频纹理，两眼始终采样同一个已上传帧。
先绘制相机背景，再使用现有投影绘制姿态/HUD；没有相机深度时不声称与远端物体正确遮挡。
视频与姿态共用 GL 绘制目标，不合并网络 peer 或资源所有权。
XRWebGLLayer 使用 ignoreDepthValues=true；RGB 未提供真实场景深度，不能把 HUD/骨架的深度当作相机实景深度。
固定 foveation 设为0，要求最小外围降采样；不增加 framebuffer 超采样。
具体解释以 [WebXR规范](https://www.w3.org/TR/webxr/#dom-xrwebgllayer-fixedfoveation) 和
[Meta FFR文档](https://developers.meta.com/vr/documentation/web/webxr-ffr/) 为准，实际边缘清晰度与头显渲染负载须复测。
视频 renderer 只拥有自己的 shader/texture；视频错误不得销毁共享 GL、XRSession 或姿态 peer。
当前上传使用 texImage2D；历史 Quest Browser 152 HEVC 的 texSubImage2D 曾触发 INVALID_VALUE，
不增加第二条上传路径。

展示字段包括 projection（camera/plane）、swap_eyes bool、saturation 在 [0,2]、gamma 在 [0.5,2]。
plane 几何：height_m/distance_m 在 [0.05,100] 米、offset_x_m/offset_y_m 在 [-100,100] 米，
aspect_ratio 为 None 或 [0.1,10]。范围仅用于输入校验，不表示任何取值都舒适。
颜色参数默认均为1，保持原色；saturation=0为灰度。两眼共用同一组色彩参数，
在线更新只改变 shader 参数，不重建连接或 XRSession。
有效视频需 SDK source 启用、页面开关、可用 WebGL2 以及最近250ms有新解码帧；
无有效视频时显示黑色背景，姿态采集与 HUD 继续。

视频协商 `POST /api/video/offer` 的请求示意：

```json
{
  "sdp": "<浏览器生成的完整 SDP>",
  "type": "offer",
  "peer_id": "47a2ed96-57ca-4e19-a48f-85ad140eeac1"
}
```

peer_id 是页面为每次连接生成的新 UUID，用作本次连接的 nonce，不是鉴权凭据。
关闭请求 `POST /api/video/close` 仅包含同一次连接的 UUID：

```json
{"peer_id": "47a2ed96-57ca-4e19-a48f-85ad140eeac1"}
```

返回 `{"closed":true}` 或 `{"closed":false}`。
匹配当前或协商中的 peer 时取消协商、关闭该 peer/编码管线；旧连接的延迟关闭请求
不能关闭新连接。页面关闭视频、退出 XR 或离开页面时发送关闭请求，停止无人接收的编码。
这一操作不停止 SDK source、不关闭姿态或服务。每个原生 worker 最多承载一条媒体连接；
下一次协商前由 SDK 回收旧进程并重新拉起 worker，共享内存、最新 RGB 和采集时间戳继续保留。
采集线程在进程启动期间仍只校验和交接最新帧，不等待编码或网络；重入会增加启动延迟。
`video.running` 表示 SDK 视频源就绪，`worker_running` 单独报告子进程是否存活；重启期间不让页面误取消自己的协商。
宿主仍可继续提交 RGB，也可自行停止相机。整个视频源的生命周期由 `stop_video()` 管理。

## 环境与迁移

CLI 仅保留 POSE_HOST、POSE_PORT、POSE_LOG_ENABLED（默认0）、POSE_CERT_FILE/POSE_KEY_FILE。
SDK 构造参数显式配置，不从环境隐式拉起服务。录制每段最大256MiB/15分钟、总配额5GiB，
满队列丢旧录制帧并统计；录制错误不会停止姿态发布。

0.2 调用方需改：输出地址 8001/ws→8000/ws；固定QSTR/UDP消费改为实际帧事件消费；
移除旧流API导入；发送端更新为804 B/v5，无video_return或bit7。
视频接入使用SDK RGB入口，不再调用旧video-host/video-presence/video-stats，
不再使用相机专用启动器或外部CloudXR页面。SDK视频状态位于 /health.video。

## 冻结与验证

0.3.0的最终配置和验收范围统一见[冻结与验收](ACCEPTANCE.md)，
使用方式见[使用文档](../USAGE.md)，相对main的设计变化见[改造说明](REFACTOR.md)。

发布保留协议、坐标、生命周期、帧所有权、客户端新鲜度、实际GLSL渲染及清理回归。
测试通过只证明对应契约；短时资源平稳不能证明长期无泄漏。

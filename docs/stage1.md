# Quest 3 + ZED Mini 实时双目视觉链路：现状、问题与建议

## 1. 当前系统现状

现有系统核心硬件为：

* Meta Quest 3
* ZED Mini 双目摄像头
* 本地 PC

目前 `quest-crt` 已承担 Quest 3 与 PC 之间的实时通信，主要用于：

* Quest 手部识别
* 上肢/手部姿态数据
* WebRTC DataChannel / WebSocket 通讯
* PC 侧实时消费姿态数据

当前链路可以概括为：

```text
Quest 3
  ↓
WebXR Hand / Body Tracking
  ↓
quest-crt
  ↓
PC
```

接下来希望加入第二条反向视觉链路：

```text
ZED Mini
  ↓
PC
  ↓
quest-crt
  ↓
Quest 3
```

目标是让佩戴 Quest 3 的驾驶员通过 ZED Mini 双目相机获得尽可能自然、清晰、低延迟、流畅的第一人称立体视觉。

---

## 2. ZED Mini 选型判断

ZED Mini 仍然适合继续作为当前阶段的双目视觉设备。

其重要特点并不是“视野完全等同于人眼”，而是：

* 左右摄像头基线约 63 mm，与典型人眼双眼间距接近；
* 水平视场与 Quest 3 的显示视场比较接近；
* 原生提供同步左右 RGB；
* 可以提供校正后的双目图像；
* 同时可以提供 Depth / Disparity；
* SDK 可以获取完整相机内参、畸变参数以及双目外参。

因此它非常适合作为 teleoperation / 第一人称双目视觉输入。

但需要注意：

**ZED Mini 的 63 mm 基线接近人眼，并不意味着把相机左右画面直接铺到 Quest 左右眼就一定自然。**

最终视觉舒适度仍取决于：

* 双目校正；
* 左右眼对应关系；
* FOV 匹配；
* Quest 用户 IPD；
* 相机投影参数；
* 画面裁剪/缩放方式；
* 延迟；
* 帧同步；
* 后续可能的 depth reprojection。

---

## 3. 当前最主要的问题

此前在 `quest-crt` 中进行视频实验时，已经发现明显的：

**层层压缩导致的像素损失。**

这应该作为当前视觉链路的首要问题解决。

需要重点排查当前链路是否存在类似：

```text
ZED RGB
 ↓
CPU/Numpy
 ↓
JPEG
 ↓
WebSocket/Base64
 ↓
浏览器解码
 ↓
Canvas
 ↓
再次编码/转换
 ↓
Quest 显示
```

或者其他重复的：

* RGB/YUV 转换；
* JPEG 压缩；
* H.264 重编码；
* Canvas 中转；
* CPU/GPU 来回复制；
* 分辨率反复缩放。

对于驾驶视觉，应该遵守一个核心原则：

> **ZED 图像从采集到 Quest 显示，理想情况下只发生一次有损视频压缩。**

否则即使最终码率很高，也无法恢复前面已经损失掉的图像细节。

---

## 4. 不建议把视频放入现有 Pose 通道

现有 `quest-crt` 的姿态通信适合：

* 数据量小；
* 高频率；
* latest-only；
* 强实时；
* 控制用途。

因此现有 Pose DataChannel 应继续专门负责：

```text
Quest → PC
手部 / 上肢 / 控制数据
```

不建议：

* 把 RGB frame 塞进 Pose DataChannel；
* 使用 JSON 传图片；
* 使用 Base64；
* 使用普通 WebSocket 连续发送 JPEG；
* 让视频流和控制数据共享同一个消息处理循环。

姿态链路和视觉链路的 QoS 特征完全不同。

---

## 5. 推荐总体架构

建议继续在 **同一个 `quest-crt` 项目内**建设视频功能，但逻辑上将 Pose 和 Video 分开。

推荐：

```text
                    Quest 3
                       │
        ┌──────────────┴──────────────┐
        │                             │
        ▼                             ▼
 Pose PeerConnection            Video PeerConnection
        │                             ▲
        │                             │
        ▼                             │
       PC                        WebRTC Video
                                      ▲
                                      │
                                 H.264 Encode
                                      ▲
                                      │
                                  ZED Mini
```

即：

### 通道 1：Pose

```text
Quest
 ↓
WebRTC DataChannel
 ↓
quest-crt
 ↓
PC / Robot
```

保持现在的设计。

### 通道 2：Stereo Video

```text
ZED Mini
 ↓
ZED SDK
 ↓
Rectified Left + Right
 ↓
H.264 Low-Latency Encode
 ↓
WebRTC RTP
 ↓
Quest
 ↓
WebXR Stereo Rendering
```

两条链路属于同一个 `quest-crt` 服务和前端，但尽可能采用两个独立 `RTCPeerConnection`。

---

## 6. 为什么推荐两个 PeerConnection

第一阶段不建议 Pose 和 Video 共用同一个 PeerConnection。

原因是：

### Pose 是控制链路

要求：

* 极低延迟；
* 极少数据；
* 视频卡顿不能影响控制；
* 网络异常后应快速恢复。

### Video 是高带宽链路

可能发生：

* 码率波动；
* packet loss；
* congestion control；
* resolution adaptation；
* encoder restart；
* SDP renegotiation；
* decoder reset；
* Wi-Fi 带宽突变。

如果二者过度耦合，会增加视频问题影响控制链路的风险。

因此推荐：

```text
PeerConnection #1
└── pose DataChannel

PeerConnection #2
└── stereo video track
```

但两者仍可以共享：

* HTTPS Server；
* Quest 页面；
* signaling 服务；
* connection/session ID；
* metrics；
* 配置系统。

---

## 7. 第一阶段视频规格建议

第一版优先测试：

```text
ZED Mini
1280 × 720 Left
+
1280 × 720 Right
@ 60 FPS
```

形成：

```text
2560 × 720 @ 60 FPS
Stereo Side-by-Side
```

然后：

```text
Stereo frame
 ↓
H.264
 ↓
WebRTC
 ↓
Quest
```

推荐首先选择：

**720P / eye + 60 FPS**

而不是：

**1080P / eye + 30 FPS**

原因是驾驶/遥操作场景中：

* 转头；
* 车辆运动；
* 手眼协调；
* 距离判断；

都对时间连续性非常敏感。

60 FPS 往往比提高静态分辨率更重要。

后续再根据实际：

* GPU 编码能力；
* Wi-Fi 带宽；
* Quest 解码能力；
* 端到端延迟；

向更高分辨率推进。

---

## 8. 编码链路建议

如果 PC 有 NVIDIA GPU，优先考虑：

```text
ZED SDK
 ↓
GPU Buffer
 ↓
NVENC H.264
 ↓
WebRTC
```

尽可能避免：

```text
GPU
 ↓
CPU RGB
 ↓
numpy
 ↓
重新转换
 ↓
software encoder
```

需要重点调查：

* ZED SDK frame 当前在哪里；
* 是否发生 GPU → CPU copy；
* 是否经过 OpenCV；
* 是否转成 JPEG；
* aiortc 当前编码路径；
* 是否可以直接使用硬件 encoder；
* encoder 输出是否能直接进入 WebRTC RTP；
* Quest Browser 最终是否走 hardware decode。

第一阶段的优化重点应该是：

> **减少 copy + 减少颜色空间转换 + 单次编码。**

而不是仅仅提高码率。

---

## 9. Quest 端不能简单显示普通 SBS 视频

即使传过来的是：

```text
Left | Right
```

Quest 端也不能简单把整张 SBS 当一个普通大屏视频。

正确思路应该是：

```text
Stereo Texture
      │
 ┌────┴────┐
 ▼         ▼
Left      Right
 │          │
 ▼          ▼
Quest      Quest
Left Eye   Right Eye
```

即：

* 左相机只进入 Quest 左眼；
* 右相机只进入 Quest 右眼；
* 使用 WebGL/WebXR 分别渲染。

并且需要按照 ZED 的相机参数控制：

* crop；
* UV；
* projection；
* FOV；
* aspect ratio。

**禁止为了填满 Quest FOV 而直接非等比例拉伸 ZED 图像。**

否则会出现：

* 空间尺度失真；
* 物体变宽/变窄；
* 深度判断异常；
* 双眼疲劳。

---

## 10. 关于“深度场合理”

第一阶段不一定需要传 ZED Depth。

只要做到：

```text
ZED Left → Quest Left Eye
ZED Right → Quest Right Eye
```

并且：

* 左右帧严格同步；
* 正确 rectification；
* 几何投影正确；

人眼本身就能够通过双目视差感知真实深度。

因此：

> **Stereo RGB 本身就是第一层深度信息。**

ZED Depth 不应该被误解为“有 Depth 才能有立体感”。

---

## 11. Depth 应作为第二阶段能力

当基础双目 RGB 已经稳定后，再加入 ZED Depth。

Depth 最有价值的用途包括：

### 1. View Reprojection

驾驶员轻微移动头部时，根据：

```text
RGB + Depth + Quest Head Pose
```

重建新的视角。

这能够减少：

“眼睛在移动，但摄像头视点完全不移动”

产生的不自然感。

### 2. AR Occlusion

例如：

* 虚拟机械臂；
* 导航；
* 路径；
* 抓取点；
* HUD；

可以与真实世界产生正确的前后遮挡关系。

### 3. IPD / Viewpoint Compensation

ZED 固定 63 mm baseline，而 Quest 用户实际 IPD 不一定为 63 mm。

Depth 可以用于更高级的重新投影，从而减少固定 baseline 的影响。

### 4. Scene Geometry

后续机器人可以共享：

```text
RGB
Depth
Robot State
Hand Pose
Head Pose
```

构建统一的实时 3D teleoperation 空间。

---

## 12. Depth 暂时不要直接按全分辨率 60 FPS 发送

第一阶段不建议同时传：

```text
Stereo RGB @60Hz
+
Full-resolution Float Depth @60Hz
```

网络和处理压力会明显增加。

后续 Depth 可以考虑：

* 低分辨率；
* 15–30 FPS；
* 16-bit；
* disparity；
* quantized depth；
* GPU texture compression；
* 独立 DataChannel；
* 独立 video/depth track。

具体方案应在 RGB 链路稳定以后再决定。

---

## 13. 推荐 `quest-crt` 工程结构

后续可以逐渐整理成类似：

```text
quest-crt/
│
├── server.py
│
├── quest_crt/
│   │
│   ├── pose/
│   │   ├── transport
│   │   └── protocol
│   │
│   ├── video/
│   │   ├── zed_capture
│   │   ├── stereo
│   │   ├── encoder
│   │   ├── webrtc_video
│   │   └── metrics
│   │
│   └── calibration/
│       ├── zed
│       └── quest
│
└── static/
    └── Quest WebXR Client
        │
        ├── Pose PeerConnection
        ├── Video PeerConnection
        └── Stereo Renderer
```

因此不是另起一个完全孤立的工程，而是让 `quest-crt` 从：

```text
Quest Pose Transport
```

逐渐变成：

```text
Quest Teleoperation Transport
```

其内部包含：

```text
Pose
Video
Depth
Calibration
Metrics
```

---

## 14. 接下来代码评估时优先调查的问题

建议按以下顺序检查现有仓库。

### A. 当前图像链路

查清：

```text
ZED
→ ?
→ ?
→ Quest
```

每一步具体的数据格式。

尤其检查是否存在：

* JPEG；
* PNG；
* Base64；
* Canvas；
* OpenCV encode；
* WebSocket binary；
* repeated resize；
* repeated RGB/BGR/YUV conversion。

### B. WebRTC 架构

确认：

* `RTCPeerConnection` 创建位置；
* `/api/webrtc/offer` 当前逻辑；
* DataChannel 生命周期；
* PeerConnection close 条件；
* 是否容易增加第二套 signaling；
* Quest 端当前 PeerConnection 管理方式。

### C. PC 视频编码能力

确认机器：

* GPU 型号；
* NVENC 是否可用；
* H.264 encoder；
* GStreamer；
* FFmpeg；
* aiortc/PyAV 当前实际编码器。

### D. ZED 采集路径

确认：

* 当前 ZED SDK 版本；
* 720p60 是否稳定；
* rectified Left/Right 获取路径；
* GPU Mat / CPU Mat；
* timestamp；
* stereo synchronization；
* camera calibration 参数。

### E. Quest 视频消费

确认 Quest Browser：

* H.264 decode；
* video element；
* WebGL texture update；
* WebXR stereo layer；
* 每眼独立绘制方式；
* 是否存在 video → canvas → texture 的额外 copy。

---

## 15. 第一阶段验收指标

第一阶段不要只用“看起来还可以”判断。

建议至少记录：

### 图像

* ZED 输入分辨率
* Quest 实际解码分辨率
* 编码 bitrate
* encoder FPS
* decoder FPS
* dropped frames

### 延迟

分别测：

```text
Capture
→ Encode
→ Network
→ Decode
→ Render
```

最终得到：

```text
Capture-to-Display Latency
```

### 网络

记录：

* RTT
* packet loss
* jitter
* available bitrate
* actual bitrate

### Quest

记录：

* WebRTC inbound stats
* decode time
* dropped frames
* WebXR render FPS

### 视觉

人工重点观察：

* 静态文字清晰度；
* 远处细节；
* 快速运动拖影；
* 转向时卡顿；
* 左右眼是否同步；
* 立体深度是否自然；
* 是否眼疲劳；
* 近距离物体是否出现异常视差。

---

## 16. 当前建议的施工顺序

建议严格按阶段推进：

```text
Phase 0
现有代码视频链路审计
        ↓
Phase 1
ZED 720p60 Left/Right 稳定采集
        ↓
Phase 2
单次 H.264 低延迟编码
        ↓
Phase 3
第二个 WebRTC PeerConnection
PC → Quest
        ↓
Phase 4
Quest 普通视频验证
        ↓
Phase 5
Quest WebXR 左右眼独立渲染
        ↓
Phase 6
FOV / Calibration / Stereo Geometry
        ↓
Phase 7
质量、码率、延迟优化
        ↓
Phase 8
Depth
        ↓
Phase 9
Depth-based View Reprojection
```

不要一开始同时做：

```text
4K
+ Depth
+ Head Reprojection
+ Stereo
+ Robot Overlay
```

否则很难判断问题到底来自：

* 编码；
* 网络；
* Quest；
* 双目几何；
* 深度；
* WebXR。

---

## 17. 当前最核心的技术结论

整个项目现阶段可以浓缩成四点：

**第一，ZED Mini 可以继续用。**
63 mm 双目 baseline 对第一人称立体 teleoperation 是合理的，但需要正确处理 FOV 和双目几何。

**第二，最大问题不是“压缩”本身，而是“重复压缩”。**
实时无线视频必须编码，但应该尽量做到一次 H.264 编码、一次 Quest 解码。

**第三，视频应该建设在 `quest-crt` 内，但与 Pose Transport 解耦。**
推荐第二个 WebRTC PeerConnection，而不是把视频塞进当前 Pose DataChannel/WebSocket。

**第四，先把 Stereo RGB 做正确，再做 Depth。**
左右眼 RGB 本身已经能够产生真实立体深度；Depth 的主要价值是后续 view reprojection、IPD 补偿、遮挡以及空间增强。

当前第一优先目标应明确为：

> **ZED Mini 720p60 双目 → 单次低延迟 H.264 → WebRTC → Quest 3 → 正确的 WebXR 左右眼显示，并建立完整端到端质量与延迟指标。**

这个目标完成后，再决定是否需要提高分辨率、调整编码方案以及引入 Depth。

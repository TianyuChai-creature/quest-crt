# Quest 3 + ZED Mini 双目视觉链路：最终修订方案（stage1-revise）

> 基线文档：`docs/stage1.md`（原始提议）
> 修订依据：13 条评审意见 + 对仓库代码与 aiortc 1.15.0 源码的事实核验
> 核验日期：2026-08-11

本方案是 `stage1.md` 的最终修订版。与原始提议相比，变化最大的三处是：**会话模型**（transport_session_id 与 pose_stream_id 分离）、**signaling 契约**（recvonly offer / sendonly answer）、**编码路径**（aiortc 内建编码器仅作 smoke test，av.Packet + NVENC 尽早进入）。

---

## 1. 代码事实核验结论（评审意见 vs 实际代码/API）

本节记录 13 条评审意见与仓库实际行为的对照。**评审意见与代码冲突的地方以代码事实为准，已在文档正文修正。**

### 1.1 已确认的评审判断

| # | 评审意见 | 核验结论 |
|---|---|---|
| 1 | `PoseFrame.session_id` 在 XR 进入后才创建，与 transport 生命周期不同 | ✅ 确认。`quest_crt/static/index.html:1020` 在 `navigator.xr.requestSession()` 之后才 `crypto.randomUUID()`；pose WebRTC 连接在页面初始化即建立（`index.html:316`）。session_id 写入 QCRT 二进制包 16 字节（`index.html:612`）与 JSON 帧（`index.html:1148`），服务器 `_binary_message_order` 从偏移 28:44 读取（`server.py:577-583`）——这是 **pose_stream_id**，Phase 0 不动 |
| 2 | answer 方向应为 sendonly 而非 sendrecv | ✅ 方向结论确认，**但机制表述修正（Phase 1 实测）**：answer m-line 方向不是 `reverse_direction()` 的直接产物，而是 `and_direction(transceiver.direction, transceiver._offerDirection)`（`rtcpeerconnection.py:575-578`，位与：`DIRECTIONS = [inactive, sendonly, recvonly, sendrecv]`）。远端 recvonly offer 时 `_offerDirection = sendonly`（`rtcpeerconnection.py:936-941`）；若服务器无本地 track（transceiver=recvonly），`recvonly & sendonly = inactive`，**应答无方向、sender 不启动**（`__connect` 只在 `currentDirection in [sendonly, sendrecv]` 时 send，`:1084-1086`）。实测：无 track 应答 recvonly offer → answer 无 direction、currentDirection=inactive；先 `addTrack`（transceiver=sendrecv）再应答 → `sendrecv & sendonly = sendonly` ✓。**服务器必须在 `setRemoteDescription` 之前 `addTrack`** |
| 3 | 8000→8002 跨 Origin 需要 CORS | ✅ 确认。`server.py` 无任何 CORSMiddleware；POST JSON 会触发 preflight；证书 SAN 已含 LAN IP（`server.py:959-996`），两端口复用同一证书无额外问题 |
| 5 | av.Packet track 由 aiortc 负责后续 RTP 全链路 | ✅ 确认，无需重写 pipeline。aiortc 1.15.0 `_next_encoded_frame`（`rtcrtpsender.py:313-343`）对 `Frame` 走 `encoder.encode(data, force_keyframe)`，对非 Frame（即 `av.Packet`）走 `encoder.pack(data)`；`H264Encoder.pack()` 存在（`codecs/h264.py:298`）。`MediaStreamTrack.recv()` 返回类型声明为 `Union[Frame, Packet]`（`mediastreams.py:55`） |
| 5 | PLI/FIR 不会天然传到外部 NVENC | ✅ 确认，这是本项目必须自建的桥。PLI/FIR→`_send_keyframe()`→`__force_keyframe=True`（`rtcrtpsender.py:278-281, 351-356`），但 `__force_keyframe` 只在 encode 分支消费（`:316-319`），**pack 分支不接收**；全包无任何 track 级 keyframe 回调。且 `RTCRtpSender` 由 `RTCPeerConnection` 内部直接构造（`rtcpeerconnection.py:1199`），公开 API 无法子类化 |
| 9 | rVFC metadata 含 rtpTimestamp/captureTime/receiveTime/expectedDisplayTime | 基本确认（Chromium WebRTC 扩展字段），`captureTime` 可能为 null，Phase 5 需 probe 验证 Quest Browser 实际暴露 |
| 12 | fail-safe 必须在 PC/机器人侧 | ✅ 确认。quest-crt 只提供 pose 最新值中继（`LatestPose`）与健康报告（`/health`），无控制输出——fail-safe 属下游机器人职责，本方案以验收标准形式约束 |

### 1.2 需要补充/修正的评审判断

| # | 评审意见 | 补充/修正 |
|---|---|---|
| 2 | "video offer 必须存在 alive 的 pose WebRTC，否则 403" 删除 | ✅ 采纳：视频绑定 transport_session_id 而非 pose PC 存活状态 |
| 7 | probe 写法 `"XRMediaBinding" in window` | 补充：这只是构造器存在性检查。完整 probe = 构造器存在（`typeof XRMediaBinding !== "undefined"`）+ `requestSession` 传 `optionalFeatures: ["layers"]` 后检查 `session.enabledFeatures` 含 `"layers"` + `createQuadLayer(video, {layout:"stereo-left-right"})` try/catch。另注意：**Meta Quest Browser 至今未公开支持 XRMediaBinding**，WebGL fallback 大概率是实际路径，probe 只是确认而非期望 |
| 7 | "video→WebGL 是 zero-copy" | 修正：不得描述为绝对 zero-copy，只能写"不经 canvas，走 Chromium GPU path"，真实 copy 次数 Phase 5 profile 确认 |
| 8 | "直接使用 ZED projection matrix" | ✅ 采纳修正：Phase 1-4 用 fx/fy/cx/cy 计算每眼角视场与光心→决定 quad 角尺寸/裁剪/UV；完整 projection matrix 留到 Depth 重投影阶段 |
| 10 | video-control DataChannel 从初始 SDP 建立 | 补充关键实现约束：**浏览器必须在 offer 里创建该 DataChannel**（与现有 pose channel 同模式，`index.html:318`），否则 offer 无 SCTP m-line，服务器侧 `createDataChannel` 无法在单次协商内加入。服务器侧注意：现有 `on("datachannel")` 关闭非 `"pose"` label（`server.py:1066-1068`），video PC 需要独立 label 策略（接受 `video-control`、后续 `depth`） |
| 3 | 未来独立进程 | ✅ 采纳：模块边界按可独立进程化设计（见 §3.2） |
| 6 | profile/level 能力协商 | 补充 aiortc 事实：内建 `H264Encoder` 硬编码 Baseline profile（`codecs/h264.py:281`），这正是必须走 NVENC 路径的原因之一；profile-level-id 从协商后的 `RTCRtpCodecParameters.parameters` 读取 |

### 1.3 评审未提及、但 Phase 0 必须处理的代码事实

- `ActivePoseSource` 是全局单控制源门（`server.py:443-486`），`test_latest_ingress.py` 直接依赖其公共 API 与语义——Phase 0 重构必须保持其"单一控制源"语义与测试兼容。
- 现有前端重连是**全量关闭重试**（`closeCurrentTransport()`，`index.html:255-272`）+ 固定 1s（`:274-277`）+ `connectionAttempt` 覆盖机制（`:387-405`）。video 通道加入后该逻辑不能复制粘贴，需抽象为 transportManager。
- WSS fallback URL 目前无 query 参数（`index.html:367`）；FastAPI `websocket.query_params` 已有先例（`server.py:1181` 的 `format` 参数），`?tsid=` 直接复用该机制。

---

## 2. 最终架构

### 2.1 会话模型（修订核心）

```text
transport_session_id            pose_stream_id
─────────────────               ──────────────
页面/Quest 客户端生命周期        每轮 pose stream 生命周期
页面初始化即生成                 XR 进入时生成（现有 index.html:1020 逻辑不动）
页面刷新即失效                   新一轮 XR 可重新生成
归属：                         归属：
  pose WebRTC                    现有 PoseFrame.session_id
  pose WSS fallback              QCRT 二进制包内嵌（offset 28:44）
  video WebRTC                   保持现状，Phase 0 不触碰
```

**服务器端 SessionContext**（Phase 0 实现于新模块 `quest_crt/transport_session.py`）：

```python
class TransportSession:
    transport_session_id: str        # key
    pose_generation: int             # pose 通道每次 attach 递增
    video_generation: int            # video 通道（Phase 1 起使用）
    channels: set[str]               # {"pose"} / {"pose","video"}
    last_seen_monotonic_ms: float    # lease 心跳
    created_at_monotonic_ms: float
    client_name: str
```

**generation 语义**：通道关闭回调必须携带其 attach 时的 generation；`end_channel(tsid, channel, stale_generation)` 与当前 generation 不匹配时**忽略**——防止旧 PC 延迟触发的 close 把已重连成功的新 PC 从 session 清掉，或释放掉新 PC 持有的控制源。

**lease 语义**：`last_seen` 由 pose 帧流量与通道事件刷新；空闲超过 `TRANSPORT_SESSION_LEASE_MS`（默认 10s）且无活跃通道的 session 被惰性回收（`begin_channel` 时顺带 sweep，Phase 0 不引入后台线程）。

**与 ActivePoseSource 的关系**：

```text
ActivePoseSource（保留，语义不变）
  "当前控制源唯一"——任一时刻最多一个 transport_session 持有控制权
  但不再承担 transport session 生命周期：
  - acquire/release 的 owner 改为 session 条目（按 transport_session_id 键控）
  - session 的所有通道全部关闭且未被复用后才 release
```

### 2.2 端口与进程边界

```text
8000  Pose / Quest HTTPS 页面 / Pose signaling (/api/webrtc/offer) / pose WSS (/ws)
8001  existing downstream viewer（3D viewer + /ws/stream StablePoseStream）——不动
8002  Video signaling (/api/webrtc/video/offer) + Video RTC（独立 uvicorn 线程/loop）
```

- 8002 独立线程模式复用现有 `viewer_thread` 先例（`server.py:1390-1402`），提供：**signaling 隔离、RTC callback/lifecycle 隔离、event-loop 级隔离、未来拆进程的工程边界**。
- **明确不是**：网络 QoS 隔离（同一 Wi-Fi airtime 共享）、完整 CPU/GPU 资源隔离。
- CORS：8002 加 `CORSMiddleware`，允许 origin 为 8000 的 `https://<host>:8000`（POST JSON 需处理 preflight）。后续可换 reverse proxy 统一 origin，代码边界设计为可独立成进程（video 相关代码全部位于独立 app/模块，不引用 8000 的全局单例，仅通过 session 表与事件总线交互）。

### 2.3 Signaling 契约

**Pose PC（现状，仅加字段）**：浏览器 offerer、非 trickle 单次 POST、`/api/webrtc/offer`；`WebRTCOffer` 增加必填 `transport_session_id`。WSS fallback 增加 `?tsid=` query 参数。`PoseFrame.session_id`（pose_stream_id）语义不变。

**Video PC（Phase 1）**：

```js
// Quest/browser
videoPc.addTransceiver("video", { direction: "recvonly" })
videoPc.createDataChannel("video-control", { ordered: false, maxPacketLifeTime: 100 })
const offer = await videoPc.createOffer()
→ POST /api/webrtc/video/offer { transport_session_id, sdp, type }

// PC/服务器（8002 独立 loop）——顺序关键：
// 先 addTrack（transceiver=sendrecv）再 setRemoteDescription，
// 否则 and_direction(sendrecv, sendonly)=sendonly 无从成立，
// 无 track 时 recvonly & sendonly = inactive（§1.1 表行 2 实测）
pc.addTrack(video_track, stream)
setRemoteDescription(offer)
→ createAnswer → setLocalDescription
```

- **无 renegotiation**。answer 方向 `sendonly`（机制见 §1.1 表行 2）。
- video-control DataChannel **由浏览器在 offer 中创建**（SCTP m-line 进 offer），服务器 `on("datachannel")` 按 label 路由：`video-control` 接受，其余关闭。video PC 的 label 策略独立于 pose PC（现有 pose PC 仍只接受 `"pose"`）。
- video offer 只校验 `transport_session_id` 合法且未过期，**不要求 pose PC 存活**（支持 pose down / video up）。

### 2.4 服务端 Video pipeline（Phase 2-3）

```text
ZED SDK（独立采集线程）
  → rectified L/R → SBS 拼接（如选 SBS）
  → latest-only 帧队列（容量 2~3，复刻 PoseStreamProcessor 反压哲学）
  → NVENC（PyAV，参数与协商 profile/level 严格一致）
  → av.Packet 直出
  → 自定义 VideoStreamTrack.recv() 返回 av.Packet   ← aiortc 原生 pack 分支
  → aiortc RTP 发送（packetization / seq / RTCP / NACK / RTX / 拥塞反馈，全部复用）
```

**PLI→NVENC IDR 桥（必须自建，Phase 3 验收项）**：

```text
Quest 解码失败 → RTCP PLI/FIR → aiortc sender._send_keyframe()
  → 桥接器（attach 到 sender，通知编码侧）→ NVENC 下一帧强制 IDR
```

实现约束（§1.1 确认）：`_send_keyframe` 是私有 API 且 `RTCRtpSender` 无法公开子类化。方案：`addTrack` 后通过 `pc.getSenders()` 拿到对应 sender，包装 `_send_keyframe`（调用原实现 + 通知编码桥）。**这是对 aiortc 私有 API 的耦合，升级 aiortc 时需复核**；短 GOP（IDR 间隔 1~2s）作为桥失效时的恢复兜底。

### 2.5 编码 profile/level 协商（能力协商式，不钉死）

```text
Quest SDP offer → 读取 H.264 fmtp profile-level-id
  → 服务端选择兼容 profile/level（Main/High、Level 4.x/5.x 以实测为准）
  → NVENC 参数与协商结果严格一致
```

**禁止 SDP 宣称一种 profile/level 而实际 bitstream 发另一种**。目标规格 2560×720@60 需 Level ≥4.2（宏块率 432,000 MB/s）；内建 `H264Encoder` 硬编码 Baseline（`codecs/h264.py:281`）且无 bf/tune 控制，**仅用于 smoke test**（见 §4 Phase 2）。

### 2.6 Quest 渲染（Phase 4）

**首选路径（需 probe 确认，§1.2 已述）**：

```text
单条 SBS H.264 → 单个 <video> → XRMediaBinding.createQuadLayer({layout:"stereo-left-right"})
→ XRQuadLayer → head-locked
```

一次编码、一次解码、双眼天然同步、compositor 直接处理 stereo layout。

**fallback 路径（大概率实际路径）**：

```text
<video> → requestVideoFrameCallback → texImage2D 直传 WebGL（不经 canvas）
→ SBS UV split（左眼 [0,0]-[0.5,1]，右眼 [0.5,0]-[1,1]）
```

**head-locked**：视频 quad 每帧跟随 viewer pose（model matrix = viewer pose + 前向 offset），不缩放、不拉伸。**禁用非等比拉伸填满 Quest FOV**。

**ZED 投影（Phase 1-4 用角视场方法）**：

```text
ZED fx/fy/cx/cy → 每眼 angular FOV / optical center → quad 角尺寸 / crop / UV mapping
```

完整 camera projection matrix 仅在 Depth→3D reconstruction→novel-view reprojection 阶段使用。并明确：**depth reprojection 只能补偿有限头部位移，无法解决大角度转头看到 ZED 原始视野之外区域的问题**。

### 2.7 延迟测量与遥测（Phase 5）

**优先使用 rVFC metadata，而非 mediaTime 换算**：

```text
server: { rtpTimestamp → capture timestamp } 环形映射（采集线程写入）
browser: rVFC 记录 metadata.rtpTimestamp / captureTime / receiveTime /
         expectedDisplayTime / performance.now()
→ 端到端关联
```

**时钟同步：四时间戳法**（不用 RTT/2）：

```text
PC send t0 → Quest recv t1 → Quest send t2 → PC recv t3
offset = ((t1 - t0) + (t2 - t3)) / 2    RTT = (t3 - t0) - (t2 - t1)
```

**video-control DataChannel 职责**（telemetry 5~10Hz，不跟 60Hz 视频同频，不进 pose 通道）：

```text
clock probe / video telemetry / encoder & renderer 状态 / config ack / 后续 depth metadata
```

### 2.8 丢包韧性（Phase 6 验收）

按实际恢复链路验证，不简单假设"丢包→坏到下一个 IDR"：

```text
loss → NACK → RTX（aiortc 内建，rtcrtpsender._retransmit）→ 无法及时恢复 → decoder/request PLI
→ PLI/FIR 到达 → PLI→NVENC IDR 桥闭环 → 恢复
```

重点验收：NACK/RTX 是否正常、PLI/FIR 是否到达、PLI→NVENC IDR 是否闭环、恢复耗时多少 ms。短 GOP 保留为兜底。**ULPFEC 不作为普通配置项**，仅在实测 Wi-Fi loss 表明 RTX+PLI 不足时专项评估。

### 2.9 transportManager 状态机（Phase 0 前端重构）

```text
pose down / video up  → 机器人侧 safe-stop（下游职责）；实时视频继续显示；UI 明确 CONTROL LOST
video down / pose up  → UI 显示 VIDEO LOST + last live frame: xxx ms ago；
                        画面 dim/黑屏/明确警告（禁止长期停最后一帧误导驾驶员）；
                        机器人侧按安全策略禁止继续驾驶
```

视频重连用指数退避（1s→2s→5s→10s），pose 保持现有 1s 重连。

### 2.10 Depth 归属（Phase 7）

```text
Video PeerConnection
├── stereo H.264 media
├── video-control DataChannel
└── depth DataChannel          ← Phase 7
```

depth DataChannel 特性钉死：`unordered`、`latest-only`、低/零 retransmission、严格 bitrate cap、queue full → drop old。**不依赖"浏览器天然给媒体比 DataChannel 更高优先级"的假设**。

---

## 3. 明确不做 / 延后事项

- 不重写 RTP pipeline（aiortc 原生 pack 路径已足够，§1.1）。
- dual-track（每眼一路）不作为默认优化路线，仅在实测证明 SBS 单流有明显收益缺口时评估。
- ULPFEC 不预置（§2.8）。
- depth reprojection 不解决大角度转头视野外问题（§2.6）。
- quest-crt 不承担机器人侧 fail-safe（§2.9，下游职责）。

---

## 4. 阶段计划（Phase 0-7 + Optional）

### Phase 0 — TransportSession 基础设施（纯架构重构）

**目标**：建立会话/代次/租约模型与前端 transportManager，现有 Quest→PC pose WebRTC + WSS fallback 行为完全兼容。

**任务**：
- 服务端：`quest_crt/transport_session.py`（TransportSession / generation / lease / 惰性回收）；`WebRTCOffer` + `transport_session_id`；`/ws` + `?tsid=`；`ActivePoseSource` 关系重构（保留单一控制源语义与 `test_latest_ingress.py` 兼容）；`pose_stream_id` 全链路不动。
- 前端：页面初始化生成 `transportSessionId`；WebRTC offer 携带；WSS URL 携带；连接逻辑抽象为 `transportManager`（单 pose 通道实例，状态/重连/覆盖机制与现状一致）；`XRMediaBinding` / WebXR `layers` capability probe（仅探测与日志）。

**验收**：现有 pytest 全量通过 + 新增 session 管理单测；手工回归 WebRTC 连接、WSS fallback、断线重连行为与重构前一致。

### Phase 1 — Video 骨架（:8002）

**任务**：独立 uvicorn 线程 + CORS；`/api/webrtc/video/offer`（recvonly offer / sendonly answer）；video-control DataChannel（浏览器 offer 内创建）；synthetic SBS 源（测试图案）；video 独立 reconnect/teardown；session 归属验证（pose down / video up 等矩阵）。

**验收**：两个 PC 共存于同一 transport_session；任一通道独立断开/重连不影响另一个。

### Phase 2 — 内建编码器 smoke test（低规格）

**任务**：aiortc 内建 H.264 编码器 + 低分辨率/30fps → Quest `<video>` 显示。

**验收**：仅验证 SDP / ICE / ontrack / 浏览器 decode / reconnect / transportManager / 基础显示。**不评估最终 2560×720@60 的画质码率延迟**。

### Phase 3 — ZED + NVENC 目标路径

**任务**：ZED 双目采集 + latest-only 帧队列 + NVENC（PyAV）+ av.Packet track；H.264 profile/level 协商落地；NACK/RTX 验证；**PLI/FIR→NVENC IDR 桥**；目标规格转向 2560×720@60。

**验收**：PLI→IDR 闭环可测（人为丢包注入验证）；协商 profile/level 与实际 bitstream 一致。

### Phase 4 — Quest 渲染

**任务**：XRMediaBinding + stereo-left-right + head-locked 优先；不支持则 rVFC + WebGL SBS UV split；ZED fx/fy/cx/cy → 角视场/光心标定。

**验收**：左右眼各自只显示对应半幅；无拉伸；head-locked 无画面滞后感。

### Phase 5 — 延迟与遥测

**任务**：rtpTimestamp↔capture 环形映射；四时间戳时钟同步（经 video-control DC）；rVFC metadata 全量记录；`getStats` 采集；`VideoTelemetry`（复用 `IngressTelemetry` 模式）。

**验收**：capture-to-display 延迟可测量、可重复；captureTime 为 null 时的降级路径明确。

### Phase 6 — 视频质量优化

**任务**：bitrate / GOP / IDR；丢包恢复链路验证（§2.8）；reconnect 调优；长时间稳定性；左右眼清晰度一致性；capture-to-display latency budget 达成。

**验收**：§2.8 恢复链路各环节指标达标；24h 连续运行无泄漏、无状态漂移。

### Phase 7 — Depth

**任务**：depth DataChannel（§2.10 特性）；IPD compensation；limited view reprojection；AR occlusion。

### Optional — 仅在实测需要时

- dual track（每眼一路）
- ULPFEC
- 更深度的自定义 RTP pipeline

---

## 5. 与 aiortc 1.15.0 的耦合点与升级风险

| 耦合点 | 位置 | 升级风险 |
|---|---|---|
| pack 分支支持 av.Packet | `rtcrtpsender.py:_next_encoded_frame` | 低（长期存在的特性） |
| PLI→`_send_keyframe` 私有 API 桥 | `rtcrtpsender.py:_send_keyframe` | 中（私有 API，升级需复核） |
| `reverse_direction` / sendonly answer | `rtcpeerconnection.py` | 低（标准协商行为） |
| `H264Encoder` Baseline 硬编码 | `codecs/h264.py:281` | 仅影响 smoke test，无关 NVENC 路径 |

---

## 6. 立即行动（本文档之后）

1. 新建分支（自 `ui-integration` 切出）。
2. 实施 **Phase 0**（§4），完成后跑全量回归。
3. Phase 0 施工中若发现与本文档假设冲突的实际代码事实，先报告事实与影响，再调整方案。

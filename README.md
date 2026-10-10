# Quest XR Bridge 0.3.0

嵌入式 Quest 姿态与通用 RGB 视频 SDK。一个 `QuestServer` 对象管理 HTTPS 服务、
姿态连接及可选视频进程；相机、机器人和云台由宿主程序控制。

- 姿态：独立 aiortc WebRTC 数据连接，严格 QCRT v5 / 804 B，latest-only。
- 视频：独立 GStreamer/NVENC WebRTC 媒体连接，成对 RGB 输入、单轨 SBS。
- 同一 HTTPS 入口与 XRSession；视频故障不关闭姿态或 XR。
- 通用 `/ws` 输出、Viewer、坐标配置、可选录制；无相机品牌依赖。

## 安装与启动

基础功能需要 Python 3.13+。从 [v0.3.0 Release](https://github.com/TianyuChai-creature/quest-xr-bridge/releases/tag/v0.3.0)
下载 wheel 后安装：

```bash
python -m pip install quest_xr_bridge-0.3.0-py3-none-any.whl
quest-xr-bridge
```

Quest 打开 `https://<PC-IP>:8000/`；PC 查看 `https://<PC-IP>:8000/viewer`。
接受开发证书后点击 **Start prep**，三秒准备结束开始发送姿态。
需支持并允许 WebXR 手部及身体追踪的 Quest Browser。

嵌入宿主程序，无需另外维护服务终端：

```python
from quest_xr_bridge import QuestServer

with QuestServer() as service:
    print(service.url)
    # 在此运行宿主业务；离开上下文会关闭 SDK 自有资源。
```

视频需额外部署 Linux/NVIDIA、GStreamer/GI 与本仓库的 WebRTC 引用修复；
这些原生组件不包含在通用 Python wheel 中。缺少视频依赖不影响基础姿态功能。

## 本次冻结的显示效果

2026-10-10 用户实机确认 OK，冻结以下配置：

| 项目 | 冻结值 |
|---|---|
| 输入采集 | 外部程序提供每眼 1280×720、60 FPS、同步校正 RGB |
| 参考预处理 | 左右各裁1列、纵向隔行取样、JPEG quality=80 |
| SDK 实际输入 | 每眼1278×360；单轨 SBS 为2556×360 |
| 更新节奏 | 已取消原参考脚本的0.03秒等待，以新帧驱动、上限60 FPS |
| 显示面 | 高8m、宽高比1.66667、前方7m、下方1m |
| 色彩/眼序 | saturation=1、gamma=1、正常眼序 |

参数为显式配置，不隐藏在相机驱动中。可从已安装的 wheel 导入通用示例：

```python
from quest_xr_bridge import QuestServer
from quest_xr_bridge.examples.frozen_stereo import start_frozen_video, prepare_eye

# prepare_eye 的 JPEG 处理需要额外安装 Pillow：python -m pip install Pillow
with QuestServer() as service:
    start_frozen_video(service)
    for left_rgb, right_rgb, timestamp_ns in your_camera_frame_pairs:
        service.submit_video(prepare_eye(left_rgb), prepare_eye(right_rgb),
                             timestamp_ns=timestamp_ns)
```

`your_camera_frame_pairs` 由宿主提供，须在其生命周期内采集同步、已校正的 RGB；
示例不会打开相机。已经是1278×360的处理后 RGB 应直接提交，不要重复预处理。
显示距离是虚拟平面的几何，不是相机焦距或真实物体距离。

当前视觉验收通过不等于完整720p60、长期无泄漏或所有网络故障均已验收。
视频仍使用 NVENC/WebRTC，参考 JPEG 阶段后还会再次视频编码。

## 文档

- [使用与安装](docs/USAGE.md)：生命周期、RGB 接入、显示设置、原生依赖与迁移。
- [SDK API参考](docs/API.md)：Python接口参数、返回值、异常和线程契约。
- [工程手册](docs/reference/ARCHITECTURE.md)：职责边界、协议、背压、资源与错误处理。
- [相对 main 的改造说明](docs/reference/REFACTOR.md)：基线124ae02、决策及兼容性变化。
- [冻结与验收结论](docs/reference/ACCEPTANCE.md)：实测范围、冻结参数及未完成项。
- [通用双目示例](examples/frozen_stereo.py)：与相机品牌无关的参考预处理。

## 开发检查

```bash
uv sync
uv run python -m unittest discover -s tests -v
node --test tests/*.cjs
uv run python scripts/check_runtime_contracts.py --ca certs/cert.pem --inject
uv build --wheel --out-dir dist/v0.3.0
```

`--inject` 需要已启动服务且没有实际 Quest 占用姿态连接；验证固定协议与通用输出。
实际 GPU 渲染回归需要 Node、Linux Mesa/EGL，否则该项明确跳过。
本项目面向可信局域网；应用层鉴权和机器人安全控制由部署方负责。

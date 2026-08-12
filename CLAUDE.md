# quest-crt — Quest 3 + ZED Mini 立体远程操作

阶段文档（权威事实来源，开工先读）：
- `docs/phase3-zed-nvenc.md` — 当前阶段设计 + 实测事实 + Gate 清单 + OPEN ISSUE
- `docs/phase2-quest-acceptance.md`、`docs/stage1-revise.md` — 前置阶段验收记录

## 当前分支与状态

- 分支 `phase3-zed-nvenc`（main 是基线）
- Phase 3 = ZED→NVENC→encoded av.Packet→aiortc RTP→Quest，Mode A（2560×720@30）/ Mode B（1920×540@60，重点）
- **有 OPEN ISSUE 未解决**：Quest 上 NVENC 流颜色交换（见 phase3 文档 §11 OPEN ISSUE）——用户指示先不改
- 服务端启动：`PHASE3_MODE=B|A|synthetic uv run python server.py`（:8000 页面 / :8002 信令+healthz；ZED 惰性启动）

## 本机环境陷阱（违反会浪费大量时间）

1. **跑测试必须先清 PYTHONPATH**（shell 设了 `/opt/ros/jazzy/lib/python3.12/site-packages`，会泄漏进 uv 环境弄坏 pytest）：
   `PYTHONPATH= uv run --with pytest --with pytest-asyncio python -m pytest tests/ -q`
2. **禁止 `pkill -f` / `pgrep -f`**——会匹配到自己的 shell 命令行导致自杀（exit 144）。杀服务端用：
   `fuser -k 8000/tcp 8002/tcp`
3. pyzed 5.4 已装进项目 uv 环境（pyproject 锁定官方 wheel URL）；pyzed 5.x API 变动：`VIDEO_SETTINGS`（原 `CAMERA_SETTINGS`）、`get_camera_settings` 返回 `(ERROR_CODE, value)`、EXPOSURE 是 0-100 级别非 µs（`EXPOSURE_TIME` 仅 GMSL2 ZED-X 有）
4. PyAV 17：无法设置 `AV_FRAME_FLAG_KEY`；h264_nvenc 输出有 ~2 帧延迟（pts FIFO 已处理）；encoder rebuild（IDR 方案）实测 ~425ms
5. 服务端占用 ZED 相机时，本地抓帧脚本会 `CAMERA STREAM FAILED TO START`——先 `fuser -k 8000/tcp 8002/tcp`

## 验证工具（在 /tmp，未入库）

- `e2e_probe.py` / `pli_timing.py` / `capture_rtp.py` — RTP 解码、PLI→IDR 计时、抓帧存 PNG

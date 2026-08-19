# CloudXR + ZED Integration Workflow

## Objective

Keep Quest CRT's sensing and downstream contracts while replacing the abandoned custom video
path with ZED Mini -> Televiz -> CloudXR Runtime -> CloudXR.js.

## Branch and invariants

- Branch: `codex/cloudxr-zed-integration`, created from `main` at `4388995`.
- QCRT magic and current 604/628-byte compatibility stay unchanged.
- QSTR v1, `/ws`, `/ws/stream`, coordinate transforms, JSONL logging, real-Teleop, and DIME
  contracts stay unchanged.
- One immersive WebXR session must own both CloudXR rendering and QCRT capture.
- NVIDIA upstream source is not vendored. Integrations use packages, configuration, and thin
  adapters.
- The selected operating point is 1280x720 per eye at 60 FPS with native ZED disparity on a
  head-locked 102-degree presentation cylinder. Rejected 30 FPS profiles are not shipped.

## Evidence policy

- Automated checks must leave a command or machine-readable report.
- Stereo manual gates must use eye-specific labels or markers; disparity-only animation is not
  sufficient evidence that each eye receives the correct view.
- Hardware evidence is written to `/tmp/quest-crt-zed-check` unless explicitly requested
  otherwise.
- Manual gates H1-H6 stop the goal until the user returns `PASS` or actionable feedback.
- A failed manual gate is fixed within that phase before later integration work starts.

## Gates

1. **Baseline**: all existing unit tests pass before integration changes.
2. **H1 - ZED raw capture**: HD720@60 timing passes and left/right/color images are accepted.
3. **H2 - Televiz desktop**: replay and direct ZED window paths are accepted.
4. **H3 - CloudXR synthetic**: stereo eye routing, color, smoothness, and comfort are accepted.
5. **H4 - CloudXR ZED**: quality and latency are accepted against the old Phase 3 result.
6. **H5 - Unified client**: one WebXR session carries CloudXR video and QCRT capture.
7. **H6 - Final**: fault isolation, downstream compatibility, 30-minute stability, and operator
   experience pass.

## Execution phases

### Phase 0 - Baseline

- Verify clean `main`, create the branch, run the full test suite, and record protocol routes.

### Phase 1 - ZED hardware isolation

- Run Stereolabs diagnostics.
- Open ZED Mini in HD720@60 without depth.
- Verify timestamps, FPS, frame sizes, exposure/gain, eye order, and color.
- Save SBS/left/right evidence and stop at H1.

### Phase 2 - camera_viz / Televiz desktop isolation

- Obtain Isaac Teleop in a temporary reference checkout.
- Accept/download CloudXR components only with user approval.
- Validate replay, then direct ZED, in desktop window mode.
- Stop at H2.

### Phase 3 - CloudXR synthetic on Quest

- Validate H.264/H.265/AV1 capability and a bounded resolution/bitrate matrix.
- Collect capture, streaming, decode, render, drop, jitter, and GPU metrics.
- Stop at H3.

### Phase 4 - Direct ZED over CloudXR

- Use direct GPU ZED capture and true stereo composition layers.
- Compare codecs and only a small set of operating points.
- Stop at H4 before changing Quest CRT's browser client.

### Phase 5 - Quest CRT regression

- Re-run QCRT/QSTR, health, logging, coordinate, `/ws`, and `/ws/stream` checks.

### Phase 6 - Unified WebXR client

- Move the existing body-frame and QCRT exporter into the CloudXR.js client.
- Keep CloudXR and QCRT on independent connections within one immersive session.
- Validate synthetic video plus live QCRT first, then stop at H5.

### Phase 7 - Integrated ZED and fault isolation

- Validate video loss, pose loss, ZED loss, and short network loss independently.
- Run the selected operating point for 30 minutes.
- Verify real-Teleop and DIME contracts before H6.

### Phase 8 - Handoff

- Add preflight/launcher support, document dependencies and licensing, and preserve the legacy
  Quest page as rollback until the new path is accepted.

## Progress

- [x] Branch created from clean `main`.
- [x] Baseline: 35 tests passed.
- [x] USB identifies ZED-M camera and HID; SDK 5.4.0, CUDA 13.0, and GPU diagnostics pass.
- [x] Physical replug restored the ZED-M sensor MCU; firmware recovery was not used.
- [x] Root cause evidence: video module SN is `10661128`; the ZED-M HID descriptor exposes
  `iSerial 0` in the failed state, despite correct device permissions and an available
  `2560x720` video node.
- [x] Phase 1 automated capture: 180/180 frames, 0 failures, 60.281 FPS, 16.828 ms p95,
  monotonic camera timestamps, and correct SBS/left/right dimensions.
- [x] H1 raw image acceptance passed by the user.
- [x] Phase 2 isolated environment: Isaac Teleop `1.5.95rc1`, Televiz, CloudXR, CuPy CUDA 13,
  OpenCV, and pyzed 5.4 installed in the temporary reference checkout.
- [x] Phase 2 automated desktop smoke: synthetic ~58 FPS, replay 30 FPS, and ZED direct
  ~69 FPS steady-state, all with zero missed renders.
- [x] A 1280x720-per-eye 30 FPS candidate was verified at 29.9-30.0 FPS with zero missed
  renders, then rejected during H4 because its motion sampling caused visible judder.
- [x] H2 Televiz desktop acceptance passed by the user at the 30 FPS operating point.
- [x] User accepted the NVIDIA CloudXR EULA; CloudXR Runtime 6.3.0, Quest3 profile,
  WSS proxy, and locally hosted Web Client are running.
- [x] H3 synthetic stereo config added at 1280x720 per eye and 30 FPS.
- [x] Quest client connected from `192.168.8.222`; WSS signaling and UDP media are active.
- [x] H3 transport smoke: OpenXR system/session and Vulkan device created, synthetic source
  28-30 FPS, Quest render 65-72 FPS, zero missed frames, and ~6 ms GPU-end-to-encode-end.
- [x] H3 visual gate repeated with explicit LEFT EYE / RIGHT EYE content after the upstream
  disparity-only pattern proved ambiguous to the operator.
- [x] H3 synthetic stereo operator acceptance passed.
- [x] H4 automated ZED stream: 30.0 FPS capture, 72.3-72.7 FPS Quest render, zero missed
  frames, ~6 ms GPU-end-to-encode-end, 39% encoder utilization, and ~2.6 GiB total GPU memory.
- [x] H4 initial quad was rejected: extra stereo baseline caused discomfort, the world/lazy quad
  looked like a cinema screen, and the camera image was too dark.
- [x] H4 geometry retest used native ZED disparity only (`stereo_baseline_mm: 0`) on a
  head-locked 102-degree presentation cylinder. Brightness was accepted without adjustment.
- [x] H4 geometry feedback: cylinder fills the view better, native disparity is preferred, and
  brightness is acceptable. Stable 30 FPS capture still has visible motion judder at 72 Hz.
- [x] H4 frame-rate A/B automated 60 FPS result: 60.0 FPS capture, ~72 FPS Quest render,
  zero missed frames, ~6.25 ms GPU-end-to-encode-end, 39% encoder utilization, and ~2.65 GiB
  total GPU memory.
- [x] H4 operator comparison selected 60 FPS: motion judder improved while clarity, color,
  native stereo fusion, brightness, and 102-degree cylinder presentation remained acceptable.
- [x] H4 ZED quality/latency operator acceptance passed for 720p60.
- [x] H4 reopened for user-requested 1080p30 comparison. Hardware calibration reports rectified
  FOV 65.3x39.6 degrees at HD1080, versus 82.7x52.7 degrees at HD720; the first 1080p test uses
  the calibrated 65.3-degree cylinder instead of stretching it to the prior presentation angle.
- [x] H4 1080p30 automated result: 29.9 FPS capture, ~72 FPS Quest render, zero missed frames,
  ~6.2 ms GPU-end-to-encode-end, 28% encoder utilization, and ~3.02 GiB total GPU memory.
- [x] H4 operator rejected 1080p30: detail improvement was not substantial, 30 FPS motion caused
  dizziness, and sustained Quest render later fell to roughly 52-57 FPS. Final selection: 720p60.
- [x] H4 final acceptance passed.
- [x] Phase 5 live TLS/WSS regression: `/health` and coordinate API returned 200; one injected
  Pose v4 produced Viewer HTS/body output with shoulders and 21 landmarks per hand, plus a
  638-byte QSTR envelope with pose_seq 1 and 21 landmarks per hand.
- [x] Phase 6 client preparation keeps NVIDIA's cached `bundle.js` byte-identical and injects a
  thin QCRT exporter before it. The exporter attaches a second frame callback to the same
  immersive `XRSession`, uses an independent WebRTC/WSS pose connection, and retains the
  3-second no-pose preparation interval and 628-byte QCRT packet.
- [x] Phase 6 automated checks: generated client marker/order verified, CloudXR-origin CORS
  preflight verified, JavaScript syntax passed, and all 37 Python tests passed.
- [x] The local CloudXR service now hosts the generated client at
  `https://192.168.8.122:48322/client/`; the official cached client remains unmodified.
- [ ] H5-H6 pending.

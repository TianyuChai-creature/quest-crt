// Run: node tests/test_head_angles.cjs
const assert = require("node:assert/strict")
const fs = require("node:fs")
const path = require("node:path")
const vm = require("node:vm")

for (const file of ["static/index.html", "cloudxr/qcrt-exporter.js"]) {
  const source = fs.readFileSync(path.join(__dirname, "..", file), "utf8")
  function between(start, end) {
    const left = source.indexOf("function " + start + "(")
    const right = source.indexOf("function " + end + "(", left)
    assert(left >= 0 && right > left, file + ": missing function")
    return source.slice(left, right)
  }
  const context = vm.createContext({ BODY_FRAME_EPSILON: 1e-6, BINARY_PACKET_SIZE: 636, Math, Number })
  vm.runInContext(
    between("dotVectors", "crossVectors") +
    between("quaternionToMatrix", "multiplyMatrix3") +
    between("readHead", "transformHandToBodyFrame"),
    context
  )
  const body = { matrix: [[0, 0, -1], [0, 1, 0], [1, 0, 0]] }
  function angles(x, y, z, w) {
    const frame = {
      getViewerPose() {
        return { transform: { orientation: { x, y, z, w } } }
      },
    }
    return vm.runInContext("readHead", context)(frame, {}, body)
  }
  function near(actual, expected) {
    assert(Math.abs(actual - expected) < 1e-6, file + ": " + actual + " != " + expected)
  }
  near(angles(0, 0, 0, 1).yaw_deg, 0)
  near(angles(0, -Math.sin(Math.PI / 12), 0, Math.cos(Math.PI / 12)).yaw_deg, 30)
  near(angles(0, Math.sin(Math.PI / 12), 0, Math.cos(Math.PI / 12)).yaw_deg, -30)
  near(angles(Math.sin(Math.PI / 18), 0, 0, Math.cos(Math.PI / 18)).pitch_deg, 20)
  near(angles(-Math.sin(Math.PI / 18), 0, 0, Math.cos(Math.PI / 18)).pitch_deg, -20)
  const missing = vm.runInContext("readHead", context)({ getViewerPose: () => null }, {}, body)
  assert.equal(missing.tracked, false)
  assert.equal(missing.yaw_deg, null)

  vm.runInContext(
    between("encodePosePacket", file.startsWith("static") ? "multiplyMatrices" : "makePacket"),
    context
  )
  const hand = { tracked: false, points: Array(21).fill(null), wrist_orientation: null }
  const joint = { tracked: false, position: null }
  const packet = {
    session_id: "407b7a3f-4791-479c-aa81-48e813aca057",
    seq: 1, timestamp_ms: 100, capture_epoch_ms: 1000,
    hands: { left: hand, right: hand },
    elbows: { left: joint, right: joint },
    shoulders: { left: joint, right: joint },
    head: { tracked: true, yaw_deg: 30, pitch_deg: -20 },
  }
  const bytes = vm.runInContext("encodePosePacket", context)(packet)
  const view = new DataView(bytes)
  assert.equal(bytes.byteLength, 636)
  assert.equal(view.getUint8(4), 4)
  assert.equal(view.getUint8(5), 1 << 6)
  near(view.getFloat32(628, true), 30)
  near(view.getFloat32(632, true), -20)
  packet.video_return = true
  const videoView = new DataView(vm.runInContext("encodePosePacket", context)(packet))
  assert.equal(videoView.getUint8(5), (1 << 6) | (1 << 7))
}
console.log("Head angle and packet checks passed")

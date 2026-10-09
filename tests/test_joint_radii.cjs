// Run: node tests/test_joint_radii.cjs
const assert = require("node:assert/strict")
const fs = require("node:fs")
const path = require("node:path")
const vm = require("node:vm")
const packets = []

for (const file of ["static/index.html", "cloudxr/qcrt-exporter.js"]) {
  const source = fs.readFileSync(path.join(__dirname, "..", file), "utf8")
  function between(start, end) {
    const left = source.indexOf("function " + start + "(")
    const right = source.indexOf("function " + end + "(", left)
    assert(left >= 0 && right > left)
    return source.slice(left, right)
  }
  const context = vm.createContext({ BINARY_PACKET_SIZE: 804, referenceSpace: {}, Math, Number })
  vm.runInContext(source.match(/const HAND_JOINTS = \[[\s\S]*?\n\s*\]/)[0] + "\n" +
    between("readPose", "subtractVectors"), context)
  const joints = vm.runInContext("HAND_JOINTS", context)
  const sourceHand = { hand: new Map(joints.map(name => [name, name])) }
  let calls = 0
  const frame = {
    getJointPose(space) {
      calls++
      return { radius: space === "index-finger-tip" ? 0.008 : 0.01,
        transform: { position: { x: 1, y: 2, z: 3 }, orientation: { x: 0, y: 0, z: 0, w: 1 } } }
    },
    getPose() { throw new Error("joint pose must use getJointPose") },
  }
  const read = vm.runInContext("readHand", context)
  const hand = read(frame, sourceHand, {})
  assert.equal(calls, 21)
  assert.equal(hand.tracked, true)
  assert.equal(hand.radii[8], 0.008)
  assert.equal(hand.radii.length, hand.points.length)
  assert(read(frame, null, {}).radii.every(r => r === null))
  for (const radius of [undefined, -1, Infinity, NaN]) {
    const invalid = { getJointPose() { return { ...frame.getJointPose("wrist"), radius } } }
    assert(read(invalid, sourceHand, {}).radii.every(r => r === null))
  }
  const fallback = read({ getPose: () => ({ transform: frame.getJointPose("wrist").transform }) }, sourceHand, {})
  assert.equal(fallback.tracked, true)
  assert(fallback.radii.every(r => r === null))
  assert(read({ getJointPose: () => null }, sourceHand, {}).radii.every(r => r === null))

  context.transformPointToBodyFrame = p => p
  context.transformOrientationToBodyFrame = q => q
  vm.runInContext(between("transformHandToBodyFrame", "encodePosePacket") +
    between("encodePosePacket", file.startsWith("static") ? "multiplyMatrices" : "makePacket"), context)
  const transformed = vm.runInContext("transformHandToBodyFrame", context)(hand, {})
  assert.deepEqual(transformed.radii, hand.radii)
  const missing = read(frame, null, {})
  const joint = { tracked: false, position: null }
  const packet = {
    session_id: "407b7a3f-4791-479c-aa81-48e813aca057", seq: 1,
    timestamp_ms: 100, capture_epoch_ms: 1000,
    hands: { left: transformed, right: missing },
    elbows: { left: joint, right: joint }, shoulders: { left: joint, right: joint },
    head: { tracked: true, yaw_deg: 30, pitch_deg: -20 }, video_return: false,
  }
  const bytes = vm.runInContext("encodePosePacket", context)(packet)
  const view = new DataView(bytes)
  assert.equal(bytes.byteLength, 804)
  assert.equal(view.getUint8(4), 5)
  assert(Math.abs(view.getFloat32(636 + 8 * 4, true) - 0.008) < 1e-8)
  assert(Number.isNaN(view.getFloat32(636 + 21 * 4, true)))
  packets.push(Buffer.from(bytes).toString("base64"))
}
console.log(process.argv.includes("--packets") ? JSON.stringify(packets) : "Joint radius capture and packet checks passed")

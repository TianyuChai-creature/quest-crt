const assert = require("node:assert/strict")
const fs = require("node:fs")
const vm = require("node:vm")
const source = fs.readFileSync(__dirname + "/../static/video-layer.js", "utf8")

function environment({shaderFails = false} = {}) {
  const calls = [], resources = new Set(), poseFramebuffer = {name: "native pose framebuffer"}
  let currentFramebuffer = poseFramebuffer, sampler = null, pendingError = 0, failUpload = false, failDraw = false
  const gl = {}
  for (const name of ["createProgram", "createTexture", "createVertexArray", "createShader"])
    gl[name] = () => {const object = {name}; resources.add(object); return object}
  for (const name of ["FRAMEBUFFER", "FRAMEBUFFER_BINDING", "TEXTURE_2D", "VERTEX_SHADER", "FRAGMENT_SHADER", "COMPILE_STATUS",
    "LINK_STATUS", "TEXTURE_MIN_FILTER", "TEXTURE_MAG_FILTER", "LINEAR", "TEXTURE_WRAP_S", "TEXTURE_WRAP_T",
    "CLAMP_TO_EDGE", "RGBA", "UNSIGNED_BYTE", "BLEND", "DEPTH_TEST", "CULL_FACE", "SCISSOR_TEST", "TEXTURE0",
    "UNPACK_FLIP_Y_WEBGL", "TRIANGLES"]) gl[name] = name
  gl.NO_ERROR = 0
  for (const name of ["shaderSource", "compileShader", "attachShader", "linkProgram", "texParameteri", "useProgram",
    "bindVertexArray", "disable", "activeTexture", "pixelStorei", "uniform1i", "uniform1f", "uniform2f",
    "uniform3f", "uniform4fv", "uniformMatrix3fv"])
    gl[name] = (...args) => calls.push([name, ...args])
  gl.bindFramebuffer = (...args) => {calls.push(["bindFramebuffer", ...args]); currentFramebuffer = args[1]}
  gl.getParameter = option => {assert.equal(option, gl.FRAMEBUFFER_BINDING); return currentFramebuffer}
  gl.bindTexture = (...args) => {calls.push(["bindTexture", ...args]); sampler = args[1]}
  gl.texImage2D = (...args) => {calls.push(["texImage2D", ...args]); if (failUpload) pendingError = 0x501}
  gl.drawArrays = (...args) => {
    assert.equal(currentFramebuffer, poseFramebuffer, "camera draws into the caller's existing framebuffer")
    assert.equal(sampler?.name, "createTexture")
    calls.push(["drawArrays", ...args]); if (failDraw) pendingError = 0x502
  }
  for (const name of ["deleteTexture", "deleteVertexArray", "deleteProgram", "deleteShader"])
    gl[name] = object => resources.delete(object)
  gl.getShaderParameter = () => !shaderFails
  gl.getShaderInfoLog = () => "GPU shader error"
  gl.getProgramParameter = () => true
  gl.getUniformLocation = (program, name) => name
  gl.getError = () => {const error = pendingError; pendingError = 0; return error}
  gl.isContextLost = () => false
  const context = vm.createContext({Float32Array, Uint8Array})
  vm.runInContext(source.replace(/^export /gm, "") + "\nthis.factory = createCameraRenderer; this.projection = cameraProjection", context)
  return {factory: () => context.factory(gl), projection: context.projection, gl, calls, resources, poseFramebuffer,
    failUpload(value) {failUpload = value}, failDraw(value) {failDraw = value}, priorError(value) {pendingError = value}}
}

const env = environment(), renderer = env.factory(), video = {videoWidth: 2560, videoHeight: 720}
const nativeProjection = new Float32Array([2, 0, 0, 0, 0, 3, 0, 0, 0.25, -0.125, -1, -1, 0, 0, -0.2, 0])
const originalProjection = Array.from(nativeProjection)
const eyeTransform = new Float32Array([1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, -0.033, 0.002, 0.01, 1])
const left = {eye: "left", projectionMatrix: nativeProjection}, right = {...left, eye: "right"}
const cameraView = {transform: {matrix: eyeTransform}}
const config = {width: 1280, height: 720, mode: "stereo",
  left_intrinsics: {fx: 727, fy: 728, cx: 627, cy: 361}, right_intrinsics: {fx: 731, fy: 732, cx: 631, cy: 358}}
const display = {swap_eyes: false, saturation: 1, gamma: 1}
const p = env.projection(left, cameraView, config, false)
assert.deepEqual(Array.from(p.projection), [2, 3, 0.25, -0.125], "native off-axis terms stay intact")
assert.deepEqual(Array.from(p.rotation), [1, 0, 0, 0, 1, 0, 0, 0, 1], "native eye translation never adds a camera baseline")
assert.deepEqual(Array.from(p.intrinsics), [727, 728, 627, 361], "principal points come from the source calibration")
for (const [view, swap, expectedK, expectedCrop] of [
  [left, false, config.left_intrinsics, [0.5, 0]], [right, false, config.right_intrinsics, [0.5, 0.5]],
  [left, true, config.right_intrinsics, [0.5, 0.5]], [right, true, config.left_intrinsics, [0.5, 0]],
]) {
  const params = env.projection(view, cameraView, config, swap)
  assert.deepEqual(Array.from(params.intrinsics), [expectedK.fx, expectedK.fy, expectedK.cx, expectedK.cy])
  assert.deepEqual(Array.from(params.crop), expectedCrop, "eye swapping pairs the source half with its own K")
}
env.priorError(0x502)
renderer.prepare(video, 10)
renderer.drawView(left, cameraView, config, display)
renderer.drawView(right, cameraView, config, display)
renderer.prepare(video, 10)
assert.equal(env.calls.filter(call => call[0] === "texImage2D" && call.at(-1) === video).length, 1)
assert.equal(env.calls.filter(call => call[0] === "drawArrays").length, 2, "both eyes sample the same uploaded frame")
assert.deepEqual(Array.from(env.calls.filter(call => call[0] === "uniform4fv" && call[1] === "nativeProjection")[0][2]), Array.from(p.projection))
assert.deepEqual(Array.from(nativeProjection), originalProjection)
assert.equal(env.calls.filter(call => call[0] === "bindFramebuffer").at(-1)[2], env.poseFramebuffer)
const colorCalls = env.calls.filter(call => call[0] === "uniform1f" && ["saturation", "gamma"].includes(call[1]))
assert.deepEqual(colorCalls.map(call => call.slice(1)), [["saturation", 1], ["gamma", 1], ["saturation", 1], ["gamma", 1]])
const mono = {...config, mode: "mono", right_intrinsics: null}
assert.deepEqual(Array.from(env.projection(right, cameraView, mono, true).crop), [1, 0])
assert.deepEqual(Array.from(env.projection(right, cameraView, mono, true).intrinsics), Array.from(p.intrinsics))
const planeDisplay = {...display, projection: "plane", height_m: 8, distance_m: 7,
  aspect_ratio: 1.66667, offset_x_m: 0, offset_y_m: -1}
const uncalibrated = {...config, left_intrinsics: null, right_intrinsics: null}
const pp = env.projection(left, cameraView, uncalibrated, false, planeDisplay)
assert.deepEqual(Array.from(pp.plane), [8 * 1.66667, 8, 0, -1])
assert.deepEqual(Array.from(pp.eyePosition), Array.from(eyeTransform.slice(12,15)))
renderer.drawView(left, cameraView, uncalibrated, planeDisplay)
assert.deepEqual(env.calls.filter(c => c[0] === "uniform1i" && c[1] === "usePlane").at(-1), ["uniform1i", "usePlane", 1])
assert.deepEqual(env.calls.filter(c => c[0] === "uniform1f" && c[1] === "planeDistance").at(-1), ["uniform1f", "planeDistance", 7])
const rotated = {transform: {matrix: new Float32Array([0, 1, 0, 0, -1, 0, 0, 0, 0, 0, 1, 0, 9, 8, 7, 1])}}
assert.deepEqual(Array.from(env.projection(left, rotated, config, false).rotation), [0, 1, 0, -1, 0, 0, 0, 0, 1])
assert(source.includes("(ndc + nativeProjection.zw) / nativeProjection.xy"))
assert(source.includes("vec3(headRay.x, -headRay.y, -headRay.z)"))
assert(source.includes("intrinsics.xy * cameraRay.xy / cameraRay.z + intrinsics.zw"))
assert(source.includes("lessThan(pixel, vec2(-0.5))") && source.includes("greaterThan(pixel, imageSize - 0.5)"))
assert(source.includes("color = vec4(0.0, 0.0, 0.0, 1.0); return;"))
assert(source.includes("0.5 / imageSize, 1.0 - 0.5 / imageSize"))
assert(source.includes("uv.x * crop.x + crop.y, 1.0 - uv.y"))
env.failUpload(true)
assert.throws(() => renderer.prepare(video, 20), /upload: WebGL 0x501/)
assert.equal(env.calls.filter(call => call[0] === "bindFramebuffer").at(-1)[2], env.poseFramebuffer)
env.failUpload(false)
env.failDraw(true)
assert.throws(() => renderer.drawView(left, cameraView, config, display), /draw: WebGL 0x502/)
assert.equal(env.calls.filter(call => call[0] === "bindTexture").at(-1)[2], null)
assert.equal(env.calls.filter(call => call[0] === "bindVertexArray").at(-1)[1], null)
renderer.dispose()
assert.equal(env.resources.size, 0)
const failed = environment({shaderFails: true})
assert.throws(() => failed.factory(), /GPU shader error/)
assert.equal(failed.resources.size, 0)
console.log("Calibrated native projection, source K, paired eye crops, shared uploads, GL isolation and cleanup checks passed")

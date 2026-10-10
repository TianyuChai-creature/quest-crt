const assert = require("node:assert/strict")
const fs = require("node:fs")
const path = require("node:path")
const vm = require("node:vm")

async function checkPose() {
  const html = fs.readFileSync(path.join(__dirname, "../static/index.html"), "utf8")
  const script = html.match(/<script type="module">([\s\S]*?)<\/script>/)[1]
    .replace(/^\s*import .*$/m, "").replace(/\s+connectTransport\(\)\s*$/, "")
  let now = 0, loseCount = 0, disposeCount = 0, failGL = false
  const events = {}, sessions = [], packets = [], rendered = [], videoInstances = [], drawOrder = []
  const elements = new Map()
  function element() {
    return { textContent: "", disabled: false, checked: false, events: {},
      classList: { add() {}, remove() {} },
      addEventListener(name, handler) { this.events[name] = handler },
      contains: () => false, querySelector: () => element(),
      replaceWith(canvas) { elements.set("#xr-canvas", canvas) }, }
  }
  const gl = { async makeXRCompatible() { if (failGL) throw new Error("GL failure") },
    getExtension: () => ({ loseContext() { loseCount++ } }) }
  const posePeer = { close() { this.closed = true } }
  const poseChannel = { readyState: "open", bufferedAmount: 0,
    send(packet) { packets.push(packet) }, close() { this.closed = true } }
  const context = vm.createContext({ console, performance: { now: () => now, timeOrigin: 1000 },
    crypto: { randomUUID: () => "407b7a3f-4791-479c-aa81-48e813aca057" },
    setTimeout, clearTimeout, AbortController, AbortSignal,
    document: { activeElement: null,
      querySelector(selector) {
        if (!elements.has(selector)) elements.set(selector, element())
        return elements.get(selector)
      },
      createElement: () => ({ ...element(), id: "", getContext: () => gl }),
    },
    window: { addEventListener(name, handler) { events[name] = handler } },
    location: { reload() {} },
    VideoReturn: class {
      constructor() { videoInstances.push(this) }
      setEnabled() {} tick() {}
      beginFrame(frame) { this.frame = frame; drawOrder.push("video prepare") }
      renderView() { drawOrder.push("video") }
      setSession(session, layer, space, context) { this.session = session; this.gl = context }
      close() { this.closed = true }
    },
    XRWebGLLayer: class {
      constructor(session, actualGL, options) {
        assert.equal(actualGL, gl)
        assert.equal(options.ignoreDepthValues, true, "RGB supplies no scene depth for compositor reprojection")
        assert.equal(options.alpha, false)
        assert.equal(options.antialias, false)
      }
    },
    navigator: { xr: {
      isSessionSupported: async () => true,
      async requestSession(mode, options) {
        assert.equal(mode, "immersive-vr")
        assert(options.requiredFeatures.includes("body-tracking"))
        assert.equal(options.optionalFeatures, undefined)
        const session = { inputSources: [], events: {}, enabledFeatures: [],
          addEventListener(name, handler) { this.events[name] = handler },
          requestReferenceSpace: async type => ({ type }),
          updateRenderState(state) {
            assert(state.baseLayer instanceof context.XRWebGLLayer)
            assert.equal(state.layers, undefined)
            this.renderState = state
          },
          requestAnimationFrame(callback) { this.frame = callback },
          async end() { this.ended = true; this.events.end() }, }
        sessions.push(session)
        return session
      },
    } },
    posePeer, poseChannel,
    rendererFactory: () => ({ render(...args) { drawOrder.push("pose"); rendered.push(args.at(-1)) }, dispose() { disposeCount++ } }),
  })
  vm.runInContext(script + "\ncreateXRRenderer = rendererFactory; dataChannel = poseChannel; peerConnection = posePeer", context)
  await vm.runInContext("Promise.all([enterXR(), enterXR()])", context)
  assert.equal(sessions.length, 1, "duplicate taps request only one XR session")
  const session = sessions[0]
  assert(session.renderState.baseLayer instanceof context.XRWebGLLayer)
  assert.equal(session.renderState.baseLayer.fixedFoveation, 0, "keep peripheral camera detail at minimum native foveation")
  const body = new Map([
    ["spine-upper", [0, 1, 0]], ["left-scapula", [-0.15, 0.9, 0]], ["right-scapula", [0.15, 0.9, 0]],
    ["left-arm-upper", [-0.2, 0.9, 0]], ["right-arm-upper", [0.2, 0.9, 0]],
    ["left-arm-lower", [-0.2, 0.5, 0]], ["right-arm-lower", [0.2, 0.5, 0]],
  ])
  const frame = { body,
    getPose(point) { return { transform: { position: { x: point[0], y: point[1], z: point[2] },
      orientation: { x: 0, y: 0, z: 0, w: 1 } } } },
    getViewerPose() { return { transform: { orientation: { x: 0, y: 0, z: 0, w: 1 } } } },
  }
  now = 2999; session.frame(now, frame)
  assert.equal(videoInstances[0].gl, gl, "video shares the existing XR WebGL context")
  assert.equal(videoInstances[0].frame, frame)
  assert.deepEqual(drawOrder, ["video prepare", "pose"], "prepare the paired video frame before pose/HUD rendering")
  const renderSource = html.slice(html.indexOf("const cameraPose ="), html.indexOf("function cleanupXR"))
  assert(renderSource.indexOf("videoReturn.renderView(view, cameraView)") < renderSource.indexOf("drawVertices(lineVertices"))
  assert(renderSource.includes("candidate.eye === view.eye"))
  assert(renderSource.includes("trackingGuides.checked || !videoMode.checked"))
  assert.equal(packets.length, 0, "three-second preparation sends no pose")
  now = 3001; session.frame(now, frame)
  assert.equal(packets.length, 1)
  assert.equal(packets[0].byteLength, 804)
  assert.equal(new DataView(packets[0]).getUint8(5) & 0x80, 0)
  now = 3100; session.frame(now, { ...frame, body: undefined })
  assert.equal(packets.length, 1)
  assert.equal(elements.get("#xr-status").textContent, "Body tracking unavailable")
  assert(rendered.at(-1).hint.includes("Body tracking unavailable"))
  now = 3200; session.frame(now, frame)
  assert.equal(packets.length, 2)
  await session.end()
  assert.equal(videoInstances[0].session, null)
  assert.equal(disposeCount, 1)
  assert.equal(loseCount, 1)
  assert.equal(posePeer.closed, undefined, "ending XR keeps the pose connection ready for another session")

  failGL = true
  await vm.runInContext("enterXR()", context)
  assert.equal(sessions[1].ended, true, "failed initialization ends the acquired XR session")
  assert.equal(loseCount, 2)
  assert(elements.get("#xr-status").textContent.includes("GL failure"))
  assert(!elements.get("#xr-status").textContent.includes("body tracking required"))
  failGL = false
  await vm.runInContext("enterXR()", context)
  assert(sessions[2].renderState.baseLayer instanceof context.XRWebGLLayer)
  assert.equal(sessions[2].renderState.layers, undefined)
  await sessions[2].end()
  events.pagehide()
  assert.equal(posePeer.closed, true)
  assert.equal(poseChannel.closed, true)
  assert.equal(videoInstances[0].closed, true)
  assert(!html.includes("connectWebSocket"))
}

function checkViewer() {
  const source = fs.readFileSync(path.join(__dirname, "../static/viewer.html"), "utf8")
  const clear = source.slice(source.indexOf("function clearPose("), source.indexOf("function resize("))
  const draw = source.slice(source.indexOf("function draw()"), source.indexOf("context.clearRect", source.indexOf("function draw()"))) + "}"
  const context = { frame: { seq: 9 }, projectedPoints: [1], framesThisInterval: 10,
    lastFrameReceivedAt: 0, performance: { now: () => 251 } }
  for (const name of ["connectionText", "sessionText", "sequenceText", "axesText", "handednessText", "fpsText", "pointsText", "coordinatesSeq", "coordinatesBody"]) context[name] = {}
  context.hoverLabel = { style: { display: "block" }, textContent: "old point coordinates" }
  vm.runInNewContext(clear + draw + "\ndraw()", context)
  assert.equal(context.frame, null)
  assert.equal(context.connectionText.textContent, "stale")
  assert.equal(context.sequenceText.textContent, "—")
  assert(context.coordinatesBody.innerHTML.includes("Waiting for fresh"))
  assert.equal(context.hoverLabel.style.display, "none")
  assert.equal(context.hoverLabel.textContent, "")
  context.frame = { seq: 10 }
  vm.runInNewContext(clear + '\nclearPose("closed")', context)
  assert.equal(context.frame, null)
  assert.equal(context.connectionText.textContent, "closed")
}

function checkCameraAndGuidesDrawOrder() {
  const html = fs.readFileSync(path.join(__dirname, "../static/index.html"), "utf8")
  const start = html.indexOf("          render(\n")
  const stop = html.indexOf("          },\n        }", start)
  const events = [], left = { eye: "left" }, right = { eye: "right" }
  const views = [left, right].map(view => ({ ...view, projectionMatrix: [view.eye],
    transform: { inverse: { matrix: [] } } }))
  const referenceSpace = {}, viewerSpace = {}, headMatrix = ["shared head pose"], hudAnchors = []
  const guides = { checked: false }
  const gl = { FRAMEBUFFER: 1, COLOR_BUFFER_BIT: 2, DEPTH_BUFFER_BIT: 4,
    bindFramebuffer() {}, disable() {}, colorMask() {}, depthMask() {}, clearColor() {},
    clear() { events.push("clear") }, enable() {}, blendFunc() {}, viewport() {} }
  const scope = { gl, viewerSpace, trackingGuides: guides, videoMode: { checked: true },
    glLayer: { framebuffer: {}, getViewport: () => ({ x: 0, y: 0, width: 1, height: 1 }) },
    XR_COLORS: {}, addHand(vertices, points) { vertices.push("guide"); points.push("guide") }, addElbow() {}, addShoulder() {},
    videoReturn: { renderView(view, cameraView) {
      assert.equal(view.eye, cameraView.eye)
      events.push("video " + view.eye)
    } },
    multiplyMatrices: projection => projection,
    drawVertices(vertices, mode, projection) {
      assert.equal(vertices.length, guides.checked ? 2 : 0)
      events.push("pose " + projection[0])
    },
    lastHudKey: "", paintHud() {},
    drawHudQuad(_projection, matrix) { hudAnchors.push(matrix) },
  }
  const render = vm.runInNewContext("({" + html.slice(start, stop) + "}}).render", scope)
  const frame = { getViewerPose(space) { return {
    views: space === referenceSpace ? views : [right, left], transform: { matrix: headMatrix },
  } } }
  render(frame, referenceSpace, [], [], null, null, null, null, { show: false })
  assert.deepEqual(events, ["clear", "video left", "pose left", "pose left", "video right", "pose right", "pose right"])
  guides.checked = true
  render(frame, referenceSpace, [], [], null, null, null, null, { show: false })
  render(frame, referenceSpace, [], [], null, null, null, null, { show: true, countdown: 1, hint: "Prep" })
  assert.deepEqual(hudAnchors, [headMatrix, headMatrix], "both eyes project the same head-anchored HUD geometry")
}

checkPose().then(() => {
  checkCameraAndGuidesDrawOrder()
  checkViewer()
  console.log("Pose preparation, body tracking, XR cleanup and viewer freshness checks passed")
}).catch(error => { console.error(error); process.exitCode = 1 })

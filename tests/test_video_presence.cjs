// Run: node tests/test_video_presence.cjs
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')

const source = fs.readFileSync(path.join(__dirname, '../cloudxr/qcrt-exporter.js'), 'utf8')
const posted = []
const pageEvents = {}
const timers = new Map()
let nextTimer = 1
let nextSession = 1
let failNextPost = false
let boot = 'boot-one'
let accepted = true
let deferredStart = null

class PendingPeer {
  createDataChannel() { return { close() {} } }
  addEventListener() {}
  createOffer() { return new Promise(() => {}) }
  close() {}
}

const navigator = { xr: { requestSession: async () => {
  const events = {}
  return {
    events,
    addEventListener(name, handler) { events[name] = handler },
    requestReferenceSpace: () => new Promise(() => {}),
  }
} } }
const context = {
  URL, URLSearchParams, AbortSignal, navigator,
  location: { search: '?qcrtUi=nvidia', protocol: 'https:', hostname: '192.168.8.122' },
  document: { readyState: 'loading', addEventListener() {} },
  window: { addEventListener(name, handler) { pageEvents[name] = handler } },
  crypto: { randomUUID: () => `00000000-0000-4000-8000-${String(nextSession++).padStart(12, '0')}` },
  performance: { now: () => 0 },
  RTCPeerConnection: PendingPeer,
  fetch(url, options) {
    if (failNextPost) {
      failNextPost = false
      throw new Error('network unavailable')
    }
    posted.push({ url, body: JSON.parse(options.body), keepalive: options.keepalive })
    return Promise.resolve({ ok: true, json: async () => {
      if (url.endsWith('/start') && deferredStart) await deferredStart
      return url.endsWith('/start') ? { lease_id: 'lease-'+nextSession, boot_id: boot } : { accepted, boot_id: boot }
    } })
  },
  setInterval(callback, delay) {
    assert.equal(delay, 1000)
    const id = nextTimer++
    timers.set(id, callback)
    return id
  },
  clearInterval(id) { timers.delete(id) },
  clearTimeout() {},
}
vm.runInNewContext(source, context)

async function check() {
  const flush = () => new Promise(setImmediate)
  const session = await navigator.xr.requestSession('immersive-vr', {})
  await flush()
  assert.equal(posted.length, 1)
  assert.ok(posted[0].body.session_id)
  assert.ok(posted[0].url.endsWith('/api/video-presence/start'))
  // No pose transport, body frame, or XR frame callback exists in this test.
  const heartbeat = timers.values().next().value
  await heartbeat()
  assert.equal(posted[1].body.seq, 1)
  assert.equal(posted[1].body.active, true)
  session.events.end()
  assert.equal(timers.size, 0)
  assert.equal(posted[2].body.active, false)
  assert.equal(posted[2].body.seq, 2)
  assert.equal(posted[2].keepalive, true)
  pageEvents.pagehide()
  assert.equal(posted.length, 3)

  failNextPost = true
  const second = await navigator.xr.requestSession('immersive-vr', {})
  await flush()
  assert.equal(posted.length, 3)  // Failed first heartbeat did not block XR.
  await timers.values().next().value()
  assert.ok(posted.at(-1).url.endsWith('/start'))
  pageEvents.pagehide()
  assert.equal(posted.at(-1).body.active, false)
  pageEvents.pageshow()
  await flush()
  assert.ok(posted.at(-1).url.endsWith('/start'))
  accepted = false
  boot = 'boot-two'
  await timers.values().next().value()
  await timers.values().next().value()
  assert.ok(posted.at(-1).url.endsWith('/start'))
  await timers.values().next().value()
  assert.equal(timers.size, 0) // Superseded on the same server: never reclaim.
  const count = posted.length
  pageEvents.pageshow()
  assert.equal(posted.length, count)
  second.events.end()
  let releaseStart
  deferredStart = new Promise(resolve => { releaseStart = resolve })
  const third = await navigator.xr.requestSession('immersive-vr', {})
  third.events.end()
  releaseStart()
  await flush()
  assert.equal(posted.at(-1).body.active, false)
  assert.equal(timers.size, 0)
  console.log('Video presence checks passed')
}
check().catch(error => { console.error(error); process.exitCode = 1 })

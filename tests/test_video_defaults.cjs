// Run: node tests/test_video_defaults.cjs
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')

const source = fs.readFileSync(path.join(__dirname, '../cloudxr/qcrt-exporter.js'), 'utf8')
const defaults = source.slice(source.indexOf('  const params ='), source.indexOf('  const HAND_JOINTS ='))
function configure(search) {
  let url = new URL('https://localhost/client/' + search)
  vm.runInNewContext('(() => {' + defaults + '})()', {
    URL, URLSearchParams, navigator: { xr: {} },
    location: { search, href: url.href },
    history: { replaceState(_state, _title, next) { url = next } },
  })
  return url.searchParams
}
const baseline = configure('')
assert.equal(baseline.get('perEyeWidth'), '1536')
assert.equal(baseline.get('perEyeHeight'), '1344')
// CloudXR rejects invalid dimensions before enabling CONNECT.
assert.equal(Number(baseline.get('perEyeWidth')) % 16, 0)
assert.equal(Number(baseline.get('perEyeHeight')) % 64, 0)
const repaired = configure('?perEyeWidth=1280&perEyeHeight=1120&maxStreamingBitrateMbps=50')
assert.equal(repaired.get('perEyeWidth'), baseline.get('perEyeWidth'))
assert.equal(repaired.get('perEyeHeight'), baseline.get('perEyeHeight'))
assert.equal(repaired.get('maxStreamingBitrateMbps'), '50')
assert.equal(baseline.get('maxStreamingBitrateMbps'), '25')
const override = configure('?perEyeWidth=2048&perEyeHeight=1792&maxStreamingBitrateMbps=50')
assert.equal(override.get('perEyeWidth'), '2048')
assert.equal(override.get('perEyeHeight'), '1792')
assert.equal(override.get('maxStreamingBitrateMbps'), '50')
assert.equal(configure('?qcrtUi=nvidia').has('perEyeWidth'), false)
assert.equal(configure('?qcrt=off').has('perEyeWidth'), false)
console.log('Video defaults checks passed')

async function checkStats() {
  const posted = []
  let now = 0
  let result = Promise.resolve(new Map([['video', {
    type: 'inbound-rtp', kind: 'video', framesPerSecond: 60, framesDropped: 3,
    jitter: NaN, freezeCount: 2, totalFreezesDuration: 0.4,
  }]]))
  class Peer { getStats() { assert.equal(this instanceof Peer, true); return result } }
  const statsSource = source.slice(source.indexOf('  let lastVideoStatsAt'), source.indexOf('  const state ='))
  vm.runInNewContext(statsSource, {
    window: { RTCPeerConnection: Peer }, performance: { now: () => now },
    qcrtHttpOrigin: 'https://localhost:8000', AbortSignal,
    fetch(url, options) { posted.push({ url, body: JSON.parse(options.body) }); return Promise.resolve({ ok: true }) },
  })
  const peer = new Peer()
  assert.equal(peer.getStats(), result)
  await result
  assert.equal(posted.length, 1)
  assert.equal(posted[0].body.framesDropped, 3)
  assert.equal(posted[0].body.totalFreezesDuration, 0.4)
  assert.equal('jitter' in posted[0].body, false)
  await peer.getStats()
  assert.equal(posted.length, 1)
  now = 5000
  await peer.getStats()
  assert.equal(posted.length, 2)
  result = Promise.reject(new Error('stats unavailable'))
  await assert.rejects(peer.getStats(), /stats unavailable/)
  console.log('Video stats checks passed')
}
checkStats().catch(error => { console.error(error); process.exitCode = 1 })

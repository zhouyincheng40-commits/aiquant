import { resolveProxy, fetchBuffer, portOpen } from '../lib/net.js'

const proxy = await resolveProxy()
console.log('proxy =', JSON.stringify(proxy))
if (proxy !== null) console.log('portOpen =', await portOpen(proxy.host, proxy.port))
for (const url of ['https://api.binance.com/api/v3/ping', 'https://www.okx.com/api/v5/public/time', 'https://qt.gtimg.cn/q=hkHSI']) {
  try {
    const r = await fetchBuffer(url, { timeoutMs: 15000 })
    console.log('OK  ', url, 'via', r.via, r.buffer.length, 'bytes:', r.buffer.slice(0, 80).toString('latin1').replace(/\s+/g, ' '))
  } catch (error) {
    console.log('FAIL', url, error.message)
  }
}

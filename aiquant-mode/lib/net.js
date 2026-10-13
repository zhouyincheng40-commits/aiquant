/**
 * 网络层：直连优先 + 系统代理兜底（零外部依赖）
 *
 * 为什么需要它：
 *   Node 的全局 fetch **不读 Windows 系统代理**（WinINET/注册表），而本机（以及国内多数
 *   机器）到 Binance / OKX / GitHub 这类域名的直连要么被 DNS 污染、要么被墙。
 *   PowerShell 能通是因为它走系统代理 —— 插件不能假设这一点。
 *
 * 策略：
 *   1. 先直连（A股/腾讯/新浪这些国内源直连最快）；
 *   2. 直连失败时，用代理做 CONNECT 隧道重试一次；
 *   3. 代理地址解析顺序：AIQUANT_PROXY > HTTPS_PROXY/HTTP_PROXY > Windows 注册表
 *      `HKCU\...\Internet Settings\ProxyServer`（只读查询，Windows 才做）。
 *
 * 只用 node: 内置模块（net / tls / zlib / child_process）+ 全局 fetch。
 */
import { connect as tcpConnect } from 'node:net'
import { connect as tlsConnect } from 'node:tls'
import { gunzipSync, brotliDecompressSync, inflateSync } from 'node:zlib'
import { execFile } from 'node:child_process'
import { URL } from 'node:url'

const DEFAULT_TIMEOUT_MS = 12000
const USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) dsh-aiquant-mode'

let cachedProxy
let cachedProxyAt = 0
const PROXY_TTL_MS = 60000

/** 解析代理地址：显式配置 > 环境变量 > Windows 注册表。 */
export async function resolveProxy() {
  const now = Date.now()
  if (cachedProxy !== undefined && now - cachedProxyAt < PROXY_TTL_MS) return cachedProxy
  cachedProxyAt = now
  const explicit = process.env.AIQUANT_PROXY || process.env.HTTPS_PROXY || process.env.https_proxy
    || process.env.HTTP_PROXY || process.env.http_proxy
  if (typeof explicit === 'string' && explicit.trim() !== '') {
    cachedProxy = parseProxy(explicit.trim())
    return cachedProxy
  }
  if (process.platform === 'win32') {
    const fromRegistry = await readWindowsProxy()
    if (fromRegistry !== null) {
      cachedProxy = fromRegistry
      return cachedProxy
    }
  }
  cachedProxy = null
  return cachedProxy
}

/** `host:port` / `http://host:port` → `{ host, port }`。 */
export function parseProxy(text) {
  const cleaned = String(text).replace(/^[a-z]+:\/\//i, '').replace(/\/+$/, '')
  const [host, port] = cleaned.split(':')
  if (host === undefined || host === '') return null
  const p = Number(port)
  return { host, port: Number.isFinite(p) && p > 0 ? p : 7890 }
}

/** 读 Windows 系统代理（只读 reg query；失败返回 null）。 */
function readWindowsProxy() {
  return new Promise((resolve) => {
    try {
      execFile('reg', ['query', 'HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Internet Settings', '/v', 'ProxyServer'],
        { timeout: 4000, windowsHide: true }, (error, stdout) => {
          if (error) return resolve(null)
          const match = /ProxyServer\s+REG_SZ\s+(\S+)/.exec(String(stdout))
          if (match === null) return resolve(null)
          /* 可能是 `http=127.0.0.1:7890;https=127.0.0.1:7890` 这种按协议分列的形式 */
          const value = match[1].includes('=') ? (/(?:https|http)=([^;]+)/.exec(match[1]) || [])[1] || '' : match[1]
          resolve(parseProxy(value))
        })
    } catch {
      resolve(null)
    }
  })
}

/** TCP 端口是否可连（判断代理是否还活着）。 */
export function portOpen(host, port, timeoutMs = 1500) {
  return new Promise((resolve) => {
    const socket = tcpConnect({ host, port })
    const done = (value) => {
      try { socket.destroy() } catch { /* 忽略 */ }
      resolve(value)
    }
    socket.setTimeout(timeoutMs)
    socket.once('connect', () => done(true))
    socket.once('timeout', () => done(false))
    socket.once('error', () => done(false))
  })
}

/** 直连抓取（返回 Buffer；非 2xx 抛错）。 */
async function directFetch(url, options = {}) {
  const response = await fetch(url, {
    headers: Object.assign({ 'user-agent': USER_AGENT, accept: '*/*', 'accept-encoding': 'gzip, br, deflate' }, options.headers || {}),
    redirect: 'follow',
    signal: AbortSignal.timeout(options.timeoutMs || DEFAULT_TIMEOUT_MS),
  })
  if (!response.ok) throw new Error('HTTP ' + response.status)
  return Buffer.from(await response.arrayBuffer())
}

/** 把响应正文按 Content-Encoding 解压。 */
function decompress(buffer, encoding) {
  const enc = String(encoding || '').toLowerCase()
  try {
    if (enc.includes('gzip')) return gunzipSync(buffer)
    if (enc.includes('br')) return brotliDecompressSync(buffer)
    if (enc.includes('deflate')) return inflateSync(buffer)
  } catch {
    return buffer
  }
  return buffer
}

/** 解析 chunked 正文。 */
function dechunk(buffer) {
  const parts = []
  let offset = 0
  while (offset < buffer.length) {
    const end = buffer.indexOf('\r\n', offset)
    if (end < 0) break
    const size = parseInt(buffer.slice(offset, end).toString('latin1').trim(), 16)
    if (!Number.isFinite(size) || size <= 0) break
    parts.push(buffer.slice(end + 2, end + 2 + size))
    offset = end + 2 + size + 2
  }
  return Buffer.concat(parts)
}

/**
 * 经 HTTP 代理做 CONNECT 隧道抓取 HTTPS/HTTP（返回 Buffer）。
 * 只用 net/tls 手写，避免引入 undici 依赖。
 */
function proxyFetch(url, proxy, options = {}) {
  return new Promise((resolve, reject) => {
    const target = new URL(url)
    const https = target.protocol === 'https:'
    const port = target.port !== '' ? target.port : (https ? '443' : '80')
    const timeoutMs = options.timeoutMs || DEFAULT_TIMEOUT_MS
    const socket = tcpConnect({ host: proxy.host, port: proxy.port })
    let settled = false
    let timer = setTimeout(() => finish(new Error('代理超时 ' + proxy.host + ':' + proxy.port)), timeoutMs)
    const finish = (error, value) => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      try { socket.destroy() } catch { /* 忽略 */ }
      if (error) reject(error); else resolve(value)
    }

    socket.once('error', (error) => finish(new Error('代理连接失败：' + error.message)))
    socket.once('connect', () => {
      socket.write('CONNECT ' + target.hostname + ':' + port + ' HTTP/1.1\r\n'
        + 'Host: ' + target.hostname + ':' + port + '\r\n'
        + 'Proxy-Connection: keep-alive\r\n\r\n')
    })

    let handshake = Buffer.alloc(0)
    const onHandshakeData = (chunk) => {
      handshake = Buffer.concat([handshake, chunk])
      const idx = handshake.indexOf('\r\n\r\n')
      if (idx < 0) return
      const statusLine = handshake.slice(0, idx).toString('latin1').split('\r\n')[0]
      socket.removeListener('data', onHandshakeData)
      if (!/\s200\s/.test(statusLine)) {
        finish(new Error('代理 CONNECT 被拒绝：' + statusLine))
        return
      }
      clearTimeout(timer)
      timer = setTimeout(() => { try { channel.destroy() } catch { /* 忽略 */ } ; finish(new Error('读取超时')) }, timeoutMs)

      const send = () => {
        const headerLines = [
          'GET ' + (target.pathname || '/') + (target.search || '') + ' HTTP/1.1',
          'Host: ' + target.host,
          'User-Agent: ' + USER_AGENT,
          'Accept: */*',
          'Accept-Encoding: gzip, br, deflate',
          'Connection: close',
        ]
        for (const [key, value] of Object.entries(options.headers || {})) headerLines.push(key + ': ' + value)
        channel.write(headerLines.join('\r\n') + '\r\n\r\n')
      }

      const channel = https
        ? tlsConnect({ socket, servername: target.hostname }, send)
        : socket
      let raw = Buffer.alloc(0)
      let done = false
      const tryFinish = () => {
        if (done) return false
        const split = raw.indexOf('\r\n\r\n')
        if (split < 0) return false
        const head = raw.slice(0, split).toString('latin1')
        const headerMap = {}
        for (const line of head.split('\r\n').slice(1)) {
          const i = line.indexOf(':')
          if (i > 0) headerMap[line.slice(0, i).trim().toLowerCase()] = line.slice(i + 1).trim()
        }
        const body = raw.slice(split + 4)
        const chunked = String(headerMap['transfer-encoding'] || '').includes('chunked')
        const length = Number(headerMap['content-length'])
        /* 判断正文是否收全：chunked 看结束块；有 content-length 看长度；否则只能等连接关闭 */
        if (chunked) {
          if (body.indexOf(Buffer.from('0\r\n\r\n')) < 0 && !body.toString('latin1').endsWith('0\r\n\r\n')) return false
        } else if (Number.isFinite(length) && length >= 0) {
          if (body.length < length) return false
        } else {
          return false
        }
        done = true
        const status = Number((/^HTTP\/1\.[01]\s+(\d+)/.exec(head) || [])[1] || 0)
        if (status < 200 || status >= 300) {
          finish(new Error('HTTP ' + status + '（经代理）'))
          return true
        }
        let payload = chunked ? dechunk(body) : (Number.isFinite(length) && length >= 0 ? body.slice(0, length) : body)
        payload = decompress(payload, headerMap['content-encoding'])
        finish(null, payload)
        return true
      }
      const onData = (data) => {
        raw = Buffer.concat([raw, data])
        tryFinish()
      }
      const onClose = () => {
        if (done) return
        /* 连接关闭（对端不发 close_notify 的常见情况）：把手上拿到的尽力解析出来 */
        const split = raw.indexOf('\r\n\r\n')
        if (split < 0) {
          finish(new Error('响应格式异常（收到 ' + raw.length + ' 字节，无响应头）'))
          return
        }
        const head = raw.slice(0, split).toString('latin1')
        const status = Number((/^HTTP\/1\.[01]\s+(\d+)/.exec(head) || [])[1] || 0)
        const headerMap = {}
        for (const line of head.split('\r\n').slice(1)) {
          const i = line.indexOf(':')
          if (i > 0) headerMap[line.slice(0, i).trim().toLowerCase()] = line.slice(i + 1).trim()
        }
        if (status < 200 || status >= 300) {
          finish(new Error('HTTP ' + status + '（经代理）'))
          return
        }
        let body = raw.slice(split + 4)
        if (String(headerMap['transfer-encoding'] || '').includes('chunked')) body = dechunk(body)
        done = true
        finish(null, decompress(body, headerMap['content-encoding']))
      }
      channel.on('data', onData)
      channel.on('error', (error) => finish(new Error('隧道读取失败：' + error.message)))
      channel.on('end', onClose)
      channel.on('close', onClose)
    }
    socket.on('data', onHandshakeData)
  })
}

/**
 * 统一抓取入口：直连 → 代理兜底。
 * @returns `{ buffer, via, proxy }`
 */
export async function fetchBuffer(url, options = {}) {
  const attempts = []
  try {
    const buffer = await directFetch(url, options)
    return { buffer, via: 'direct', proxy: null, attempts }
  } catch (error) {
    attempts.push('direct: ' + (error && error.message ? error.message : String(error)))
  }
  const proxy = options.proxy !== undefined ? options.proxy : await resolveProxy()
  if (proxy !== null && proxy !== undefined && options.noProxy !== true) {
    if (await portOpen(proxy.host, proxy.port)) {
      try {
        const buffer = await proxyFetch(url, proxy, options)
        return { buffer, via: 'proxy', proxy, attempts }
      } catch (error) {
        attempts.push('proxy(' + proxy.host + ':' + proxy.port + '): ' + (error && error.message ? error.message : String(error)))
      }
    } else {
      attempts.push('proxy(' + proxy.host + ':' + proxy.port + '): 端口不可连')
    }
  }
  const error = new Error('取数失败 ' + url + '（' + attempts.join(' | ') + '）')
  error.attempts = attempts
  throw error
}

/** 嗅探 charset（HTML/XML 里的 meta，或腾讯/新浪默认 GBK）。 */
function sniffCharset(buffer, fallback) {
  const head = buffer.slice(0, 1024).toString('latin1').toLowerCase()
  const meta = /charset=["']?([a-z0-9-]+)/.exec(head)
  if (meta !== null) return meta[1]
  try {
    return fallback || 'utf-8'
  } catch {
    return 'utf-8'
  }
}

/** 按 charset 解码（GBK 不可用时退回 latin1，数字仍正确）。 */
export function decodeBuffer(buffer, charset) {
  try {
    return new TextDecoder(charset || 'utf-8').decode(buffer)
  } catch {
    try { return new TextDecoder('gbk').decode(buffer) } catch { return buffer.toString('latin1') }
  }
}

/**
 * 抓文本（自动解码）。
 * @param url - 目标 URL。
 * @param options - `{ charset, headers, timeoutMs, proxy, noProxy }`
 */
export async function fetchText(url, options = {}) {
  const result = await fetchBuffer(url, options)
  const charset = options.charset || sniffCharset(result.buffer, options.defaultCharset || 'utf-8')
  return { text: decodeBuffer(result.buffer, charset), via: result.via, proxy: result.proxy, attempts: result.attempts }
}

/** 抓 JSON。 */
export async function fetchJson(url, options = {}) {
  const result = await fetchBuffer(url, options)
  const text = decodeBuffer(result.buffer, options.charset || 'utf-8')
  try {
    return { data: JSON.parse(text), via: result.via, proxy: result.proxy, text }
  } catch (error) {
    throw new Error('JSON 解析失败 ' + url + '：' + (error && error.message ? error.message : String(error)) + '；前 120 字符：' + text.slice(0, 120))
  }
}

import net from 'node:net'
import tls from 'node:tls'

const host = 'api.binance.com'
const proxy = { host: '127.0.0.1', port: 7890 }

const socket = net.connect({ host: proxy.host, port: proxy.port })
socket.on('error', (e) => console.log('SOCKET ERROR', e.message))
socket.on('close', () => console.log('SOCKET CLOSE'))
socket.on('connect', () => {
  console.log('TCP connected, sending CONNECT')
  socket.write(`CONNECT ${host}:443 HTTP/1.1\r\nHost: ${host}:443\r\nProxy-Connection: keep-alive\r\n\r\n`)
})
let hs = Buffer.alloc(0)
const onHs = (chunk) => {
  hs = Buffer.concat([hs, chunk])
  console.log('handshake bytes:', JSON.stringify(hs.toString('latin1')))
  if (hs.indexOf('\r\n\r\n') < 0) return
  socket.removeListener('data', onHs)
  const channel = tls.connect({ socket, servername: host }, () => {
    console.log('TLS secureConnect, authorized =', channel.authorized, channel.authorizationError || '')
    channel.write(`GET /api/v3/ping HTTP/1.1\r\nHost: ${host}\r\nUser-Agent: curl/8\r\nAccept: */*\r\nConnection: close\r\n\r\n`)
  })
  channel.on('data', (d) => console.log('TLS DATA', JSON.stringify(d.toString('latin1').slice(0, 200))))
  channel.on('error', (e) => console.log('TLS ERROR', e.message))
  channel.on('end', () => console.log('TLS END'))
  channel.on('close', () => { console.log('TLS CLOSE'); socket.destroy() })
}
socket.on('data', onHs)
setTimeout(() => { console.log('TIMEOUT'); process.exit(0) }, 20000)

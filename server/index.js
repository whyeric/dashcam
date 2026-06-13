import http from 'node:http'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import express from 'express'
import { WebSocketServer } from 'ws'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const repoRoot = path.resolve(__dirname, '..')

const app = express()
app.use(express.static(repoRoot))

const httpServer = http.createServer(app)

// Use Railway's PORT env var, fallback to 8000 locally
const PORT = process.env.PORT || 8000

// WebSocket on the same server/port
const wss = new WebSocketServer({ server: httpServer })

const clients = new Map()

function getPlayers() {
  return [...clients.values()]
    .filter(c => c.role === 'controller')
    .map(c => c.player)
}

function sendToScreens(msg) {
  const str = JSON.stringify(msg)
  for (const [ws, meta] of clients) {
    if (meta.role === 'screen' && ws.readyState === 1) {
      ws.send(str)
    }
  }
}

wss.on('connection', (ws) => {
  clients.set(ws, { role: null, player: null })

  ws.on('message', (raw) => {
    let msg
    try { msg = JSON.parse(raw.toString()) } catch { return }

    if (msg.type === 'register') {
      if (msg.role === 'screen') {
        clients.set(ws, { role: 'screen', player: null })
        ws.send(JSON.stringify({ type: 'state', players: getPlayers() }))
        console.log('[server] screen registered')

      } else if (msg.role === 'controller') {
        const wantedPlayer = msg.player
        const taken = getPlayers()

        if (taken.includes(wantedPlayer)) {
          ws.send(JSON.stringify({ type: 'error', reason: 'slot_taken', player: wantedPlayer }))
          return
        }
        if (taken.length >= 2) {
          ws.send(JSON.stringify({ type: 'error', reason: 'game_full' }))
          return
        }

        clients.set(ws, { role: 'controller', player: wantedPlayer })
        ws.send(JSON.stringify({ type: 'assign', player: wantedPlayer }))
        sendToScreens({ type: 'player_connected', player: wantedPlayer })
        console.log(`[server] ${wantedPlayer} joined`)
      }
      return
    }

    if (msg.action === 'press' || msg.action === 'release') {
      const meta = clients.get(ws)
      if (!meta || meta.role !== 'controller') return
      sendToScreens({ type: 'input', action: msg.action, key: msg.key, player: meta.player })
    }
  })

  ws.on('close', () => {
    const meta = clients.get(ws)
    if (meta?.role === 'controller' && meta.player) {
      sendToScreens({ type: 'player_disconnected', player: meta.player })
      console.log(`[server] ${meta.player} disconnected`)
    }
    clients.delete(ws)
  })
})

httpServer.listen(PORT, () => {
  console.log(`[server] running on port ${PORT}`)
})
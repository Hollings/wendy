/**
 * Wendy Games - Helper Library
 *
 * This library provides utilities for building multiplayer games:
 * - Persistence (save/load game state)
 * - WebSocket management (broadcast, rooms)
 * - Player tracking
 * - Automatic static file serving from ./public
 */

import { serveDir } from "https://deno.land/std/http/file_server.ts";

// ============== Persistence ==============

const STATE_FILE = Deno.env.get("STATE_FILE") || "/data/state.json";

export interface GameState {
  [key: string]: unknown;
}

let _state: GameState | null = null;

/** Load persisted game state */
export async function loadState<T extends GameState>(): Promise<T> {
  if (_state === null) {
    try {
      const text = await Deno.readTextFile(STATE_FILE);
      _state = JSON.parse(text);
    } catch {
      _state = {};
    }
  }
  return _state as T;
}

/** Save game state to disk */
export async function saveState(state: GameState): Promise<void> {
  _state = state;
  await Deno.writeTextFile(STATE_FILE, JSON.stringify(state, null, 2));
}

/** Update specific keys in state */
export async function updateState(updates: Partial<GameState>): Promise<GameState> {
  const state = await loadState();
  Object.assign(state, updates);
  await saveState(state);
  return state;
}

// ============== Leaderboard Helpers ==============

export interface LeaderboardEntry {
  name: string;
  score: number;
  timestamp?: number;
}

/** Get leaderboard from state */
export async function getLeaderboard(limit = 10): Promise<LeaderboardEntry[]> {
  const state = await loadState<{ leaderboard?: LeaderboardEntry[] }>();
  return (state.leaderboard || [])
    .sort((a, b) => b.score - a.score)
    .slice(0, limit);
}

/** Add score to leaderboard */
export async function addScore(name: string, score: number): Promise<LeaderboardEntry[]> {
  const state = await loadState<{ leaderboard?: LeaderboardEntry[] }>();
  if (!state.leaderboard) state.leaderboard = [];

  state.leaderboard.push({
    name,
    score,
    timestamp: Date.now(),
  });

  // Keep top 100
  state.leaderboard = state.leaderboard
    .sort((a, b) => b.score - a.score)
    .slice(0, 100);

  await saveState(state);
  return state.leaderboard.slice(0, 10);
}

// ============== WebSocket Management ==============

export interface Player {
  id: string;
  socket: WebSocket;
  data: Record<string, unknown>;
}

export class GameServer {
  private players = new Map<string, Player>();
  private rooms = new Map<string, Set<string>>();

  /** Get all connected players */
  getPlayers(): Player[] {
    return Array.from(this.players.values());
  }

  /** Get player by ID */
  getPlayer(id: string): Player | undefined {
    return this.players.get(id);
  }

  /** Get player count */
  get playerCount(): number {
    return this.players.size;
  }

  /** Handle new WebSocket connection */
  handleConnection(socket: WebSocket): Player {
    const id = crypto.randomUUID();
    const player: Player = { id, socket, data: {} };

    this.players.set(id, player);

    socket.onclose = () => {
      this.players.delete(id);
      // Remove from all rooms
      for (const room of this.rooms.values()) {
        room.delete(id);
      }
    };

    return player;
  }

  /** Send message to specific player */
  send(playerId: string, message: unknown): void {
    const player = this.players.get(playerId);
    if (player && player.socket.readyState === WebSocket.OPEN) {
      player.socket.send(JSON.stringify(message));
    }
  }

  /** Broadcast message to all players */
  broadcast(message: unknown, excludeId?: string): void {
    const data = JSON.stringify(message);
    for (const [id, player] of this.players) {
      if (id !== excludeId && player.socket.readyState === WebSocket.OPEN) {
        player.socket.send(data);
      }
    }
  }

  /** Broadcast to specific room */
  broadcastToRoom(room: string, message: unknown, excludeId?: string): void {
    const roomPlayers = this.rooms.get(room);
    if (!roomPlayers) return;

    const data = JSON.stringify(message);
    for (const id of roomPlayers) {
      if (id !== excludeId) {
        const player = this.players.get(id);
        if (player && player.socket.readyState === WebSocket.OPEN) {
          player.socket.send(data);
        }
      }
    }
  }

  /** Join a room */
  joinRoom(playerId: string, room: string): void {
    if (!this.rooms.has(room)) {
      this.rooms.set(room, new Set());
    }
    this.rooms.get(room)!.add(playerId);
  }

  /** Leave a room */
  leaveRoom(playerId: string, room: string): void {
    this.rooms.get(room)?.delete(playerId);
  }

  /** Get players in a room */
  getRoomPlayers(room: string): Player[] {
    const roomPlayerIds = this.rooms.get(room);
    if (!roomPlayerIds) return [];

    return Array.from(roomPlayerIds)
      .map(id => this.players.get(id))
      .filter((p): p is Player => p !== undefined);
  }
}

// ============== Server Helpers ==============

export interface ServerOptions {
  port?: number;
  onConnect?: (ws: WebSocket, playerId: string) => void;
  onMessage?: (ws: WebSocket, playerId: string, message: unknown) => void;
  onDisconnect?: (playerId: string) => void;
  onHttpRequest?: (req: Request) => Response | Promise<Response>;
}

/** Create a game server with WebSocket support */
export function createGameServer(options: ServerOptions = {}): GameServer {
  const port = options.port || parseInt(Deno.env.get("PORT") || "8000");
  const game = new GameServer();

  Deno.serve({ port }, (req) => {
    const url = new URL(req.url);

    // WebSocket upgrade
    if (req.headers.get("upgrade") === "websocket") {
      const { socket, response } = Deno.upgradeWebSocket(req);

      let player: Player;

      socket.onopen = () => {
        player = game.handleConnection(socket);
        options.onConnect?.(socket, player.id);
      };

      socket.onmessage = (e) => {
        try {
          const message = JSON.parse(e.data);
          options.onMessage?.(socket, player.id, message);
        } catch {
          // Invalid JSON, ignore
        }
      };

      socket.onclose = () => {
        options.onDisconnect?.(player.id);
      };

      return response;
    }

    // HTTP requests
    if (options.onHttpRequest) {
      return options.onHttpRequest(req);
    }

    // Default responses
    if (url.pathname === "/health") {
      return Response.json({
        status: "ok",
        players: game.playerCount
      });
    }

    // Serve static files from ./public if it exists
    return serveDir(req, { fsRoot: "./public" });
  });

  console.log(`Game server running on port ${port}`);
  return game;
}

// ============== Utilities ==============

/** Generate a short random code (for room codes, etc.) */
export function randomCode(length = 4): string {
  const chars = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789";
  let code = "";
  for (let i = 0; i < length; i++) {
    code += chars[Math.floor(Math.random() * chars.length)];
  }
  return code;
}

/** Clamp a number between min and max */
export function clamp(value: number, min: number, max: number): number {
  return Math.min(Math.max(value, min), max);
}

/** Linear interpolation */
export function lerp(a: number, b: number, t: number): number {
  return a + (b - a) * t;
}

/** Distance between two points */
export function distance(x1: number, y1: number, x2: number, y2: number): number {
  return Math.sqrt((x2 - x1) ** 2 + (y2 - y1) ** 2);
}

/** Check collision between two rectangles */
export function rectCollision(
  x1: number, y1: number, w1: number, h1: number,
  x2: number, y2: number, w2: number, h2: number
): boolean {
  return x1 < x2 + w2 && x1 + w1 > x2 && y1 < y2 + h2 && y1 + h1 > y2;
}

/** Check collision between two circles */
export function circleCollision(
  x1: number, y1: number, r1: number,
  x2: number, y2: number, r2: number
): boolean {
  return distance(x1, y1, x2, y2) < r1 + r2;
}

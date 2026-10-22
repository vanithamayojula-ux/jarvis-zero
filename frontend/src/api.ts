export type EngineState = "idle" | "listening" | "thinking" | "speaking";

export interface Latency {
  stt_s: number;
  llm_first_token_s: number;
  first_sentence_s: number;
  tts_s: number;
  first_audio_s: number;
  silence_wait_s: number;
}

export interface CallInfo {
  id: string;
  direction: "inbound" | "outbound";
  remote: string;
  provider: string;
  kind: "simulated" | "sip_lan" | "pstn";
  state: string;
  duration: number;
  error: string;
  label: string;
  ai_active: boolean;
  transcript: { role: string; text: string }[];
}

export interface Provider {
  name: string;
  kind: string;
  reaches_pstn: boolean;
  zero_cost_verified: boolean;
  uri?: string;
  allowed_ips?: string[];
}

export interface TelephonyStatus {
  providers: Provider[];
  real_calls_enabled: boolean;
  pstn_available: boolean;
  pstn_reason: string;
  emergency_locked: boolean;
  active_calls: CallInfo[];
  recent_calls: CallInfo[];
}

export type Settings = Record<string, any>;

const token = new URLSearchParams(location.search).get("token") || "";
const headers: Record<string, string> = { "Content-Type": "application/json" };
if (token) headers["x-jarvis-token"] = token;

export async function api<T = any>(path: string, method = "GET", body?: unknown): Promise<T> {
  const r = await fetch(`/api${path}`, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  if (!r.ok) {
    // Show the server's reason ("silence_ms must be a number from 150 to 5000"), not raw JSON.
    const raw = await r.text();
    let detail = raw;
    try { const d = JSON.parse(raw).detail; detail = typeof d === "string" ? d : JSON.stringify(d); } catch { /* not JSON */ }
    throw new Error(`${detail || r.statusText} (${method} ${path}, ${r.status})`);
  }
  return r.json();
}

export function connect(onEvent: (ev: any) => void, onOpen: (open: boolean) => void): () => void {
  let ws: WebSocket | null = null;
  let stopped = false;
  let retry = 500;
  const open = () => {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    ws = new WebSocket(`${proto}://${location.host}/ws${token ? `?token=${encodeURIComponent(token)}` : ""}`);
    ws.onopen = () => { retry = 500; onOpen(true); };
    ws.onmessage = (m) => onEvent(JSON.parse(m.data));
    ws.onclose = () => {
      onOpen(false);
      if (!stopped) setTimeout(open, (retry = Math.min(retry * 2, 5000)));
    };
  };
  open();
  return () => { stopped = true; ws?.close(); };
}

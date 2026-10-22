import { useCallback, useEffect, useRef, useState } from "react";
import { api, connect, CallInfo, EngineState, Latency, Settings, TelephonyStatus } from "./api";

interface Line { role: "user" | "assistant" | "heard" | "ignored" | "system"; text: string; meta?: string }

const STATE_LABEL: Record<EngineState, string> = {
  idle: "Idle", listening: "Listening", thinking: "Thinking", speaking: "Speaking",
};

export default function App() {
  const [online, setOnline] = useState(false);
  const [state, setState] = useState<EngineState>("idle");
  const [listening, setListening] = useState(false);
  const [level, setLevel] = useState(0);
  const [lines, setLines] = useState<Line[]>([]);
  const [latency, setLatency] = useState<Latency | null>(null);
  const [median, setMedian] = useState<Record<string, number>>({});
  const [settings, setSettings] = useState<Settings | null>(null);
  const [tel, setTel] = useState<TelephonyStatus | null>(null);
  const [confirmPrompt, setConfirmPrompt] = useState<string | null>(null);
  const [choices, setChoices] = useState<string[] | null>(null);
  const [llmReady, setLlmReady] = useState(false);
  const [error, setError] = useState("");
  type Tab = "voice" | "calls" | "history" | "settings";
  const [tab, setTabState] = useState<Tab>(() => (["voice", "calls", "history", "settings"].includes(location.hash.slice(1)) ? location.hash.slice(1) : "voice") as Tab);
  const setTab = (t: Tab) => { setTabState(t); history.replaceState(null, "", `#${t}`); };

  const push = useCallback((l: Line) => setLines((prev) => [...prev.slice(-200), l]), []);

  const refresh = useCallback(async () => {
    try {
      const s = await api("/status");
      setListening(s.engine.listening);
      setState(s.engine.state);
      setMedian(s.engine.latency || {});
      setTel(s.telephony);
      setLlmReady(s.llm_ready);
      setSettings(await api("/settings"));
    } catch (e) {
      setError(String(e));
    }
  }, []);

  useEffect(() => {
    // Opened from the Desktop shortcut (…/?listen=1): start listening right away.
    const params = new URLSearchParams(location.search);
    if (params.get("listen") === "1") {
      api("/listen/start", "POST").catch(() => {});
      params.delete("listen");
      history.replaceState(null, "", location.pathname + (params.toString() ? `?${params}` : "") + location.hash);
    }
    refresh();
    return connect((ev) => {
      switch (ev.type) {
        case "level": setLevel(ev.mic); setState(ev.state); break;
        case "state": setState(ev.state); break;
        case "mic": setListening(ev.listening); if (ev.info) push({ role: "system", text: ev.info }); break;
        case "transcript": push({ role: ev.role, text: ev.text }); break;
        case "heard": push({ role: "heard", text: ev.text, meta: `confidence ${Math.round(ev.confidence * 100)}%` }); break;
        case "ignored": push({ role: "ignored", text: ev.text, meta: ev.reason }); break;
        case "interrupted": push({ role: "system", text: "Interrupted — stopped speaking" }); break;
        case "oneshot": push({ role: "system", text: ev.listening ? "Hotkey — listening for one sentence (no wake word needed)" : "Hotkey — didn't hear anything, stopped listening" }); break;
        case "latency": setLatency(ev); break;
        case "confirm": setConfirmPrompt(ev.prompt); setChoices(ev.choices); break;
        case "reminder": push({ role: "system", text: `⏰ Reminder: ${ev.reminder.text}` }); break;
        case "error": push({ role: "system", text: `⚠ ${ev.message}` }); break;
        case "emergency_stop": push({ role: "system", text: "EMERGENCY STOP — speech halted, mic off, outgoing calls locked" }); refresh(); break;
        case "call": case "call_transcript": case "telephony": refreshTel(); break;
        case "settings": setSettings(ev.settings); break;
        case "llm": setLlmReady(ev.ready); break;
        case "hello": setListening(ev.status.listening); setTel(ev.telephony); break;
      }
    }, setOnline);
  }, [push, refresh]);

  const refreshTel = useRef(throttle(async () => setTel(await api("/telephony/status")), 300)).current;

  const update = async (changes: Settings) => {
    try { setSettings(await api("/settings", "PATCH", { changes })); } catch (e) { setError(String(e)); }
  };

  const emergency = async () => { await api("/emergency-stop", "POST"); refresh(); };

  return (
    <div className="app">
      <header>
        <div className="brand"><span className={`dot ${online ? "ok" : "bad"}`} /> JARVIS <em>Zero</em></div>
        <div className="badges">
          <span className="badge">LOCAL AUDIO</span>
          <span className={`badge ${llmReady ? "ok" : "warn"}`}>{llmReady ? "LLM ready" : "LLM loading"}</span>
          <span className={`badge ${listening ? "live" : ""}`}>{listening ? "● Mic on" : "Mic off"}</span>
          {tel?.emergency_locked && <span className="badge bad">Calls locked</span>}
        </div>
        <button className="danger" onClick={emergency} title="Stops speech, turns the mic off, locks outgoing calls">
          ■ Emergency stop
        </button>
      </header>
      {error && <div className="error" onClick={() => setError("")}>{error}</div>}

      <nav>
        {(["voice", "calls", "history", "settings"] as const).map((t) => (
          <button key={t} className={tab === t ? "active" : ""} onClick={() => setTab(t)}>
            {{ voice: "Voice", calls: "Calls", history: "History", settings: "Settings" }[t]}
          </button>
        ))}
      </nav>

      {tab === "voice" && settings && (
        <VoicePanel state={state} listening={listening} level={level} lines={lines} settings={settings}
          latency={latency} median={median} confirmPrompt={confirmPrompt} choices={choices} update={update} push={push}
          clear={() => setLines([])} />
      )}
      {tab === "calls" && tel && settings && <CallsPanel tel={tel} settings={settings} update={update} refresh={refreshTel} />}
      {tab === "history" && <HistoryPanel />}
      {tab === "settings" && settings && <SettingsPanel settings={settings} update={update} />}
    </div>
  );
}

function throttle(fn: () => void, ms: number) {
  let t: number | undefined;
  return () => { if (t === undefined) t = window.setTimeout(() => { t = undefined; fn(); }, ms); };
}

// ---------------- Voice ----------------
function VoicePanel(p: {
  state: EngineState; listening: boolean; level: number; lines: Line[]; settings: Settings; latency: Latency | null;
  median: Record<string, number>; confirmPrompt: string | null; choices: string[] | null; update: (c: Settings) => void; push: (l: Line) => void;
  clear: () => void;
}) {
  const [text, setText] = useState("");
  const [testResult, setTestResult] = useState<string>("");
  const pttDown = useRef(false);
  const ptt = p.settings.mode === "push_to_talk";
  const logRef = useRef<HTMLDivElement>(null);

  useEffect(() => { logRef.current?.scrollTo(0, 1e9); }, [p.lines]);

  useEffect(() => {
    // Hold Space to talk (when not typing in a field).
    const typing = (e: KeyboardEvent) => (e.target as HTMLElement)?.tagName.match(/INPUT|TEXTAREA|SELECT/);
    const down = (e: KeyboardEvent) => {
      if (e.code === "Space" && ptt && !typing(e) && !pttDown.current) { e.preventDefault(); pttDown.current = true; api("/ptt/down", "POST"); }
      if (e.code === "Escape") api("/interrupt", "POST");
    };
    const up = (e: KeyboardEvent) => {
      if (e.code === "Space" && pttDown.current) { e.preventDefault(); pttDown.current = false; api("/ptt/up", "POST"); }
    };
    window.addEventListener("keydown", down);
    window.addEventListener("keyup", up);
    return () => { window.removeEventListener("keydown", down); window.removeEventListener("keyup", up); };
  }, [ptt]);

  const send = async () => {
    if (!text.trim()) return;
    await api("/text", "POST", { text });
    setText("");
  };

  const selfTest = async () => {
    setTestResult("Running…");
    try {
      const r = await api("/test/local", "POST");
      setTestResult(r.llm_error ? `LLM error: ${r.llm_error}` :
        `Voice ${r.tts_s}s · Speech recognition ${r.stt_s}s (heard “${r.stt_heard}”) · LLM ${r.llm_s}s`);
    } catch (e) { setTestResult(String(e)); }
  };

  return (
    <main className="grid">
      <section className="card voice">
        <div className={`orb ${p.state}`} style={{ ["--lvl" as any]: Math.min(1, p.level * 12) }}>
          <span>{STATE_LABEL[p.state]}</span>
        </div>
        <div className="meter" aria-label="Microphone level"><div style={{ width: `${Math.min(100, p.level * 1200)}%` }} /></div>
        <div className="mode">
          <button className={ptt ? "active" : ""} onClick={() => p.update({ mode: "push_to_talk" })}>Push-to-talk</button>
          <button className={!ptt ? "active" : ""} onClick={() => p.update({ mode: "hands_free" })}>Hands-free</button>
        </div>
        {!ptt && (
          <label className="row">Activation
            <select value={p.settings.activation} onChange={(e) => p.update({ activation: e.target.value })}>
              <option value="wake_word">Wake word “{p.settings.wake_word}”</option>
              <option value="voice">Any speech</option>
            </select>
          </label>
        )}
        <div className="controls">
          {p.listening
            ? <button onClick={() => api("/listen/stop", "POST")}>Stop listening</button>
            : <button className="primary" onClick={() => api("/listen/start", "POST")}>Start listening</button>}
          {ptt && (
            <button className={`ptt ${p.state === "listening" ? "held" : ""}`}
              onPointerDown={() => api("/ptt/down", "POST")} onPointerUp={() => api("/ptt/up", "POST")}
              onPointerLeave={(e) => e.buttons && api("/ptt/up", "POST")}>
              Hold to talk <kbd>Space</kbd>
            </button>
          )}
          <button onClick={() => api("/interrupt", "POST")}>Interrupt <kbd>Esc</kbd></button>
        </div>
        <p className="hint"><b>From any window:</b> press <kbd>{(p.settings.listen_hotkey || "ctrl+alt+space").split("+").map((k: string) => k[0].toUpperCase() + k.slice(1)).join(" + ")}</kbd>, then just speak.</p>
        <p className="hint">{ptt ? "In this window you can also hold the button or Space while you speak."
          : p.settings.activation === "wake_word" ? `Start with “${p.settings.wake_word}” (e.g. “Jarvis, what time is it?”), then keep talking normally for ${p.settings.followup_s} s after each reply. Processed on this PC only.`
          : "JARVIS answers any speech. Best with headphones."}</p>
        {p.confirmPrompt && (
          <div className="confirm">
            <span>{p.choices ? "Which one?" : p.confirmPrompt}</span>
            {p.choices ? (
              <>
                {p.choices.map((c, i) => (
                  <button key={c} className="primary" onClick={() => api("/text", "POST", { text: ["the first one", "the second one", "the third one"][i] })}>{c}</button>
                ))}
                <button onClick={() => api("/text", "POST", { text: "none of them" })}>None</button>
              </>
            ) : (
              <>
                <button className="primary" onClick={() => api("/confirm/yes", "POST")}>Yes</button>
                <button onClick={() => api("/confirm/no", "POST")}>No</button>
              </>
            )}
          </div>
        )}
        <div className="test">
          <button onClick={selfTest}>Local assistant test</button>
          <small>{testResult}</small>
        </div>
      </section>

      <section className="card transcript">
        <div className="card-head"><h2>Conversation</h2>
          <div>
            <button className="small" onClick={async () => p.push({ role: "system", text: (await api("/conversation/summary")).summary })}>Summarize</button>
            <button className="small" onClick={async () => { await api("/conversation/new", "POST"); p.clear(); }}>New</button>
          </div>
        </div>
        <div className="log" ref={logRef}>
          {p.lines.length === 0 && <p className="empty">Say something, or type below.</p>}
          {p.lines.map((l, i) => (
            <div key={i} className={`line ${l.role}`}>
              <span className="who">{{ user: "You", assistant: "JARVIS", heard: "Heard", ignored: "Ignored", system: "•" }[l.role]}</span>
              <span className="text">{l.text}{l.meta && <small> — {l.meta}</small>}</span>
            </div>
          ))}
        </div>
        <form className="say" onSubmit={(e) => { e.preventDefault(); send(); }}>
          <input value={text} onChange={(e) => setText(e.target.value)} placeholder="Type to JARVIS…" />
          <button className="primary">Send</button>
        </form>
      </section>

      <section className="card latency">
        <h2>Latency</h2>
        {p.latency ? (
          <dl>
            <dt>Speech recognition</dt><dd>{p.latency.stt_s.toFixed(2)} s</dd>
            <dt>LLM first token</dt><dd>{p.latency.llm_first_token_s.toFixed(2)} s</dd>
            <dt>First sentence ready</dt><dd>{p.latency.first_sentence_s.toFixed(2)} s</dd>
            <dt>Voice synthesis (total)</dt><dd>{p.latency.tts_s.toFixed(2)} s</dd>
            <dt><b>First audio after you stop</b></dt><dd><b>{p.latency.first_audio_s.toFixed(2)} s</b></dd>
            <dt>+ end-of-turn silence</dt><dd>{p.latency.silence_wait_s.toFixed(2)} s</dd>
          </dl>
        ) : <p className="empty">Measured for every reply.</p>}
        {p.median.turns ? <small>Median of {p.median.turns} turns: {p.median.first_audio_s?.toFixed(2)} s to first audio</small> : null}
      </section>
    </main>
  );
}

// ---------------- Calls ----------------
function CallsPanel({ tel, settings, update, refresh }: { tel: TelephonyStatus; settings: Settings; update: (c: Settings) => void; refresh: () => void }) {
  const [say, setSay] = useState("");
  const [dest, setDest] = useState("");
  const [provider, setProvider] = useState("mock");
  const [consent, setConsent] = useState(false);
  const [decision, setDecision] = useState<{ allowed: boolean; reason: string } | null>(null);
  const [operator, setOperator] = useState("");
  type PhoneAddr = { ip: string; iface: string; network: string; uri: string; qr_svg: string; hotspot: boolean; label: string; callers_allowed: boolean };
  type PhoneSetup = {
    enabled: boolean; listening: boolean; tcp: boolean; port: number; addresses: PhoneAddr[]; hotspot_active: boolean;
    allowed: string[]; invalid_allowed: string[]; presets: Record<string, string>; owner_ips: string[];
    owner_mode_available: boolean; rtp_ports: number[]; ignored_sources: Record<string, number>;
    registrations: { aor: string; ip: string }[]; error: string; firewall_rules: boolean | null;
  };
  const [phone, setPhone] = useState<PhoneSetup | null>(null);
  const [newNet, setNewNet] = useState("");
  const [owners, setOwners] = useState(((settings.telephony.owner_ips || []) as string[]).join(", "));
  const [copied, setCopied] = useState("");
  const call: CallInfo | undefined = tel.active_calls[0];
  const sip = tel.providers.find((p) => p.name === "sip_lan");
  const allowedList: string[] = settings.telephony.sip_allowed_ips?.length ? settings.telephony.sip_allowed_ips : ["192.168.137.0/24"];

  useEffect(() => {
    let alive = true;
    const load = () => api("/telephony/phone-setup").then((r) => alive && setPhone(r)).catch(() => {});
    load();
    const t = setInterval(load, 5000);
    return () => { alive = false; clearInterval(t); };
  }, [settings.telephony.sip_lan_enabled, JSON.stringify(settings.telephony.sip_allowed_ips), JSON.stringify(settings.telephony.owner_ips)]);

  const setAllowed = (list: string[]) => update({ telephony: { sip_allowed_ips: Array.from(new Set(list.filter(Boolean))) } });
  const addNet = (net: string, warn = false) => {
    if (!net) return;
    if (warn && !confirm(`Allow every device on ${net} to call JARVIS?\n\nOnly do this for a network that only you use (your own hotspot). Never for hostel, campus or cafe Wi-Fi.`)) return;
    setAllowed([...allowedList, net.trim()]);
  };
  const copy = async (text: string) => {
    try { await navigator.clipboard.writeText(text); setCopied(text); setTimeout(() => setCopied(""), 1500); } catch { /* clipboard blocked */ }
  };
  const callable = (phone?.addresses || []).filter((a) => a.callers_allowed);
  const others = (phone?.addresses || []).filter((a) => !a.callers_allowed);
  const exampleIp = callable[0]?.ip || "192.168.137.1";
  const port = phone?.port || settings.telephony.sip_port;

  const place = async () => {
    const r = await api("/telephony/call", "POST", { provider, destination: dest, consent });
    setDecision(r);
    refresh();
  };

  return (
    <main className="grid calls">
      <section className="card" style={{ gridColumn: "1 / -1" }}>
        <h2>Call JARVIS from your phone <span className="tag lan">FREE · LOCAL NETWORK</span></h2>
        <p>Your phone calls this laptop directly with a free softphone app. No phone number, no account, no internet, ₹0.
          JARVIS answers, tells the caller it's an AI, and talks.</p>

        <table className="matrix"><tbody>
          <tr><td>SIP listener</td>
            <td className={phone?.listening ? "ok" : "bad"}>{phone?.listening ? `Listening on port ${port} (UDP${phone.tcp ? " + TCP" : ""})` : "Off"}</td>
            <td><label className="toggle"><input type="checkbox" checked={settings.telephony.sip_lan_enabled}
              onChange={(e) => update({ telephony: { sip_lan_enabled: e.target.checked } })} /> Enabled</label></td></tr>
          <tr><td>Windows Firewall</td>
            <td className={phone?.firewall_rules ? "ok" : "bad"}>{phone?.firewall_rules ? "Rules added (private networks only)" : phone?.firewall_rules === false ? "Not set up yet" : "Unknown"}</td>
            <td>{!phone?.firewall_rules && <small>Run <code>scripts\setup-phone.ps1</code> once (it asks for admin permission)</small>}</td></tr>
          <tr><td>Windows Mobile Hotspot</td>
            <td className={phone?.hotspot_active ? "ok" : ""}>{phone?.hotspot_active ? "On: phones on it can reach 192.168.137.1" : "Off"}</td>
            <td>{!phone?.hotspot_active && <small>Settings → Network &amp; internet → Mobile hotspot → On</small>}</td></tr>
          <tr><td>Allowed to call</td><td colSpan={2}>{(phone?.allowed || []).join(" · ")}</td></tr>
        </tbody></table>

        {callable.length > 0 ? (
          <div style={{ display: "flex", flexWrap: "wrap", gap: 16, margin: "12px 0" }}>
            {callable.map((a) => (
              <div key={a.ip} style={{ display: "flex", gap: 12, alignItems: "center", border: "1px solid rgba(127,127,127,.3)", borderRadius: 8, padding: 10 }}>
                <img alt={`QR code for ${a.uri}`} width={150} height={150} style={{ background: "#fff", borderRadius: 4 }}
                  src={`data:image/svg+xml;charset=utf-8,${encodeURIComponent(a.qr_svg)}`} />
                <div>
                  <div><b>{a.label}</b> <small>({a.network})</small></div>
                  <div style={{ fontSize: "1.15em", margin: "6px 0" }}><code>{a.uri}</code></div>
                  <button onClick={() => copy(a.uri)}>{copied === a.uri ? "Copied" : "Copy address"}</button>
                </div>
              </div>
            ))}
          </div>
        ) : (
          <div className="confirm" style={{ margin: "12px 0" }}>
            <span>No network that your phone can call on yet. Turn on <b>Windows Mobile Hotspot</b> and join it from your phone
              (then dial <code>sip:jarvis@192.168.137.1:{port}</code>), or connect the laptop to your phone's hotspot and allow that network below.</span>
          </div>
        )}
        {others.length > 0 && (
          <div><small>Other networks on this laptop (calls from them are ignored):</small>
            <ul className="recent">{others.map((a) => (
              <li key={a.ip}>{a.label} · {a.ip} ({a.network})
                {" "}<button className="link" onClick={() => addNet(a.network, true)}>Allow this network…</button></li>))}
            </ul>
          </div>
        )}

        <h3>Set up your phone (once)</h3>
        <ol>
          <li>Connect phone and laptop to the same <b>private</b> network: easiest is the laptop's Windows Mobile Hotspot (phone joins it).</li>
          <li>Install <b>Linphone</b> (free, Android Play Store / iPhone App Store).</li>
          <li>No account is needed. Skip the sign-in screens, open the dialer and type the address above
            (<code>sip:jarvis@{exampleIp}:{port}</code>), then press call. Tip: scan the QR code with the phone's camera to copy the address instead of typing it.</li>
          <li>If your Linphone version insists on an account: choose <i>Use a SIP account</i> / <i>Third-party SIP account</i> with
            username <code>phone</code>, any password, domain <code>{exampleIp}:{port}</code>, transport <b>UDP</b>. JARVIS accepts that sign-in locally. Then dial <code>jarvis</code>.</li>
          <li>In Linphone settings keep <b>Media encryption: None</b> and the <b>PCMU/PCMA (G.711)</b> codecs enabled.</li>
          <li><b>Zoiper</b> (free) works the same way: add an account with username <code>phone</code>, any password, domain <code>{exampleIp}:{port}</code>, skip the provider search, then dial <code>jarvis</code>.</li>
        </ol>

        <h3>Allowed networks</h3>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 6, alignItems: "center" }}>
          {allowedList.map((n) => (
            <span key={n} className="tag lan">{n} <button className="link" title="Remove" onClick={() => setAllowed(allowedList.filter((x) => x !== n))}>×</button></span>
          ))}
          <span className="tag sim">127.0.0.0/8 (this PC, always)</span>
        </div>
        <form className="say" onSubmit={(e) => { e.preventDefault(); addNet(newNet, true); setNewNet(""); }}>
          <input value={newNet} onChange={(e) => setNewNet(e.target.value)} placeholder="IP or subnet, e.g. 172.20.10.0/28" />
          <button>Add</button>
        </form>
        <div className="controls">
          {Object.entries(phone?.presets || {}).map(([label, net]) => (
            <button key={net} disabled={allowedList.includes(net)} onClick={() => addNet(net)}>{label}: {net}</button>
          ))}
        </div>
        {!!phone?.invalid_allowed?.length && <p className="bad">Ignored invalid entries: {phone.invalid_allowed.join(", ")}</p>}
        {phone && Object.keys(phone.ignored_sources || {}).length > 0 && (
          <p><small>Recently ignored (not allowed): {Object.entries(phone.ignored_sources).map(([ip, n]) => `${ip} (${n})`).join(", ")}</small></p>
        )}

        <h3>Owner mode (optional)</h3>
        <label className="row">Owner phone IPs
          <input value={owners} onChange={(e) => setOwners(e.target.value)} placeholder="e.g. 192.168.137.23"
            onBlur={() => update({ telephony: { owner_ips: owners.split(/[\s,]+/).filter(Boolean) } })} />
        </label>
        <small>Calls from these addresses can use JARVIS's laptop tools (open apps, websites, reminders), with the same
          confirmations as on the laptop. Leave empty for a talk-only assistant. Only list your own phone on a hotspot only you use:
          on a shared network another device could pretend to have that address.</small>

        <h3>Honest note about hostel Wi-Fi</h3>
        <small>Hostel and campus Wi-Fi (for example "LPU Hostels-5G") almost always blocks devices from reaching each other
          (client isolation), and Windows treats it as a Public network. So calling over it will not work, and JARVIS deliberately
          ignores it. Use a personal hotspot instead: the call audio stays between the two devices and uses no mobile data.
          Real phone numbers are not supported and stay disabled.</small>
        {phone?.error && <p className="bad"><small>Last SIP error: {phone.error}</small></p>}
      </section>

      <section className="card">
        <h2>Telephony status</h2>
        <table className="matrix">
          <tbody>
            <tr><td><span className="tag sim">SIMULATED</span></td><td>Test-call simulator</td><td className="ok">Available · no network, no cost</td></tr>
            <tr><td><span className="tag lan">SIP LAN</span></td><td>Softphone on your own Wi-Fi</td>
              <td className={sip ? "ok" : ""}>{sip ? `Listening at ${sip.uri}` : "Off"}</td></tr>
            <tr><td><span className="tag pstn">REAL PHONE</span></td><td>Ordinary phone numbers (PSTN)</td><td className="bad">Disabled — {tel.pstn_reason}</td></tr>
          </tbody>
        </table>
        <label className="row toggle">
          <input type="checkbox" checked={settings.telephony.sip_lan_enabled}
            onChange={(e) => update({ telephony: { sip_lan_enabled: e.target.checked } })} />
          Enable SIP over local network (UDP {settings.telephony.sip_port})
        </label>
        <small>Only allowed networks (and this PC) can call in; changes apply immediately.</small>
        {tel.emergency_locked && (
          <div className="confirm"><span>Outgoing calls are locked by the emergency stop.</span>
            <button onClick={async () => { await api("/emergency-reset", "POST"); refresh(); }}>Unlock</button></div>
        )}
      </section>

      <section className="card">
        <h2>Test-call simulator <span className="tag sim">SIMULATED</span></h2>
        {!call ? (
          <button className="primary" onClick={async () => { await api("/telephony/simulate/incoming", "POST"); refresh(); }}>
            Simulate incoming call
          </button>
        ) : (
          <div className="callcard">
            <div className="callhead">
              <span className={`tag ${call.kind === "simulated" ? "sim" : call.kind === "sip_lan" ? "lan" : "pstn"}`}>{call.label}</span>
              <b>{call.remote}</b> · {call.state} · {call.duration.toFixed(0)} s {call.ai_active ? "· AI answering" : "· HUMAN HANDOFF"}
            </div>
            <div className="log small">
              {call.transcript.map((t, i) => <div key={i} className={`line ${t.role === "caller" ? "user" : "assistant"}`}>
                <span className="who">{t.role === "caller" ? "Caller" : t.role === "operator" ? "You" : "JARVIS"}</span><span className="text">{t.text}</span></div>)}
            </div>
            {call.provider === "mock" && (
              <form className="say" onSubmit={async (e) => { e.preventDefault(); await api("/telephony/simulate/say", "POST", { call_id: call.id, text: say }); setSay(""); }}>
                <input value={say} onChange={(e) => setSay(e.target.value)} placeholder="Caller says…" />
                <button className="primary">Say</button>
              </form>
            )}
            <div className="controls">
              {call.ai_active
                ? <button onClick={() => api("/telephony/handoff", "POST", { call_id: call.id })}>Hand off to me</button>
                : <button onClick={() => api("/telephony/resume-ai", "POST", { call_id: call.id })}>Resume AI</button>}
              <button className="danger" onClick={() => api("/telephony/hangup", "POST", { call_id: call.id })}>Hang up</button>
            </div>
            {!call.ai_active && (
              <form className="say" onSubmit={async (e) => { e.preventDefault(); await api("/telephony/operator-say", "POST", { call_id: call.id, text: operator }); setOperator(""); }}>
                <input value={operator} onChange={(e) => setOperator(e.target.value)} placeholder="Type a reply; JARVIS speaks it into the call" />
                <button>Speak</button>
              </form>
            )}
          </div>
        )}
      </section>

      <section className="card">
        <h2>Outbound call</h2>
        <label className="row">Provider
          <select value={provider} onChange={(e) => setProvider(e.target.value)}>
            <option value="mock">Simulated (mock)</option>
            {sip && <option value="sip_lan">SIP over LAN</option>}
          </select>
        </label>
        <label className="row">Destination
          <input value={dest} onChange={(e) => setDest(e.target.value)} placeholder={provider === "mock" ? "any test label" : "sip:phone@192.168.1.23:5060"} />
        </label>
        <label className="row toggle"><input type="checkbox" checked={consent} onChange={(e) => setConsent(e.target.checked)} />
          I consent to placing this call, and the person called will be told it's an AI.</label>
        <button className="primary" disabled={!dest} onClick={place}>Place call</button>
        {decision && <p className={decision.allowed ? "ok" : "bad"}>{decision.allowed ? "Allowed: " : "Blocked: "}{decision.reason}</p>}
        <h3>Recent calls</h3>
        <ul className="recent">
          {[...tel.recent_calls].reverse().map((c) => (
            <li key={c.id}><span className={`tag ${c.kind === "simulated" ? "sim" : c.kind === "sip_lan" ? "lan" : "pstn"}`}>{c.kind}</span>
              {c.direction} · {c.remote} · {c.state}{c.duration ? ` · ${c.duration}s` : ""}{c.error && <small> — {c.error}</small>}</li>
          ))}
        </ul>
      </section>
    </main>
  );
}

// ---------------- History ----------------
function HistoryPanel() {
  const [items, setItems] = useState<{ id: string; turns: number; preview: string; channel: string }[]>([]);
  const [open, setOpen] = useState<{ id: string; messages: { role: string; text: string; t: string }[] } | null>(null);
  useEffect(() => { api("/conversations").then((r) => setItems(r.conversations)); }, []);
  return (
    <main className="grid history">
      <section className="card">
        <h2>Conversations <small>(stored locally in data/conversations)</small></h2>
        <ul className="recent">
          {items.map((c) => (
            <li key={c.id}><button className="link" onClick={async () => setOpen(await api(`/conversations/${c.id}`))}>
              {c.id}</button> · {c.channel} · {c.turns} lines · {c.preview}</li>
          ))}
          {!items.length && <p className="empty">No saved conversations yet.</p>}
        </ul>
      </section>
      {open && (
        <section className="card">
          <h2>{open.id}</h2>
          <div className="log">
            {open.messages.map((m, i) => <div key={i} className={`line ${m.role === "user" ? "user" : m.role === "tool" ? "system" : "assistant"}`}>
              <span className="who">{m.role}</span><span className="text">{m.text}</span></div>)}
          </div>
        </section>
      )}
    </main>
  );
}

// ---------------- Settings ----------------
function SettingsPanel({ settings, update }: { settings: Settings; update: (c: Settings) => void }) {
  const [devices, setDevices] = useState<{ inputs: { index: number; name: string }[]; outputs: { index: number; name: string }[] } | null>(null);
  const [voices, setVoices] = useState<string[]>([]);
  const [models, setModels] = useState<string[]>([]);
  useEffect(() => {
    api("/devices").then(setDevices);
    api("/voices").then((r) => setVoices(r.voices));
    api("/llm-models").then((r) => setModels(r.models));
  }, []);
  const num = (v: string) => (v === "" ? null : Number(v));
  return (
    <main className="grid settings">
      <section className="card">
        <h2>Audio</h2>
        <label className="row">Microphone
          <select value={settings.input_device ?? ""} onChange={(e) => update({ input_device: num(e.target.value) })}>
            <option value="">System default</option>
            {devices?.inputs.map((d) => <option key={d.index} value={d.index}>{d.name}</option>)}
          </select>
        </label>
        <label className="row">Speaker
          <select value={settings.output_device ?? ""} onChange={(e) => update({ output_device: num(e.target.value) })}>
            <option value="">System default</option>
            {devices?.outputs.map((d) => <option key={d.index} value={d.index}>{d.name}</option>)}
          </select>
        </label>
        <label className="row toggle"><input type="checkbox" checked={settings.barge_in} onChange={(e) => update({ barge_in: e.target.checked })} />
          Let me interrupt JARVIS by talking</label>
        <label className="row">End-of-turn silence: {settings.silence_ms} ms
          <input type="range" min={300} max={1500} step={50} value={settings.silence_ms} onChange={(e) => update({ silence_ms: Number(e.target.value) })} />
        </label>
        <label className="row">Wake word
          <input defaultValue={settings.wake_word} onBlur={(e) => update({ wake_word: e.target.value.trim().toLowerCase() || "jarvis" })} />
        </label>
      </section>
      <section className="card">
        <h2>Voice &amp; personality</h2>
        <label className="row">Voice
          <select value={settings.tts_voice} onChange={(e) => update({ tts_voice: e.target.value })}>
            {voices.map((v) => <option key={v}>{v}</option>)}
          </select>
        </label>
        <label className="row">Speaking speed: {Number(settings.speaking_rate).toFixed(2)}×
          <input type="range" min={0.7} max={1.5} step={0.05} value={settings.speaking_rate} onChange={(e) => update({ speaking_rate: Number(e.target.value) })} />
        </label>
        <label className="row">Personality
          <select value={settings.personality} onChange={(e) => update({ personality: e.target.value })}>
            <option value="jarvis">JARVIS (dry British wit)</option>
            <option value="friendly">Friendly</option>
            <option value="concise">Concise</option>
          </select>
        </label>
        <label className="row">Your name
          <input defaultValue={settings.user_name} onBlur={(e) => update({ user_name: e.target.value })} />
        </label>
      </section>
      <section className="card">
        <h2>Models (all local)</h2>
        <label className="row">Language model (Ollama)
          <select value={settings.llm_model} onChange={(e) => update({ llm_model: e.target.value })}>
            {models.map((m) => <option key={m}>{m}</option>)}
          </select>
        </label>
        <label className="row">Speech recognition (Whisper)
          <select value={settings.stt_model} onChange={(e) => update({ stt_model: e.target.value })}>
            <option value="tiny.en">tiny.en — fastest</option>
            <option value="base.en">base.en — balanced</option>
            <option value="small.en">small.en — most accurate, slower</option>
          </select>
        </label>
        <label className="row toggle"><input type="checkbox" checked={settings.save_conversations} onChange={(e) => update({ save_conversations: e.target.checked })} />
          Save conversation transcripts on this PC</label>
      </section>
    </main>
  );
}

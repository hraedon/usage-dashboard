"""The ``/login`` pane: a static shell that drives the enrolment API.

Server-rendered like ``/dashboard`` (no framework, no build step), but the
opposite exposure posture: the shell carries no data at all — every fetch
goes to ``/internal/v1/login/*`` with the bearer key, which the operator
supplies once per browser session. That keeps the pane consistent with the
dashboard's "private network, shell open, data gated" split while adding
nothing to the externally-routed surface.
"""
from __future__ import annotations

_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>usage dashboard — login pane</title>
<style>
:root {
  --bg: #0f1115; --card: #171a21; --card2: #1d212b; --line: #2a2f3c;
  --text: #e6e9f0; --dim: #8b93a7; --green: #22c55e; --orange: #f97316;
  --red: #ef4444; --blue: #3b82f6; --mono: ui-monospace, SFMono-Regular,
  Menlo, Consolas, monospace;
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--text);
  font: 15px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  padding: 16px;
}
.wrap { max-width: 720px; margin: 0 auto; }
h1 { font-size: 1.15rem; margin: 4px 0 2px; }
h1 a { color: var(--dim); text-decoration: none; font-weight: 400; }
.sub { color: var(--dim); font-size: .85rem; margin: 0 0 16px; }
.card {
  background: var(--card); border: 1px solid var(--line); border-radius: 10px;
  padding: 14px 16px; margin: 12px 0;
}
.card h2 { font-size: .95rem; margin: 0 0 4px; }
.card p { color: var(--dim); font-size: .82rem; margin: 4px 0 10px; }
.row { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
button {
  background: var(--card2); color: var(--text); border: 1px solid var(--line);
  border-radius: 8px; padding: 8px 14px; font-size: .88rem; cursor: pointer;
}
button:hover { border-color: var(--blue); }
button:disabled { opacity: .45; cursor: default; }
button.primary { background: #1e3a5f; border-color: #2b4f7e; }
select, input[type=text], input[type=password], textarea {
  background: var(--bg); color: var(--text); border: 1px solid var(--line);
  border-radius: 8px; padding: 8px 10px; font-size: .88rem;
}
textarea { width: 100%; font-family: var(--mono); font-size: .78rem; min-height: 64px; }
label.fl { color: var(--dim); font-size: .8rem; }
.msg { font-size: .82rem; margin-top: 8px; min-height: 1em; }
.msg.ok { color: var(--green); } .msg.err { color: var(--red); }
#jobbox { display: none; }
#jobhead { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
#jobstate { font-size: .8rem; padding: 2px 10px; border-radius: 999px;
  border: 1px solid var(--line); color: var(--dim); }
#jobstate.running { color: var(--orange); border-color: var(--orange); }
#jobstate.succeeded { color: var(--green); border-color: var(--green); }
#jobstate.failed { color: var(--red); border-color: var(--red); }
pre#transcript {
  background: var(--bg); border: 1px solid var(--line); border-radius: 8px;
  font: .78rem/1.45 var(--mono); padding: 10px; margin: 10px 0 0;
  max-height: 300px; overflow-y: auto; white-space: pre-wrap;
  word-break: break-all;
}
#codebox { display: none; margin-top: 10px; padding: 12px;
  background: var(--card2); border: 1px solid var(--line); border-radius: 8px; }
#codebox a { color: var(--blue); word-break: break-all; }
#codebox .code { font: 700 1.3rem var(--mono); letter-spacing: .12em;
  margin-top: 8px; }
#keybanner { display: none; }
details { color: var(--dim); font-size: .8rem; }
details ol { margin: 6px 0; padding-left: 18px; }
code { font-family: var(--mono); color: var(--text); }
</style>
</head>
<body>
<div class="wrap">
  <h1>usage dashboard <a href="/dashboard">← dashboard</a></h1>
  <p class="sub">Enrolment pane (Plan 006). Runs the official login ceremonies
  inside the server pod; credentials never appear in this page.</p>

  <div class="card" id="keybanner">
    <p id="keymsg">The pane needs the dashboard API key.</p>
    <div class="row">
      <input type="password" id="key" placeholder="API key" style="flex:1">
      <button id="keybtn" class="primary">Set key</button>
    </div>
  </div>

  <div class="card" id="jobbox">
    <div id="jobhead">
      <h2 id="jobname" style="margin:0">job</h2>
      <span id="jobstate">running</span>
      <span id="jobspacer" style="flex:1"></span>
      <button id="cancelbtn">Cancel</button>
    </div>
    <div id="codebox">
      <div>Open <a id="vurl" target="_blank" rel="noopener">the verification URL</a>
      and enter this code:</div>
      <div class="code" id="vcode">—</div>
    </div>
    <pre id="transcript"></pre>
    <div class="row" id="inputrow" style="margin-top:8px">
      <input type="text" id="jobinput" placeholder="paste the code the ceremony is waiting for…"
        style="flex:1" autocomplete="off">
      <button id="sendbtn" class="primary">Send</button>
    </div>
  </div>

  <div class="card">
    <h2>Claude (personal / work)</h2>
    <p>Official Claude Code login. The pane shows a one-time code to enter at
    the URL it prints; paste the confirmation back below. Takes effect
    immediately — no rollout needed.</p>
    <div class="row">
      <label class="fl">account</label>
      <select id="claude-account"><option>personal</option><option>work</option></select>
      <button id="claudebtn" class="primary">Start enrolment</button>
    </div>
    <div class="msg" id="claude-msg"></div>
  </div>

  <div class="card">
    <h2>Codex (ChatGPT plan)</h2>
    <p>Device-code login through the official App Server. The runtime App
    Server pauses for the ceremony and resumes afterwards.</p>
    <div class="row">
      <button id="codexbtn" class="primary">Start enrolment</button>
    </div>
    <div class="msg" id="codex-msg"></div>
  </div>

  <div class="card">
    <h2>Ollama</h2>
    <p>Cookie-based: sign in to ollama.com in your own browser
    (<code>ollama.com/settings</code>), then paste the resulting session
    cookie header below. It is verified against the live usage page before
    anything is stored.</p>
    <details><summary>How to capture the cookie</summary>
      <ol>
        <li>Sign in at ollama.com in this browser.</li>
        <li>Devtools → Application/Storage → Cookies → copy the
        <code>session</code>-scoped values, or copy the whole <code>Cookie:</code>
        request header from the Network tab for <code>ollama.com/settings</code>.</li>
        <li>The CLI route (<code>usage-dashboard login ollama</code>, Playwright)
        still works from any machine with a display.</li>
      </ol>
    </details>
    <textarea id="ollama-cookie" placeholder="name=value; name2=value2 …"></textarea>
    <div class="row" style="margin-top:8px">
      <button id="ollamabtn" class="primary">Verify &amp; store</button>
    </div>
    <div class="msg" id="ollama-msg"></div>
  </div>

  <div class="card">
    <h2>OpenCode Go</h2>
    <p>Same cookie flow, plus the workspace id. Sign in at opencode.ai, open
    your workspace's Go page and read the <code>wrk_…</code> id from the URL.
    Do <b>not</b> sign out afterwards — that invalidates the cookie.</p>
    <div class="row">
      <label class="fl">workspace id</label>
      <input type="text" id="opencode-workspace" placeholder="wrk_…" style="flex:1">
    </div>
    <textarea id="opencode-cookie" placeholder="auth cookie value"
      style="margin-top:8px"></textarea>
    <div class="row" style="margin-top:8px">
      <button id="opencodebtn" class="primary">Verify &amp; store</button>
    </div>
    <div class="msg" id="opencode-msg"></div>
  </div>
</div>

<script>
"use strict";
const $ = (id) => document.getElementById(id);
const KEYNAME = "ud-login-key";
let key = sessionStorage.getItem(KEYNAME) || "";
let activeJob = null, pollTimer = null, lastTranscriptLen = -1;

function showKey(msg) {
  $("keybanner").style.display = "";
  if (msg) $("keymsg").textContent = msg;
}
function setMsg(id, text, ok) {
  const el = $(id); el.textContent = text || "";
  el.className = "msg" + (ok ? " ok" : text ? " err" : "");
}

async function api(path, body, method) {
  if (!key) { showKey("The pane needs the dashboard API key."); throw new Error("no key"); }
  const init = { method: method || (body ? "POST" : "GET"),
    headers: { "Authorization": "Bearer " + key } };
  if (body) { init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body); }
  const resp = await fetch(path, init);
  if (resp.status === 401) {
    sessionStorage.removeItem(KEYNAME); key = "";
    showKey("That key was rejected — set the dashboard API key.");
    throw new Error("unauthorized");
  }
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw Object.assign(new Error(data.detail || resp.statusText),
    { status: resp.status });
  return data;
}

function renderJob(job) {
  if (!job) { $("jobbox").style.display = "none"; return; }
  $("jobbox").style.display = "";
  $("jobname").textContent =
    job.provider + (job.account ? " · " + job.account : "") + " · " + job.job_id;
  const st = $("jobstate");
  st.textContent = job.state;
  st.className = job.state;
  if (job.verification_url && job.user_code) {
    $("codebox").style.display = "";
    $("vurl").href = job.verification_url;
    $("vurl").textContent = job.verification_url;
    $("vcode").textContent = job.user_code;
  } else { $("codebox").style.display = "none"; }
  const lines = job.transcript || [];
  const pre = $("transcript");
  if (lines.length !== lastTranscriptLen) {
    pre.textContent = lines.join("\\n");
    pre.scrollTop = pre.scrollHeight;
    lastTranscriptLen = lines.length;
  }
  const interactive = job.state === "running" && job.provider === "claude";
  $("inputrow").style.display = interactive ? "" : "none";
  $("cancelbtn").style.display = job.state === "running" && job.provider !== "codex"
    ? "" : "none";
}

function schedulePoll() {
  clearTimeout(pollTimer);
  const delay = activeJob && activeJob.state === "running" ? 1500 : 5000;
  pollTimer = setTimeout(poll, delay);
}

async function poll() {
  try {
    if (activeJob && activeJob.state === "running") {
      activeJob = await api("/internal/v1/login/jobs/" + activeJob.job_id);
      renderJob(activeJob);
      if (activeJob.state === "succeeded") {
        setMsg("claude-msg", "Enrolled; credentials live.", true);
        setMsg("codex-msg", "Enrolled; Codex client resumed.", true);
      }
    } else {
      const s = await api("/internal/v1/login/status");
      activeJob = s.active;
      renderJob(activeJob);
    }
  } catch (e) { if (String(e.message) !== "no key" && e.message !== "unauthorized")
    console.warn(e); }
  schedulePoll();
}

async function guard(fn, msgId) {
  if (activeJob && activeJob.state === "running") {
    setMsg(msgId, "An enrolment is already running — finish or cancel it first.");
    return;
  }
  try { lastTranscriptLen = -1; activeJob = await fn(); renderJob(activeJob);
    setMsg(msgId, "", true); }
  catch (e) { setMsg(msgId, e.message || String(e)); }
  poll();
}

$("keybtn").onclick = () => {
  key = $("key").value.trim();
  if (key) { sessionStorage.setItem(KEYNAME, key);
    $("keybanner").style.display = "none"; poll(); }
};
$("claudebtn").onclick = () =>
  guard(() => api("/internal/v1/login/claude",
    { account: $("claude-account").value }), "claude-msg");
$("codexbtn").onclick = () =>
  guard(() => api("/internal/v1/login/codex", {}), "codex-msg");
$("cancelbtn").onclick = async () => {
  if (activeJob) try { await api("/internal/v1/login/jobs/"
      + activeJob.job_id + "/cancel", {}); } catch (e) { console.warn(e); }
};
$("sendbtn").onclick = async () => {
  const el = $("jobinput");
  if (!el.value || !activeJob) return;
  try { await api("/internal/v1/login/jobs/" + activeJob.job_id + "/input",
      { text: el.value }); el.value = ""; }
  catch (e) { setMsg("claude-msg", e.message || String(e)); }
};
$("jobinput").addEventListener("keydown", (ev) => {
  if (ev.key === "Enter") $("sendbtn").click();
});
$("ollamabtn").onclick = async () => {
  try { await api("/internal/v1/login/credential",
      { provider: "ollama", credential: $("ollama-cookie").value });
    setMsg("ollama-msg", "Verified & stored; fetcher picks it up on the next poll.", true);
    $("ollama-cookie").value = ""; }
  catch (e) { setMsg("ollama-msg", e.message || String(e)); }
};
$("opencodebtn").onclick = async () => {
  try { await api("/internal/v1/login/credential",
      { provider: "opencode", credential: $("opencode-cookie").value,
        workspace_id: $("opencode-workspace").value });
    setMsg("opencode-msg", "Verified & stored; fetcher picks it up on the next poll.", true);
    $("opencode-cookie").value = ""; }
  catch (e) { setMsg("opencode-msg", e.message || String(e)); }
};

if (!key) showKey(); poll();
</script>
</body>
</html>
"""


def render_login_html() -> str:
    """The static enrolment pane shell (data arrives via the authed API)."""
    return _PAGE

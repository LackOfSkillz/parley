"""Live two-sided viewer for the Claude <-> GPT parley.

Reads the append-only logs written by parley.py and streams them to a browser,
so both halves of the conversation are visible as they happen. The outbound
message is logged before the API call, so a question appears while its answer
is still in flight.

Stdlib only. Binds loopback exclusively -- this renders prompts and repository
context, which never belong on the LAN.

Usage:  python serve.py            (http://localhost:4688)
        python serve.py --port N
"""

from __future__ import annotations

import argparse
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
LOGDIR = HERE / "log"
PORT = 4688


# Transcripts written by parley.py end in a 32-hex digest of the canonical
# (project, thread) key. Anything else is a pre-hash legacy file whose name
# could collide; showing it would present the same conversation twice.
CURRENT_LOG = re.compile(r"-[0-9a-f]{32}$")


def threads() -> list[dict]:
    """Every thread with a log, newest activity first."""
    out = []
    if not LOGDIR.is_dir():
        return out
    for p in sorted(LOGDIR.glob("*.jsonl")):
        if not CURRENT_LOG.search(p.stem):
            continue
        last, n = None, 0
        project = thread = ""
        for rec in read_log(p):
            n += 1
            last = rec.get("ts") or last
            project = rec.get("project") or project
            thread = rec.get("thread") or thread
        out.append(
            {
                "id": p.stem,
                "project": project,
                "thread": thread,
                "messages": n,
                "last": last or "",
            }
        )
    return sorted(out, key=lambda r: r["last"], reverse=True)


def read_log(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    recs = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            recs.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return recs


def messages(tid: str, since: int) -> dict:
    """Messages for one transcript.

    The id comes from our own /api/threads listing, but it arrives via the query
    string, so it is re-validated rather than trusted: only a well-formed
    transcript name can be opened, and it cannot escape LOGDIR.
    """
    if not CURRENT_LOG.search(tid) or "/" in tid or "\\" in tid or ".." in tid:
        return {"total": 0, "messages": []}
    recs = read_log(LOGDIR / f"{tid}.jsonl")
    return {"total": len(recs), "messages": recs[since:] if since < len(recs) else []}


# Two-tone mark: Claude's colour and GPT's, the two voices in the log.
FAVICON = (
    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
    b'<rect width="32" height="32" rx="7" fill="#161b22"/>'
    b'<circle cx="12" cy="16" r="5.5" fill="#d97757"/>'
    b'<circle cx="20" cy="16" r="5.5" fill="#10a37f" fill-opacity="0.85"/>'
    b"</svg>"
)

PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="description" content="Live two-sided view of a Claude/GPT consultation.">
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<title>Parley</title><style>
*{box-sizing:border-box}
:root{--bg:#0d1117;--panel:#161b22;--edge:#21262d;--txt:#c9d1d9;--dim:#7d8590;
--claude:#d97757;--gpt:#10a37f;--accent:#58a6ff;--bad:#f85149;--warn:#d29922}
html,body{height:100%}
body{margin:0;background:var(--bg);color:var(--txt);
font:14px/1.55 ui-sans-serif,-apple-system,"Segoe UI",sans-serif;display:flex}
aside{width:270px;flex:0 0 270px;background:var(--panel);border-right:1px solid var(--edge);
display:flex;flex-direction:column;height:100vh}
aside h1{font-size:13px;letter-spacing:.14em;text-transform:uppercase;color:var(--dim);
margin:0;padding:18px 16px 12px;border-bottom:1px solid var(--edge)}
#threads{overflow-y:auto;flex:1}
.th{padding:11px 16px;border-bottom:1px solid var(--edge);cursor:pointer}
.th:hover{background:#1c2128}
.th.sel{background:#1f2937;border-left:3px solid var(--accent);padding-left:13px}
.th .p{font-weight:600;color:#e6edf3}
.th .m{font-size:11px;color:var(--dim);margin-top:3px}
main{flex:1;display:flex;flex-direction:column;height:100vh;min-width:0}
header{padding:14px 22px;border-bottom:1px solid var(--edge);background:var(--panel);
display:flex;align-items:center;gap:14px}
header .t{font-weight:600}
header .s{font-size:12px;color:var(--dim);margin-left:auto;display:flex;align-items:center;gap:7px}
.dot{width:7px;height:7px;border-radius:50%;background:var(--gpt);
animation:pulse 2s ease-in-out infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.35}}
#feed{flex:1;overflow-y:auto;padding:22px}
.msg{margin-bottom:18px;max-width:none}
.who{display:flex;align-items:center;gap:9px;font-size:11px;letter-spacing:.1em;
text-transform:uppercase;margin-bottom:7px}
.pill{width:8px;height:8px;border-radius:2px}
.claude .pill{background:var(--claude)} .gpt .pill{background:var(--gpt)}
.claude .nm{color:var(--claude)} .gpt .nm{color:var(--gpt)}
.meta{color:var(--dim);text-transform:none;letter-spacing:0;font-size:11px}
pre{margin:0;padding:14px 16px;background:var(--panel);border:1px solid var(--edge);
border-radius:7px;white-space:pre-wrap;word-wrap:break-word;overflow-wrap:anywhere;
font:13px/1.6 ui-monospace,"Cascadia Code",Consolas,monospace}
.claude pre{border-left:3px solid var(--claude)}
.gpt pre{border-left:3px solid var(--gpt)}
.err pre{border-left:3px solid var(--bad)}
.lim pre{border-left:3px solid var(--warn)}
.lim .nm{color:var(--warn)} .lim .pill{background:var(--warn)}
.err .nm{color:var(--bad)} .err .pill{background:var(--bad)}
.fold{max-height:280px;overflow:hidden;position:relative}
.fold::after{content:"";position:absolute;left:0;right:0;bottom:0;height:70px;
background:linear-gradient(transparent,var(--panel))}
.more{margin-top:7px;font-size:12px;color:var(--accent);cursor:pointer;
background:none;border:0;padding:0}
.empty{color:var(--dim);text-align:center;line-height:1.9;height:100%;
display:flex;flex-direction:column;align-items:center;justify-content:center}
.none{padding:14px 16px;font-size:12px;color:var(--dim);font-style:italic}
kbd{background:var(--panel);border:1px solid var(--edge);border-radius:4px;
padding:2px 6px;font:12px ui-monospace,monospace;color:var(--txt)}
</style></head><body>
<aside><h1>Conversations</h1><div id="threads"></div></aside>
<main>
 <header><span class="t" id="title">Parley</span>
  <span class="s"><span class="dot"></span><span id="status">watching</span></span></header>
 <div id="feed"><div class="empty">No conversations yet.<br>
  Run <kbd>parley.py</kbd> and messages appear here live.</div></div>
</main>
<script>
let sel=null, seen=0, pinned=true, gen=0;
const feed=document.getElementById('feed');
const EMPTY='<div class="empty">No conversations yet.<br>'
  +'Run <kbd>parley.py</kbd> and messages appear here live.</div>';
feed.addEventListener('scroll',()=>{
  pinned = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 60;
});
const esc=s=>String(s??'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
const clock=t=>t?t.slice(11,19)+'Z':'';

async function loadThreads(){
  const r=await fetch('/api/threads'); const list=await r.json();
  // A transcript can be archived or deleted while the page is open. Without
  // this the feed keeps rendering a conversation that no longer exists, which
  // reads as live data and is the most misleading thing this page could do.
  if(sel && !list.some(t=>t.id===sel)){
    sel=null; seen=0; gen++; feed.innerHTML=EMPTY;
    document.getElementById('title').textContent='Parley';
  }
  const box=document.getElementById('threads');
  box.innerHTML = list.length ? '' : '<div class="none">no conversations yet</div>';
  for(const t of list){
    const d=document.createElement('div');
    d.className='th'+(t.id===sel?' sel':'');
    d.innerHTML=`<div class="p">${esc(t.project.split(/[\\\\/]/).pop()||t.id)}</div>
      <div class="m">${esc(t.thread)} &middot; ${t.messages} msg</div>`;
    d.onclick=()=>{sel=t.id;seen=0;gen++;feed.innerHTML='';pinned=true;
      document.getElementById('title').textContent=
        (t.project.split(/[\\\\/]/).pop()||t.id)+'  /  '+t.thread;
      loadThreads();poll();};
    box.appendChild(d);
  }
  if(!sel && list.length){ list[0] && document.querySelector('.th').click(); }
}

function render(m){
  const who=m.error?'err':(m.limit?'lim':(m.role==='gpt'?'gpt':'claude'));
  const el=document.createElement('div'); el.className='msg '+who;
  const bits=[clock(m.ts)];
  if(m.mode) bits.push(m.mode);
  if(m.turn) bits.push('turn '+m.turn);
  if(m.attached&&m.attached.length) bits.push('+'+m.attached.join(', '));
  if(m.tokens_out) bits.push(m.tokens_in+' in / '+m.tokens_out+' out');
  // A structural inference shown as an observed capture, or a guessed backoff
  // shown as provider instruction, is exactly what the schema exists to prevent.
  if(m.limit){
    bits.push('limit: '+m.limit.kind);
    bits.push('evidence: '+(m.limit.evidence||'unknown'));
    if(m.limit.retry_after_seconds) bits.push('provider asked '+m.limit.retry_after_seconds+'s');
    else bits.push('wait would be a guess');
  }
  const long=(m.text||'').length>1400;
  el.innerHTML=`<div class="who"><span class="pill"></span>
    <span class="nm">${who==='err'?'FAILED':who==='lim'?'LIMITED':who==='gpt'?'GPT':'Claude'}</span>
    <span class="meta">${esc(bits.join(' \\u00b7 '))}</span></div>
    <pre class="${long?'fold':''}">${esc(m.text)}</pre>
    ${long?'<button class="more">show full message</button>':''}`;
  if(long){
    const b=el.querySelector('.more'), p=el.querySelector('pre');
    b.onclick=()=>{const f=p.classList.toggle('fold');
      b.textContent=f?'show full message':'collapse';};
  }
  feed.appendChild(el);
}

async function poll(){
  // Snapshot the target and a generation counter. A fetch started for thread A
  // can resolve after the user has switched to B; without this the late reply
  // is appended into B's feed and clobbers its cursor.
  const want=sel, myGen=gen;
  if(!want) return;
  try{
    const r=await fetch(`/api/messages?thread=${encodeURIComponent(want)}&since=${seen}`);
    const d=await r.json();
    if(myGen!==gen || want!==sel) return;   // selection moved: discard this reply
    if(d.messages.length){
      if(seen===0) feed.innerHTML='';
      d.messages.forEach(render);
      seen=d.total;
      if(pinned) feed.scrollTop=feed.scrollHeight;
      loadThreads();
    }
    document.getElementById('status').textContent='watching';
  }catch(e){ document.getElementById('status').textContent='disconnected'; }
}
loadThreads(); setInterval(poll,1500); setInterval(loadThreads,6000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            return self.send(PAGE.encode("utf-8"), "text/html; charset=utf-8")
        if u.path == "/api/threads":
            return self.send(json.dumps(threads()).encode(), "application/json")
        if u.path == "/api/messages":
            q = parse_qs(u.query)
            tid = (q.get("thread") or [""])[0]
            try:
                since = int((q.get("since") or ["0"])[0])
            except ValueError:
                since = 0
            return self.send(
                json.dumps(messages(tid, since)).encode(), "application/json"
            )
        # Both paths: browsers request /favicon.ico unprompted regardless of the
        # <link>, and an unanswered one puts a 404 in the console on every load.
        if u.path in ("/favicon.svg", "/favicon.ico"):
            return self.send(FAVICON, "image/svg+xml")
        if u.path == "/healthz":
            return self.send(b"ok", "text/plain")
        self.send_error(404, "Not found")

    def send(self, payload: bytes, ctype: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt: str, *a: object) -> None:
        pass  # the point of this server is the conversation, not access logs


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=PORT)
    args = ap.parse_args()
    LOGDIR.mkdir(parents=True, exist_ok=True)
    print(f"Parley viewer -> http://localhost:{args.port}")
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()

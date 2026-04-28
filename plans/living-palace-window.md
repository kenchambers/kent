# Plan: Live 3D palace viewer for kent (`kent viz`)

## Context

kent's memory is now a layered structure on disk (palace ChromaDB + per-wing
diaries + transcripts + tunnels). The user wants to *see* it — a browser
window that renders the whole thing as a 3D force-directed graph and updates
the moment new drawers/diary entries arrive.

Library choice (locked by user): [`vasturiano/3d-force-graph`](https://github.com/vasturiano/3d-force-graph)
— ThreeJS/WebGL force graph, vanilla JS, supports incremental updates by
re-applying `Graph.graphData({nodes, links})` (node objects keyed by `id`
keep their simulated positions across re-applies).

We are deliberately *not* building a SPA, a websocket protocol, a diff
algorithm, or a watchdog framework for v1. The smallest thing that satisfies
the brief is:

1. `kent viz` subcommand → starts a stdlib HTTP server on `localhost:8765`.
2. `GET /` → one static HTML page with `3d-force-graph` from CDN + ~80 lines
   of vanilla JS.
3. `GET /events` → Server-Sent Events stream (`text/event-stream`).
4. Server polls the palace+diary mtimes ~every 1 s; on change, rebuilds the
   snapshot and pushes the full `{nodes, links}` payload as one SSE event.
5. Browser receives the payload, calls `Graph.graphData(...)`. 3d-force-graph
   handles the incremental visual update for free — node positions persist
   for nodes whose `id` is unchanged.

That's the whole thing. No new Python deps, no extra processes, no
event-bus plumbing inside the agent loop.

> **Scope update (2026-04-28):** the brief grew. v1 now also includes
> (a) a side-panel **chat window** wired to `agent.loop.run()` so the user
> can talk to kent in the same window and watch their messages mint new
> drawers in real time, and (b) an explicit **animation pass** so the graph
> doesn't look like a static dotplot. Both fold cleanly into the SSE +
> snapshot architecture above — see the new sections **Chat panel** and
> **Animations & visual interest** below.

## Review findings (2026-04-28)

A three-agent review (cursor-code / code-simplifier / gemini-code) ran
against this plan. Critical correctness bugs in the pseudocode are fixed
inline below; discretionary choices are surfaced as open questions.

| # | Where | Severity | Issue (and how it's resolved here) |
|---|-------|----------|-----------------------------------|
| R1 | `snapshot.py` imports | **CRITICAL** | `get_collection` / `get_closets_collection` are not in `mempalace`'s top-level namespace. Import explicitly from `mempalace.palace`. Without this, every snapshot raises `NameError`. *(Fixed in pseudocode.)* |
| R2 | `snapshot.py` `build_graph()` | **HIGH** | `mempalace.palace_graph.build_graph()` with no args opens `~/.mempalace/palace`, **not** kent's `~/.kent/palace`. Must pass `col=` explicitly. Also primes a 60s in-process cache with wrong-palace data. *(Fixed in pseudocode.)* |
| R3 | `snapshot.py` passive tunnels | MEDIUM | Edge dict shape is `{room, wing_a, wing_b, hall, count}`. Original pseudocode produced `room→room` self-loops. Render as `wing_a → wing_b` with `label=room`. *(Fixed.)* |
| R4 | `snapshot.py` `list_tunnels()` | LOW | Tunnel records have nested `t["source"]["wing"]` / `t["source"]["room"]`, not flat keys. *(Fixed.)* |
| R5 | `server.py` SSE loop | **CRITICAL** | `_closed()` was checked *before* `wfile.write()`, but `BrokenPipeError` raises *on* the write. The handler thread crashed on every browser close. Wrap write+flush in `try/except (BrokenPipeError, ConnectionResetError)` and `return`. *(Fixed.)* |
| R6 | `server.py` mtime signature | **CRITICAL** | Directory mtime does **not** change when an existing file's contents change. Polling `~/.kent/diaries/` alone misses appended diary entries. Stat each `*.md` individually (with a 1s recheck for the rglob cost on huge palaces). *(Fixed.)* |
| R7 | `snapshot.py` error handling | **CRITICAL** | Any exception (missing palace, corrupt `tunnels.json`, malformed closet line) killed the SSE thread silently. Wrap in `try/except`, return a partial snapshot with an `error` field the UI can show. *(Fixed.)* |
| R8 | UX assumption | **CRITICAL** (verify) | `3d-force-graph`'s "node positions persist across re-applies of `graphData()` for matching `id`s" is the load-bearing UX assumption and is **not** verified in the plan. **Pre-implementation spike (≤30 min):** standalone HTML that re-applies `graphData()` and confirms positions stick. If they don't, add a tiny client-side position cache before committing the rest. *(Open task.)* |
| R9 | drawer cap | MEDIUM | Plan said "5k cap" prose but pseudocode used `limit=10_000`. Unified to a single `DRAWER_HARD_CAP` constant. *(Fixed.)* |
| R10 | browser error handling | MEDIUM | `JSON.parse` and `EventSource` errors were uncaught. Added `try/catch` + `es.onerror` in the sketch. *(Fixed.)* |
| R11 | `cmd_viz` import precheck | MEDIUM | If `mempalace` isn't installed, the subcommand crashed mid-snapshot with no actionable error. Added a precheck. *(Fixed.)* |
| R12 | poll cost on write storms | LOW | A `mempalace mine` ingest can mutate the palace dozens of times per second. Added a 1s settle-debounce: if mtime is still moving on the next tick, defer the snapshot. *(Fixed.)* |
| R13 | `/snapshot` endpoint | LOW (simplifier) | Pseudocode added a `GET /snapshot` route that the HTML never uses. Dropped. *(Fixed.)* |
| R14 | scope: 8 node + 8 edge types | discretionary | Simplifier suggested deferring `closet`, `diary_file`, and `identity` to v1.1. Kept in v1 because the user asked for a *visually interesting* graph and these add structure. Surfaced as open question Q5. |
| R15 | `identity → every wing` edge | discretionary | Already flagged in the original plan's Q4. Default decision: keep `identity` as a labeled floating node, drop the radial edges. *(Updated Q4.)* |
| R16 | `test_server.py` flakiness | discretionary | Spinning a real server in-thread to assert one SSE event is brittle on CI (port binding, EventSource semantics). Recommend replacing with a unit test of `mtime_signature()` + an in-process `build_snapshot()` test. *(Updated test list.)* |

## What to render (mapping MemPalace → graph)

Verified from the codebase (`mempalace/layers.py`, `mempalace/palace.py`,
`mempalace/palace_graph.py`, `agent/memory/*`):

| Node type    | Source                                                            | Color        | `val` (size)            |
|--------------|-------------------------------------------------------------------|--------------|-------------------------|
| `identity`   | `~/.mempalace/identity.txt` (L0, single node)                     | gold         | 8                       |
| `wing`       | dirs under `~/.kent/diaries/`                                     | cyan         | 4 + drawer_count·0.05   |
| `room`       | distinct `meta.room` in `mempalace_drawers`                       | purple       | 1 + drawer_count·0.1    |
| `drawer`     | each row in `mempalace_drawers`                                   | by `kind` (1)| 0.5 + importance·0.2    |
| `closet`     | each row in `mempalace_closets`                                   | grey         | 0.7                     |
| `diary_file` | `~/.kent/diaries/<wing>/<date>.md`                                | green        | 1 + (#entries · 0.1)    |
| `tunnel`     | explicit tunnels from `~/.mempalace/tunnels.json`                 | red dashed   | n/a (link only)         |

(1) Drawer color by kind: OBSERVATION=blue, FINDING=green, DECISION=orange,
PATTERN=violet, transcript-derived=light grey.

| Edge type        | Connects                                                |
|------------------|---------------------------------------------------------|
| `wing→room`      | every `(wing, room)` pair seen in drawer metadata       |
| `room→drawer`    | each drawer to its room                                 |
| `drawer→closet`  | every closet line `→drawer_ids` reference               |
| `wing→diary_file`| each diary md file to its wing                          |
| `diary_file→drawer` | when `meta.source_file` matches the diary file path  |
| `room↔room` (passive tunnel) | same room name across ≥2 wings (already in `palace_graph.build_graph()`'s edges) |
| `explicit tunnel`| from `~/.mempalace/tunnels.json` endpoints              |
| `identity→wing`  | identity node connects to every wing (L0 anchor)        |

The existing `palace_graph.build_graph()` already produces room nodes +
passive tunnel edges and has a 60s in-memory cache with write
invalidation. We reuse it and add drawer/closet/diary nodes around it.

## User-locked decisions (confirmed 2026-04-28)

All six locked. Build against these.

1. ✅ **Standalone process.** `kent viz` is its own command. Doesn't depend
   on a kent REPL being open. Reads the palace from disk, polls for
   changes.
2. ✅ **mtime-poll** on `~/.kent/palace/chroma.sqlite3`,
   `~/.mempalace/tunnels.json`, and each `*.md` under `~/.kent/diaries/`
   (per-file, not directory — see R6), every 1 s. Not watchdog, not
   in-process hooks. Catches writes from any source — kent itself,
   mempalace MCP, `mempalace mine`, manual edits, and the `/chat` panel.
3. ✅ **Server-Sent Events** over stdlib `http.server`. Two streams:
   `/events` for snapshots (server → browser), `/chat` for agent events
   (one POST → SSE response). Not WebSockets; no `aiohttp`/`fastapi`.
4. ✅ **Full-snapshot push.** Browser re-applies `Graph.graphData()`;
   `3d-force-graph` reuses node objects by id so positions persist (R8
   spike still recommended as cheap insurance). Diff layer deferred until
   the 5k-drawer cap actually bites.
5. ✅ **No build step.** Single static HTML; `3d-force-graph`, `three.js`,
   and `UnrealBloomPass` from `cdn.jsdelivr.net`. No npm, no Vite, no
   React.
6. ✅ **Localhost-only.** Bind `127.0.0.1:8765`, no auth. Local dev tool —
   anyone who can hit localhost can see the palace, which is already
   true for the palace files themselves.

> **Forward-looking note.** 1-second mtime polling is the v1 floor, not
> the ceiling. Long-term we want the palace to feel *more dynamic* — the
> graph should react within ~50–100 ms of a write, not "up to 1 s after
> the next mtime tick". Path forward when we get to it: keep mtime polling
> as the safety net (catches external writers — `mempalace mine`, MCP,
> manual edits) and add an in-process notification channel inside
> `MemPalaceStore` / `DiaryWriter` that the viz server subscribes to via
> a unix socket or a `multiprocessing.Queue`. Server pushes a snapshot
> immediately on notify, polling falls back to ~5 s. Out of scope for v1
> but worth designing the snapshot/SSE shape so it's drop-in compatible.

## File-by-file changes

```
agent/viz/
  __init__.py            # exports start_server(palace, kent_home, port, chat_session)
  snapshot.py            # build_snapshot(palace, kent_home) -> {nodes, links, ...}
  server.py              # stdlib http.server + 2 SSE handlers (snapshot + chat)
  chat.py                # ChatSession: wraps LLM + ToolRegistry + MemPalaceStore;
                         # .send(message) yields normalized event dicts
  static/
    index.html           # 1 file, CDN imports, vanilla JS, chat panel + graph
agent/cli.py             # add `kent viz [--port N] [--read-only]` subcommand
tests/viz/
  test_snapshot.py       # build_snapshot() against a synthetic palace fixture
  test_mtime_signature.py # FIXED (R16): catches the directory-mtime regression
  test_chat_session.py   # ChatSession.send() with stub LLM; assert event order
```

`agent/viz/chat.py` is the only piece that *imports* the existing agent
stack — `agent.loop.run`, `agent.tools.ToolRegistry`, the builtin tools,
`agent.memory.MemPalaceStore`. Everything else (`snapshot.py`,
`server.py`) is a strict reader. No changes to `agent/loop.py`,
`agent/memory/*`, `agent/tools.py`, `agent/critic.py`.

### `agent/viz/chat.py` — sketch

```python
import asyncio, queue, threading
from pathlib import Path

from agent.llm import LLM
from agent.tools import ToolRegistry
from agent.builtin.shell import Shell, detect_shell_backend
from agent.builtin.web_fetch import WebFetch
from agent.builtin.web_search import WebSearch
from agent.loop import run as agent_run
from agent.memory.mempalace_store import MemPalaceStore


class ChatSession:
    """Thin wrapper around agent.loop.run for the viz chat panel.

    Owns one MemPalaceStore + ToolRegistry + conversation history. Each
    .send(message) call drives one agent turn-loop and yields normalized
    event dicts (type, data) the SSE handler forwards verbatim.

    Only one .send() may run at a time — guarded by a lock. Concurrent
    POSTs from the same browser are serialized.
    """

    def __init__(self, *, llm: LLM, palace: Path, kent_home: Path):
        self.llm = llm
        self.store = MemPalaceStore(palace_path=palace, kent_home=kent_home)
        self.tools = self._build_tools()
        self.system = _system_prompt()  # same as cmd_run uses
        self.history: list[dict] = []
        self._lock = threading.Lock()

    def send(self, user_message: str):
        """Generator of {type, data} dicts. Synchronous facade over the
        async agent loop — uses a queue + bg event-loop thread."""
        with self._lock:
            self.history.append({"role": "user", "content": user_message})
            q: queue.Queue = queue.Queue()
            STOP = object()

            async def _drive():
                try:
                    async for ev in agent_run(
                        messages=list(self.history),
                        tools=self.tools, llm=self.llm,
                        system=self.system, max_turns=20,
                        memory_store=self.store,
                    ):
                        q.put(_to_dict(ev))
                        if ev.__class__.__name__ == "AssistantMessageComplete":
                            self.history.append(ev.message.to_openai_dict())
                finally:
                    q.put(STOP)

            t = threading.Thread(
                target=lambda: asyncio.new_event_loop().run_until_complete(_drive()),
                daemon=True,
            )
            t.start()

            while True:
                ev = q.get()
                if ev is STOP: break
                yield ev

    def _build_tools(self) -> ToolRegistry:
        reg = ToolRegistry()
        reg.register(Shell(backend=detect_shell_backend()))
        reg.register(WebSearch())
        reg.register(WebFetch())
        # memory + diary tools wired the same way the REPL does
        return reg


def _to_dict(ev) -> dict:
    """Normalize an agent event into {type, data} for the SSE wire."""
    name = ev.__class__.__name__
    if name == "TextDelta":
        return {"type": name, "data": {"text": ev.text}}
    if name == "ToolCallComplete":
        c = ev.call
        return {"type": name, "data": {"name": c.name, "arguments": c.arguments}}
    if name == "ToolResult":
        return {"type": name, "data": {"call_id": ev.call_id, "ok": ev.ok}}
    if name == "AssistantMessageComplete":
        return {"type": name, "data": {}}
    if name == "ModelError":
        return {"type": name, "data": {"error": str(ev.error)}}
    if name == "Terminal":
        return {"type": name, "data": {"reason": ev.reason}}
    return {"type": name, "data": {}}
```

That's the whole bridge. ~80 LOC, no new top-level deps.

### `agent/viz/snapshot.py` — pseudocode

```python
import logging, time
from pathlib import Path

from mempalace.palace import get_collection, get_closets_collection
import mempalace.palace_graph as palace_graph

from agent.memory.wings import list_wings

logger = logging.getLogger(__name__)
DRAWER_HARD_CAP = 5_000

# first_seen registry — server-side, persisted across snapshots so the
# browser can fade in nodes that are genuinely new vs. reapply existing
# ones. Keyed by node id; values are epoch seconds.
_first_seen: dict[str, float] = {}


def _stamp_first_seen(node: dict) -> dict:
    nid = node["id"]
    node["first_seen"] = _first_seen.setdefault(nid, time.time())
    return node


def build_snapshot(palace_path: Path, kent_home: Path) -> dict:
    nodes: list[dict] = []
    links: list[dict] = []
    error: str | None = None
    truncated = False

    # Defensive: missing palace = empty snapshot, viewer keeps polling.
    if not (palace_path / "chroma.sqlite3").exists():
        return {"nodes": [], "links": [], "empty": True}

    try:
        col = get_collection(str(palace_path), create=False)

        # L0 — identity (floating, no radial edges; see Q4)
        nodes.append(_stamp_first_seen({
            "id": "identity", "type": "identity",
            "label": "kent", "color": "gold", "val": 8,
        }))

        # Wings + rooms + passive tunnels.
        # CRITICAL (R2): pass col= explicitly so build_graph reads kent's
        # palace, not mempalace's default at ~/.mempalace/palace.
        rooms, edges = palace_graph.build_graph(col=col)

        for wing in list_wings(home=kent_home):
            nodes.append(_stamp_first_seen({
                "id": f"wing:{wing}", "type": "wing",
                "label": wing, "color": "cyan", "val": 4,
            }))

        for room, data in rooms.items():
            nodes.append(_stamp_first_seen({
                "id": f"room:{room}", "type": "room",
                "label": room, "color": "purple",
                "val": 1 + len(data.get("wings", [])) * 0.1,
            }))
            for w in data["wings"]:
                links.append({"source": f"wing:{w}", "target": f"room:{room}"})

        # Passive tunnels — same room across two wings.
        # FIXED (R3): connect the two *wings* through the shared room,
        # not a self-loop on the room node.
        for e in edges:
            links.append({
                "source": f"wing:{e['wing_a']}",
                "target": f"wing:{e['wing_b']}",
                "type": "passive_tunnel",
                "label": e["room"],
                "color": "#888",
            })

        # Drawers — single col.get() pass, capped.
        # FIXED (R9): single DRAWER_HARD_CAP constant.
        batch = col.get(include=["metadatas"], limit=DRAWER_HARD_CAP + 1)
        ids = batch.get("ids") or []
        metas = batch.get("metadatas") or []
        if len(ids) > DRAWER_HARD_CAP:
            truncated = True
            ids, metas = ids[:DRAWER_HARD_CAP], metas[:DRAWER_HARD_CAP]
        for did, meta in zip(ids, metas):
            kind_color = {
                "OBSERVATION": "#5af", "FINDING": "#5f8",
                "DECISION": "#fa5", "PATTERN": "#a5f",
            }.get((meta or {}).get("kind", ""), "#bbb")
            imp = float((meta or {}).get("importance", 1.0))
            nodes.append(_stamp_first_seen({
                "id": f"drawer:{did}", "type": "drawer",
                "label": (meta or {}).get("topic") or did[:8],
                "color": kind_color, "val": 0.5 + imp * 0.2,
            }))
            room = (meta or {}).get("room")
            if room:
                links.append({"source": f"room:{room}", "target": f"drawer:{did}"})

        # Closets — parse "topic|entities|→drawer_ids" lines for back-links.
        try:
            closets_col = get_closets_collection(str(palace_path), create=False)
            cb = closets_col.get(include=["documents", "metadatas"], limit=DRAWER_HARD_CAP)
            for cid, doc in zip(cb.get("ids") or [], cb.get("documents") or []):
                nodes.append(_stamp_first_seen({
                    "id": f"closet:{cid}", "type": "closet",
                    "label": (doc or "")[:40], "color": "#aaa", "val": 0.7,
                }))
                # Back-links: lines look like "topic | ent | →id1,id2"
                for line in (doc or "").splitlines():
                    arrow = line.find("→")
                    if arrow < 0: continue
                    for ref in line[arrow+1:].split(","):
                        ref = ref.strip()
                        if ref:
                            links.append({
                                "source": f"closet:{cid}",
                                "target": f"drawer:{ref}",
                                "type": "closet_ref",
                            })
        except Exception:
            logger.debug("closets not available", exc_info=True)

        # Diary files. (mtime polling for *content* edits is in server.py.)
        for wing in list_wings(home=kent_home):
            wing_dir = kent_home / "diaries" / wing
            if not wing_dir.exists(): continue
            for md in wing_dir.glob("*.md"):
                nid = f"diary:{wing}/{md.name}"
                nodes.append(_stamp_first_seen({
                    "id": nid, "type": "diary_file",
                    "label": md.name, "color": "#5f5", "val": 1.0,
                }))
                links.append({"source": f"wing:{wing}", "target": nid})

        # Explicit tunnels.
        # FIXED (R4): tunnel records have nested source/target dicts.
        for t in palace_graph.list_tunnels():
            links.append({
                "source": f"wing:{t['source']['wing']}",
                "target": f"wing:{t['target']['wing']}",
                "type": "tunnel",
                "label": t.get("label", ""),
                "color": "red",
            })

    except Exception as e:
        # FIXED (R7): never kill the SSE thread. Return what we have plus
        # an error tag the UI can surface in the HUD.
        error = f"{type(e).__name__}: {e}"
        logger.exception("snapshot build failed")

    return {
        "nodes": nodes, "links": links,
        "truncated": truncated, "error": error,
    }
```

### `agent/viz/server.py` — pseudocode

```python
import http.server, json, time, asyncio, threading
from pathlib import Path

# Provided by start_server() at boot — not module-level globals so tests
# can spin up isolated instances.
class VizHandler(http.server.BaseHTTPRequestHandler):
    palace: Path = None      # type: ignore[assignment]
    kent_home: Path = None   # type: ignore[assignment]
    chat_session = None      # ChatSession; see below

    # ---------- routing ----------

    def do_GET(self):
        if self.path == "/":
            self._serve_static("index.html", "text/html")
        elif self.path == "/events":
            self._sse_snapshot_loop()
        else:
            self.send_error(404)

    def do_POST(self):
        # FIXED (R13): no /snapshot — the HTML never used it.
        if self.path == "/chat":
            self._sse_chat()
        else:
            self.send_error(404)

    # ---------- /events: snapshot SSE ----------

    def _sse_snapshot_loop(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        last_sig = None
        while True:
            sig = mtime_signature(self.palace, self.kent_home)
            if sig != last_sig:
                # FIXED (R12): debounce write storms. If mtime is still
                # moving 1s later, defer this snapshot.
                time.sleep(1.0)
                if mtime_signature(self.palace, self.kent_home) != sig:
                    continue
                snap = build_snapshot(self.palace, self.kent_home)
                if not self._sse_send(snap):
                    return  # client gone
                last_sig = sig
            time.sleep(1.0)

    def _sse_send(self, payload: dict) -> bool:
        """Write one SSE event. Returns False if client disconnected.
        FIXED (R5): wrap write+flush, not just a check before write."""
        try:
            self.wfile.write(f"data: {json.dumps(payload)}\n\n".encode())
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError):
            return False

    # ---------- /chat: agent stream ----------

    def _sse_chat(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        message = (body.get("message") or "").strip()
        if not message:
            self.send_error(400, "missing 'message'")
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        # The chat session owns kent's LLM, ToolRegistry, MemPalaceStore,
        # and conversation history. Tool calls (diary_write, set_wing,
        # memory_recall, ...) write to the palace → mtimes change → the
        # /events stream above pushes a new snapshot. The user literally
        # sees their conversation creating drawers.
        for ev in self.chat_session.send(message):
            if not self._sse_send({
                "type": ev["type"],
                "data": ev["data"],
            }):
                return  # tab closed; the agent loop keeps running


def mtime_signature(palace: Path, kent_home: Path) -> tuple:
    """FIXED (R6): stat each diary file individually. Directory mtime
    does not change on *content* edits to existing files."""
    sigs: list[float] = []
    for p in (
        palace / "chroma.sqlite3",
        Path.home() / ".mempalace" / "tunnels.json",
    ):
        try: sigs.append(p.stat().st_mtime)
        except FileNotFoundError: sigs.append(0.0)

    diaries = kent_home / "diaries"
    if diaries.exists():
        for md in sorted(diaries.rglob("*.md")):
            try: sigs.append(md.stat().st_mtime)
            except FileNotFoundError: pass
    return tuple(sigs)


def start_server(palace: Path, kent_home: Path, *, port: int = 8765,
                 chat_session=None) -> None:
    VizHandler.palace = palace
    VizHandler.kent_home = kent_home
    VizHandler.chat_session = chat_session  # may be None for read-only mode
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), VizHandler)
    srv.daemon_threads = True
    srv.serve_forever()
```

Notes:
- `ThreadingHTTPServer` + `daemon_threads=True` so SSE loops don't block
  shutdown and concurrent browser tabs each get their own thread.
- `mtime_signature` stat()s 2 fixed paths plus every `*.md` under
  `~/.kent/diaries/`. On a palace with thousands of diaries this is a few
  ms per poll — fine. Optimize with a cached file list later if it bites.
- ChromaDB SQLite uses WAL — concurrent reader (snapshot) + writer
  (chat session) is safe; worst case is a few hundred ms of read-lock
  hold during a `col.get()`.

### `agent/viz/static/index.html` — sketch

Layout: graph fills the window; a 380px chat column docks on the right.
The chat sends to `POST /chat` and reads back an SSE stream of agent
events. Snapshot updates arrive on a separate `/events` SSE — the user
sees the graph mutate as the agent's tool calls hit disk.

```html
<!doctype html>
<html><head><title>kent palace</title>
<style>
  body{margin:0;background:#000;color:#bbb;font-family:ui-monospace,monospace;overflow:hidden}
  #app{display:flex;height:100vh;width:100vw}
  #g{flex:1;position:relative}
  #hud{position:absolute;top:8px;left:8px;color:#888;font-size:12px;pointer-events:none;z-index:2}
  #err{position:absolute;top:8px;right:8px;color:#f88;font-size:12px;z-index:2;display:none}
  #chat{width:380px;border-left:1px solid #222;display:flex;flex-direction:column;background:#0a0a0a}
  #log{flex:1;overflow-y:auto;padding:10px;font-size:13px;line-height:1.4}
  #log .msg{margin-bottom:10px;white-space:pre-wrap;word-wrap:break-word}
  #log .user{color:#9cf}
  #log .asst{color:#dfe}
  #log .tool{color:#fc8;font-size:11px;font-style:italic}
  #log .err{color:#f88}
  #composer{display:flex;border-top:1px solid #222;padding:6px;gap:6px}
  #input{flex:1;background:#111;color:#dfe;border:1px solid #222;padding:6px;font:inherit}
  #send{background:#1a3;color:#fff;border:0;padding:6px 14px;cursor:pointer;font:inherit}
  #send:disabled{opacity:0.4;cursor:wait}
</style>
</head><body>
<div id="app">
  <div id="g">
    <div id="hud">nodes: <span id="n">…</span> · drawers: <span id="d">…</span> · upd: <span id="u">…</span></div>
    <div id="err"></div>
  </div>
  <div id="chat">
    <div id="log"></div>
    <form id="composer">
      <input id="input" autocomplete="off" placeholder="talk to kent…"/>
      <button id="send">send</button>
    </form>
  </div>
</div>

<script src="//cdn.jsdelivr.net/npm/three@0.155.0/build/three.min.js"></script>
<script src="//cdn.jsdelivr.net/npm/3d-force-graph"></script>
<script src="//cdn.jsdelivr.net/npm/three@0.155.0/examples/js/postprocessing/UnrealBloomPass.js"></script>
<script>
// ---------- graph ----------
const G = ForceGraph3D()(document.getElementById('g'))
  .backgroundColor('#000010')
  .nodeId('id').nodeVal('val').nodeLabel('label')
  .nodeColor(n => n.color || '#bbb')
  .nodeOpacity(0.95)
  .linkColor(l => l.color || '#333')
  .linkOpacity(0.35)
  // --- animation hooks (see "Animations & visual interest") ---
  .linkDirectionalParticles(l => (l.type === 'tunnel' ? 6 : (l.type === 'passive_tunnel' ? 3 : 1)))
  .linkDirectionalParticleSpeed(0.006)
  .linkDirectionalParticleWidth(l => (l.type === 'tunnel' ? 1.4 : 0.6))
  .nodeRelSize(4);

// Bloom for soft glow on the bright nodes (identity, wings).
const bloom = new THREE.UnrealBloomPass(new THREE.Vector2(window.innerWidth, window.innerHeight), 0.8, 0.4, 0.1);
G.postProcessingComposer().addPass(bloom);

// Slow auto-orbit; pause on user interaction.
G.controls().autoRotate = true;
G.controls().autoRotateSpeed = 0.25;
G.controls().addEventListener('start', () => { G.controls().autoRotate = false; });

// New-node fade-in: scale val from 0 → target over ~1.2s using first_seen.
const FADE_MS = 1200;
G.nodeVal(n => {
  const age = Date.now()/1000 - (n.first_seen || 0);
  if (age < 0 || age > FADE_MS/1000) return n.val;
  return n.val * (age * 1000 / FADE_MS);
});
// Bump the simulation each tick during fade window so visuals refresh.
setInterval(() => G.refresh(), 100);

// ---------- /events: snapshot stream ----------
let lastData = {nodes: [], links: []};
const es = new EventSource('/events');
es.onmessage = (e) => {
  let data;
  try { data = JSON.parse(e.data); }
  catch (err) { console.error('bad snapshot json', err); return; }   // FIXED (R10)

  if (data.empty) {
    document.getElementById('hud').textContent = 'palace not initialized — start kent to populate';
    return;
  }
  lastData = data;
  G.graphData(data);  // 3d-force-graph reuses node objects by id → positions persist (verify R8)
  document.getElementById('n').textContent = data.nodes.length;
  document.getElementById('d').textContent = data.nodes.filter(n => n.type === 'drawer').length;
  document.getElementById('u').textContent = new Date().toLocaleTimeString();

  const errEl = document.getElementById('err');
  if (data.error) { errEl.textContent = 'snapshot: ' + data.error; errEl.style.display = 'block'; }
  else            { errEl.style.display = 'none'; }
  if (data.truncated) {
    document.getElementById('hud').textContent += ' · ⚠ truncated at cap';
  }
};
es.onerror = () => {
  document.getElementById('err').textContent = 'live updates disconnected (auto-reconnecting)';
  document.getElementById('err').style.display = 'block';
};

// ---------- /chat: agent stream ----------
const log = document.getElementById('log');
function append(cls, text) {
  const d = document.createElement('div');
  d.className = 'msg ' + cls;
  d.textContent = text;
  log.appendChild(d);
  log.scrollTop = log.scrollHeight;
  return d;
}

document.getElementById('composer').onsubmit = async (e) => {
  e.preventDefault();
  const input = document.getElementById('input');
  const send = document.getElementById('send');
  const message = input.value.trim();
  if (!message) return;
  input.value = ''; send.disabled = true;
  append('user', '› ' + message);

  let asstLine = null;
  try {
    const resp = await fetch('/chat', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({message}),
    });
    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let buf = '';
    while (true) {
      const {value, done} = await reader.read();
      if (done) break;
      buf += dec.decode(value, {stream: true});
      let idx;
      while ((idx = buf.indexOf('\n\n')) >= 0) {
        const block = buf.slice(0, idx); buf = buf.slice(idx + 2);
        if (!block.startsWith('data: ')) continue;
        const ev = JSON.parse(block.slice(6));
        if (ev.type === 'TextDelta') {
          if (!asstLine) asstLine = append('asst', '');
          asstLine.textContent += ev.data.text || '';
          log.scrollTop = log.scrollHeight;
        } else if (ev.type === 'ToolCallComplete') {
          append('tool', '⚙ ' + ev.data.name + '(' + JSON.stringify(ev.data.arguments).slice(0, 80) + ')');
        } else if (ev.type === 'ToolResult') {
          // Snapshot stream will paint the new nodes; nothing to show here.
        } else if (ev.type === 'AssistantMessageComplete') {
          asstLine = null;
        } else if (ev.type === 'ModelError') {
          append('err', '✗ ' + (ev.data.error || 'model error'));
        }
      }
    }
  } catch (err) {
    append('err', '✗ ' + err.message);
  } finally {
    send.disabled = false; input.focus();
  }
};
</script></body></html>
```

Hover gives `nodeLabel`. Auto-color-by-type via `nodeAutoColorBy('type')`
is the cheaper alternative to explicit colors if we drop the per-kind
drawer palette.

### `agent/cli.py` change

```python
def cmd_viz(args):
    # FIXED (R11): precheck mempalace import. Without this we crash mid-
    # snapshot with a generic ModuleNotFoundError, not an actionable msg.
    try:
        import mempalace.palace_graph  # noqa: F401
    except ImportError:
        print("kent viz needs mempalace. install: uv pip install mempalace",
              file=sys.stderr)
        return 1

    from .viz import start_server
    from .viz.chat import ChatSession
    from .memory.mempalace_store import _DEFAULT_PALACE, _DEFAULT_KENT_HOME

    chat = None
    if not args.read_only:
        # Build the same LLM stack as `kent run` / the REPL.
        cfg = load_config()
        svc_id = cfg.get("service_id") or next(iter(SUPPORTED_SERVICES))
        svc = SUPPORTED_SERVICES.get(svc_id, next(iter(SUPPORTED_SERVICES.values())))
        model = cfg.get("model") or svc["default_model"]
        api_key = resolve_api_key(svc_id, prompt_if_missing=False) or ""
        if not api_key:
            print("no API key configured. run `kent auth` or pass --read-only.",
                  file=sys.stderr)
            return 2
        from .llm import OpenAICompatibleLLM
        llm = OpenAICompatibleLLM(base_url=svc["base_url"], api_key=api_key, model=model)
        chat = ChatSession(llm=llm, palace=_DEFAULT_PALACE, kent_home=_DEFAULT_KENT_HOME)

    mode = "chat+graph" if chat else "graph (read-only)"
    print(f"kent viz [{mode}] → http://127.0.0.1:{args.port}")
    start_server(_DEFAULT_PALACE, _DEFAULT_KENT_HOME,
                 port=args.port, chat_session=chat)
    return 0

# in _build_parser:
p_viz = sub.add_parser("viz", help="Open the live 3D palace viewer + chat")
p_viz.add_argument("--port", type=int, default=8765)
p_viz.add_argument("--read-only", action="store_true",
                   help="disable the chat panel; just render the palace")
p_viz.set_defaults(func=cmd_viz)
```

## Chat panel — how it ties to the graph

The chat panel is wired to `agent.loop.run` through `viz/chat.py`. The
SSE flow is intentionally split into two streams:

- `GET /events` — palace snapshot stream (server → browser, push every
  ~1s when mtimes change). Already specified above.
- `POST /chat` (SSE response) — single agent turn-loop (browser →
  server with the user message; server → browser with text deltas, tool
  calls, results).

The two streams are **not** synchronized. The graph updates *because*
the agent's tool calls (`diary_write`, `set_wing`, `memory_recall`,
`shell`) write to disk, mtime ticks, the snapshot loop notices, and
the new node arrives in `/events` with `first_seen ≈ now`. The browser's
fade-in animation (see below) makes the new node bloom into existence
visibly. No special "agent just made a thing" event needed.

Concurrency rules:

- One `ChatSession` per server (one conversation, one history). The
  `_lock` in `ChatSession.send` serializes concurrent POSTs from a
  single tab (the browser shouldn't but might). A second tab opening
  `/chat` will queue behind the first.
- `MemPalaceStore` is owned by the chat session. Nothing else writes.
- The snapshot reader holds short SQLite read transactions (WAL-safe).
- If the browser tab closes mid-turn, the agent loop keeps running (so
  the turn finishes and any partial work persists); the SSE write just
  fails silently on the next emission.

Read-only mode (`kent viz --read-only`) skips the LLM/auth setup and
passes `chat_session=None`. The frontend should hide the chat column
when the server doesn't expose `/chat` — implemented by a `GET /chat`
returning 405 vs. 404, which the JS probes once on load.

## Animations & visual interest

The default `3d-force-graph` simulation already animates: nodes settle
under force, links spring, the camera responds to drag. Beyond that
baseline, four cheap additions make it feel *alive* without protocol
or backend changes — all live in `index.html`:

1. **Particles flowing along edges.** `linkDirectionalParticles` puts
   small dots travelling `source → target`. Default to 1 per edge, but
   bump tunnels (6 particles, wider) and passive tunnels (3) so
   cross-wing connections visibly *pulse*. This is the single biggest
   "looks alive" win and costs ~3 lines.
2. **Bloom post-processing.** `UnrealBloomPass` from three.js examples
   (CDN-hosted) gives the bright nodes (gold `identity`, cyan `wing`)
   a soft halo. Drawers stay matte. ~2 lines, large visual lift.
3. **Slow camera auto-orbit.** `controls().autoRotate = true` with
   `autoRotateSpeed = 0.25`. Pause on user interaction (mousedown
   listener). Turntable effect when the user looks away.
4. **Fade-in for new nodes.** Server stamps every node with
   `first_seen` (epoch seconds) the first time it sees that id, and
   reuses the stamp on subsequent snapshots. Browser scales `nodeVal`
   from 0 → target over a 1.2s window via the `nodeVal` accessor +
   a 100ms `G.refresh()` interval. Old nodes do nothing. When the chat
   triggers `diary_write`, the user *sees* the new diary node bloom
   into existence next to its wing.

Optional (~20 LOC each, defer if v1 feels rich enough):

5. **Hover highlight.** `onNodeHover(n => …)` dims non-neighbor nodes
   and links to opacity 0.1, brightens the hovered node + neighbors.
   Standard 3d-force-graph demo pattern.
6. **Click to focus.** `onNodeClick(n => …)` recenters the camera
   on the clicked node with a smooth flyTo over 800ms. `controls`
   already supports this.
7. **Particle direction = data flow.** Make `wing → room` edges
   show particles flowing toward the rooms (already default), but
   reverse them on `room → drawer` edges so the visual story is
   "wings spawn rooms, drawers feed back into rooms".

These are all cosmetic and can ship incrementally without changing the
snapshot or chat protocols.

## What we're explicitly NOT building (v1)

- No WebSocket / aiohttp / FastAPI. Stdlib only. (Both SSE streams use
  `http.server` + `ThreadingHTTPServer`.)
- No watchdog observer. mtime polling, with per-file stat for diaries.
- No diff protocol on the snapshot stream. Full-snapshot push, browser
  reuses node ids. (Verify via R8 spike before relying on this.)
- No build step / npm / React / Vite. CDN scripts only.
- No write hooks in `MemPalaceStore` or `DiaryWriter`. Disk is the
  source of truth, polling catches everyone (kent + mempalace MCP +
  manual edits + the chat panel itself).
- No auth / multi-user. localhost-only. The chat panel runs the LLM
  with the same credentials the CLI uses.
- No multi-session chat history persistence beyond the current process.
  Reload the page → new conversation. (The palace, of course, persists.)
- No drawer detail panel, search bar, or drawer-content reader. Hover
  label + chat panel only. A side panel can be added later without
  touching snapshot/transport code.
- No graceful handling of palaces > 5k drawers beyond `truncated: true`.
  Diff/streaming waits until someone hits the limit.

## Open questions for the user

1. **Plan filename.** I named it `living-palace-window.md` to fit the
   `we-want-…` / `lets-assume-…` style. Want a different name?
2. **Auto-launch?** Should `kent` (REPL) print `viz: http://localhost:8765`
   on startup and spawn the server in a background thread, or keep `kent
   viz` strictly a separate command the user runs in another terminal?
   (My instinct: separate command, because the REPL and viz both want
   their own `MemPalaceStore` and double-instantiating is messy.)
3. **Drawer cap.** Is 5000 the right ceiling for v1 before truncation?
   (My palace is empty so I'm guessing at scale.)
4. **Identity edge.** Connecting `identity` to every wing as a hub looks
   nice on small graphs but visually noisy past ~10 wings. **Default
   updated:** drop the radial edges, float `identity` as a labeled node.
   Flip if you want the hub look back.
5. **Node-type scope (R14).** Simplifier suggested cutting `closet`,
   `diary_file`, and `identity` from v1. I kept all three because you
   asked for a *visually interesting* graph and these add structure +
   particle-flow targets. Keep, or trim to wings/rooms/drawers/tunnels
   only?
6. **Chat scope.** Right now the chat is one global session keyed to
   the server process. Reasonable v1, but: do you want a "new chat"
   button (resets `self.history`)? Per-tab sessions? Persistence across
   server restarts? My default: just a "new chat" button later, no
   per-tab, no persistence.
7. **Chat ↔ active wing.** The chat session currently inherits the wing
   recorded in `~/.kent/wings/active`. Should the chat panel expose a
   wing-switcher dropdown (calls `set_wing` tool), or leave it implicit
   to whatever the user last set in the REPL?
8. **R8 spike.** Want me to write the standalone HTML position-persistence
   test as a sanity check before I commit to the full implementation?
   It's ~30 minutes; if `3d-force-graph` doesn't preserve positions, the
   "fade-in for new nodes" trick + the no-jitter UX both need rework.

## Sources

- [vasturiano/3d-force-graph (GitHub)](https://github.com/vasturiano/3d-force-graph)
- [3d-force-graph live demo / API](https://vasturiano.github.io/3d-force-graph/)
- [3d-force-graph on npm](https://www.npmjs.com/package/3d-force-graph)
- [three.js UnrealBloomPass docs](https://threejs.org/docs/#examples/en/postprocessing/UnrealBloomPass)
- [MDN: Server-Sent Events / `EventSource`](https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events/Using_server-sent_events)
- Internal: `agent/loop.py` (event types streamed to the chat panel),
  `agent/memory/mempalace_store.py` (palace location + WAL behaviour),
  `mempalace/palace_graph.py` (wings/rooms/passive-tunnel builder).

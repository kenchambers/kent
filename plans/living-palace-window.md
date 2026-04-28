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

## User-locked decisions (please confirm or edit before I build)

1. **In-process viz vs. standalone process.** Standalone — `kent viz` is its
   own command. Doesn't depend on a kent REPL being open. Reads the palace
   from disk, polls for changes. *(Default; flip if you'd rather have it
   spawned automatically by the REPL.)*
2. **Update mechanism.** mtime-poll on `~/.kent/palace/chroma.sqlite3`,
   `~/.kent/diaries/`, and `~/.mempalace/tunnels.json`, every 1 s. *(Not
   watchdog. Not in-process hooks. mtime polling is dead-simple, dep-free,
   and catches writes from any source — kent itself, mempalace MCP,
   `mempalace mine`, manual edits.)*
3. **Transport.** Server-Sent Events over stdlib `http.server`. *(Not
   WebSockets. SSE is one-way which is all we need; built into the browser
   as `EventSource`; works in stdlib without adding `aiohttp`/`fastapi`.)*
4. **Snapshot strategy.** Full snapshot per change, browser re-applies
   `graphData()`. *(Not diffs. 3d-force-graph reuses node objects by id, so
   positions are preserved. Becomes a problem above ~5k drawers — at that
   point we add a diff layer; not before.)*
5. **No build step.** Single static HTML, 3d-force-graph from
   `cdn.jsdelivr.net`. *(No npm, no Vite, no React.)*
6. **Server scope.** Bind to `127.0.0.1:8765` only, no auth. *(Local dev
   tool. Anyone who can hit localhost can see your palace; that's already
   true for the palace files themselves.)*

## File-by-file changes

```
agent/viz/
  __init__.py            # exports start_server(palace, kent_home, port=8765)
  snapshot.py            # build_snapshot(palace, kent_home) -> {nodes, links}
  server.py              # stdlib http.server + SSE handler + 1s mtime loop
  static/
    index.html           # 1 file, CDN imports, vanilla JS
agent/cli.py             # add `kent viz` subcommand → calls start_server()
tests/viz/
  test_snapshot.py       # snapshot building against a tiny synthetic palace
  test_server.py         # spin up server, GET /events, assert one event
```

That's all of it. No changes to `agent/loop.py`, `agent/memory/*`,
`agent/tools.py`, `agent/critic.py`. The viz is strictly a reader.

### `agent/viz/snapshot.py` — pseudocode

```python
def build_snapshot(palace_path: Path, kent_home: Path) -> dict:
    nodes, links = [], []

    # L0 — identity
    nodes.append({"id": "identity", "type": "identity", ...})

    # Wings + rooms + passive tunnels: reuse mempalace.palace_graph.build_graph()
    rooms, edges = mempalace.palace_graph.build_graph()
    for wing in list_wings(home=kent_home):
        nodes.append({"id": f"wing:{wing}", ...})
        links.append({"source": "identity", "target": f"wing:{wing}"})
    for room, data in rooms.items():
        nodes.append({"id": f"room:{room}", ...})
        for w in data["wings"]:
            links.append({"source": f"wing:{w}", "target": f"room:{room}"})
    for e in edges:  # passive tunnels
        links.append({"source": f"room:{e['room']}", "target": ...})

    # Drawers + closets — single col.get() pass over each collection
    col = get_collection(str(palace_path), create=False)
    batch = col.get(include=["metadatas"], limit=10_000)
    for drawer_id, meta in zip(batch["ids"], batch["metadatas"]):
        nodes.append({"id": f"drawer:{drawer_id}", ...})
        links.append({"source": f"room:{meta['room']}", "target": f"drawer:..."})

    closets = get_closets_collection(str(palace_path), create=False)
    # similar; parse "topic|entities|→ids" lines for drawer back-links

    # Diary files
    for wing in list_wings(home=kent_home):
        for md in (kent_home / "diaries" / wing).glob("*.md"):
            nodes.append({"id": f"diary:{wing}/{md.name}", ...})
            links.append({"source": f"wing:{wing}", "target": ...})

    # Explicit tunnels
    for t in mempalace.palace_graph.list_tunnels():
        links.append({"source": ..., "target": ..., "type": "tunnel"})

    return {"nodes": nodes, "links": links}
```

Hard cap: if drawer count > 5000, drop drawer/closet nodes and render only
wings+rooms+diary_files (return a `truncated: true` flag the UI shows).
Keeps first paint fast even on a huge palace.

### `agent/viz/server.py` — pseudocode

```python
class VizHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/":
            self._serve_static("index.html", "text/html")
        elif self.path == "/snapshot":
            self._json(build_snapshot(palace, kent_home))
        elif self.path == "/events":
            self._sse_loop()
        else:
            self.send_error(404)

    def _sse_loop(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        last_sig = None
        while not self._closed():
            sig = mtime_signature(palace, kent_home)  # tuple of mtimes
            if sig != last_sig:
                snap = build_snapshot(palace, kent_home)
                self.wfile.write(f"data: {json.dumps(snap)}\n\n".encode())
                self.wfile.flush()
                last_sig = sig
            time.sleep(1.0)
```

`mtime_signature` stat()s ~3 paths — cheap. `_closed()` is a `try/except
BrokenPipeError` on the next write. Threading model: `ThreadingHTTPServer`
so multiple browser tabs work.

### `agent/viz/static/index.html` — sketch

```html
<!doctype html>
<html><head><title>kent palace</title>
<style>body{margin:0;background:#000;color:#888;font-family:monospace}</style>
</head><body>
<div id="g"></div>
<div id="hud">drawers: <span id="n">…</span> · last update: <span id="u">…</span></div>
<script src="//cdn.jsdelivr.net/npm/3d-force-graph"></script>
<script>
const G = ForceGraph3D()(document.getElementById('g'))
  .nodeId('id').nodeVal('val').nodeLabel('label')
  .nodeColor(n => n.color)
  .linkColor(l => l.color || '#444')
  .linkOpacity(0.4);

const es = new EventSource('/events');
es.onmessage = (e) => {
  const data = JSON.parse(e.data);
  G.graphData(data);  // 3d-force-graph reuses by id → positions persist
  document.getElementById('n').textContent = data.nodes.length;
  document.getElementById('u').textContent = new Date().toLocaleTimeString();
};
</script></body></html>
```

That's the entire frontend. Hover for `nodeLabel`, click handlers can be
added later. Auto-color-by-type via `nodeAutoColorBy('type')` if we want
to skip explicit colors.

### `agent/cli.py` change

```python
def cmd_viz(args):
    from .viz import start_server
    from .memory.mempalace_store import _DEFAULT_PALACE, _DEFAULT_KENT_HOME
    print(f"kent viz → http://127.0.0.1:{args.port}")
    start_server(_DEFAULT_PALACE, _DEFAULT_KENT_HOME, port=args.port)

# in _build_parser:
p_viz = sub.add_parser("viz", help="Open the live 3D palace viewer")
p_viz.add_argument("--port", type=int, default=8765)
p_viz.set_defaults(func=cmd_viz)
```

## What we're explicitly NOT building (v1)

- No WebSocket / aiohttp / FastAPI. Stdlib only.
- No watchdog observer. mtime polling.
- No diff protocol. Full-snapshot push, browser reuses node ids.
- No build step / npm / React / Vite.
- No write hooks in `MemPalaceStore` or `DiaryWriter`. Disk is the source of
  truth, polling catches everyone (kent + mempalace MCP + manual edits).
- No auth / multi-user. localhost-only.
- No node detail panel, search bar, or drawer-content reader. Hover label
  only. (One-line label is enough for v1; we can add a side panel later
  without touching the snapshot/transport code.)
- No graceful handling of palaces > 5k drawers beyond truncation flag. The
  diff/streaming optimization waits until someone hits it.

## Open questions for the user

1. **Plan filename.** I named it `living-palace-window.md` to fit the
   `we-want-…` / `lets-assume-…` style. Want a different name?
2. **Auto-launch?** Should `kent` (REPL) print `viz: http://localhost:8765`
   on startup and spawn the server in a background thread, or keep `kent
   viz` strictly a separate command the user runs in another terminal?
3. **Drawer cap.** Is 5000 the right ceiling for v1 before truncation?
   (My palace is empty so I'm guessing at scale.)
4. **Identity edge.** Connecting `identity` to every wing as a hub looks
   nice on small graphs but visually noisy past ~10 wings. Drop the
   identity-wing edges and just float `identity` as a labeled node?

## Sources

- [vasturiano/3d-force-graph (GitHub)](https://github.com/vasturiano/3d-force-graph)
- [3d-force-graph live demo / API](https://vasturiano.github.io/3d-force-graph/)
- [3d-force-graph on npm](https://www.npmjs.com/package/3d-force-graph)

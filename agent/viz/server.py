import http.server
import json
import time
from pathlib import Path

from .snapshot import build_snapshot

_STATIC_DIR = Path(__file__).parent / "static"


class VizHandler(http.server.BaseHTTPRequestHandler):
    palace: Path = None      # type: ignore[assignment]
    kent_home: Path = None   # type: ignore[assignment]
    chat_session = None      # ChatSession; None in read-only mode

    def log_message(self, format: str, *args: object) -> None:
        pass  # suppress default access log noise

    def do_GET(self) -> None:
        if self.path == "/":
            self._serve_static("index.html", "text/html; charset=utf-8")
        elif self.path == "/events":
            self._sse_snapshot_loop()
        elif self.path == "/chat":
            # Probe endpoint: 200 if chat is enabled, 405 if read-only.
            if self.chat_session is not None:
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"chat enabled\n")
            else:
                self.send_error(405, "chat disabled (read-only mode)")
        elif self.path == "/pathways":
            self._serve_pathways()
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        if self.path == "/chat":
            self._sse_chat()
        else:
            self.send_error(404)

    # ---------- static ----------

    def _serve_static(self, filename: str, content_type: str) -> None:
        path = _STATIC_DIR / filename
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # ---------- /events: snapshot SSE ----------

    def _sse_snapshot_loop(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        # Shorter debounce keeps the graph responsive while kent is mid-turn
        # (the agent loop writes to the palace several times per second via the
        # sweeper). The MAX_DEFER cap ensures a snapshot always lands within
        # ~2s of any write, even during sustained activity from an external
        # `mempalace mine` ingest — without it, the original 1s recheck could
        # defer snapshots indefinitely.
        DEBOUNCE_S = 0.5
        MAX_DEFER_S = 2.5
        POLL_S = 0.75

        last_sig = None
        first_seen_change_at: float | None = None
        while True:
            sig = mtime_signature(self.palace, self.kent_home)
            if sig != last_sig:
                now = time.time()
                if first_seen_change_at is None:
                    first_seen_change_at = now

                time.sleep(DEBOUNCE_S)
                new_sig = mtime_signature(self.palace, self.kent_home)
                still_moving = new_sig != sig
                deferred_too_long = (time.time() - first_seen_change_at) > MAX_DEFER_S

                if still_moving and not deferred_too_long:
                    continue  # writes ongoing; keep waiting up to MAX_DEFER_S

                snap = build_snapshot(self.palace, self.kent_home)
                if not self._sse_send(snap):
                    return  # client gone
                last_sig = new_sig
                first_seen_change_at = None
            time.sleep(POLL_S)

    def _sse_send(self, payload: dict) -> bool:
        """Write one SSE event. Returns False if client disconnected.
        FIXED (R5): wrap write+flush, not just a check before write."""
        try:
            self.wfile.write(f"data: {json.dumps(payload)}\n\n".encode())
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError):
            return False

    # ---------- /pathways: training pathway overlay ----------

    def _serve_pathways(self) -> None:
        """Return pathway summary JSON for the viz HUD."""
        try:
            from agent.training.scout import read_pathways, PATHWAYS_PATH
            pathways = read_pathways(PATHWAYS_PATH)
            by_wing: dict[str, int] = {}
            by_resource: dict[str, int] = {}
            for p in pathways:
                wing = p.get("wing") or "unknown"
                resource = p.get("resource", "")
                by_wing[wing] = by_wing.get(wing, 0) + 1
                by_resource[resource] = by_resource.get(resource, 0) + 1
            payload = json.dumps({
                "count": len(pathways),
                "by_wing": by_wing,
                "by_resource": by_resource,
                "recent": sorted(pathways, key=lambda x: x.get("created_at", 0), reverse=True)[:5],
            }).encode()
        except Exception as e:
            payload = json.dumps({"error": str(e), "count": 0}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    # ---------- /chat: agent stream ----------

    def _sse_chat(self) -> None:
        if self.chat_session is None:
            self.send_error(405, "chat disabled (read-only mode)")
            return

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

        for ev in self.chat_session.send(message):
            if not self._sse_send({
                "type": ev["type"],
                "data": ev["data"],
            }):
                return  # tab closed; the agent loop keeps running


def mtime_signature(palace: Path, kent_home: Path) -> tuple:
    """FIXED (R6): stat each diary file individually. Directory mtime
    does not change on content edits to existing files."""
    sigs: list[float] = []
    for p in (
        palace / "chroma.sqlite3",
        Path.home() / ".mempalace" / "tunnels.json",
    ):
        try:
            sigs.append(p.stat().st_mtime)
        except FileNotFoundError:
            sigs.append(0.0)

    diaries = kent_home / "diaries"
    if diaries.exists():
        for md in sorted(diaries.rglob("*.md")):
            try:
                sigs.append(md.stat().st_mtime)
            except FileNotFoundError:
                pass
    return tuple(sigs)


def start_server(palace: Path, kent_home: Path, *, port: int = 8765,
                 chat_session=None) -> None:
    VizHandler.palace = palace
    VizHandler.kent_home = kent_home
    VizHandler.chat_session = chat_session  # may be None for read-only mode
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), VizHandler)
    srv.daemon_threads = True
    srv.serve_forever()

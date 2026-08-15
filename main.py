"""
Mirror Link - WebRTC Signaling Server (v3 - peer-joined race fix)

Single WS endpoint. Client sends a "join" message first with room + role.
role: "phone" (host, screen stays on) or "browser" (viewer).

Message protocol (relayed verbatim between the two peers in a room):
  client -> server: {"type": "join", "room": "ABC123", "role": "phone"}
  server -> client: {"type": "joined", "room": "ABC123"}
  server -> client: {"type": "error", "message": "..."}
  server -> other client (if already present): {"type": "peer-joined"}
  phone  -> server -> browser: {"type": "offer", "sdp": {...}}
  browser -> server -> phone : {"type": "answer", "sdp": {...}}
  either -> server -> other  : {"type": "ice-candidate", "candidate": {...}}
  server -> remaining peer   : {"type": "peer-left"}
"""

import json
import secrets
import string
import time
from dataclasses import dataclass, field
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="Mirror Link Signaling Server")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

ROOM_CODE_LENGTH = 6
ROOM_TTL_SECONDS = 600


@dataclass
class Room:
    code: str
    phone_ws: Optional[WebSocket] = None
    browser_ws: Optional[WebSocket] = None
    created_at: float = field(default_factory=time.time)


rooms: dict[str, Room] = {}


def generate_room_code() -> str:
    alphabet = string.ascii_uppercase + string.digits
    alphabet = alphabet.replace("O", "").replace("0", "").replace("I", "").replace("1", "")
    while True:
        code = "".join(secrets.choice(alphabet) for _ in range(ROOM_CODE_LENGTH))
        if code not in rooms:
            return code


def cleanup_stale_rooms():
    now = time.time()
    stale = [
        c for c, r in rooms.items()
        if r.phone_ws is None and r.browser_ws is None
        and now - r.created_at > ROOM_TTL_SECONDS
    ]
    for c in stale:
        rooms.pop(c, None)


@app.post("/room/create")
async def create_room():
    """Phone calls this first to get a room code to display on screen."""
    cleanup_stale_rooms()
    code = generate_room_code()
    rooms[code] = Room(code=code)
    return {"room_code": code}


@app.get("/room/{code}/status")
async def room_status(code: str):
    room = rooms.get(code)
    if not room:
        raise HTTPException(404, "room not found")
    return {
        "exists": True,
        "phone_connected": room.phone_ws is not None,
        "browser_connected": room.browser_ws is not None,
    }


@app.get("/healthz")
async def healthz():
    return {"status": "ok", "active_rooms": len(rooms)}


@app.websocket("/ws")
async def signaling_socket(websocket: WebSocket):
    await websocket.accept()

    room: Optional[Room] = None
    role: Optional[str] = None

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            msg_type = msg.get("type")

            if msg_type == "join":
                room_code = msg.get("room")
                role = msg.get("role")

                if role not in ("phone", "browser"):
                    await websocket.send_text(json.dumps(
                        {"type": "error", "message": "role must be phone or browser"}
                    ))
                    continue

                room = rooms.get(room_code)
                if not room:
                    await websocket.send_text(json.dumps(
                        {"type": "error", "message": "room not found"}
                    ))
                    continue

                if role == "phone":
                    if room.phone_ws is not None:
                        await websocket.send_text(json.dumps(
                            {"type": "error", "message": "phone already connected"}
                        ))
                        room = None
                        continue
                    room.phone_ws = websocket
                else:
                    if room.browser_ws is not None:
                        await websocket.send_text(json.dumps(
                            {"type": "error", "message": "browser already connected"}
                        ))
                        room = None
                        continue
                    room.browser_ws = websocket

                await websocket.send_text(json.dumps({"type": "joined", "room": room_code}))

                # Tell the OTHER peer (if already connected) that this one just
                # joined. This is what the phone waits on before calling
                # webrtc_service.start() / creating its offer — without it,
                # the offer can fire before the browser is listening and gets
                # silently dropped below (peer is None -> no relay).
                other = room.browser_ws if role == "phone" else room.phone_ws
                if other is not None and other is not websocket:
                    await other.send_text(json.dumps({"type": "peer-joined"}))

                continue

            # everything else (offer, answer, ice-candidate) just relays
            if room is None or role is None:
                continue

            peer = room.browser_ws if role == "phone" else room.phone_ws
            if peer is not None:
                await peer.send_text(json.dumps(msg))

    except WebSocketDisconnect:
        pass
    finally:
        if room is not None and role is not None:
            if role == "phone":
                room.phone_ws = None
            else:
                room.browser_ws = None

            other = room.browser_ws if role == "phone" else room.phone_ws
            if other is not None:
                try:
                    await other.send_text(json.dumps({"type": "peer-left"}))
                except Exception:
                    pass

            if room.phone_ws is None and room.browser_ws is None:
                rooms.pop(room.code, None)
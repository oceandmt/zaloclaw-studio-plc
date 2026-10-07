"""Client for the single-session Zalo daemon (bridge/sessiond.mjs).

One Zalo session per account is the hard rule. This module:
  * spawns/reuses one `sessiond.mjs` per account (unix socket),
  * multiplexes commands and listener events over that socket,
  * gives both the campaign sender and the G2G pipeline a SHARED session so
    they can never kick each other off the account.

Usage:
    from sessiond_client import session
    session.cmd("nick1", {"cmd":"send","thread":gid,"type":"group","text":"..."})
    session.subscribe("nick1", handler)          # listener events (G2G)
    session.stream("nick1", {"cmd":"group-members",...}, on_event)
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent
BRIDGE = ROOT / "bridge"
DATA = ROOT / "data"
ACCOUNTS_DIR = DATA / "accounts"
NODE = os.environ.get("ZS_NODE_BIN", "node")

_LOCK = threading.RLock()
_CONNS: dict[str, "_Conn"] = {}


def sock_path(account_id: str) -> str:
    return str(DATA / f"sessiond-{account_id}.sock")


class _Conn:
    def __init__(self, account_id: str, path: str):
        self.account_id = account_id
        self.path = path
        self.sock: socket.socket | None = None
        self._buf = ""
        self._pending: dict[int, dict] = {}
        self._req = 0
        self._subs: list[Callable[[dict], None]] = []
        self._stream: dict[int, Callable[[dict], None]] = {}
        self._ready = threading.Event()
        self.hello: dict = {}
        self._reader: threading.Thread | None = None
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()

    # -- wiring -----------------------------------------------------------
    def connect(self, timeout: float = 20.0) -> None:
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.settimeout(max(0.5, deadline - time.time()))
                s.connect(self.path)
                s.settimeout(None)
                self.sock = s
                self._reader = threading.Thread(target=self._read_loop, name=f"sd-{self.account_id}", daemon=True)
                self._reader.start()
                return
            except Exception as e:  # noqa: BLE001
                last = e
                time.sleep(0.4)
        raise RuntimeError(f"sessiond connect failed: {last}")

    def _read_loop(self) -> None:
        assert self.sock is not None
        try:
            while True:
                chunk = self.sock.recv(65536)
                if not chunk:
                    break
                self._buf += chunk.decode("utf-8", "replace")
                while "\n" in self._buf:
                    line, self._buf = self._buf.split("\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    self._dispatch(obj)
        except Exception:
            pass
        finally:
            self._ready.clear()

    def _dispatch(self, obj: dict) -> None:
        if "id" in obj and isinstance(obj.get("id"), int) and ("ok" in obj):
            with self._lock:
                slot = self._pending.pop(obj["id"], None)
            if slot is not None:
                slot["result"] = obj
                slot["ev"].set()
                return
        if obj.get("event") == "hello":
            self.hello = obj
            if obj.get("ready"):
                self._ready.set()
            return
        if obj.get("event") == "ready":
            self.hello = obj
            self._ready.set()
        ev = obj.get("event")
        rid = obj.get("req_id")
        if ev == "scan" and isinstance(rid, int):
            with self._lock:
                cb = self._stream.get(rid)
            if cb:
                try:
                    cb(obj)
                except Exception:
                    pass
            return  # scan events are progress, not listener events
        for cb in list(self._subs):
            try:
                cb(obj)
            except Exception:
                pass

    # -- api --------------------------------------------------------------
    def send(self, obj: dict) -> int:
        assert self.sock is not None
        with self._lock:
            self._req += 1
            rid = self._req
            obj = {"id": rid, **obj}
        self.sock.sendall((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
        return rid

    def cmd(self, obj: dict, timeout: float = 120.0) -> dict:
        slot = {"ev": threading.Event(), "result": None}
        with self._lock:
            self._req += 1
            rid = self._req
            self._pending[rid] = slot
        try:
            self.sock.sendall((json.dumps({"id": rid, **obj}, ensure_ascii=False) + "\n").encode("utf-8"))  # type: ignore[union-attr]
        except Exception as e:  # noqa: BLE001
            with self._lock:
                self._pending.pop(rid, None)
            return {"ok": False, "error": f"send failed: {e}"}
        if not slot["ev"].wait(timeout):
            with self._lock:
                self._pending.pop(rid, None)
            return {"ok": False, "error": "timeout"}
        return slot["result"] or {"ok": False, "error": "no reply"}

    def stream(self, obj: dict, on_event: Callable[[dict], None], timeout: float = 900.0) -> dict:
        with self._lock:
            self._req += 1
            rid = self._req
        self._stream[rid] = on_event
        try:
            return self.cmd({"req_id": rid, **obj}, timeout=timeout)
        finally:
            self._stream.pop(rid, None)

    def subscribe(self, handler: Callable[[dict], None]) -> None:
        with self._lock:
            if handler not in self._subs:
                self._subs.append(handler)

    def unsubscribe(self, handler: Callable[[dict], None]) -> None:
        with self._lock:
            if handler in self._subs:
                self._subs.remove(handler)

    @property
    def ready(self) -> bool:
        return self._ready.is_set()


class _Sessions:
    """Per-account sessiond manager (spawn/reuse, multiplex)."""

    def __init__(self) -> None:
        self._procs: dict[str, subprocess.Popen] = {}
        self._spawn_spec: dict[str, str] = {}

    def _alive(self, account_id: str) -> bool:
        p = self._procs.get(account_id)
        return bool(p and p.poll() is None)

    def conn(self, account_id: str) -> _Conn:
        account_id = account_id or "default"
        with _LOCK:
            live = _CONNS.get(account_id)
            if live and live.sock is not None and live.ready:
                return live
            # try attaching to an existing socket first (another app may own it)
            c = _Conn(account_id, sock_path(account_id))
            if not live and Path(sock_path(account_id)).exists():
                try:
                    c.connect(timeout=8)
                    _CONNS[account_id] = c
                    return c
                except Exception:
                    pass
            raise RuntimeError("sessiond not running")

    def ensure(self, account_id: str, watch_spec: str = "all", wait: float = 40.0) -> _Conn:
        account_id = account_id or "default"
        with _LOCK:
            live = _CONNS.get(account_id)
            if live and live.sock is not None and live.ready:
                return live
            # attach to a socket someone else already started
            if Path(sock_path(account_id)).exists():
                c = _Conn(account_id, sock_path(account_id))
                try:
                    c.connect(timeout=6)
                    _CONNS[account_id] = c
                    return c
                except Exception:
                    pass
            # spawn our own
            if not self._alive(account_id):
                DATA.mkdir(parents=True, exist_ok=True)
                try:
                    os.unlink(sock_path(account_id))
                except FileNotFoundError:
                    pass
                cmd = [NODE, str(BRIDGE / "sessiond.mjs"),
                       "--account", account_id,
                       "--sock", sock_path(account_id),
                       "--creds-dir", str(ACCOUNTS_DIR),
                       "--watch-groups", watch_spec]
                log_path = DATA / f"sessiond-{account_id}.log"
                lf = open(log_path, "ab", buffering=0)
                self._procs[account_id] = subprocess.Popen(
                    cmd, cwd=str(ROOT), stdout=lf, stderr=lf, start_new_session=True)
            c = _Conn(account_id, sock_path(account_id))
            c._proc = self._procs.get(account_id)
            c.connect(timeout=wait)
            if not c.ready:
                try:
                    c._ready.wait(wait)
                except Exception:
                    pass
            _CONNS[account_id] = c
            return c

    def stop(self, account_id: str) -> None:
        with _LOCK:
            c = _CONNS.pop(account_id, None)
            if c and c.sock is not None:
                try:
                    c.cmd({"cmd": "stop"}, timeout=5)
                except Exception:
                    pass
            p = self._procs.pop(account_id, None)
            if p and p.poll() is None:
                try:
                    p.terminate()
                except Exception:
                    pass

    def status(self, account_id: str) -> dict:
        p = self._procs.get(account_id)
        return {"spawned": bool(p and p.poll() is None), "sock": sock_path(account_id)}


session = _Sessions()


def cmd(account_id: str, obj: dict, timeout: float = 120.0) -> dict:
    return session.ensure(account_id).cmd(obj, timeout=timeout)


def stream(account_id: str, obj: dict, on_event, timeout: float = 900.0) -> dict:
    return session.ensure(account_id).stream(obj, on_event, timeout=timeout)


def subscribe(account_id: str, handler) -> _Conn:
    c = session.ensure(account_id)
    c.subscribe(handler)
    return c

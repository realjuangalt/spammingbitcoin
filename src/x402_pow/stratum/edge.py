from __future__ import annotations

"""
Pool Stratum V1 edge — Agents (Bitaxe, miners) connect here.

We are the Pool. Work is validated, Sites are credited, pool-grade shares
are forwarded to the Upstream pool.
"""

import json
import logging
import secrets
import socket
import struct
import threading
import time
from dataclasses import dataclass, field

from x402_pow.config import get_settings
from x402_pow.pool.metrics import metrics
from x402_pow.publishers import service as publishers
from x402_pow.stratum.upstream import (
    UpstreamJob,
    digest_to_difficulty,
    share_meets_pool_difficulty,
    sha256d,
    upstream_client,
)

log = logging.getLogger("x402_pow.stratum.edge")


def _parse_site_id(worker: str) -> str | None:
    """Worker forms: pub_xxx.bitaxe | pub_xxx | anything → default Site."""
    settings = get_settings()
    w = (worker or "").strip()
    if w.startswith("pub_"):
        site = w.split(".", 1)[0]
        if publishers.get_by_id(site):
            return site
    default = settings.stratum_default_site_id.strip()
    if default and publishers.get_by_id(default):
        return default
    return None


@dataclass
class MinerSession:
    conn: socket.socket
    # addr kept only for socket lifecycle — never logged or exported
    addr: tuple
    extranonce1: str = ""
    authorized: bool = False
    worker: str = ""
    site_id: str | None = None
    difficulty: float = 1024.0
    lock: threading.Lock = field(default_factory=threading.Lock)
    shares_accepted: int = 0
    shares_rejected: int = 0
    connected_at: float = field(default_factory=time.time)

    def send(self, obj: dict) -> None:
        data = (json.dumps(obj) + "\n").encode()
        with self.lock:
            self.conn.sendall(data)


class StratumEdge:
    """TCP Stratum V1 listener for the Pool."""

    def __init__(self) -> None:
        self._running = False
        self._thread: threading.Thread | None = None
        self._sock: socket.socket | None = None
        self._sessions: list[MinerSession] = []
        self._sessions_lock = threading.Lock()
        self._accepts = 0
        self._rejects = 0
        self._started_at: float | None = None

    @property
    def status(self) -> dict:
        settings = get_settings()
        with self._sessions_lock:
            miners = [
                {
                    "worker": s.worker or "(authorizing)",
                    "siteId": s.site_id,
                    "accepted": s.shares_accepted,
                    "rejected": s.shares_rejected,
                    "authorized": s.authorized,
                    "difficulty": s.difficulty,
                    "connectedSec": round(time.time() - s.connected_at, 1),
                }
                for s in self._sessions
            ]
        return {
            "listening": self._running and self._sock is not None,
            "bind": f"{settings.stratum_listen_host}:{settings.stratum_listen_port}",
            "public": f"{settings.public_stratum_host}:{settings.public_stratum_port}",
            "edgeDifficulty": settings.stratum_edge_difficulty,
            "miners": len(miners),
            "minerDetail": miners,
            "sharesAccepted": self._accepts,
            "sharesRejected": self._rejects,
            "startedAt": self._started_at,
        }

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._running = True
        upstream_client.on_job(self._broadcast_job)
        self._thread = threading.Thread(target=self._serve, name="stratum-edge", daemon=True)
        self._thread.start()
        log.info("Pool Stratum edge starting")

    def stop(self) -> None:
        self._running = False
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass

    def _serve(self) -> None:
        settings = get_settings()
        host = settings.stratum_listen_host
        port = settings.stratum_listen_port
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
        sock.listen(64)
        sock.settimeout(1.0)
        self._sock = sock
        self._started_at = time.time()
        log.info("Pool Stratum listening on %s:%s", host, port)
        while self._running:
            try:
                conn, addr = sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            t = threading.Thread(target=self._handle, args=(conn, addr), daemon=True)
            t.start()

    def _handle(self, conn: socket.socket, addr: tuple) -> None:
        settings = get_settings()
        session = MinerSession(conn=conn, addr=addr, difficulty=settings.stratum_edge_difficulty)
        with self._sessions_lock:
            self._sessions.append(session)
        log.info("Agent connected (socket open)")
        buf = b""
        conn.settimeout(300)
        try:
            while self._running:
                try:
                    chunk = conn.recv(65536)
                except socket.timeout:
                    continue
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, _, buf = buf.partition(b"\n")
                    if not line.strip():
                        continue
                    try:
                        msg = json.loads(line.decode())
                    except json.JSONDecodeError:
                        continue
                    self._dispatch(session, msg)
        except (OSError, ConnectionError) as e:
            log.info("Agent disconnected worker=%s (%s)", session.worker or "?", type(e).__name__)
        finally:
            with self._sessions_lock:
                if session in self._sessions:
                    self._sessions.remove(session)
            try:
                conn.close()
            except OSError:
                pass

    def _dispatch(self, session: MinerSession, msg: dict) -> None:
        method = msg.get("method")
        mid = msg.get("id")
        params = msg.get("params") or []

        if method == "mining.subscribe":
            self._on_subscribe(session, mid)
        elif method == "mining.authorize":
            self._on_authorize(session, mid, params)
        elif method == "mining.submit":
            self._on_submit(session, mid, params)
        elif method == "mining.extranonce.subscribe":
            session.send({"id": mid, "result": True, "error": None})
        elif method == "mining.configure":
            # Bitaxe asks for version-rolling; advertise a standard mask.
            mask = "1fffe000"
            session.send(
                {
                    "id": mid,
                    "result": {
                        "version-rolling": True,
                        "version-rolling.mask": mask,
                    },
                    "error": None,
                }
            )
            session.send({"id": None, "method": "mining.set_version_mask", "params": [mask]})
        elif method:
            if mid is not None:
                session.send({"id": mid, "result": True, "error": None})

    def _on_subscribe(self, session: MinerSession, mid) -> None:
        job = upstream_client.latest_job
        # Use upstream extranonce1 so shares can be forwarded as-is.
        if job:
            session.extranonce1 = job.extranonce1
            en2_size = job.extranonce2_size
        else:
            session.extranonce1 = secrets.token_hex(4)
            en2_size = 4
        # [[notifications], extranonce1, extranonce2_size]
        session.send(
            {
                "id": mid,
                "result": [
                    [["mining.notify", session.extranonce1], ["mining.set_difficulty", "1"]],
                    session.extranonce1,
                    en2_size,
                ],
                "error": None,
            }
        )
        session.send({"id": None, "method": "mining.set_difficulty", "params": [session.difficulty]})
        if job:
            self._send_notify(session, job, clean=True)

    def _on_authorize(self, session: MinerSession, mid, params: list) -> None:
        worker = str(params[0]) if params else ""
        session.worker = worker
        session.site_id = _parse_site_id(worker)
        session.authorized = True
        session.send({"id": mid, "result": True, "error": None})
        log.info("Agent authorized worker=%s site=%s", worker, session.site_id)
        job = upstream_client.latest_job
        if job:
            self._send_notify(session, job, clean=False)

    def _send_notify(self, session: MinerSession, job: UpstreamJob, *, clean: bool) -> None:
        # Same job fields as Upstream so mining.submit can be forwarded.
        session.send(
            {
                "id": None,
                "method": "mining.notify",
                "params": [
                    job.job_id,
                    job.prevhash,
                    job.coinb1,
                    job.coinb2,
                    job.merkle_branches,
                    job.version,
                    job.nbits,
                    job.ntime,
                    clean,
                ],
            }
        )

    def _broadcast_job(self, job: UpstreamJob) -> None:
        with self._sessions_lock:
            sessions = list(self._sessions)
        for s in sessions:
            if not s.authorized:
                continue
            en1_changed = bool(s.extranonce1) and s.extranonce1 != job.extranonce1
            s.extranonce1 = job.extranonce1
            try:
                if en1_changed:
                    s.send(
                        {
                            "id": None,
                            "method": "mining.set_extranonce",
                            "params": [job.extranonce1, job.extranonce2_size],
                        }
                    )
                self._send_notify(s, job, clean=True)
            except OSError:
                pass

    def _on_submit(self, session: MinerSession, mid, params: list) -> None:
        # params: [worker, job_id, extranonce2, ntime, nonce] or + version_bits (Bitaxe)
        if not session.authorized or len(params) < 5:
            session.send({"id": mid, "result": False, "error": [20, "Not authorized", ""]})
            session.shares_rejected += 1
            self._rejects += 1
            metrics.record_share(
                source="stratum",
                worker=session.worker,
                site_id=session.site_id,
                difficulty=session.difficulty,
                accepted=False,
            )
            return

        worker, job_id, extranonce2, ntime, nonce_hex = params[:5]
        version_bits = int(str(params[5]), 16) if len(params) >= 6 else 0
        job = upstream_client.latest_job
        if job is None or job.job_id != str(job_id):
            session.send({"id": mid, "result": False, "error": [21, "Job not found", ""]})
            session.shares_rejected += 1
            self._rejects += 1
            log.info(
                "share reject stale worker=%s job=%s have=%s",
                worker,
                job_id,
                None if job is None else job.job_id,
            )
            metrics.record_share(
                source="stratum",
                worker=str(worker),
                site_id=session.site_id,
                difficulty=session.difficulty,
                accepted=False,
            )
            return

        try:
            nonce = int(str(nonce_hex), 16)
            en2 = str(extranonce2)
            en1 = session.extranonce1 or job.extranonce1
            prefix = job.build_prefix76(
                en2,
                extranonce1=en1,
                ntime=str(ntime),
                version_bits=version_bits,
            )
            header = prefix + struct.pack("<I", nonce & 0xFFFFFFFF)
            digest = sha256d(header)
        except Exception as e:
            log.warning("share build error: %s", e)
            session.send({"id": mid, "result": False, "error": [20, "Invalid share", ""]})
            session.shares_rejected += 1
            self._rejects += 1
            metrics.record_share(
                source="stratum",
                worker=str(worker),
                site_id=session.site_id,
                difficulty=session.difficulty,
                accepted=False,
            )
            return

        if not share_meets_pool_difficulty(digest, session.difficulty):
            session.send({"id": mid, "result": False, "error": [23, "Low difficulty", ""]})
            session.shares_rejected += 1
            self._rejects += 1
            log.info(
                "share reject lowdiff worker=%s digest=%s need_diff=%s verbits=%08x",
                worker,
                digest[::-1].hex()[:16],
                session.difficulty,
                version_bits,
            )
            metrics.record_share(
                source="stratum",
                worker=str(worker),
                site_id=session.site_id,
                difficulty=session.difficulty,
                accepted=False,
            )
            return

        # Accept at Pool edge
        session.send({"id": mid, "result": True, "error": None})
        session.shares_accepted += 1
        self._accepts += 1

        site_id = session.site_id or _parse_site_id(str(worker))
        if site_id:
            try:
                publishers.credit_share(site_id, 1)
            except Exception:
                log.exception("credit_share failed")

        forwarded = False
        # Forward only when Agent en1 matches Upstream and share meets Upstream diff
        if en1 == job.extranonce1 and share_meets_pool_difficulty(digest, job.difficulty):
            upstream_client.submit_share(
                upstream_job_id=job.job_id,
                extranonce2=en2,
                ntime=str(ntime),
                nonce=nonce,
            )
            forwarded = True
            log.info(
                "share accepted+forwarded worker=%s site=%s nonce=%s digest=%s",
                worker,
                site_id,
                nonce_hex,
                digest[::-1].hex()[:16],
            )
        else:
            log.info(
                "share accepted (edge only) worker=%s site=%s nonce=%s digest=%s",
                worker,
                site_id,
                nonce_hex,
                digest[::-1].hex()[:16],
            )

        metrics.record_share(
            source="stratum",
            worker=str(worker),
            site_id=site_id,
            difficulty=session.difficulty,
            accepted=True,
            forwarded=forwarded,
            hash_difficulty=digest_to_difficulty(digest),
        )


stratum_edge = StratumEdge()

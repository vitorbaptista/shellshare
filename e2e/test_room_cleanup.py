"""
E2E tests for room TTL cleanup.

These need a server with a short TTL, so each test spawns its own via the
dedicated_server fixture instead of using the shared one.
"""

import subprocess
import time

import requests

from conftest import (
    CLI_COMMAND,
    SocketListener,
    broadcast_message,
    poll_until,
    random_id,
)


def _room_has_history(server_url, room, marker, settle=1.0):
    """Join the room as a late viewer and report whether history contains marker.

    The join must be confirmed (usersCount received) before the verdict
    counts: history is emitted before usersCount on join, so a confirmed
    join with no marker means the room really has no history - not that
    we were too slow connecting (a real risk on Windows CI runners).
    """
    listener = SocketListener(room, server_url=server_url)
    listener.connect()  # waits for join confirmation (usersCount)
    try:
        if not poll_until(lambda: listener.get_last_user_count() >= 1, timeout=15):
            raise TimeoutError(
                f"viewer connect to {server_url} was never confirmed"
            )
        deadline = time.time() + settle
        while time.time() < deadline:
            if marker in listener.get_accumulated_messages():
                return True
            time.sleep(0.1)
        return False
    finally:
        listener.disconnect()


class TestRoomTtlCleanup:
    def test_inactive_room_is_evicted(self, dedicated_server):
        server = dedicated_server("--cleanup-interval", 1, "--room-ttl", 2)
        room = f"ttl-{random_id()}"

        assert broadcast_message(server.url, room, "pass-a", "ephemeral-data") == 200
        assert _room_has_history(server.url, room, "ephemeral-data")
        time.sleep(5)  # > TTL (2s) + checking join + cleanup interval (1s)
        assert not _room_has_history(
            server.url, room, "ephemeral-data", settle=0.5
        ), "Room history was not evicted after TTL"

    def test_only_the_broadcaster_refreshes_the_ttl(self, dedicated_server):
        """Watching a dead broadcast does not keep its room alive.

        Rooms outlive their broadcaster now, so TTL eviction is the only
        teardown left. Neither an attached viewer nor a polled
        /r/:room.bin read counts as activity - otherwise a forgotten
        browser tab or a polling agent would pin a finished broadcast
        forever. Only the broadcaster's own frames (and its 30s
        keepalive pings) refresh the room.
        """
        server = dedicated_server("--cleanup-interval", 1, "--room-ttl", 2)
        room = f"ttl-{random_id()}"

        assert broadcast_message(server.url, room, "pass-a", "polled-data") == 200

        def watched_and_polled_but_gone():
            # A fresh join every round, because a join is the only viewer
            # event that ever touched the room - one join could not keep
            # a room alive past its TTL on its own
            listener = SocketListener(room, server_url=server.url)
            listener.connect()
            try:
                return requests.get(f"{server.url}/r/{room}.bin").status_code == 404
            finally:
                listener.disconnect()

        # Join and poll faster than the TTL, for longer than the TTL
        assert poll_until(
            watched_and_polled_but_gone, timeout=10, interval=0.5
        ), "room outlived its TTL while only being watched and polled"

    def test_an_attached_but_silent_broadcaster_keeps_its_room(
        self, dedicated_server
    ):
        """A quiet producer must not lose the room out from under itself.

        `journalctl -f` on an idle unit sends nothing for long stretches,
        and reads no longer refresh anything, so the room survives only
        because the client keeps pinging. Without those pings the room is
        evicted mid-broadcast and the next line of output fails fatally
        with an authorization error.
        """
        server = dedicated_server("--cleanup-interval", 1, "--room-ttl", 2)
        room = f"ttl-{random_id()}"

        proc = subprocess.Popen(
            CLI_COMMAND + ["--json", "-s", server.url, "-r", room, "-W", "pw"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            proc.stdin.write("hello\n")
            proc.stdin.flush()
            # Silent for well past the TTL, stdin still open
            time.sleep(6)
            assert (
                requests.get(f"{server.url}/r/{room}.bin").status_code == 200
            ), "room was evicted while its broadcaster was still attached"
        finally:
            proc.stdin.close()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

    def test_evicted_room_password_is_released(self, dedicated_server):
        server = dedicated_server("--cleanup-interval", 1, "--room-ttl", 2)
        room = f"ttl-{random_id()}"

        assert broadcast_message(server.url, room, "original-pass", "x") == 200
        # Wrong password is rejected while the room is alive
        assert broadcast_message(server.url, room, "other-pass", "y") == 401

        # After eviction, a different password can claim the room
        assert poll_until(
            lambda: broadcast_message(server.url, room, "other-pass", "y") == 200,
            timeout=15,
        ), "Password was not released after room eviction"

    def test_active_room_survives_cleanup_cycles(self, dedicated_server):
        server = dedicated_server("--cleanup-interval", 1, "--room-ttl", 3)
        room = f"ttl-{random_id()}"

        assert broadcast_message(server.url, room, "pass-a", "persistent-data") == 200

        # Keep the room active well past several cleanup cycles
        for _ in range(6):
            time.sleep(1)
            assert broadcast_message(server.url, room, "pass-a", "tick") == 200

        # Still owned by the original password, history intact
        assert broadcast_message(server.url, room, "other-pass", "steal") == 401
        assert _room_has_history(server.url, room, "persistent-data")

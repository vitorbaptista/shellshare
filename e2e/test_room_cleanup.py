"""
E2E tests for room TTL cleanup.

These need a server with a short TTL, so each test spawns its own via the
dedicated_server fixture instead of using the shared one on :3000.
"""

import time

from conftest import SocketListener, broadcast_message, poll_until, random_id


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
                f"Socket.IO join to {server_url} was never confirmed"
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
        # NOTE: checking history joins the room, which itself refreshes
        # last_activity - so wait passively and check exactly once.
        time.sleep(5)  # > TTL (2s) + checking join + cleanup interval (1s)
        assert not _room_has_history(
            server.url, room, "ephemeral-data", settle=0.5
        ), "Room history was not evicted after TTL"

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

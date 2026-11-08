import asyncio

import pytest

from app.services import secure_runtime as rt
from test_secure_runtime import FakeLock


def test_overlapping_list_detail_wait_for_owner_instead_of_503():
    clock = [0.0]
    lock = FakeLock()
    runtime = rt.SecureRuntime(lock, check_admission=lambda: None, monotonic=lambda: clock[0])

    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []
        responses = {}

        async def app(scope, receive, send):
            assert lock.held
            calls.append(scope["path"])
            if scope["path"] == "/uploads/room":
                entered.set()
                await release.wait()
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"{}"})

        async def request(path):
            async def receive():
                await asyncio.Future()

            async def send(message):
                if message["type"] == "http.response.start":
                    responses[path] = message["status"]

            await rt.SecureBoundary(app, runtime)({"type": "http", "headers": [], "path": path}, receive, send)

        owner = asyncio.create_task(request("/uploads/room"))
        await entered.wait()
        pending = []
        for path in ("/families/room/records", "/records/image", "/families"):
            pending.append(asyncio.create_task(request(path)))
            await asyncio.sleep(0)
        # Reproduce a normal >2s owner without slowing the suite by wall-clock sleeps.
        await asyncio.sleep(0.02)
        clock[0] = 3.2
        await asyncio.sleep(0.05)
        release.set()
        await asyncio.gather(owner, *pending)
        assert responses == {path: 200 for path in (
            "/uploads/room", "/families/room/records", "/records/image", "/families")}
        assert calls == ["/uploads/room", "/families/room/records", "/records/image", "/families"]
        assert lock.releases == 4

    asyncio.run(run())


@pytest.mark.parametrize("outcome", ["cancel", "timeout", "closed"])
def test_waiter_cleanup_and_fresh_admission(outcome, monkeypatch):
    lock = FakeLock()
    admission = [True]

    def check():
        if not admission[0]:
            raise rt.SecureUnavailable()

    runtime = rt.SecureRuntime(lock, check_admission=check)
    if outcome == "timeout":
        monkeypatch.setattr(rt, "_ADMISSION_QUEUE_TIMEOUT", 0.02)

    async def run():
        entered, release = asyncio.Event(), asyncio.Event()

        async def owner():
            async with runtime.operation_guard():
                entered.set()
                await release.wait()

        async def waiter():
            async with runtime.operation_guard():
                pytest.fail("cancelled, expired or closed waiter must not enter")

        active = asyncio.create_task(owner())
        await entered.wait()
        queued = asyncio.create_task(waiter())
        await asyncio.sleep(0)
        if outcome == "cancel":
            queued.cancel()
            with pytest.raises(asyncio.CancelledError):
                await queued
        elif outcome == "timeout":
            with pytest.raises(rt.SecureUnavailable):
                await queued
        else:
            admission[0] = False
            release.set()
            with pytest.raises(rt.SecureUnavailable):
                await queued
        release.set()
        await active
        assert runtime._admission_pending == 0
        assert not lock.held
        admission[0] = True
        async with runtime.operation_guard():
            assert lock.held

    asyncio.run(run())


def test_queue_capacity_rejects_before_native_lock_or_admission(monkeypatch):
    monkeypatch.setattr(rt, "_ADMISSION_QUEUE_LIMIT", 2)
    calls = []
    runtime = rt.SecureRuntime(FakeLock(), check_admission=lambda: calls.append(True))

    async def run():
        entered, release = asyncio.Event(), asyncio.Event()

        async def owner():
            async with runtime.operation_guard():
                entered.set()
                await release.wait()

        async def waiter():
            async with runtime.operation_guard():
                pass

        active = asyncio.create_task(owner())
        await entered.wait()
        queued = asyncio.create_task(waiter())
        await asyncio.sleep(0)
        with pytest.raises(rt.SecureUnavailable):
            async with runtime.operation_guard():
                pytest.fail("full queue entered")
        assert calls == [True]
        release.set()
        await asyncio.gather(active, queued)
        assert calls == [True, True]
        assert runtime._admission_pending == 0

    asyncio.run(run())


def test_external_native_lock_still_has_two_second_deadline():
    clock = [0.0]
    lock = FakeLock()
    lock.held = True
    calls = []
    runtime = rt.SecureRuntime(lock, check_admission=lambda: calls.append(True), monotonic=lambda: clock[0])

    async def run():
        async def waiter():
            async with runtime.operation_guard():
                pytest.fail("external lock bypassed")

        queued = asyncio.create_task(waiter())
        await asyncio.sleep(0.01)
        clock[0] = 2.01
        with pytest.raises(rt.SecureUnavailable):
            await queued
        assert calls == []
        assert lock.held and lock.releases == 0
        assert runtime._admission_pending == 0

    asyncio.run(run())


def test_disconnected_queued_request_never_sends_protected_response():
    lock = FakeLock()
    runtime = rt.SecureRuntime(lock, check_admission=lambda: None)

    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        sent = []

        async def owner():
            async with runtime.operation_guard():
                entered.set()
                await release.wait()

        async def app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"protected"})

        async def receive():
            return {"type": "http.disconnect"}

        async def send(message):
            sent.append(message)

        active = asyncio.create_task(owner())
        await entered.wait()
        queued = asyncio.create_task(rt.SecureBoundary(app, runtime)(
            {"type": "http", "headers": []}, receive, send))
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(active, queued)
        assert sent == []
        assert not lock.held
        assert runtime._admission_pending == 0

    asyncio.run(run())

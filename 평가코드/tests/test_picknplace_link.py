"""통신 부품 PlcLink 시험. 가짜 Transport로 장비 없이 돈다."""

import asyncio
import contextlib
import io
import time
import unittest
from types import SimpleNamespace

from picknplace_full.link import PlcLink
from picknplace_full.transport import AutoTransport
from tests.picknplace_fakes import FakeTransport, until

RESULTS = ("보냄", "접수", "거부", "미확인")


def auto_transport(present, inner, probes):
    """present의 포트는 모두 응답 있음이고, 열면 inner를 쓰는 AutoTransport. 시험한 포트는 probes에 남는다."""

    async def probe(port):
        probes.append(port)
        return "응답 있음"

    async def open_(port):
        return inner

    auto = AutoTransport(list_ports=lambda: [SimpleNamespace(device=d, description="USB") for d in present],
                         probe=probe)
    auto._open = open_
    return auto


class LinkTest(unittest.IsolatedAsyncioTestCase):
    async def start(self, transport, **kw):
        link = PlcLink(transport, **{"interval": 0.01, "fast_interval": 0.005, "timeout": 0.2, **kw})
        self.events = []
        link.on_event(self.events.append)
        await link.start()
        self.addAsyncCleanup(link.close)
        return link

    def texts(self, tag):
        return [e.text for e in self.events if e.tag == tag]

    async def test_reads_and_updates_status(self):
        fake = FakeTransport(raw=2)
        link = await self.start(fake)
        await until(lambda: link.status is not None, 0.05)
        self.assertEqual(link.status.raw, 2)
        self.assertTrue(link.comm_ok)
        await until(lambda: fake.reads >= 3)
        self.assertEqual(self.texts("상태"), ["---- → 0002 (정지 · 열림 · 정상)"])

    async def test_first_read_is_immediate(self):
        fake = FakeTransport(raw=2)
        link = await self.start(fake, interval=1.0)
        await until(lambda: link.status is not None, 0.1)
        self.assertEqual(link.status.raw, 2)

    async def test_send_accepted(self):
        fake = FakeTransport(raw=2, respond={9991: 1}.get)
        link = await self.start(fake)
        r = await link.send(9991)
        self.assertEqual(r.result, "접수")
        self.assertEqual(r.status.robot, 0)
        self.assertEqual(link.status.robot, 0)
        self.assertEqual(fake.writes, [9991])
        self.assertIn("0002 → 0001 (동작 중 · 열림 · 정상)", self.texts("상태"))

    async def test_send_rejected(self):
        fake = FakeTransport(raw=2, respond={3333: 7}.get)
        link = await self.start(fake)
        r = await link.send(3333)
        self.assertEqual(r.result, "거부")
        self.assertEqual(r.status.plc, 1)

    async def test_write_failure_is_unconfirmed_and_not_resent(self):
        fake = FakeTransport(raw=2, respond={9991: 1}.get, fail_write=True)
        link = await self.start(fake)
        r = await link.send(9991)
        self.assertEqual(r.result, "미확인")
        self.assertIn("쓰기", r.reason)
        fake.fail_write = False  # 이제 쓰면 기록된다. 다시 보내면 안 된다
        await asyncio.sleep(0.3)
        self.assertEqual(fake.writes, [])
        self.assertEqual(fake.write_tries, 1)

    async def test_mailbox_not_cleared_is_unconfirmed(self):
        fake = FakeTransport(raw=2, respond=lambda code: None)
        link = await self.start(fake)
        r = await link.send(9991)
        self.assertEqual(r.result, "미확인")
        self.assertIn("PLC 처리 확인 안 됨", r.reason)
        self.assertEqual(fake.writes, [9991])

    async def test_previous_mailbox_blocks_write(self):
        fake = FakeTransport(raw=2, mailbox=9991, respond=lambda code: None)
        link = await self.start(fake)
        r = await link.send(2222)
        self.assertEqual(r.result, "미확인")
        self.assertEqual(r.reason, "이전 명령이 처리되지 않음")
        self.assertEqual(fake.writes, [])
        self.assertEqual(self.texts("상태")[0], "---- → 0002 (정지 · 열림 · 정상) · 우편함 9991")

    async def test_estop_jumps_queue(self):
        fake = FakeTransport(raw=2, respond=lambda code: 2, process_after_reads=5)
        link = await self.start(fake)
        first = asyncio.create_task(link.send(9991))
        await until(lambda: fake.writes)
        later = [asyncio.create_task(link.send(code)) for code in (3333, 9995)]
        results = await asyncio.gather(first, *later)
        self.assertEqual(fake.writes, [9991, 9995, 3333])
        self.assertEqual([r.result for r in results], ["접수"] * 3)

    async def test_read_failure_keeps_status(self):
        fake = FakeTransport(raw=2)
        link = await self.start(fake)
        await until(lambda: link.comm_ok)
        fake.fail_reads = 5
        await until(lambda: not link.comm_ok)
        self.assertEqual(link.status.raw, 2)
        await until(lambda: link.comm_ok)
        self.assertEqual(link.status.raw, 2)
        comm = self.texts("통신")
        self.assertEqual(len(comm), 2)
        self.assertIn("실패", comm[0])
        self.assertIn("회복", comm[1])

    async def test_wait_uses_fresh_reads_only(self):
        fake = FakeTransport(raw=2)
        link = await self.start(fake)
        await until(lambda: link.comm_ok)
        fake.fail_reads = 5
        waiting = asyncio.create_task(link.wait(lambda s: s.raw == 2, 1.0))
        await until(lambda: not link.comm_ok)
        self.assertEqual(link.status.raw, 2)
        self.assertFalse(waiting.done())  # 남은 status는 pred를 만족하지만 새 읽기가 아니다
        s = await waiting
        self.assertEqual(s.raw, 2)
        self.assertEqual(fake.fail_reads, 0)
        self.assertTrue(link.comm_ok)
        t0 = time.monotonic()
        self.assertIsNone(await link.wait(lambda s: s.raw == 1, 0.1))
        self.assertGreaterEqual(time.monotonic() - t0, 0.07)  # 바로 None을 주지 않는다 (여유 2 × 16 ms)

    async def test_send_rejects_out_of_range(self):
        fake = FakeTransport(raw=2, respond=lambda code: 2)
        link = await self.start(fake)
        for code in (0, 65536):
            with self.assertRaises(ValueError):
                await link.send(code)
        await asyncio.sleep(0.05)
        self.assertEqual(fake.writes, [])
        self.assertEqual(fake.write_tries, 0)
        self.assertEqual(self.texts("보냄"), [])

    async def test_events_for_send(self):
        fake = FakeTransport(raw=2, respond={9991: 1}.get)
        link = await self.start(fake)
        await link.send(9991)
        sent = [e for e in self.events if e.tag in RESULTS]
        self.assertEqual([e.tag for e in sent], ["보냄", "접수"])
        for e in sent:
            self.assertIn("9991", e.text)

    async def test_unknown_raw(self):
        fake = FakeTransport(raw=0, respond={9991: 0}.get)
        link = await self.start(fake)
        await until(lambda: link.status is not None)
        self.assertFalse(link.status.valid)
        self.assertIn("알 수 없는 코드", self.texts("상태")[0])
        r = await link.send(9991)  # 우편함은 비었지만 접수·거부를 가릴 수 없다
        self.assertEqual((r.result, r.reason), ("미확인", "상태 알 수 없는 코드 0"))

    async def test_close_ends_pending_send_and_wait(self):
        fake = FakeTransport(raw=2, respond=lambda code: None)
        link = await self.start(fake, timeout=2.0)
        sending = asyncio.create_task(link.send(9991))
        waiting = asyncio.create_task(link.wait(lambda s: False, 5.0))
        await until(lambda: fake.writes)
        await link.close()
        r = await asyncio.wait_for(sending, 1.0)
        self.assertEqual((r.result, r.reason), ("미확인", "통신 종료"))
        self.assertEqual([e.tag for e in self.events if e.tag in RESULTS], ["보냄", "미확인"])  # 보냄마다 결과 줄
        self.assertTrue(self.texts("미확인")[0].startswith("9991 통신 종료"))
        self.assertIsNone(await asyncio.wait_for(waiting, 1.0))
        r = await link.send(2222)
        self.assertEqual((r.result, r.reason), ("미확인", "통신 종료"))
        self.assertEqual(fake.writes, [9991])

    async def test_auto_transport_ports_and_rescan_on_lost(self):
        present, probes = ["COM5"], []
        inner = FakeTransport(raw=2)
        auto = auto_transport(present, inner, probes)
        link = await self.start(auto)
        await until(lambda: link.comm_ok)
        inner.fail_reads = 3  # 포트가 목록에 남아 있으면 다시 찾지 않는다
        await until(lambda: inner.fail_reads == 0 and link.comm_ok)
        self.assertEqual(len(self.texts("통신")), 2)  # 실패와 회복
        self.assertEqual(probes, ["COM5"])
        self.assertEqual(len(self.texts("포트")), 1)
        present[:] = ["COM8"]  # 쓰던 포트가 목록에서 사라짐
        inner.fail_reads = 1
        await until(lambda: auto.port == "COM8" and link.comm_ok)
        self.assertEqual(probes, ["COM5", "COM8"])
        self.assertTrue(await link.rescan())
        ports = self.texts("포트")
        self.assertEqual(len(ports), 3)
        self.assertIn("COM5", ports[0])
        self.assertIn("COM8", ports[1])
        self.assertFalse(await PlcLink(FakeTransport()).rescan())

    async def test_raising_callback_does_not_stop_comm(self):
        fake = FakeTransport(raw=2, respond={9995: 3}.get)

        def broken(event):
            raise OSError("로그 파일 쓰기 실패")

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            link = await self.start(fake)
            link.on_event(broken)  # 첫 읽기 전에 붙는다. 이벤트마다 예외를 낸다
            await until(lambda: fake.reads >= 3)
            r = await asyncio.wait_for(link.send(9995), 1.0)
        self.assertEqual(r.result, "접수")
        self.assertEqual(fake.writes, [9995])
        self.assertTrue(link.comm_ok)
        self.assertEqual(link.status.raw, 3)
        self.assertEqual([e.tag for e in self.events if e.tag in RESULTS], ["보냄", "접수"])
        self.assertIn("로그 파일 쓰기 실패", err.getvalue())

    async def test_comm_task_crash_fails_pending_sends(self):
        inner = FakeTransport(raw=2)  # 처리하지 않는다
        auto = auto_transport(["COM5"], inner, [])

        def lost():
            raise RuntimeError("포트 목록 오류")

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            link = await self.start(auto, timeout=2.0)
            await until(lambda: link.comm_ok)
            auto.lost = lost
            sends = [asyncio.create_task(link.send(code)) for code in (9991, 3333)]
            await until(lambda: inner.writes)  # 9991 처리 중, 3333 대기
            inner.fail_reads = 100  # 읽기 실패 → lost()가 예외 → 통신 작업이 끝난다
            results = await asyncio.wait_for(asyncio.gather(*sends), 1.0)
        self.assertEqual([(r.result, r.code) for r in results], [("미확인", 9991), ("미확인", 3333)])
        self.assertIn("포트 목록 오류", results[0].reason)
        self.assertFalse(link.comm_ok)
        r = await asyncio.wait_for(link.send(9995), 1.0)  # 멈춘 뒤에 보내도 기다리지 않는다
        self.assertEqual(r.result, "미확인")
        self.assertEqual(inner.writes, [9991])
        self.assertEqual(sum("멈춤" in t for t in self.texts("통신")), 1)
        self.assertEqual([e.tag for e in self.events if e.tag in RESULTS], ["보냄", "미확인"])
        self.assertIn("포트 목록 오류", err.getvalue())


if __name__ == "__main__":
    unittest.main()

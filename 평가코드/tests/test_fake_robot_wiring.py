"""배선 JSON 클라이언트 (docs/handoff/virtual-wiring.md 2.3). 가짜 PLC 서버는 포트 0으로 연다."""

import asyncio
import socket
import time
import unittest

from fake_robot.wiring import WiringClient
from tests.fake_robot_samples.doubles import FakePlc, until

ALL_OFF = {"DI0": 0, "DI1": 0, "DI2": 0, "DI3": 0}


class WiringClientTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.plc = FakePlc()
        await self.plc.start()
        self.client = WiringClient(port=self.plc.port)
        self.task = asyncio.create_task(self.client.run())
        await until(lambda: self.client.connected and self.plc.got)

    async def asyncTearDown(self):
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)
        await self.plc.stop()

    async def test_receives_outputs_and_sends_inputs_on_change(self):
        self.assertEqual(self.plc.got[0][1], {"type": "inputs", "P00004": 0, "P00005": 0})  # 붙자마자 보냄
        self.plc.send('{"type":"outputs","P00044":1,"P00045":0,"P00046":0,"P00047":1}')
        await until(lambda: self.client.di == {"DI0": 1, "DI1": 0, "DI2": 0, "DI3": 1})
        n, t0 = len(self.plc.got), time.monotonic()
        self.client.set_do({"DO0": 1, "DO1": 0})
        await until(lambda: len(self.plc.got) > n)
        at, msg = self.plc.got[n]
        self.assertEqual(msg, {"type": "inputs", "P00004": 1, "P00005": 0})
        self.assertLess(at - t0, 0.2)                      # 0.5초 주기를 기다리지 않고 바로

    async def test_resends_every_half_second(self):
        await until(lambda: len(self.plc.got) >= 4)
        gaps = [b[0] - a[0] for a, b in zip(self.plc.got[1:], self.plc.got[2:])]
        for gap in gaps:
            self.assertGreater(gap, 0.4)
            self.assertLess(gap, 0.7)

    async def test_ignores_bad_lines_and_keeps_the_connection(self):
        for line in ["깨진 줄 {", "[1, 2]", '{"type":"other","P00047":1}',
                     '{"type":"outputs","P00044":1,"P00045":7,"P09999":1}']:
            self.plc.send(line)
        await until(lambda: self.client.di["DI0"] == 1)
        self.assertEqual(self.client.di, {"DI0": 1, "DI1": 0, "DI2": 0, "DI3": 0})  # 7은 0/1이 아니므로 무시
        self.assertTrue(self.client.connected)
        self.assertEqual(len(self.plc.accepted), 1)        # 다시 붙지 않았다

    async def test_disconnect_reads_all_di_off_and_reconnects_after_1s(self):
        self.plc.send('{"type":"outputs","P00044":1,"P00045":1,"P00046":1,"P00047":1}')
        await until(lambda: self.client.di["DI3"] == 1)
        t0 = time.monotonic()
        self.plc.drop()
        await until(lambda: not self.client.connected)
        self.assertEqual(self.client.di, ALL_OFF)
        await until(lambda: len(self.plc.accepted) == 2)
        self.assertGreater(self.plc.accepted[1] - t0, 0.9)
        self.assertLess(self.plc.accepted[1] - t0, 2.0)


class StartOrderTest(unittest.IsolatedAsyncioTestCase):
    async def test_robot_started_before_plc_connects_later(self):
        with socket.socket() as s:                         # 비어 있는 포트를 하나 얻는다
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        client = WiringClient(port=port)
        task = asyncio.create_task(client.run())
        await asyncio.sleep(0.3)                           # 접속 실패 뒤 1초 대기 중
        self.assertFalse(client.connected)
        self.assertEqual(client.di, ALL_OFF)
        plc = FakePlc()
        await plc.start(port)
        try:
            await until(lambda: client.connected, timeout=2.0)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await plc.stop()


if __name__ == "__main__":
    unittest.main()

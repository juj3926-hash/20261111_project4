"""PICKNPLACE_FULL 상태 해석과 가상 장비 시험."""

import asyncio
import time
import unittest

from picknplace_full.model import ModelTransport, VirtualPlant
from picknplace_full.status import decode


async def wait_for(pred, timeout=1.0):
    end = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > end:
            raise AssertionError("시간 안에 조건이 맞지 않음")
        await asyncio.sleep(0.005)


def opened(plant):
    """첫 스캔이 돈 뒤 첫 그리퍼 열기가 끝났다."""
    return not plant.plc.first and plant.bit("G_OPENING") == 0


class DecodeTest(unittest.TestCase):
    def test_decode_table(self):
        for raw in range(1, 13):
            n = raw - 1
            s = decode(raw, 0, 0.0)
            self.assertTrue(s.valid)
            self.assertEqual((s.robot, s.gripper, s.plc), (n % 3, n // 3 % 2, n // 6))
        self.assertEqual(decode(7, 0, 0).robot, 0)
        self.assertEqual((decode(7, 0, 0).gripper, decode(7, 0, 0).plc), (0, 1))
        s = decode(11, 0, 0)
        self.assertEqual((s.robot, s.gripper, s.plc), (1, 1, 1))

    def test_decode_invalid(self):
        for raw in (0, 13):
            s = decode(raw, 0, 0.0)
            self.assertFalse(s.valid)
            self.assertEqual((s.robot, s.gripper, s.plc), (None, None, None))
            self.assertIn("알 수 없는 코드", s.text())

    def test_status_text(self):
        self.assertEqual(decode(5, 0, 0).text(), "정지 · 닫힘 · 정상")
        self.assertEqual(decode(1, 0, 0).text(), "동작 중 · 열림 · 정상")


class PlantTest(unittest.IsolatedAsyncioTestCase):
    async def test_plant_check_command(self):
        plant = VirtualPlant(speed=20)
        await plant.start()
        try:
            await wait_for(lambda: opened(plant), 1.0)
            plant.write(1, 2222)
            await wait_for(lambda: plant.read() == (2, 0), 0.5)
        finally:
            await plant.stop()

    async def test_plant_runs_cycle_fast(self):
        plant = VirtualPlant(speed=20)
        await plant.start()
        try:
            await wait_for(lambda: opened(plant), 1.0)
            plant.write(1, 1111)
            await wait_for(lambda: plant.read()[1] == 0, 1.0)
            plant.write(1, 9991)
            await wait_for(lambda: plant.read()[0] == 5, 1.0)
        finally:
            await plant.stop()

    async def test_model_transport_shape(self):
        t = ModelTransport(VirtualPlant(speed=20))
        self.assertTrue(await t.connect())
        try:
            raw, box = await t.read()
            self.assertIsInstance(raw, int)
            self.assertIsInstance(box, int)
            await wait_for(lambda: opened(t.plant), 1.0)
            await t.write_command(2222)
            await wait_for(lambda: t.plant.read()[1] == 0, 1.0)
            self.assertFalse(t.lost())
        finally:
            await t.close()


if __name__ == "__main__":
    unittest.main()

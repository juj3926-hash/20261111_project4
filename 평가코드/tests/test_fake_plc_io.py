"""가짜 PLC 입출력: Modbus TCP와 배선 JSON (docs/handoff/virtual-wiring.md 2.2, 2.3). 포트는 0만 연다."""

import asyncio
import json
import re
import struct
import tempfile
import time
import unittest
from collections import deque
from pathlib import Path

from pymodbus.client import AsyncModbusTcpClient

from fake_plc.engine import Engine
from fake_plc.modbus import start_modbus
from fake_plc.wiring import Wiring


async def until(pred, timeout=2.0):
    """pred()가 참이 될 때까지 기다린다. 넉넉한 제한 시간 안에 안 되면 실패."""
    end = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > end:
            raise AssertionError("%.1f초 안에 조건이 맞지 않음" % timeout)
        await asyncio.sleep(0.01)


async def raw(port, unit, pdu):
    """MBAP 머리를 붙여 PDU 하나를 보내고, 응답 프레임 전체를 받는다."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(struct.pack(">HHHB", 7, 0, len(pdu) + 1, unit) + pdu)
    head = await reader.readexactly(7)
    body = await reader.readexactly(struct.unpack(">H", head[4:6])[0] - 1)
    writer.close()
    await writer.wait_closed()
    return head + body


class ModbusTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.words = {}
        self.log = deque(maxlen=100)
        self.server, self.port = await start_modbus(self.words, self.log, port=0)
        self.client = AsyncModbusTcpClient("127.0.0.1", port=self.port, timeout=2, retries=0)
        await self.client.connect()

    async def asyncTearDown(self):
        self.client.close()
        await self.server.shutdown()

    async def test_registers_are_d_1000_plus_n(self):
        self.words["D01000"] = 2
        await self.client.write_register(1, 1111, device_id=1)             # FC06
        await self.client.write_registers(2, [5, 6], device_id=1)           # FC16
        r = await self.client.read_holding_registers(0, count=4, device_id=1)   # FC03
        self.assertEqual(r.registers, [2, 1111, 5, 6])
        self.assertEqual(self.words, {"D01000": 2, "D01001": 1111, "D01002": 5, "D01003": 6})

    async def test_other_function_codes_get_01(self):
        for pdu in (b"\x01\x00\x00\x00\x01", b"\x04\x00\x00\x00\x01", b"\x05\x00\x00\xff\x00",
                    b"\x08\x00\x00\x12\x34", b"\x2b\x0e\x01\x00"):
            with self.subTest(fc=pdu[0]):
                self.assertEqual((await raw(self.port, 1, pdu))[7:], bytes([pdu[0] | 0x80, 0x01]))

    async def test_other_unit_gets_0b(self):
        self.assertEqual((await raw(self.port, 2, b"\x03\x00\x00\x00\x01"))[7:], b"\x83\x0b")

    async def test_log_lines(self):
        await self.client.write_register(1, 1111, device_id=1)
        await self.client.read_holding_registers(0, count=2, device_id=1)
        await raw(self.port, 1, b"\x01\x00\x00\x00\x01")
        await raw(self.port, 2, b"\x03\x00\x00\x00\x01")
        self.assertEqual([e["text"] for e in self.log],
                         ["FC06 주소 1 값 1111 → 정상", "FC03 주소 0 수량 2 → 0, 1111",
                          "FC01 → 예외 01", "국번 2 FC03 주소 0 수량 1 → 예외 0B"])
        self.assertTrue(all(re.fullmatch(r"\d\d:\d\d:\d\d\.\d{3}", e["at"]) for e in self.log))


def outputs(p44, p45=0, p46=0, p47=0):
    """virtual-wiring.md 2.3의 PLC → 로봇 한 줄."""
    return b'{"type":"outputs","P00044":%d,"P00045":%d,"P00046":%d,"P00047":%d}\n' % (p44, p45, p46, p47)


class WiringTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        (Path(tmp.name) / "w.ld.txt").write_text("OUT P00044 := P00004\nOUT P00047 := P00005", encoding="utf-8")
        self.engine = Engine()
        self.engine.open(Path(tmp.name) / "w.ld.txt")
        self.wiring = Wiring(self.engine)
        self.port = await self.wiring.start(port=0)
        self.reader, self.writer = await asyncio.open_connection("127.0.0.1", self.port)

    async def asyncTearDown(self):
        self.writer.close()
        await self.writer.wait_closed()
        await self.wiring.close()

    async def line(self):
        return await asyncio.wait_for(self.reader.readline(), 2)

    async def nothing(self):
        with self.assertRaises(TimeoutError):
            await asyncio.wait_for(self.reader.readline(), 0.1)

    def send(self, data):
        self.writer.write(data)

    def scanned(self, dev):
        """한 스캔 돌린 뒤 그 디바이스 값."""
        self.engine.scan()
        return self.engine.plc.bit(dev)

    async def test_outputs_on_connect_change_and_every_half_second(self):
        self.assertEqual(await self.line(), outputs(0))           # 붙으면 바로 (STOP이라 0)
        self.engine.forced["P00004"] = 1
        self.engine.run()
        self.engine.scan()
        self.wiring.publish(10.0)
        self.assertEqual(await self.line(), outputs(1))           # 바뀌면 그 스캔에
        self.wiring.publish(10.3)
        await self.nothing()                                      # 그대로면 0.5초까지 안 보낸다
        self.wiring.publish(10.5)
        self.assertEqual(await self.line(), outputs(1))           # 0.5초마다 다시
        self.engine.stop()
        self.wiring.publish(10.6)
        self.assertEqual(await self.line(), outputs(0))           # STOP이면 출력 0

    async def test_inputs_and_ignored_lines(self):
        await self.line()
        self.send(b'{"type":"inputs","P00004":1,"P00005":0}\n')
        await until(lambda: self.engine.wired == {"P00004": 1, "P00005": 0})
        self.send(b"not json\n" + b'{"type":"other","P00004":0}\n' + b"[1, 2]\n"
                  + b'{"type":"inputs","P00004":7,"P00044":1,"X":2}\n' + b"x" * 70000 + b"\n"
                  + b"[" * 30000 + b"]" * 30000 + b"\n")       # 너무 깊은 JSON(RecursionError)도 깨진 줄
        self.send(b'{"type":"inputs","P00005":1}\n')              # 연결은 살아 있다
        await until(lambda: self.engine.wired == {"P00004": 1, "P00005": 1})

    async def test_disconnect_reads_inputs_as_zero(self):
        await self.line()
        self.engine.run()
        self.send(b'{"type":"inputs","P00004":1,"P00005":1}\n')
        await until(lambda: self.scanned("P00004") == 1 and self.engine.plc.bit("P00005") == 1)
        self.writer.close()
        await self.writer.wait_closed()
        # 스캔이 0을 읽는다 (스캔은 들어온 입력만 쓰므로, 끊기면 0을 넣어 줘야 한다)
        await until(lambda: self.scanned("P00004") == 0 and self.engine.plc.bit("P00005") == 0)
        self.assertEqual(self.engine.plc.bit("P00044"), 0)       # 래더(OUT P00044 := P00004)도 0을 본다


if __name__ == "__main__":
    unittest.main()

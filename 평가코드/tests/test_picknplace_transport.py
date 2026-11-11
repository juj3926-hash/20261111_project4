"""연결 대상 시험. 실제 COM 포트는 열지 않는다."""

import asyncio
import socket
import unittest
from types import SimpleNamespace
from unittest import mock

from pymodbus.server import ModbusTcpServer
from pymodbus.simulator import DataType, SimData, SimDevice

from picknplace_full.model import ModelTransport
from picknplace_full import transport as tr
from picknplace_full.status import TransportError
from picknplace_full.transport import AutoTransport, SerialTransport, TcpTransport, make_transport


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def ports(*pairs):
    return lambda: [SimpleNamespace(device=d, description=t) for d, t in pairs]


class MakeTransportTest(unittest.TestCase):
    def test_make_transport_targets(self):
        self.assertIsInstance(make_transport("auto"), AutoTransport)
        t = make_transport("serial:COM7")
        self.assertIsInstance(t, SerialTransport)
        self.assertEqual(t.name, "COM7")
        self.assertIsInstance(make_transport("tcp:127.0.0.1:5020"), TcpTransport)
        self.assertIsInstance(make_transport("model"), ModelTransport)
        for bad in ("COM7", "tcp:x", ""):
            with self.assertRaises(ValueError):
                make_transport(bad)


class AutoTransportTest(unittest.IsolatedAsyncioTestCase):
    async def test_auto_prefers_usb_and_picks_first_responder(self):
        tried, events = [], []
        answers = {"COM5": "응답 없음", "COM6": "응답 있음", "COM1": "응답 있음"}

        async def probe(port):
            tried.append(port)
            return answers[port]

        lp = ports(("COM1", "통신 포트"), ("COM5", "Silicon Labs CP210x"), ("COM6", "USB-SERIAL CH340"))
        t = AutoTransport(list_ports=lp, probe=probe, emit=lambda k, m: events.append((k, m)))
        t._open = _no_open
        self.assertTrue(await t.connect())
        self.assertEqual(t.port, "COM6")
        self.assertEqual(tried, ["COM5", "COM6"])
        self.assertEqual([r[0] for r in t.results], ["COM5", "COM6"])
        self.assertEqual(events[-1], ("포트", "COM6 (USB-SERIAL CH340): 응답 있음 → 사용"))

    async def test_auto_none_found(self):
        async def probe(port):
            return "응답 없음"

        t = AutoTransport(list_ports=ports(("COM1", "a"), ("COM2", "b")), probe=probe)
        self.assertFalse(await t.connect())
        self.assertIsNone(t.port)
        self.assertEqual([r[0] for r in t.results], ["COM1", "COM2"])

    async def test_auto_rescans_when_port_vanishes(self):
        present = [("COM5", "USB")]

        async def probe(port):
            return "응답 있음"

        t = AutoTransport(list_ports=lambda: [SimpleNamespace(device=d, description=x) for d, x in present], probe=probe)
        t._open = _no_open
        await t.connect()
        self.assertEqual(t.port, "COM5")
        self.assertFalse(t.lost())
        present[:] = [("COM8", "USB")]
        self.assertTrue(t.lost())
        self.assertTrue(await t.rescan())
        self.assertEqual(t.port, "COM8")


async def _no_open(port):
    return None


class LostTest(unittest.IsolatedAsyncioTestCase):
    async def _connected(self, present):
        async def probe(port):
            return "응답 있음"

        t = AutoTransport(list_ports=lambda: [SimpleNamespace(device=d, description="USB") for d in present], probe=probe)
        t._open = _no_open
        await t.connect()
        return t

    async def test_poll_failure_with_port_listed_is_not_lost(self):
        t = await self._connected(["COM5"])
        t._inner = SimpleNamespace(lost=lambda: True)
        self.assertFalse(t.lost())

    async def test_port_removed_is_lost(self):
        present = ["COM5"]
        t = await self._connected(present)
        present.clear()
        self.assertTrue(t.lost())

    async def test_no_port_selected_is_not_lost(self):
        t = AutoTransport(list_ports=ports(), probe=None)
        self.assertFalse(t.lost())


class FakeClient:
    def __init__(self, connect=True, reply=None, exc=None):
        self._connect, self._reply, self._exc = connect, reply, exc
        self.closed = False

    async def connect(self):
        return self._connect

    async def read_holding_registers(self, *a, **k):
        if self._exc:
            raise self._exc
        return self._reply

    def close(self):
        self.closed = True


class ProbeTest(unittest.IsolatedAsyncioTestCase):
    async def probe(self, client):
        with mock.patch.object(tr, "_serial_client", lambda port, timeout: client):
            result = await tr.probe_port("COM9")
        self.assertTrue(client.closed)
        return result

    async def test_open_failure(self):
        self.assertEqual(await self.probe(FakeClient(connect=False)), tr.OPEN_FAIL)
        self.assertEqual(tr.OPEN_FAIL, "열기 실패 (다른 프로그램이 사용 중일 수 있음 — QModMaster 확인)")

    async def test_exception_response(self):
        r = tr.ExceptionResponse(3, 2)
        self.assertEqual(await self.probe(FakeClient(reply=r)), "예외 응답 코드 2")

    async def test_no_answer(self):
        self.assertEqual(await self.probe(FakeClient(exc=TimeoutError())), "응답 없음")
        r = SimpleNamespace(isError=lambda: True)
        self.assertEqual(await self.probe(FakeClient(reply=r)), "응답 없음")

    async def test_ok(self):
        r = SimpleNamespace(isError=lambda: False)
        self.assertEqual(await self.probe(FakeClient(reply=r)), "응답 있음")


class RescanAndClientTest(unittest.IsolatedAsyncioTestCase):
    async def test_open_failure_continues_to_next_port(self):
        async def probe(port):
            return "응답 있음"

        async def open_(port):
            if port == "COM5":
                raise TransportError("x")

        t = AutoTransport(list_ports=ports(("COM5", "USB a"), ("COM6", "USB b")), probe=probe)
        t._open = open_
        self.assertTrue(await t.connect())
        self.assertEqual(t.port, "COM6")
        self.assertEqual(t.results, [("COM5", tr.OPEN_FAIL), ("COM6", "응답 있음")])

    def test_serial_client_kwargs(self):
        with mock.patch.object(tr, "AsyncModbusSerialClient") as cls:
            tr._serial_client("COM7", 1.5)
        cls.assert_called_once_with(
            "COM7", framer=tr.FramerType.RTU, baudrate=9600, bytesize=8, parity="N", stopbits=1,
            timeout=1.5, retries=0, reconnect_delay=0)


class TcpTransportTest(unittest.IsolatedAsyncioTestCase):
    async def test_tcp_transport_reads_and_writes(self):
        device = SimDevice(1, [SimData(0, values=[5, 0, 0, 0], datatype=DataType.REGISTERS)])
        port = free_port()
        server = ModbusTcpServer(device, address=("127.0.0.1", port))
        task = asyncio.create_task(server.serve_forever())
        t = TcpTransport("127.0.0.1", port, timeout=1.0)
        try:
            for _ in range(100):
                if server.is_active():
                    break
                await asyncio.sleep(0.05)
            self.assertTrue(await t.connect())
            self.assertEqual(await t.read(), (5, 0))
            await t.write_command(3333)
            self.assertEqual(await server.async_getValues(1, 3, 1, 1), [3333])
        finally:
            await t.close()
            await server.shutdown()
            await task

    async def test_tcp_transport_failure_raises(self):
        t = TcpTransport("127.0.0.1", free_port(), timeout=0.3)
        try:
            with self.assertRaises(TransportError):
                await t.read()
        finally:
            await t.close()


if __name__ == "__main__":
    unittest.main()

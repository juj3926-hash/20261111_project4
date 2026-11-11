"""가짜 로봇 시험용 대역: 가짜 PLC(배선 서버만, 참조 래더 + Modbus), 서버 시험 틀(Rig). 포트는 모두 0(OS가 고른 포트)."""

import asyncio
import json
import socket
import threading
import time
import urllib.request
from pathlib import Path
from urllib.error import HTTPError

from pymodbus.constants import ExcCodes
from pymodbus.server import ModbusTcpServer
from pymodbus.simulator import DataType, SimData, SimDevice

from fake_robot.core import Robot
from fake_robot.server import FakeRobot, fixed_loop, make_server
from fake_robot.wiring import WiringClient
from tools.xgwx.ladder_src import load
from tools.xgwx.sim import Plc

LADDER = Path(__file__).resolve().parents[2] / "ladder" / "PICKNPLACE_FULL.ld.txt"
FAST = dict(start_delay=0.05, move_time=0.2, stop_time=0.05)   # 시험용 짧은 시간
J = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]


async def until(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > end:
            raise AssertionError("시간 안에 조건이 참이 되지 않음")
        await asyncio.sleep(0.01)


class FakePlc:
    """배선 JSON 서버 역할만 한다. 받은 메시지를 (시각, dict)로 모으고, 붙은 로봇에게 줄을 보낸다."""

    async def start(self, port=0):
        self.got, self.writers, self.accepted = [], [], []
        self.server = await asyncio.start_server(self._client, "127.0.0.1", port)
        self.port = self.server.sockets[0].getsockname()[1]

    async def _client(self, reader, writer):
        self.writers.append(writer)
        self.accepted.append(time.monotonic())
        while line := await reader.readline():
            self.got.append((time.monotonic(), json.loads(line)))

    def send(self, text):
        for w in self.writers:
            w.write(text.encode() + b"\n")

    def outputs(self, di0=0, di1=0, di2=0, di3=0):
        self.send(json.dumps({"type": "outputs", "P00044": di0, "P00045": di1, "P00046": di2, "P00047": di3}))

    def do_changes(self):
        """받은 (DO0, DO1) 값의 변화 순서. 같은 값이 이어지면 하나로 본다."""
        seq = []
        for _, msg in self.got:
            pair = (msg["P00004"], msg["P00005"])
            if not seq or seq[-1] != pair:
                seq.append(pair)
        return seq

    def drop(self):
        for w in self.writers:
            w.close()
        self.writers.clear()

    async def stop(self):
        self.drop()
        self.server.close()
        await self.server.wait_closed()


class PlcDouble:
    """가짜 PLC 대역: 참조 래더를 tools.xgwx.sim.Plc로 스캔한다. 하위 프로젝트 1(가짜 PLC)이 오기 전의 자리다.

    - 배선 JSON 서버(로봇 쪽, docs/handoff/virtual-wiring.md 2.3): P00044~P00047을 바뀔 때와 0.5초마다 보내고
      로봇의 P00004·P00005를 받는다. 로봇이 끊기면 둘 다 0으로 본다.
    - Modbus TCP 서버(PC 쪽, 2.2): 국번 1, 보유 레지스터 주소 n ↔ D(1000+n), FC 03·06·16. 쓰기는 다음 스캔 전에 들어간다.
    - speed배로 돈다: 실제 10 ms마다 래더 시간 10 ms × speed만큼 스캔한다.
    """

    OUTPUTS = ("P00044", "P00045", "P00046", "P00047")

    def __init__(self, speed=1.0):
        source = load(LADDER)
        self.sym, self.plc, self.speed = source.symbols, Plc(source.rungs), speed
        self.inputs = {"P00004": 0, "P00005": 0}                 # 로봇 DO0·DO1
        self.history = [(0, 0)]                                  # 받은 (DO0, DO1)의 변화
        self.writers, self.sent, self.sent_at = [], None, 0.0

    def bit(self, symbol):
        return self.plc.bit(self.sym[symbol])

    async def start(self):
        self.wiring = await asyncio.start_server(self._robot, "127.0.0.1", 0)
        self.wiring_port = self.wiring.sockets[0].getsockname()[1]
        with socket.socket() as s:                               # ModbusTcpServer에 줄 빈 포트
            s.bind(("127.0.0.1", 0))
            self.modbus_port = s.getsockname()[1]
        device = SimDevice(1, [SimData(0, values=[0, 0], datatype=DataType.REGISTERS)], action=self._modbus)
        self.modbus = ModbusTcpServer(device, address=("127.0.0.1", self.modbus_port))
        self.tasks = [asyncio.create_task(self.modbus.serve_forever()), asyncio.create_task(fixed_loop(self._scan))]
        await until(self.modbus.is_active)

    async def stop(self):
        for w in self.writers:
            w.close()
        self.wiring.close()
        await self.wiring.wait_closed()
        await self.modbus.shutdown()
        for t in self.tasks:
            t.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    def _scan(self, dt):
        self.plc.scan(self.inputs, round(dt * 1000 * self.speed))
        out = [self.plc.bit(device) for device in self.OUTPUTS]
        now = time.monotonic()
        if out != self.sent or now - self.sent_at >= 0.5:
            self.sent, self.sent_at = out, now
            line = json.dumps({"type": "outputs", **dict(zip(self.OUTPUTS, out))}).encode() + b"\n"
            for w in self.writers:
                w.write(line)

    def _set_inputs(self, do0, do1):
        self.inputs = {"P00004": do0, "P00005": do1}
        if self.history[-1] != (do0, do1):
            self.history.append((do0, do1))

    async def _robot(self, reader, writer):
        self.writers.append(writer)
        self.sent = None                                         # 붙자마자 지금 출력을 보낸다
        try:
            while line := await reader.readline():
                msg = json.loads(line)
                if msg.get("type") == "inputs":
                    self._set_inputs(msg["P00004"], msg["P00005"])
        finally:
            self.writers.remove(writer)
            writer.close()
            self._set_inputs(0, 0)

    async def _modbus(self, function_code, start_address, address, count, registers, values):
        if function_code not in (3, 6, 16):
            return ExcCodes.ILLEGAL_FUNCTION
        if values is None:                                       # 읽기: 지금 D01000·D01001
            registers[0], registers[1] = self.plc.word("D01000"), self.plc.word("D01001")
        else:                                                    # 쓰기: 스캔 사이에 들어간다
            for i, value in enumerate(values):
                self.plc.write_word(f"D{1000 + address + i:05d}", value)
        return None


def wait_for(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > end:
            raise AssertionError("시간 안에 조건이 참이 되지 않음")
        time.sleep(0.01)


class Rig:
    """이벤트 루프 스레드에 가짜 PLC(배선 서버), 가짜 로봇, HTTP 서버를 포트 0으로 띄운다. 요청은 시험 스레드에서 한다."""

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        self.call(self._start())

    def call(self, coro, timeout=5):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def run(self, fn, *args):
        """루프 스레드에서 fn(*args)."""
        async def go():
            return fn(*args)
        return self.call(go())

    async def _start(self):
        self.plc = FakePlc()
        await self.plc.start()
        self.wiring = WiringClient(port=self.plc.port)
        self.robot = FakeRobot(self.wiring, Robot(**FAST))
        self.tasks = [asyncio.create_task(self.wiring.run()), asyncio.create_task(fixed_loop(self.robot.tick))]
        self.http = make_server(self.robot, self.loop, 0)
        self.port = self.http.server_port
        threading.Thread(target=self.http.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def request(self, method, path, body=None, host=None, headers=None):
        """(상태 코드, JSON이면 dict 아니면 bytes, Content-Type)"""
        data = body if body is None or isinstance(body, bytes) else json.dumps(body).encode()
        h = {"Host": host or f"127.0.0.1:{self.port}"}
        if data is not None:
            h["Content-Type"] = "application/json"
        h.update(headers or {})
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, headers=h, method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                status, raw, ctype = r.status, r.read(), r.headers.get_content_type()
        except HTTPError as e:
            with e:
                status, raw, ctype = e.code, e.read(), e.headers.get_content_type()
        return status, (json.loads(raw) if ctype == "application/json" else raw), ctype

    def state(self, since=0):
        return self.request("GET", f"/api/state?log={since}")[1]

    def texts(self):
        return [(e["kind"], e["text"]) for e in self.state()["log"]]

    def io(self, events=(), log=(), joints=J, state="쉼"):
        return self.request("POST", "/api/io", {"events": list(events), "log": list(log), "joints": joints, "state": state})

    def close(self):
        self.http.shutdown()
        self.http.server_close()

        async def stop():
            for t in self.tasks:
                t.cancel()
            await asyncio.gather(*self.tasks, return_exceptions=True)
            await self.plc.stop()
        self.call(stop())
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)
        self.loop.close()

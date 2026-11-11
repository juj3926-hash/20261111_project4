"""가짜 PLC 합격 시험 (docs/handoff/fake-plc.md 4절).

(a) 해석기 동등성: 원본 → ladder_src.load → layout() → encode → decode → DiagramPlc가
    같은 원본의 sim.Plc와 같은 입력 열에서 스캔마다 모든 비트, 워드, 타이머 누적까지 같다.
(b) 표본 해석: tests/samples/xgwx의 표본이 풀리고 돈다. 미지원이 있으면 3.5 형식 줄로 낸다.
(c) 통합: 가짜 PLC(포트 0) + 로봇 대역 + PC 프로그램으로 tasks 6절 1~9행. PC 프로그램이 없으면 건너뛴다.
"""

import asyncio
import contextlib
import json
import os
import random
import re
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from pymodbus.client import AsyncModbusTcpClient

from picknplace_full.model import VirtualRobot
from tools.xgwx import codec, ladder_src, sim
from tools.xgwx.build import program_devices
from tools.xgwx.layout import layout

from fake_plc.app import FakePlc
from fake_plc.diagram import DiagramPlc, read_programs
from fake_plc.engine import Engine
from tests.test_e2e_ladder import Robot
from tests.test_fake_plc_io import until

ROOT = Path(__file__).resolve().parents[1]
E2E_SRC = ROOT / "ladder" / "e2e_pickplace.ld.txt"
E2E_XGWX = ROOT / "ladder" / "e2e_pickplace.xgwx"
FULL_SRC = ROOT / "ladder" / "PICKNPLACE_FULL.ld.txt"
FULL_XGWX = ROOT / "ladder" / "PICKNPLACE_FULL.xgwx"
PC_APP = ROOT / "picknplace_full" / "app.py"
SYM = ladder_src.load(FULL_SRC).symbols            # 참조 래더의 기호 → 디바이스
SAMPLES = ROOT / "tests" / "samples" / "xgwx"
CODES = (1111, 9991, 9992, 9995, 9993, 9994, 3333, 2222, 7777)   # 7777은 모르는 코드


class Counted(sim.Plc):
    """sim.Plc + 조건이 한 번이라도 참이 된 렁 번호. 입력 열이 모든 렁을 지나갔는지 본다."""

    def __init__(self, rungs):
        super().__init__(rungs)
        self.fired = set()

    def _cond(self, i, rung):
        c = super()._cond(i, rung)
        if c:
            self.fired.add(i)
        return c


def nonzero(d):
    return {k: v for k, v in d.items() if v}


def state(plc):
    """비교할 상태. 없는 디바이스는 0이다."""
    return nonzero(plc.bits), nonzero(plc.words), nonzero(plc.acc)


def e2e_feed():
    """e2e: 로봇 흉내(시험 E2E 계약) + PC가 D01000에 쓰는 일정. 300은 순서 중간이라 무시된다."""
    robot = Robot(ladder_src.load(E2E_SRC).symbols)
    pc = {5: 1111, 300: 1111, 700: 1111, 1400: 100}

    def feed(n, ref):
        return ({"D01000": pc[n]} if n in pc else {}), robot.step(ref)
    return feed


def full_feed(pc):
    """PICKNPLACE_FULL: 가상 로봇(spec 5절) + PC가 D01001에 쓰는 일정 {스캔 번호: 명령}."""
    sym = ladder_src.load(FULL_SRC).symbols
    robot = VirtualRobot()

    def feed(n, ref):
        do = robot.step({s: ref.bit(sym[s]) for s in ("DI0", "DI1", "DI3")}, 0.01)
        return ({"D01001": pc[n]} if n in pc else {}), {sym["DO0"]: do["DO0"], sym["DO1"]: do["DO1"]}
    return feed


# 초 → 명령. 상태 코드 1~12, 명령 8가지와 모르는 코드, 자동 그리퍼, 즉시 정지와 재개를 모두 지난다.
FULL_SCRIPT = {0.5: 9991, 3.5: 2222, 4.0: 1111, 4.5: 9991, 5.5: 9992, 10.5: 2222, 11.0: 9992,
               12.0: 9991, 17.0: 9995, 17.5: 1111, 18.0: 9993, 19.0: 3333, 22.5: 9995, 23.0: 1111,
               23.5: 9993, 24.0: 3333, 27.5: 1111, 28.0: 9994, 28.5: 7777, 29.0: 9991, 29.5: 9995,
               30.5: 9993}


class EquivalenceTest(unittest.TestCase):
    def run_both(self, src_path, feed, scans, recs=None):
        """같은 입력 열을 sim과 해석기에 넣고 스캔마다 상태를 비교한다. sim 쪽(Counted)을 돌려준다."""
        rungs = ladder_src.load(src_path).rungs
        ref = Counted(rungs)
        plc = DiagramPlc([recs if recs is not None else codec.decode(codec.encode(layout(rungs)))])
        for n in range(scans):
            words, inputs = feed(n, ref)
            for dev, v in words.items():                  # PC 쓰기는 스캔 사이에
                ref.write_word(dev, v)
                plc.write_word(dev, v)
            ref.scan(inputs)
            plc.scan(inputs)
            self.assertEqual(state(plc), state(ref), "스캔 %d" % n)
        return ref

    def assert_all_rungs_fired(self, ref):
        self.assertEqual([r.line for i, r in enumerate(ref.rungs) if i not in ref.fired], [],
                         "조건이 한 번도 참이 되지 않은 렁(원본 줄 번호)")

    def test_a_e2e(self):
        self.assert_all_rungs_fired(self.run_both(E2E_SRC, e2e_feed(), 1600))

    def test_a_e2e_xgwx_file(self):
        [(_, recs)] = read_programs(E2E_XGWX)              # 커밋된 프로젝트 파일을 그대로
        self.run_both(E2E_SRC, e2e_feed(), 1600, recs)

    def test_a_e2e_random(self):
        rnd = random.Random(1)

        def feed(n, ref):
            words = {"D01000": rnd.choice((0, 100, 1111, 1111))} if rnd.random() < 0.01 else {}
            return words, {"P00004": int(rnd.random() < 0.2), "P00005": int(rnd.random() < 0.2)}
        self.run_both(E2E_SRC, feed, 3000)

    def test_a_picknplace_full(self):
        pc = {round(t * 100): code for t, code in FULL_SCRIPT.items()}
        self.assert_all_rungs_fired(self.run_both(FULL_SRC, full_feed(pc), 3300))

    def test_a_picknplace_full_random(self):
        rnd = random.Random(2)
        pc = {n: rnd.choice(CODES) for n in range(3000) if rnd.random() < 0.02}
        self.run_both(FULL_SRC, full_feed(pc), 3000)


class SamplesTest(unittest.TestCase):
    def test_b_samples_run(self):
        rnd = random.Random(3)
        for name in ("055_main.bin", "07_start_main.bin", "08_home_main.bin", "555_main.bin",
                     "e2e_template.xgwx"):
            with self.subTest(name):
                e = Engine()
                e.open(SAMPLES / name)
                self.assertEqual(e.problems, [])
                e.run()
                inputs = sorted(d for _, rungs in e.programs for d in program_devices(rungs) if d[0] == "P")
                for _ in range(300):
                    e.wired = {d: int(rnd.random() < 0.5) for d in inputs}
                    e.scan()

    def test_b_unsupported_list_is_precise(self):
        # 07_start의 MOV 상자를 모두 MOVP로 바꾸면 그 렁마다 3.5 형식 한 줄이 나오고 RUN하지 않는다.
        [(_, rungs)] = read_programs(SAMPLES / "07_start_main.bin")
        for rung in rungs:
            for el in rung.elements:
                if el.fields.get("code") == 148:
                    el.fields.update(code=404, text="MOVP" + el.fields["text"][3:])
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "movp.bin").write_bytes(codec.encode(rungs))
            e = Engine()
            e.open(Path(d) / "movp.bin")
        self.assertEqual(e.problems, ["미지원: 렁 %d, 요소 BOX:MOVP (kind=0x22, code=404)" % n
                                      for n in (29, 31, 33, 35, 37, 39, 40)])
        self.assertRaises(ValueError, e.run)


class RobotClient:
    """로봇 대역: 가상 로봇(picknplace_full.model.VirtualRobot)을 배선 JSON 클라이언트로 감쌌다.
    PLC 출력(DI0~DI3)을 받아 약 10 ms마다 로봇을 진행하고, DO0·DO1을 바뀌면 바로, 그리고 0.5초마다 보낸다."""

    DI = {"DI0": "P00044", "DI1": "P00045", "DI2": "P00046", "DI3": "P00047"}

    def __init__(self, port, robot):
        self.port, self.robot = port, robot
        self.di = dict.fromkeys(self.DI, 0)

    async def run(self):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        listening = asyncio.create_task(self._listen(reader))
        last, sent, sent_at = time.monotonic(), None, 0.0
        try:
            while True:
                await asyncio.sleep(0.01)
                now = time.monotonic()
                do = self.robot.step(self.di, now - last)
                last = now
                if do != sent or now - sent_at >= 0.5:
                    msg = {"type": "inputs", "P00004": do["DO0"], "P00005": do["DO1"]}
                    writer.write((json.dumps(msg) + "\n").encode())
                    sent, sent_at = do, now
        finally:
            listening.cancel()
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()

    async def _listen(self, reader):
        while line := await reader.readline():
            msg = json.loads(line)
            if msg.get("type") == "outputs":
                self.di = {s: msg[d] for s, d in self.DI.items()}


class Rig:
    """가짜 PLC(참조 .xgwx, 포트 0) + 로봇 대역. 상태 D01000의 변화와 받은 명령을 기록한다."""

    async def start(self, move_time=0.3):
        self.app = FakePlc(FULL_XGWX)
        self.app.modbus_log = []                     # 시험에서는 기록을 버리지 않는다
        await self.app.start(modbus_port=0, wiring_port=0, http_port=0)
        robot = VirtualRobot(start_delay=0.05, move_time=move_time, stop_time=0.05)
        self.tasks = [asyncio.create_task(RobotClient(self.app.ports["wiring"], robot).run()),
                      asyncio.create_task(self._watch())]
        self.statuses = [self.raw()]
        await until(lambda: self.app.wiring.writers)  # 로봇이 붙었다

    async def _watch(self):
        while True:
            self._record()
            await asyncio.sleep(0.005)

    def _record(self):
        """지금 D01000이 마지막 기록과 다르면 더한다. mark·since도 부른다: 감시 작업이 아직 못 본
        새 값 위에 mark를 찍으면 since가 한 칸 앞에서 시작한다."""
        if self.raw() != self.statuses[-1]:
            self.statuses.append(self.raw())

    async def ready(self):
        """첫 스캔에 시작한 그리퍼 열기 3초가 끝날 때까지 기다린다. 그동안 온 명령은 거부된다."""
        await until(lambda: self.app.engine.scans > 1 and not self.bit(SYM["G_OPENING"]), 8)

    def raw(self):
        return self.app.engine.words.get("D01000", 0)

    def mailbox(self):
        return self.app.engine.words.get("D01001", 0)

    def bit(self, dev):
        return self.app.engine.plc.bit(dev)

    def mark(self):
        """지금 위치. since와 commands에 넘긴다."""
        self._record()
        return len(self.statuses) - 1, len(self.app.modbus_log)

    def since(self, mark):
        """mark 뒤 상태 코드 열 (첫 값은 mark 때의 상태)."""
        self._record()
        return self.statuses[mark[0]:]

    def commands(self, mark):
        """mark 뒤 PC가 우편함(주소 1)에 쓴 명령."""
        return [int(m.group(1)) for e in self.app.modbus_log[mark[1]:]
                if (m := re.match(r"FC(?:06|16) 주소 1 값 (\d+)", e["text"]))]

    async def monitor(self, path):
        """가짜 PLC 모니터 API (예: /api/stop)."""
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (self.app.ports["http"], path), data=b"{}",
                                     headers={"Content-Type": "application/json"})
        await asyncio.to_thread(lambda: urllib.request.urlopen(req, timeout=5).close())

    async def close(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.app.close()


class SystemTest(unittest.IsolatedAsyncioTestCase):
    async def test_robot_over_sockets(self):
        # PC 대신 Modbus 클라이언트가 9991을 쓴다. Modbus → 해석기(참조 .xgwx) → 배선 → 로봇 대역 → 배선 → 상태
        rig = Rig()
        await rig.start()
        self.addAsyncCleanup(rig.close)
        client = AsyncModbusTcpClient("127.0.0.1", port=rig.app.ports["modbus"], timeout=2, retries=0)
        await client.connect()
        self.addCleanup(client.close)
        await rig.ready()
        mark = rig.mark()
        await client.write_register(1, 9991, device_id=1)
        await until(lambda: rig.since(mark) == [2, 1, 2], 5)   # 정지 → 동작 중 → 정지
        self.assertEqual((await client.read_holding_registers(0, count=2, device_id=1)).registers, [2, 0])
        self.assertEqual(rig.commands(mark), [9991])
        self.assertIn("FC06 주소 1 값 9991 → 정상", [e["text"] for e in rig.app.modbus_log])


class PcProgram:
    """PC 프로그램: python -m picknplace_full --target tcp:127.0.0.1:<포트> --http-port 0 (PICKNPLACE_FULL 계획 작업 8)."""

    def __init__(self):
        self.proc = self.draining = None
        self.lines = []

    async def start(self, modbus_port):
        """띄우고, 주소가 나오고 PC가 PLC 상태를 처음 읽을 때까지 기다린다(통신은 따로 돌며 붙는다).
        PC 프로그램의 로그 파일은 저장소의 logs/에 남는다(git이 무시한다)."""
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1", "PYTHONPATH": str(ROOT)}
        self.proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "picknplace_full", "--target", "tcp:127.0.0.1:%d" % modbus_port,
            "--http-port", "0", cwd=ROOT, env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        self.draining = asyncio.create_task(self._drain())   # 처음부터 읽는다. 출력이 쌓여 막히지 않게
        with contextlib.suppress(AssertionError):
            await until(lambda: self._url() or self.proc.returncode is not None, 20)
        if not self._url():
            raise AssertionError("PC 프로그램이 주소를 내지 않음:\n" + "".join(self.lines))
        self.base = self._url()
        for _ in range(100):                                # 10초
            s = await self.status()
            if s.get("comm_ok") and (s.get("status") or {}).get("valid"):
                return
            await asyncio.sleep(0.1)
        raise AssertionError("PC 프로그램이 PLC 상태를 읽지 못함: %s" % s)

    async def status(self):
        def go():
            with urllib.request.urlopen(self.base + "/api/status", timeout=5) as r:
                return json.loads(r.read())
        return await asyncio.to_thread(go)

    def _url(self):
        found = [m.group(0) for line in self.lines if (m := re.search(r"http://127\.0\.0\.1:\d+", line))]
        return found[0] if found else None

    async def _drain(self):
        while line := await self.proc.stdout.readline():
            self.lines.append(line.decode("utf-8", "replace"))

    async def post(self, path, body):
        """돌려주는 값: (상태 코드, 본문). 거절한 누름(200 + message)도 잘못된 요청(400)도 예외 없이 돌려준다."""
        def go():
            req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=5) as r:
                    return r.status, r.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as err:
                with err:
                    return err.code, err.read().decode("utf-8", "replace")
        return await asyncio.to_thread(go)

    async def send(self, rig, button=None, code=None, sent=None):
        """버튼(또는 직접 코드)을 PLC가 명령 sent를 받을 때까지 누른다. PC가 상태를 아직 못 읽어
        버튼이 잠깐 꺼져 있으면 거절(200)하고 보내지 않으므로, 1초 안에 명령이 안 오면 다시 누른다."""
        mark, reply = rig.mark(), None
        for _ in range(5):
            reply = await (self.post("/api/press", {"button": button}) if button
                           else self.post("/api/code", {"code": code}))
            with contextlib.suppress(AssertionError):
                await until(lambda: sent in rig.commands(mark), 1.0)
                return
        raise AssertionError("PLC가 %s를 받지 못함. 마지막 응답: %s" % (sent, reply))

    async def close(self):
        if self.proc is None:
            return
        if self.proc.returncode is None:
            self.proc.terminate()
        await self.proc.wait()
        await asyncio.gather(self.draining, return_exceptions=True)


@unittest.skipUnless(PC_APP.exists(), "picknplace_full/app.py가 아직 없다(PICKNPLACE_FULL 계획 작업 8). "
                                      "들어오면 PC 프로그램을 띄워 tasks 6절 1~9행을 돈다")
class TasksTableTest(unittest.IsolatedAsyncioTestCase):
    async def seq(self, rig, mark, want, timeout):
        """mark 뒤 상태 열이 want가 될 때까지 기다린다. 안 되면 실제 열을 보여 주며 실패한다."""
        with contextlib.suppress(AssertionError):
            await until(lambda: rig.since(mark) == want, timeout)
        self.assertEqual(rig.since(mark), want)

    async def test_c_tasks_table(self):
        # 로봇 이동은 2초로 둔다. "이동 중에" 누르는 단계(3, 4, 5행)에 여유가 있어야 한다.
        rig, pc = Rig(), PcProgram()
        self.rig = rig                                       # 끝난 뒤 상태 기록(rig.statuses)을 볼 수 있게
        self.addAsyncCleanup(rig.close)
        self.addAsyncCleanup(pc.close)                       # start가 실패해도 프로세스를 남기지 않는다
        await rig.start(move_time=2.0)
        await pc.start(rig.app.ports["modbus"])
        await rig.ready()

        # 1행: 2222 → D01001 0, 상태 0002
        await pc.send(rig, code=2222, sent=2222)
        await until(lambda: rig.mailbox() == 0 and rig.raw() == 2)
        # 2행: 시작 → 한 사이클 0002 → 0001 → 0002 → 0005 → 0004 → 0005 → 0002
        n = rig.mark()
        await pc.send(rig, "start", sent=1111)
        await self.seq(rig, n, [2, 1, 2, 5, 4, 5, 2], 30)
        # 3행: 이동1 중 정지 → 이동1과 자동 닫기까지 하고 0005에서 9994 접수, 다음 이동 없음
        n = rig.mark()
        await pc.send(rig, "start", sent=1111)
        await until(lambda: rig.raw() == 1, 5)
        self.assertEqual((await pc.post("/api/press", {"button": "stop"}))[0], 200)
        await until(lambda: 9994 in rig.commands(n), 15)
        await asyncio.sleep(1.0)
        self.assertEqual((rig.since(n), rig.commands(n)), ([2, 1, 2, 5], [1111, 9991, 9994]))
        # 3-1행: 스크립트2 → 로봇만 움직이고 그리퍼는 닫힌 채. 자동 열기 없음(있다면 끝나고 3초 안에 0002)
        n = rig.mark()
        await pc.send(rig, "script2", sent=9992)
        await self.seq(rig, n, [5, 4, 5], 6)
        await asyncio.sleep(3.5)
        self.assertEqual(rig.since(n), [5, 4, 5])
        # 그리퍼를 연다 (4~7행은 열림에서 시작)
        await pc.send(rig, "gripper", sent=3333)
        await until(lambda: rig.raw() == 2, 6)
        # 4행: 이동 중 즉시 정지 → 재개 → 사이클 끝까지
        n = rig.mark()
        await pc.send(rig, "start", sent=1111)
        await until(lambda: rig.raw() == 1, 5)
        # 로봇이 실제로 움직인 뒤(DO0 ON) 누른다. 그 전에 누르면 9995가 아직 받지 않은 요청을 취소하므로
        # (spec 결정 3) 자동 닫기가 없고 PC의 자동 순서가 그리퍼를 기다리다 중단된다. 그것은 이 행이 아니다.
        await until(lambda: rig.bit(SYM["DO0"]) == 1, 5)
        await asyncio.sleep(0.5)
        await pc.send(rig, "estop", sent=9995)
        await until(lambda: rig.raw() == 3)
        await pc.send(rig, "resume", sent=9993)
        await self.seq(rig, n, [2, 1, 3, 1, 2, 5, 4, 5, 2], 30)
        # 5행: 로봇 동작 중 3333 → 거부, PLC 이상 0007, 그리퍼 그대로
        await pc.send(rig, "script1", sent=9991)
        await until(lambda: rig.raw() == 1, 5)
        await pc.send(rig, code=3333, sent=3333)
        await until(lambda: rig.raw() == 7)
        await until(lambda: rig.raw() == 8, 6)               # 이동이 끝나면 이상·열림·정지
        # 6행: 스크립트1만 → 로봇만 움직이고 그리퍼는 그대로
        n = rig.mark()
        await pc.send(rig, "script1", sent=9991)
        await self.seq(rig, n, [8, 1, 2], 6)
        # 7행: 3333 두 번 (열림에서) → 3초 뒤 0005, 다시 → 3초 뒤 0002
        n = rig.mark()
        await pc.send(rig, "gripper", sent=3333)
        await until(lambda: rig.raw() == 5, 6)
        await pc.send(rig, "gripper", sent=3333)
        await self.seq(rig, n, [2, 5, 2], 6)
        # 8행: 모르는 코드 1234 → 거부, PLC 이상 0008, D01001 → 0
        await pc.send(rig, code=1234, sent=1234)
        await until(lambda: rig.raw() == 8 and rig.mailbox() == 0)
        # 9행: PLC STOP → RUN. 첫 스캔에 0002, 처음 3초 그리퍼 열기 출력, 그동안 온 시작은 거부(0008),
        # 3초 뒤 2222는 접수(0002)
        n = rig.mark()
        await rig.monitor("/api/stop")
        await rig.monitor("/api/run")
        await until(lambda: rig.bit(SYM["G_OPEN"]) == 1, 1)
        await pc.send(rig, code=1111, sent=1111)
        await until(lambda: rig.raw() == 8)
        await until(lambda: rig.bit(SYM["G_OPEN"]) == 0, 5)
        await pc.send(rig, code=2222, sent=2222)
        await until(lambda: rig.raw() == 2)
        self.assertEqual(rig.since(n), [8, 2, 8, 2])


if __name__ == "__main__":
    unittest.main()

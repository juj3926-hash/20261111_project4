"""가짜 로봇 ↔ 가짜 PLC 대역(참조 래더) ↔ PC 쪽. 포트는 모두 0(OS가 고른 포트).

- 대역 PlcDouble(tests/fake_robot_samples/doubles.py): 참조 래더를 tools.xgwx.sim.Plc로 10배속 스캔하고
  배선 JSON 서버와 Modbus TCP 서버를 연다. 하위 프로젝트 1(가짜 PLC)이 오기 전의 자리다.
- 가짜 로봇은 내장 모드로 실시간 루프에서 돈다(짧은 시간 설정).
- PC 쪽: LadderLoopTest는 picknplace_full의 PlcLink·Sequencer를 그대로 쓴다.
  합격 (b)는 python -m picknplace_full(app.py)이 오면 HTTP로 tasks 6절 시험 표 1~9행을 돈다.
"""

import asyncio
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import unittest
import urllib.request
from pathlib import Path

from fake_robot.core import Robot
from fake_robot.server import FakeRobot, fixed_loop
from fake_robot.wiring import WiringClient
from tests.fake_robot_samples.doubles import LADDER, PlcDouble, until, wait_for

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "picknplace_full" / "app.py"
QUICK = dict(start_delay=0.02, move_time=0.5, stop_time=0.03)   # 로봇 시간을 줄인다 (래더는 10배속)


async def start_robot(plc):
    """내장 모드 가짜 로봇을 대역의 배선 포트에 붙인다. (로봇, 작업들)"""
    wiring = WiringClient(port=plc.wiring_port)
    robot = FakeRobot(wiring, Robot(**QUICK))
    tasks = [asyncio.create_task(wiring.run()), asyncio.create_task(fixed_loop(robot.tick))]
    await until(lambda: wiring.connected)
    return robot, tasks


async def stop_tasks(tasks):
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


@unittest.skipUnless(LADDER.exists(), "ladder/PICKNPLACE_FULL.ld.txt 없음 (feature/PICKNPLACE_FULL 병합 전)")
class LadderLoopTest(unittest.IsolatedAsyncioTestCase):
    """PC 쪽 PlcLink·Sequencer → Modbus → 대역(참조 래더) → 배선 → 가짜 로봇(내장 모드)."""

    async def asyncSetUp(self):
        from picknplace_full.link import PlcLink
        from picknplace_full.sequence import Sequencer
        self.plc = PlcDouble(speed=10)
        await self.plc.start()
        self.robot, self.tasks = await start_robot(self.plc)
        self.link = PlcLink(f"tcp:127.0.0.1:{self.plc.modbus_port}", interval=0.02, fast_interval=0.01, timeout=1.0)
        self.raws = []
        self.link.on_event(lambda e: self.raws.append(self.link.status.raw) if e.tag == "상태" else None)
        self.seq = Sequencer(self.link, move_timeout=5.0, grip_timeout=1.0)
        await self.link.start()

    async def asyncTearDown(self):
        await self.link.close()
        await stop_tasks(self.tasks)
        await self.plc.stop()

    async def ready(self):
        """첫 스캔의 그리퍼 열기 3초(래더 시간, 실제 0.3초)가 끝나고 0002를 본다."""
        await until(lambda: not self.plc.bit("G_OPENING"), 2.0)
        self.assertIsNotNone(await self.link.wait(lambda s: s.raw == 2, 1.0))

    def assert_raws_in_order(self, expected):
        rest = iter([r for i, r in enumerate(self.raws) if i == 0 or self.raws[i - 1] != r])
        self.assertTrue(all(raw in rest for raw in expected), f"raw 변화 {self.raws}에 {expected}가 차례로 없음")

    async def test_auto_cycle(self):                                     # 시험 표 2행
        await self.ready()
        self.assertEqual(await self.seq.auto_cycle(), "완료")
        self.assert_raws_in_order([2, 1, 2, 5, 4, 5, 2])
        self.assertEqual(self.plc.history, [(0, 0), (1, 0), (0, 0), (1, 0), (0, 0)])  # 로봇 DO0이 두 번

    async def test_estop_and_resume_during_a_move(self):                 # 시험 표 4행
        await self.ready()
        await self.seq.press("start")
        await until(lambda: self.robot.robot.phase == "동작", 3.0)       # 이동1 중
        await self.seq.press("estop")
        self.assertIsNotNone(await self.link.wait(lambda s: s.robot == 2, 2.0))
        await until(lambda: self.plc.history[-1] == (1, 1), 2.0)         # 감속 뒤 DO1, DO0은 그대로
        await self.seq.press("resume")
        await until(lambda: not self.seq.running, 6.0)
        self.assertEqual(self.seq.last_result, "완료")
        self.assert_raws_in_order([1, 3, 1, 2, 5])


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@unittest.skipUnless(APP.exists(), "picknplace_full/app.py 없음: PC 프로그램(PICKNPLACE_FULL 계획 Task 8)이 "
                                   "feature/PICKNPLACE_FULL에 오면 합격 (b)를 돈다")
class AcceptanceBTest(unittest.TestCase):
    """합격 (b): 가짜 PLC 대역 + PC 프로그램(python -m picknplace_full --target tcp:…) + 가짜 로봇. tasks 6절 1~9행.

    PC 프로그램의 HTTP(계획 Task 8): GET /api/status, POST /api/press {"button"}, POST /api/code {"code"}.
    """

    def setUp(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        self.plc = PlcDouble(speed=10)
        self.call(self.plc.start())
        self.robot, self.tasks = self.call(start_robot(self.plc))
        self.http = free_port()
        self.pc = subprocess.Popen(
            [sys.executable, "-m", "picknplace_full", "--target", f"tcp:127.0.0.1:{self.plc.modbus_port}",
             "--http-port", str(self.http), "--interval", "0.02"],
            cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        self.addCleanup(self.cleanup)
        wait_for(lambda: self.status_or_none() is not None, 10.0)

    def cleanup(self):
        self.pc.terminate()
        self.pc.wait(5)
        self.call(stop_tasks(self.tasks))
        self.call(self.plc.stop())
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)
        self.loop.close()

    def call(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(10)

    def request(self, path, body=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.http}{path}", method="POST" if body else "GET",
                                     data=json.dumps(body).encode() if body else None,
                                     headers={"Content-Type": "application/json"} if body else {})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())

    def status_or_none(self):
        try:
            return self.request("/api/status")
        except OSError:
            return None

    def raw(self):
        s = self.request("/api/status")["status"]
        return s and s["raw"]

    def raw_changes(self):
        """PC 로그의 상태 이벤트("0002 → 0001 (…)")에서 뽑은 raw 변화 열."""
        out = []
        for e in self.request("/api/status")["events"]:                 # 오래된 것부터라고 본다 (계획 Task 8)
            m = re.search(r"→ (\d{4})", e["text"]) if e["tag"] == "상태" else None
            if m and (not out or out[-1] != int(m.group(1))):
                out.append(int(m.group(1)))
        return out

    def press(self, button):
        return self.request("/api/press", {"button": button})["message"]

    def code(self, code):
        return self.request("/api/code", {"code": code})["message"]

    def ready(self):
        wait_for(lambda: not self.plc.bit("G_OPENING") and self.raw() == 2, 5.0)

    def moves(self):
        """PLC가 본 로봇 DO0의 상승 수 = 로봇이 시작한 이동 수."""
        h = self.plc.history
        return sum(1 for a, b in zip(h, h[1:]) if a[0] == 0 and b[0] == 1)

    def until_done(self, result):
        wait_for(lambda: self.request("/api/status")["sequence"]["last_result"] == result, 10.0)

    def assert_in_order(self, expected):
        rest = iter(self.raw_changes())
        self.assertTrue(all(raw in rest for raw in expected), f"raw 변화 {self.raw_changes()}에 {expected}가 차례로 없음")

    def test_tasks_table_rows_1_to_9(self):
        self.ready()
        with self.subTest("1 상태 확인"):
            self.assertIn("접수", self.code(2222))
            self.assertEqual(self.raw(), 2)
        with self.subTest("2 한 사이클"):
            self.press("start")
            self.until_done("완료")
            self.assert_in_order([2, 1, 2, 5, 4, 5, 2])
        with self.subTest("3 정지 버튼, 3-1 스크립트2"):
            before = self.moves()
            self.press("start")
            wait_for(lambda: self.robot.robot.phase == "동작", 5.0)
            self.press("stop")
            self.until_done("정지")
            self.assertEqual(self.raw(), 5)                              # 이동1과 자동 닫기까지 하고 멈춤
            time.sleep(0.5)                                              # 로봇 이동 한 번(0.5초)보다 길게 본다
            self.assertEqual(self.moves(), before + 1)                   # 다음 이동 없음
            self.press("script2")
            wait_for(lambda: self.robot.robot.phase == "동작", 5.0)
            wait_for(lambda: self.robot.robot.phase == "쉼" and self.raw() == 5, 5.0)
            self.press("gripper")                                        # 다음 행을 위해 연다
            wait_for(lambda: self.raw() == 2, 5.0)
        with self.subTest("4 즉시 정지 → 재개"):
            self.press("start")
            wait_for(lambda: self.robot.robot.phase == "동작", 5.0)
            self.press("estop")
            wait_for(lambda: self.raw() == 3, 5.0)
            self.press("resume")
            self.until_done("완료")
            self.assert_in_order([1, 3, 1])
        with self.subTest("5 로봇 동작 중 그리퍼"):
            self.press("script1")
            wait_for(lambda: self.robot.robot.phase == "동작", 5.0)
            self.assertIn("거부", self.code(3333))
            self.assertIn(self.raw(), (7, 10))
            wait_for(lambda: self.raw() == 8, 5.0)                       # 이동이 끝나도 PLC 자리는 이상
        with self.subTest("6 스크립트1만"):
            self.press("script1")
            wait_for(lambda: self.robot.robot.phase == "동작", 5.0)
            wait_for(lambda: self.robot.robot.phase == "쉼" and self.raw() == 2, 5.0)  # 그리퍼는 그대로 열림
        with self.subTest("7 그리퍼 두 번"):
            self.press("gripper")
            wait_for(lambda: self.raw() == 5, 5.0)
            self.press("gripper")
            wait_for(lambda: self.raw() == 2, 5.0)
        with self.subTest("8 모르는 코드"):
            self.assertIn("거부", self.code(1234))
            self.assertEqual(self.raw(), 8)
        with self.subTest("9 PLC STOP → RUN"):
            self.plc.speed = 1                                           # 열기 3초를 실제 3초로: 명령 다섯 개가 들어간다
            self.plc.plc.first = True                                    # RUN마다 첫 스캔
            wait_for(lambda: self.plc.bit("G_OPEN"), 2.0)                # 처음 3초는 그리퍼 열기 출력
            self.ready()                                                 # 그 뒤 0002
            self.plc.plc.first = True                                    # 다시 RUN. 이번에는 3초 안에 명령을 보낸다
            # 첫 스캔은 우편함을 판정 없이 지운다. 그래서 첫 스캔이 돈 뒤(열기 중)에 보낸다
            wait_for(lambda: self.plc.bit("G_OPENING"), 2.0)
            for code in (1111, 9991, 9992, 9994, 3333):                  # 시작·이동·해제·그리퍼
                self.assertIn("거부", self.code(code))
            self.assertTrue(self.plc.bit("G_OPENING"))                   # 다섯 개 모두 3초 안에 보냈다
            wait_for(lambda: not self.plc.bit("G_OPENING") and self.raw() == 8, 5.0)  # 거부가 남긴 PLC 이상
            self.assertIn("접수", self.code(2222))
            self.assertEqual(self.raw(), 2)


if __name__ == "__main__":
    unittest.main()

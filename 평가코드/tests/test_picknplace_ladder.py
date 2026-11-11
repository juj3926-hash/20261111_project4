"""PICKNPLACE_FULL 래더 원본을 시뮬레이터로 스캔하고 가상 로봇을 붙여 tasks 6절 시험 표를 확인한다."""

import unittest
from itertools import groupby
from pathlib import Path

from picknplace_full.model import VirtualRobot
from tools.xgwx.ladder_src import load
from tools.xgwx.sim import Plc

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "ladder" / "PICKNPLACE_FULL.ld.txt"
DT = 10
SIGNALS = ("DI0", "DI1", "DI2", "DI3", "G_CLOSE", "G_OPEN", "AUTO")


class Rig:
    """래더(Plc)와 가상 로봇을 잇는다. 디바이스는 원본의 MAP 기호로 찾는다."""

    def __init__(self, robot=None):
        source = load(SRC)
        self.sym = source.symbols
        self.plc = Plc(source.rungs)
        self.robot = robot or VirtualRobot()
        self.force = {}      # 로봇 출력을 덮어쓰는 값. 예: {"DO0": 0}
        self.log = []

    @property
    def raw(self):
        return self.plc.word("D01000")

    @property
    def t(self):
        """지난 시간(ms). 첫 스캔이 끝나면 10."""
        return len(self.log) * DT

    def scan(self, ms=DT):
        """스캔마다 DI0~3 출력을 로봇에 주고, 로봇의 DO0·DO1을 입력으로 넣는다."""
        for _ in range(ms // DT):
            di = {s: self.plc.bit(self.sym[s]) for s in ("DI0", "DI1", "DI2", "DI3")}
            do = {**self.robot.step(di, DT / 1000), **self.force}
            self.plc.scan({self.sym[s]: v for s, v in do.items()}, DT)
            self.log.append({"t": self.t + DT, "D01000": self.raw,
                             "D01001": self.plc.word("D01001"),
                             **{s: self.plc.bit(self.sym[s]) for s in SIGNALS}})

    def send(self, code):
        """D01001에 쓰고 우편함이 0이 될 때까지 스캔한다. 그 스캔의 (D01000, PLC 자리)."""
        self.plc.write_word("D01001", code)
        for _ in range(5):
            self.scan()
            if self.plc.word("D01001") == 0:
                return self.raw, (self.raw - 1) // 6
        raise AssertionError("우편함이 0이 되지 않음: %d" % code)

    def until(self, pred, ms=10000):
        """pred()가 참이 될 때까지 스캔한다. ms 안에 안 되면 실패."""
        for _ in range(ms // DT):
            if pred():
                return
            self.scan()
        if not pred():
            raise AssertionError("%d ms 안에 조건이 맞지 않음" % ms)

    def changes(self, key, since=0):
        """log[since:]의 key 값에서 연속 중복을 뺀 열."""
        return [v for v, _ in groupby(r[key] for r in self.log[since:])]

    def ready(self):
        """첫 스캔의 그리퍼 열기 3초가 지나도록 스캔한다."""
        self.scan(3100)


class LadderTest(unittest.TestCase):
    def setUp(self):
        self.rigs = []

    def rig(self, robot=None):
        rig = Rig(robot)
        self.rigs.append(rig)
        return rig

    def test_row1_status_check(self):
        rig = self.rig()
        rig.ready()
        self.assertEqual(rig.send(2222), (2, 0))

    def test_row2_full_cycle(self):
        rig = self.rig()
        rig.ready()
        self.assertEqual(rig.send(1111), (2, 0))
        start = len(rig.log) - 1                     # 1111 접수 스캔
        self.assertEqual(rig.send(9991), (1, 0))
        rig.until(lambda: rig.raw == 5)
        self.assertEqual(rig.send(9992), (4, 0))
        rig.until(lambda: rig.raw == 2)
        self.assertEqual(rig.changes("D01000", start), [2, 1, 2, 5, 4, 5, 2])
        self.assertEqual(rig.log[-1]["AUTO"], 0)
        self.assertEqual(rig.send(1111), (2, 0))

    def test_row3_stop_after_move1(self):
        rig = self.rig()
        rig.ready()
        rig.send(1111)
        start = len(rig.log)
        rig.send(9991)
        rig.until(lambda: rig.raw == 5)
        self.assertEqual(rig.send(9994), (5, 0))
        self.assertEqual(rig.send(9992), (4, 0))
        rig.until(lambda: rig.raw == 5)              # 이동2 끝
        end = len(rig.log) - 1
        rig.scan(4000)
        self.assertEqual(rig.changes("D01000", end), [5])     # 자동 열기 없이 0005
        self.assertFalse(any(r["G_OPEN"] for r in rig.log[start:]))

    def test_row4_estop_resume(self):
        rig = self.rig()
        rig.ready()
        rig.send(1111)
        start = len(rig.log)
        rig.send(9991)
        rig.until(lambda: rig.robot.state == "동작")
        rig.scan(500)
        self.assertEqual(rig.send(9995), (3, 0))
        i = len(rig.log)
        rig.scan(1000)
        self.assertFalse(any(r["G_CLOSE"] for r in rig.log[i:]))     # 정지 중 END 없음
        rig.send(9993)
        rig.until(lambda: rig.raw == 5)
        self.assertEqual(rig.send(9992), (4, 0))
        rig.until(lambda: rig.raw == 2)
        self.assertEqual(rig.changes("DI2"), [0, 1, 0])
        self.assertAlmostEqual(sum(r["DI2"] for r in rig.log) * DT, 500, delta=20)
        self.assertEqual(rig.changes("D01000", start), [1, 3, 1, 2, 5, 4, 5, 2])

    def test_row5_gripper_rejected_while_moving(self):
        rig = self.rig()
        rig.ready()
        rig.send(9991)
        rig.until(lambda: rig.robot.state == "동작")
        start = len(rig.log)
        self.assertEqual(rig.send(3333), (7, 1))
        rig.until(lambda: rig.raw == 8)              # 이동 끝
        rig.scan(3000)                               # 잘못 받았다면 3초 뒤 그리퍼 자리가 바뀐다
        self.assertEqual({(r["D01000"] - 1) // 3 % 2 for r in rig.log[start:]}, {0})
        self.assertFalse(any(r["G_CLOSE"] for r in rig.log[start:]))

    def test_row6_script1_only(self):
        rig = self.rig()
        rig.ready()
        start = len(rig.log)
        self.assertEqual(rig.send(9991), (1, 0))
        rig.until(lambda: rig.raw == 2)
        rig.scan(3500)
        self.assertEqual(rig.changes("D01000", start), [1, 2])
        self.assertFalse(any(r["G_CLOSE"] or r["G_OPEN"] for r in rig.log[start:]))

    def test_row7_gripper_twice(self):
        rig = self.rig()
        rig.ready()
        self.assertEqual(rig.send(3333), (2, 0))
        start = len(rig.log)
        rig.scan(3000)
        self.assertEqual(rig.changes("D01000", start), [2, 5])   # 그 사이 2, 3000 ms 뒤 5
        self.assertEqual(rig.send(3333), (5, 0))
        t0 = rig.t
        rig.scan(1000)
        self.assertEqual(rig.send(3333), (11, 1))    # 3초 안에 보낸 세 번째: 거부, 그리퍼는 아직 닫힘
        # PLC 자리는 다음 접수까지 1로 남는다(spec 2.3). 2222로 지워야 아래 0002가 보인다.
        self.assertEqual(rig.send(2222), (5, 0))
        rig.scan(t0 + 3000 - rig.t)
        self.assertEqual(rig.raw, 2)

    def test_row7_back_to_back_takes_full_3s(self):
        for same_scan in (False, True):
            with self.subTest(same_scan=same_scan):
                rig = self.rig()
                rig.ready()
                rig.send(3333)
                if same_scan:
                    rig.scan(2990)                   # 닫기가 끝나는 그 스캔에 접수 (TGRIP의 !TGRIP 확인)
                else:
                    rig.until(lambda: rig.raw == 5)  # 닫힘이 된 바로 다음 스캔에 접수
                self.assertEqual(rig.send(3333), (5, 0))
                start = len(rig.log)
                rig.scan(2900)
                self.assertEqual(rig.changes("D01000", start), [5])
                rig.until(lambda: rig.raw == 2, 200)

    def test_row8_unknown_code(self):
        rig = self.rig()
        rig.ready()
        self.assertEqual(rig.send(1234), (8, 1))
        self.assertEqual(rig.plc.word("D01001"), 0)
        self.assertEqual(rig.send(2222), (2, 0))

    def test_row9_init_rejects(self):
        for code in (1111, 9991, 9992, 9994, 3333):
            with self.subTest(code=code):
                rig = self.rig()
                rig.scan(100)
                self.assertEqual(rig.send(code)[1], 1)
                rig.scan(3000)
                self.assertEqual([r["G_OPEN"] for r in rig.log],
                                 [int(r["t"] < 3000) for r in rig.log])

    def test_row9_restart_clears_latches(self):
        for estop in (False, True):
            with self.subTest(estop=estop):
                rig = self.rig()
                rig.ready()
                rig.send(1111)
                rig.send(9991)                       # 이동1 요청 중 (DI0)
                if estop:
                    rig.until(lambda: rig.robot.state == "동작")
                    rig.send(9995)                   # 즉시 정지 중 (DI3)
                before = rig.log[-1]
                self.assertEqual((before["AUTO"], before["DI3" if estop else "DI0"]), (1, 1))
                rig.plc.write_word("D01001", 9992)   # 처리 전 우편함
                rig.plc.first = True                 # STOP → RUN
                rig.scan()
                self.assertEqual([rig.log[-1][s] for s in ("DI0", "DI1", "DI3", "AUTO")],
                                 [0, 0, 0, 0])
                self.assertEqual(rig.plc.word("D01001"), 0)
                rig.scan()
                self.assertIn(1, [r["G_OPEN"] for r in rig.log[-2:]])

    def test_first_scan_ignores_stale_mailbox(self):
        rig = self.rig()
        rig.plc.write_word("D01001", 1111)
        rig.scan()
        self.assertEqual(rig.plc.word("D01001"), 0)
        self.assertEqual(rig.log[-1]["AUTO"], 0)
        self.assertEqual((rig.raw - 1) // 6, 0)

    def test_accept_scan_shows_moving(self):
        rig = self.rig()
        rig.ready()
        self.assertEqual((rig.send(9991)[0] - 1) % 3, 0)
        rig.until(lambda: rig.raw == 2)
        self.assertEqual((rig.send(9992)[0] - 1) % 3, 0)

    def test_estop_cancels_pending_request(self):
        rig = self.rig(VirtualRobot(ignore_requests=True))
        rig.ready()
        rig.send(1111)
        self.assertEqual(rig.send(9991), (1, 0))
        start = len(rig.log)
        rig.scan(500)
        self.assertTrue(all(r["DI0"] and r["D01000"] == 1 for r in rig.log[start:]))
        self.assertEqual(rig.send(9995), (3, 0))
        self.assertEqual(rig.log[-1]["DI0"], 0)
        rig.scan(500)                                # 로봇 DO1이 켜진다
        rig.send(9993)
        rig.until(lambda: rig.raw == 2, 3 * DT)      # 접수 스캔에는 DO1이 남아 있을 수 있다
        start = len(rig.log)
        rig.scan(1000)
        self.assertFalse(any(r["G_CLOSE"] for r in rig.log[start:]))

    def test_no_end_while_estop_even_if_do0_drops(self):
        rig = self.rig()
        rig.ready()
        rig.send(1111)
        rig.send(9991)
        rig.until(lambda: rig.robot.state == "동작")
        rig.scan(500)
        rig.send(9995)
        rig.force = {"DO0": 0, "DO1": 1}             # 대안 방식(spec 8.4): 일시정지 중 DO0 꺼짐
        rig.scan(1000)
        rig.force = {"DO0": 0, "DO1": 0}             # 둘 다 꺼진 순간도 끝이 아니다
        rig.scan(500)
        self.assertEqual(rig.changes("G_CLOSE"), [0])
        rig.send(9993)
        rig.force = {}                               # 로봇 값(DO0=1, DO1=0)으로 돌린다
        rig.until(lambda: rig.raw == 5)
        self.assertEqual(rig.changes("G_CLOSE"), [0, 1, 0])   # 자동 닫기 한 번

    def test_gripper_outputs_never_both_on(self):
        """위 시나리오를 모두 다시 돌려 모은 스캔에서 G_CLOSE와 G_OPEN이 함께 켜진 적이 없다."""
        for name in dir(self):
            if name.startswith("test_") and name != self._testMethodName:
                getattr(self, name)()
        rows = [r for rig in self.rigs for r in rig.log]
        self.assertTrue(rows)
        self.assertFalse(any(r["G_CLOSE"] and r["G_OPEN"] for r in rows))


def run(robot, di, s, dt=0.01):
    """di를 s초 동안 넣는다. 매 step의 출력 목록."""
    return [robot.step(di, dt) for _ in range(round(s / dt))]


class VirtualRobotTest(unittest.TestCase):
    def test_start_move_end(self):
        r = VirtualRobot()
        out = run(r, {"DI0": 1}, 0.19)
        self.assertEqual((r.state, out[-1]["DO0"]), ("시작", 0))
        self.assertEqual(run(r, {"DI0": 1}, 0.01)[-1]["DO0"], 1)     # 0.2초 뒤 DO0
        out = run(r, {"DI0": 1}, 1.99)
        self.assertTrue(all(o["DO0"] for o in out))
        self.assertEqual(r.state, "동작")
        self.assertEqual(run(r, {"DI0": 1}, 0.01)[-1]["DO0"], 1)     # 2초 동안 움직임. 요청이 켜져 있으면 DO0 유지
        self.assertEqual(r.state, "끝대기")
        out = run(r, {"DI0": 1}, 1.0)                                # 요청이 켜져 있으면 다시 안 함
        self.assertEqual((r.state, out[-1]["DO0"]), ("끝대기", 1))
        self.assertEqual(run(r, {"DI0": 0}, 0.01)[-1]["DO0"], 0)     # 요청이 꺼지면 DO0을 끈다 (2026-11-10 강사 결정)
        self.assertEqual(r.state, "쉼")

    def test_stop_and_resume(self):
        r = VirtualRobot()
        run(r, {"DI0": 1}, 0.2)
        run(r, {}, 1.0)                                              # 이동 1초, 남은 1초
        out = run(r, {"DI3": 1}, 0.3)
        self.assertEqual([o["DO1"] for o in out].index(1), 29)      # 0.3초 뒤 DO1
        out = run(r, {"DI3": 1}, 2.0)                                # 남은 시간이 멈춤
        self.assertTrue(all(o == {"DO0": 1, "DO1": 1} for o in out))
        out = run(r, {}, 1.0)
        self.assertEqual(out[0], {"DO0": 1, "DO1": 0})
        # 감속 0.3초 동안은 움직였으므로 남은 시간은 약 0.7초
        self.assertAlmostEqual([o["DO0"] for o in out].index(0) * 0.01, 0.7, delta=0.02)
        self.assertEqual(r.state, "쉼")

    def test_estop_while_idle_sets_do1(self):
        r = VirtualRobot()
        out = run(r, {"DI3": 1}, 0.5)
        self.assertEqual([o["DO1"] for o in out].index(1), 29)      # 0.3초 뒤 DO1
        self.assertEqual((r.state, out[-1]["DO0"]), ("쉼", 0))
        self.assertEqual(run(r, {}, 0.01)[-1]["DO1"], 0)

    def test_ignore_requests(self):
        r = VirtualRobot(ignore_requests=True)
        out = run(r, {"DI0": 1, "DI1": 1}, 3.0)
        self.assertFalse(any(o["DO0"] for o in out))
        self.assertEqual(r.state, "쉼")


if __name__ == "__main__":
    unittest.main()

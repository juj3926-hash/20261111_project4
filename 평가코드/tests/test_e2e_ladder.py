import tempfile
import unittest
from pathlib import Path

from tools.xgwx.ladder_src import devices, load
from tools.xgwx.sim import Plc

SRC = Path(__file__).resolve().parents[1] / "ladder" / "e2e_pickplace.ld.txt"
DT = 10
EXISTING = {"P00000", "P00001", "P00007", "P00008", "P00041", "P00042", "P00046",
            "M00100", "M00101", "M00102", "M00103", "T0100"}
# 시험은 디바이스를 원본의 MAP에서 읽는다. 그리퍼 MAP을 바꿔도 같은 시험이 돈다(최종 리뷰 I3).
SIGNALS = ("DI0", "DI1", "DO0", "DO1", "GRIP", "RELEASE")


class Robot:
    """spec 5절 계약: 요청이 켜지는 순간 종료를 끄고, move_ms 뒤에 종료를 켠다."""

    def __init__(self, sym, move_ms=500, do0=0, do1=0):
        self.pairs = ((sym["DI0"], sym["DO0"]), (sym["DI1"], sym["DO1"]))
        self.move_ms, self.do = move_ms, {sym["DO0"]: do0, sym["DO1"]: do1}
        self.prev = {req: 0 for req, _ in self.pairs}
        self.left = {}

    def step(self, plc):
        for req, done in self.pairs:
            v = plc.bit(req)
            if v and not self.prev[req]:
                self.do[done] = 0
                self.left[done] = self.move_ms
            self.prev[req] = v
        for done in list(self.left):
            self.left[done] -= DT
            if self.left[done] <= 0:
                self.do[done] = 1
                del self.left[done]
        return dict(self.do)


def run(plc, robot, ms, sym):
    """ms 동안 스캔하며 매 스캔의 D01000과 기호 신호(DI0, GRIP …)를 기록한다."""
    log = []
    for _ in range(ms // DT):
        plc.scan(robot.step(plc), DT)
        log.append({"D01000": plc.word("D01000"), **{s: plc.bit(sym[s]) for s in SIGNALS}})
    return log


def changes(log, key="D01000"):
    out = []
    for row in log:
        if not out or out[-1] != row[key]:
            out.append(row[key])
    return out


class E2ELadderTest(unittest.TestCase):
    def setUp(self):
        self.use(SRC)

    def use(self, src):
        source = load(src)
        self.sym = source.symbols
        self.plc = Plc(source.rungs)

    def robot(self, **kw):
        return Robot(self.sym, **kw)

    def run_ms(self, robot, ms):
        return run(self.plc, robot, ms, self.sym)

    def test_first_scan_clears_stale_1111(self):
        self.plc.write_word("D01000", 1111)
        log = self.run_ms(self.robot(), 200)
        self.assertEqual(changes(log), [0])
        self.assertFalse(any(r["DI0"] for r in log))

    def test_one_cycle(self):
        self._check_one_cycle()

    def test_one_cycle_after_gripper_remap(self):
        """최종 리뷰 I3: 문서 절차대로 MAP GRIP/RELEASE만 바꿔도 같은 시험이 통과해야 한다."""
        text = (SRC.read_text(encoding="utf-8")
                .replace("MAP GRIP M00320", "MAP GRIP P00047")
                .replace("MAP RELEASE M00321", "MAP RELEASE P00048"))
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "remap.ld.txt"
            p.write_text(text, encoding="utf-8")
            self.use(p)
        self.assertEqual((self.sym["GRIP"], self.sym["RELEASE"]), ("P00047", "P00048"))
        self._check_one_cycle()

    def _check_one_cycle(self):
        robot = self.robot()
        self.run_ms(robot, 50)
        self.plc.write_word("D01000", 1111)
        log = self.run_ms(robot, 8000)
        self.assertEqual(changes(log), [1111, 2222, 4444, 3333, 5555])
        grip = [r for r in log if r["D01000"] == 2222]
        release = [r for r in log if r["D01000"] == 3333]
        self.assertGreaterEqual(len(grip) * DT, 2000)
        self.assertTrue(all(r["GRIP"] and not r["RELEASE"] for r in grip[1:]))
        self.assertTrue(all(r["RELEASE"] and not r["GRIP"] for r in release[1:]))
        self.assertTrue(all(r["DI0"] for r in log if r["D01000"] == 1111))
        self.assertFalse(any(r["DI0"] or r["DI1"] for r in log if r["D01000"] == 5555))

    def test_stale_done_does_not_skip(self):
        robot = self.robot(move_ms=500, do0=1)
        self.run_ms(robot, 50)
        self.plc.write_word("D01000", 1111)
        log = self.run_ms(robot, 400)
        self.assertEqual(changes(log), [1111])
        self.assertTrue(all(r["DI0"] for r in log[1:]))

    def test_second_cycle_after_rewrite(self):
        robot = self.robot()
        self.run_ms(robot, 50)
        self.plc.write_word("D01000", 1111)
        self.run_ms(robot, 8000)
        self.plc.write_word("D01000", 1111)
        self.assertEqual(changes(self.run_ms(robot, 8000)), [1111, 2222, 4444, 3333, 5555])

    def test_1111_mid_cycle_ignored(self):
        robot = self.robot()
        self.run_ms(robot, 50)
        self.plc.write_word("D01000", 1111)
        self.run_ms(robot, 1000)                   # 잡기 중 (2222)
        self.plc.write_word("D01000", 1111)
        log = self.run_ms(robot, 7000)
        self.assertEqual(changes(log)[-3:], [4444, 3333, 5555])

    def test_other_values_ignored(self):
        robot = self.robot()
        for v in (100, 200, 250, 0):
            self.plc.write_word("D01000", v)
            log = self.run_ms(robot, 300)
            self.assertFalse(any(r["DI0"] for r in log), v)

    def test_no_devices_of_existing_program(self):
        self.assertEqual(devices(load(SRC)) & EXISTING, set())


if __name__ == "__main__":
    unittest.main()

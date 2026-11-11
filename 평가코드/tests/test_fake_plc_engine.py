"""가짜 PLC 스캔 엔진 (docs/handoff/fake-plc.md 3.6, 3.7). 같은 래더를 .ld.txt(sim)와 .bin(해석기)으로 연다."""

import asyncio
import tempfile
import unittest
from collections import Counter
from pathlib import Path

from tools.xgwx import codec
from tools.xgwx.codec import RungRec, make_box, make_contact, make_hline

from fake_plc.engine import CATCH_UP, GUESS, LATCH, ORDER, Engine, scan_forever
from tests.test_fake_plc_diagram import E2E_XGWX, encoded, write_two_programs


class EngineTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def engines(self, text):
        """래더 원본 text를 .ld.txt와 .bin으로 써서 연 엔진 두 대: [(파일 이름, 엔진)…]."""
        (self.dir / "t.ld.txt").write_text(text, encoding="utf-8")
        (self.dir / "t.bin").write_bytes(encoded(text))
        out = []
        for name in ("t.ld.txt", "t.bin"):
            e = Engine()
            e.open(self.dir / name)
            out.append((name, e))
        return out

    def test_first_scan_each_run(self):
        for name, e in self.engines("OUT M00001 := F0009B"):
            with self.subTest(name):
                seen = []
                for _ in range(2):
                    e.run()
                    for _ in range(2):
                        e.scan()
                        seen.append(e.plc.bit("M00001"))
                self.assertEqual(seen, [1, 0, 1, 0])

    def test_timer_units(self):
        for name, e in self.engines("TON T0200 20 := P00000\nTON T0500 20 := P00000"):
            with self.subTest(name):
                e.forced["P00000"] = 1
                e.run()
                first = {}                                # 타이머 → 완료 비트가 처음 켜진 스캔 번호
                for n in range(1, 201):
                    e.scan()
                    for t in ("T0200", "T0500"):
                        if e.plc.bit(t) and t not in first:
                            first[t] = n
                # 2000 ms(T0000~T0499는 100 ms 단위), 200 ms(T0500부터 10 ms 단위)
                self.assertEqual(first, {"T0200": 200, "T0500": 20})

    def test_outputs_in_written_order(self):
        text = ("SET M00001, RST M00001 := F0009B\nRST M00002, SET M00002 := F0009B\n"
                "MOV 1 D00001, MOV 2 D00001 := F0009B")
        for name, e in self.engines(text):
            with self.subTest(name):
                e.run()
                e.scan()
                self.assertEqual((e.plc.bit("M00001"), e.plc.bit("M00002"), e.words["D00001"]), (0, 1, 2))

    def test_run_clears_bits_keeps_words(self):
        for name, e in self.engines("SET M00001 := P00000\nTON T0000 10 := P00000\nMOV 7 D00001 := P00000"):
            with self.subTest(name):
                e.forced["P00000"] = 1
                e.run()
                for _ in range(5):
                    e.scan()
                self.assertEqual((e.plc.bit("M00001"), e.plc.acc["T0000"], e.words), (1, 50, {"D00001": 7}))
                e.run()
                self.assertEqual((e.plc.bits, e.plc.acc, e.plc.words), ({}, {}, {"D00001": 7}))

    def test_open_clears_words_in_place(self):
        # 채점 때 학생 파일을 차례로 열어도 앞 파일의 D01000·D01001이 남아 그럴듯한 묵은 상태를 보이지 않는다
        for name, e in self.engines("MOV 7 D01000 := F0009B"):
            with self.subTest(name):
                words = e.words                           # Modbus 서버가 같은 dict를 읽고 쓴다
                e.run()
                e.scan()
                self.assertEqual(words, {"D01000": 7})
                e.open(self.dir / name)
                self.assertIs(e.words, words)
                self.assertEqual(words, {})

    def test_stop_freezes(self):
        for name, e in self.engines("TON T0000 10 := P00000\nOUT M00001 := P00000"):
            with self.subTest(name):
                e.forced["P00000"] = 1
                e.run()
                e.scan()
                e.stop()
                e.forced["P00000"] = 0
                before = (dict(e.plc.bits), dict(e.plc.acc))
                for _ in range(3):
                    e.scan()
                self.assertEqual((e.plc.bits, e.plc.acc), before)

    def test_force_release_falls_back_to_wiring_or_zero(self):
        # 스캔은 들어온 입력만 쓴다. 강제를 풀면 배선 값(없으면 0)을 한 번 써 줘야 묵은 값이 남지 않는다.
        for name, e in self.engines("OUT M00001 := P00004\nOUT M00002 := P00000"):
            with self.subTest(name):
                e.wired["P00004"] = 1
                e.forced.update({"P00004": 0, "P00000": 1})
                e.run()
                e.scan()
                self.assertEqual((e.plc.bit("M00001"), e.plc.bit("M00002")), (0, 1))
                e.release("P00004")
                e.release("P00000")
                e.scan()
                self.assertEqual(e.forced, {})
                self.assertEqual([e.plc.bit(d) for d in ("P00004", "M00001", "P00000", "M00002")], [1, 1, 0, 0])

    def test_force_beats_wiring(self):
        for name, e in self.engines("OUT M00001 := P00004\nOUT M00002 := P00005"):
            with self.subTest(name):
                e.wired.update({"P00004": 1, "P00005": 1})
                e.forced["P00004"] = 0
                e.run()
                e.scan()
                self.assertEqual((e.plc.bit("M00001"), e.plc.bit("M00002")), (0, 1))

    def test_programs_run_in_xml_order(self):
        # 둘째 프로그램이 첫 프로그램의 첫 스캔 SET M00310을 같은 스캔에 본다. 첫 프로그램 끝의 END가 둘째를 막지 않는다.
        # 두 프로그램 모두 렁 0의 같은 자리(행 0, 칸 0)에 상승 접점이 있다. 에지 기억은 프로그램마다 따로다.
        write_two_programs(self.dir / "two.xgwx", encoded("OUT M00002 := ^P00000\nOUT M00003 := M00310"),
                           first=encoded("OUT M00001 := ^P00000\nSET M00310 := F0009B"))
        e = Engine()
        e.open(self.dir / "two.xgwx")
        e.run()
        e.scan()
        self.assertEqual([name for name, _ in e.programs], ["E2E_PICKNPLACE", "SECOND"])
        self.assertEqual(e.plc.bit("M00003"), 1)
        e.forced["P00000"] = 1
        e.scan()
        self.assertEqual((e.plc.bit("M00001"), e.plc.bit("M00002")), (1, 1))

    def test_refuses_run_when_unsupported(self):
        ctu = codec.encode([RungRec(0, [make_contact("P00000", 1, 0), make_hline(4, 0, 85),
                                        make_box("CTU,C0001,10", 88, 0)])])
        write_two_programs(self.dir / "two.xgwx", ctu)
        e = Engine()
        e.open(self.dir / "two.xgwx")
        self.assertEqual(e.problems, ["SECOND: 미지원: 렁 0, 요소 BOX:CTU (kind=0x22, code=925)"])
        with self.assertRaises(ValueError):
            e.run()
        self.assertFalse(e.running)

    def test_open_records_errors(self):
        # 열 수 없는 파일은 예외 대신 problems 한 줄. RUN은 거부한다.
        (self.dir / "bad.bin").write_bytes(b"\x00\x01\x02")
        (self.dir / "bad.ld.txt").write_text("OUT M00001 := ??", encoding="utf-8")
        for name in ("missing.xgwx", "bad.bin", "bad.ld.txt"):
            with self.subTest(name):
                e = Engine()
                e.open(self.dir / name)
                self.assertEqual(len(e.problems), 1)
                self.assertTrue(e.problems[0].startswith("열 수 없음: "), e.problems)
                self.assertRaises(ValueError, e.run)

    def test_open_chooses_programs(self):
        write_two_programs(self.dir / "two.xgwx", encoded("OUT M00002 := P00000"),
                           first=encoded("SET M00001 := F0009B"))
        e = Engine()
        e.open(self.dir / "two.xgwx", names=["SECOND"])
        self.assertEqual((e.names, [name for name, _ in e.programs]), (["E2E_PICKNPLACE", "SECOND"], ["SECOND"]))
        e.forced["P00000"] = 1
        e.run()
        e.scan()
        self.assertEqual((e.plc.bit("M00001"), e.plc.bit("M00002")), (0, 1))

    def test_warnings(self):
        e = Engine()
        e.open(E2E_XGWX)                                  # 상승 접점, SET, RST가 있다
        self.assertEqual(e.warnings, [LATCH, GUESS])
        (self.dir / "plain.ld.txt").write_text("OUT M00001 := P00000", encoding="utf-8")
        e.open(self.dir / "plain.ld.txt")
        self.assertEqual(e.warnings, [LATCH])
        write_two_programs(self.dir / "two.xgwx", encoded("OUT M00002 := P00000"))
        e.open(self.dir / "two.xgwx")
        self.assertEqual(e.warnings, [LATCH, GUESS, ORDER])


class FakeTime:
    """가짜 시계. sleep은 Windows처럼 늘 15.6 ms 이상 걸리고, stall_at번째 sleep은 stall초를 더 잔다."""

    def __init__(self, stall_at=None, stall=0.0):
        self.now, self.sleeps, self.stall_at, self.stall = 0.0, 0, stall_at, stall

    def clock(self):
        return self.now

    async def sleep(self, seconds):
        self.sleeps += 1
        self.now += max(seconds, 0.0156) + (self.stall if self.sleeps == self.stall_at else 0)


class Done(Exception):
    pass


class ScanLoopTest(unittest.TestCase):
    def scan_times(self, count, fake):
        """scan_forever를 가짜 시계로 count스캔 돌리고 스캔마다의 시각을 돌려준다."""
        times = []

        def scan():
            times.append(fake.now)
            if len(times) == count:
                raise Done
        with self.assertRaises(Done):
            asyncio.run(scan_forever(scan, clock=fake.clock, sleep=fake.sleep))
        return times

    def test_catch_up_rate(self):
        times = self.scan_times(300, FakeTime())
        for k, t in enumerate(times):                 # k번째 스캔은 마감 k·10 ms 뒤, sleep 한 번 안에 돈다
            self.assertTrue(k * 0.01 - 1e-9 <= t <= k * 0.01 + 0.0157, (k, t))

    def test_catch_up_cap(self):
        times = self.scan_times(300, FakeTime(stall_at=50, stall=0.5))   # 0.5초 동안 깨어나지 못했다
        burst_at, burst = Counter(times).most_common(1)[0]
        self.assertEqual(burst, CATCH_UP)             # 깨어나서 10스캔만 몰아서 돌린다
        after = [t for t in times if t > burst_at]
        self.assertAlmostEqual(after[50] - after[0], 0.5, delta=0.016)   # 그 뒤는 다시 10 ms마다 (밀린 스캔은 버림)

    def test_real_time_rate(self):
        async def count_for(seconds):
            scans = []
            task = asyncio.create_task(scan_forever(lambda: scans.append(1)))
            await asyncio.sleep(seconds)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            return len(scans)
        n = asyncio.run(count_for(0.6))
        self.assertTrue(50 <= n <= 70, n)             # 60 ± 100 ms 여유 (윈도우 sleep ≈ 16 ms)


if __name__ == "__main__":
    unittest.main()

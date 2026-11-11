import unittest

from tools.xgwx.ladder_src import parse
from tools.xgwx.sim import Plc


def plc(text):
    return Plc(parse(text).rungs)


class SimTest(unittest.TestCase):
    def test_first_scan_flag(self):
        p = plc("SET M00001 := F0009B\nOUT M00002 := F0009B")
        p.scan(); self.assertEqual((p.bit("M00001"), p.bit("M00002")), (1, 1))
        p.scan(); self.assertEqual((p.bit("M00001"), p.bit("M00002")), (1, 0))

    def test_rising_edge_one_scan(self):
        p = plc("OUT M00001 := ^P00000")
        seen = []
        for v in (0, 1, 1, 0, 1):
            p.scan({"P00000": v}); seen.append(p.bit("M00001"))
        self.assertEqual(seen, [0, 1, 0, 0, 1])

    def test_ton_bases(self):
        p = plc("TON T0200 20 := M00001\nTON T0500 20 := M00001\nSET M00001 := F0009B")
        p.scan()                                   # M00001은 이 스캔 끝에 켜짐
        for _ in range(19): p.scan()               # 190 ms
        self.assertEqual((p.bit("T0200"), p.bit("T0500")), (0, 0))
        p.scan()                                   # 200 ms
        self.assertEqual((p.bit("T0200"), p.bit("T0500")), (0, 1))
        for _ in range(180): p.scan()              # 2000 ms
        self.assertEqual(p.bit("T0200"), 1)

    def test_mov_compare_brst(self):
        p = plc("MOV 1111 D01000 := F0009B\nOUT M00001 := [= D01000 1111]\n"
                "MOV D01000 D01001 := M00001\nBRST M0031E 3 := M00002")
        p.scan()
        self.assertEqual((p.word("D01000"), p.bit("M00001"), p.word("D01001")), (1111, 1, 1111))
        for d in ("M0031E", "M0031F", "M00320", "M00321"):
            p.bits[d] = 1
        p.scan({"M00002": 1})
        self.assertEqual([p.bit(d) for d in ("M0031E", "M0031F", "M00320", "M00321")], [0, 0, 0, 1])

    def test_write_word(self):
        p = plc("OUT M00001 := [= D01000 1111]")
        p.write_word("D01000", 1111); p.scan()
        self.assertEqual(p.bit("M00001"), 1)


if __name__ == "__main__":
    unittest.main()

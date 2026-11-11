import unittest

from tools.xgwx.ladder_src import Action, Cmp, Lit, ParseError, devices, parse

GOOD = """
# 주석
MAP DO0 P00004 ; 로봇 DO0
SET M00312, RST M00311, MOV 2222 D01000 := ^DO0 & M00311 ; 움직임1 종료
OUT M00300 := [= D01000 1111] ; 시작 명령
OUT P00041 := M00101 | !M00102
TON T0200 20 := M00312
BRST M00310 5, SET M00310, MOV 0 D01000 := F0009B
"""


class ParseTest(unittest.TestCase):
    def test_good(self):
        src = parse(GOOD)
        self.assertEqual(src.symbols, {"DO0": "P00004"})
        r = src.rungs
        self.assertEqual(r[0].actions, [Action("SET", ("M00312",)), Action("RST", ("M00311",)),
                                        Action("MOV", ("2222", "D01000"))])
        self.assertEqual(r[0].branches, [[Lit("P00004", edge=True), Lit("M00311")]])
        self.assertEqual((r[0].comment, r[0].line), ("움직임1 종료", 4))
        self.assertEqual(r[1].branches, [[Cmp("=", "D01000", "1111")]])
        self.assertEqual(r[2].branches, [[Lit("M00101")], [Lit("M00102", neg=True)]])
        self.assertEqual(r[3].actions, [Action("TON", ("T0200", "20"))])
        self.assertEqual(r[4].actions[0], Action("BRST", ("M00310", "5")))

    def test_devices_expands_brst(self):
        d = devices(parse(GOOD))
        self.assertTrue({"M00310", "M00311", "M00312", "M00313", "M00314"} <= d)
        self.assertNotIn("M00315", d)
        self.assertTrue({"P00004", "D01000", "T0200", "F0009B", "P00041"} <= d)
        self.assertNotIn("2222", d)

    def test_errors(self):
        bad = {
            "OUT XX := M00300": "기호",
            "OUT M3 := M00300": "디바이스",
            "FOO M00300 := M00301": "동작",
            "MOV 1 M00300 := M00301": "MOV",
            "TON M00300 20 := M00301": "TON",
            "SET M00300, RST M00301 := M00310 | M00311": "가지",
            "OUT M00300 := ": "조건",
            "OUT M00300 := [= D01000 abc]": "비교",
            "OUT M00300 := M00301 ; " + "가" * 201: "설명",
            "MAP A1 P00004\nMAP A1 P00005": "MAP",
            "MAP A1 Q00004": "MAP",
            # 최종 리뷰 I2: 워드 부분은 4자리 10진, 비트만 16진
            "OUT M003A0 := M00300": "디바이스",
            "BRST M0FFFF 3 := M00300": "디바이스",
            # 최종 리뷰 I1: BMP 밖 문자(UTF-16 두 단위)는 설명에 못 씀
            "OUT M00300 := M00301 ; 잡기 \U0001F916 완료": "설명",
        }
        for text, word in bad.items():
            with self.subTest(text=text[:40]), self.assertRaises(ParseError) as cm:
                parse(text)
            self.assertRegex(str(cm.exception), r"^\d+행: ")
            self.assertIn(word, str(cm.exception))


if __name__ == "__main__":
    unittest.main()

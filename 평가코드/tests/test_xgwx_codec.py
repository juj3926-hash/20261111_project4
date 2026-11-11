import unittest
from pathlib import Path

from tools.xgwx import codec

SAMPLES = Path(__file__).parent / "samples" / "xgwx"
BLOBS = ("555_main.bin", "055_main.bin", "07_start_main.bin", "08_home_main.bin")


class FidelityTest(unittest.TestCase):
    def test_samples_round_trip(self):
        for name in BLOBS:
            with self.subTest(name):
                raw = (SAMPLES / name).read_bytes()
                self.assertEqual(codec.encode(codec.decode(raw)), raw)

    def test_samples_contain_needed_elements(self):
        def texts(name):
            rungs = codec.decode((SAMPLES / name).read_bytes())
            return [(codec.name_of(e), e.fields.get("text") or e.fields.get("device"))
                    for r in rungs for e in r.elements]
        self.assertIn(("BOX:=", "=,D01000,1234"), texts("055_main.bin"))
        self.assertIn(("BOX:MOV", "MOV,1,D00100"), texts("07_start_main.bin"))
        self.assertIn(("CONTACT_P", "P00000"), texts("08_home_main.bin"))
        self.assertIn(("CONTACT_NO", "F0009B"), texts("555_main.bin"))


class CStringTest(unittest.TestCase):
    """최종 리뷰 I1: CString 길이 바이트는 UTF-16 단위 수여야 한다."""

    def test_non_bmp_comment_round_trips(self):
        text = "잡기 \U0001F916 완료"
        raw = codec.encode([codec.RungRec(0, [codec.make_comment(text, 0)], text)])
        self.assertEqual(codec.decode(raw)[0].comment, text)

    def test_too_long_in_utf16_units(self):
        text = "\U0001D465" * 128                    # 128 글자 = 256 UTF-16 단위
        with self.assertRaises(ValueError):
            codec.encode([codec.RungRec(0, [codec.make_comment(text, 0)], text)])


class MakeBoxTest(unittest.TestCase):
    def test_make_box(self):
        e = codec.make_box("MOV,2222,D01000", 88, 5)
        self.assertEqual((e.kind, e.x, e.y, e.fields["code"]), (0x22, 88, 5, 148))
        self.assertEqual(codec.make_box("=,D01000,1111", 1, 0).fields["code"], 1254)
        self.assertEqual(codec.make_box("BRST,M00310,5", 88, 0).fields["code"], 357)
        with self.assertRaises(ValueError):
            codec.make_box("FOO,1", 88, 0)


if __name__ == "__main__":
    unittest.main()

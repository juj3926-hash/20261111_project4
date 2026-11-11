import unittest
from pathlib import Path

from tools.xgwx import codec
from tools.xgwx.ladder_src import load, parse
from tools.xgwx.layout import encoded, layout, shape

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "tests" / "samples" / "xgwx"


def sample(name, pred):
    rungs = codec.decode((SAMPLES / name).read_bytes())
    return next(r for r in rungs if pred(r))


def first_rung(line):
    recs = encoded(layout(parse(line).rungs))
    return next(r for r in recs if not r.comment)


def has(text):
    return lambda r: any((e.fields.get("text") or e.fields.get("device")) == text
                         for e in r.elements)


class SampleShapeTest(unittest.TestCase):
    CASES = [
        ("555_main.bin", lambda r: r.index == 0, "SET M00100 := F0009B"),
        ("555_main.bin", lambda r: r.index == 1,
         "SET M00101, RST M00100 := M00100 & P00000 & !P00001"),
        ("555_main.bin", lambda r: r.index == 3, "SET M00102, RST M00101 := M00101 & P00008"),
        ("555_main.bin", lambda r: r.index == 7, "TON T0100 10 := M00102"),
        ("555_main.bin", lambda r: r.index == 9, "OUT P00042 := M00100"),
        ("555_main.bin", lambda r: r.index == 10, "OUT P00041 := M00101 | M00102 | M00103"),
        ("055_main.bin", has("=,D01000,1234"), "OUT P00046 := [= D01000 1234]"),
    ]

    def test_layout_matches_xg5000_samples(self):
        for name, pred, line in self.CASES:
            with self.subTest(line=line):
                self.assertEqual(shape(first_rung(line)), shape(sample(name, pred)))


class LayoutTest(unittest.TestCase):
    def test_three_actions_with_mov(self):
        got = shape(first_rung("SET M00312, RST M00311, MOV 2222 D01000 := ^P00004 & M00311"))
        self.assertEqual(got, [
            ("CONTACT_P", 1, 0, "P00004", None), ("CONTACT_NO", 4, 0, "M00311", None),
            ("VLINE", 6, 0, None, None), ("HLINE", 7, 0, None, 91), ("SET", 94, 0, "M00312", None),
            ("VLINE_REF", 6, 0, None, None), ("VLINE", 6, 1, None, None),
            ("HLINE", 7, 1, None, 91), ("RST", 94, 1, "M00311", None),
            ("VLINE_REF", 6, 1, None, None), ("HLINE", 7, 2, None, 85),
            ("BOX:MOV", 88, 2, "MOV,2222,D01000", None), ("BOX_PART1", 88, 2, None, None),
            ("BOX_PART2", 88, 2, None, None),
        ])

    def test_too_many_contacts(self):
        cond = " & ".join("M%05X" % i for i in range(29))
        with self.assertRaises(ValueError):
            layout(parse("TON T0200 20 := " + cond).rungs)

    def test_e2e_program_structure(self):
        recs = layout(load(ROOT / "ladder" / "e2e_pickplace.ld.txt").rungs)
        self.assertEqual([r.index for r in recs], list(range(len(recs))))
        self.assertEqual(codec.name_of(recs[-1].elements[-1]), "END")
        again = encoded(recs)
        self.assertEqual([shape(r) for r in again], [shape(r) for r in encoded(again)])


if __name__ == "__main__":
    unittest.main()

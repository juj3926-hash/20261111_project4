import tempfile
import unittest
from collections import Counter
from pathlib import Path

from tools.xgwx import build, codec, container
from tools.xgwx.ladder_src import Cmp, Lit, load
from tools.xgwx.layout import encoded, layout, shape

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "tests" / "samples" / "xgwx" / "e2e_template.xgwx"
SRC = ROOT / "ladder" / "PICKNPLACE_FULL.ld.txt"
COMMITTED = ROOT / "ladder" / "PICKNPLACE_FULL.xgwx"
PROGRAM = "E2E_PICKNPLACE"       # 템플릿의 프로그램 이름


class PicknplaceBuildTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name) / "out.xgwx"

    def tearDown(self):
        self.tmp.cleanup()

    def body(self, path):
        return container.get_program_data(container.read(path), PROGRAM)

    def test_layout_fits(self):
        layout(load(SRC).rungs)

    def test_build_decodes_to_layout(self):
        build.build(SRC, TEMPLATE, self.out, PROGRAM)
        got = codec.decode(self.body(self.out))
        want = encoded(layout(load(SRC).rungs))
        self.assertEqual([shape(r) for r in got], [shape(r) for r in want])

    def test_only_program_data_changes(self):
        build.build(SRC, TEMPLATE, self.out, PROGRAM)
        t, o = container.read(TEMPLATE), container.read(self.out)
        self.assertNotEqual(container.get_program_data(o, PROGRAM), container.get_program_data(t, PROGRAM))
        mask = lambda x: container.set_program_data(x, b"", PROGRAM).xml
        self.assertEqual(mask(o), mask(t))
        self.assertEqual(o.trailer, t.trailer)
        th, oh = bytearray(t.header), bytearray(o.header)
        for a, b in ((0x40, 0x44), (0x86, 0x8a)):          # 체크섬, 길이
            th[a:b] = oh[a:b] = b"\0" * (b - a)
        self.assertEqual(oh, th)

    def test_out_coils_unique(self):
        outs = [a.args[0] for r in load(SRC).rungs for a in r.actions if a.op == "OUT"]
        self.assertEqual([d for d, n in Counter(outs).items() if n > 1], [])

    def test_only_verified_elements(self):
        rungs = load(SRC).rungs
        ops = {a.op for r in rungs for a in r.actions}
        self.assertLessEqual(ops, {"OUT", "SET", "RST", "TON", "MOV", "BRST"})
        lits = [l for r in rungs for b in r.branches for l in b]
        self.assertEqual({l.op for l in lits if isinstance(l, Cmp)} - {"="}, set())
        self.assertFalse([l for l in lits if isinstance(l, Lit) and l.edge])

    def test_committed_xgwx_is_fresh(self):
        build.build(SRC, TEMPLATE, self.out, PROGRAM)
        self.assertEqual(self.body(COMMITTED), self.body(self.out))


if __name__ == "__main__":
    unittest.main()

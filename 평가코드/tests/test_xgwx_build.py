import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from tools.xgwx import build, codec, container
from tools.xgwx.ladder_src import devices, load
from tools.xgwx.layout import encoded, layout, shape

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "tests" / "samples" / "xgwx"
TEMPLATE = SAMPLES / "e2e_template.xgwx"
SRC = ROOT / "ladder" / "e2e_pickplace.ld.txt"
NEW = "E2E_PICKNPLACE"          # 사용자가 XG5000에서 만든 템플릿의 프로그램 이름


class BuildTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name) / "out.xgwx"

    def tearDown(self):
        self.tmp.cleanup()

    def test_template_programs(self):
        self.assertEqual(container.program_names(container.read(TEMPLATE)), [NEW])

    def test_template_round_trip_bytes(self):
        container.write(container.read(TEMPLATE), self.out)
        self.assertEqual(self.out.read_bytes(), TEMPLATE.read_bytes())

    def test_only_e2e_program_changes(self):
        build.build(SRC, TEMPLATE, self.out)
        t, o = container.read(TEMPLATE), container.read(self.out)
        self.assertNotEqual(container.get_program_data(o, NEW), container.get_program_data(t, NEW))
        mask = lambda x: container.set_program_data(x, b"", NEW).xml
        self.assertEqual(mask(o), mask(t))
        self.assertEqual(o.trailer, t.trailer)
        th, oh = bytearray(t.header), bytearray(o.header)
        for a, b in ((0x40, 0x44), (0x86, 0x8a)):          # 체크섬, 길이
            th[a:b] = oh[a:b] = b"\0" * (b - a)
        self.assertEqual(oh, th)

    def test_e2e_program_decodes_to_layout(self):
        build.build(SRC, TEMPLATE, self.out)
        got = codec.decode(container.get_program_data(container.read(self.out), NEW))
        want = encoded(layout(load(SRC).rungs))
        self.assertEqual([shape(r) for r in got], [shape(r) for r in want])

    def test_no_overlap_with_existing_program(self):
        old = codec.decode((SAMPLES / "555_main.bin").read_bytes())   # 05_실습2_네상태
        strip = lambda s: {d for d in s if not d.startswith("F")}
        self.assertEqual(strip(build.program_devices(old)) & strip(devices(load(SRC))), set())
        self.assertIn("T0100", build.program_devices(old))

    def test_missing_program_fails_without_output(self):
        with self.assertRaises(ValueError):
            build.build(SRC, TEMPLATE, self.out, program="NOPE")
        self.assertFalse(self.out.exists())

    def test_unreadable_body_is_not_written(self):
        """최종 리뷰 I1: 만든 본문을 다시 읽어 확인하고, 깨졌으면 파일을 쓰지 않는다."""
        real = codec.encode
        build.codec.encode = lambda rungs: real(rungs) + b"\x00"
        try:
            with self.assertRaises(ValueError):
                build.build(SRC, TEMPLATE, self.out)
        finally:
            build.codec.encode = real
        self.assertFalse(self.out.exists())

    def test_cli(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(build.main([str(SRC), str(TEMPLATE), str(self.out)]), 0)
        self.assertTrue(self.out.exists())
        self.assertIn(NEW, buf.getvalue())


if __name__ == "__main__":
    unittest.main()

"""가짜 PLC 렁 그림: 파일 읽기와 해석기 (docs/handoff/fake-plc.md 3.1~3.5)."""

import itertools
import re
import tempfile
import unittest
from pathlib import Path

from tools.xgwx import codec, container, ladder_src
from tools.xgwx.codec import (
    Element, RungRec, make_box, make_coil, make_contact, make_end, make_hline, make_ton, make_vline)
from tools.xgwx.layout import layout

from fake_plc.diagram import DiagramPlc, fidelity, read_programs, unsupported

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "tests" / "samples" / "xgwx"
E2E_SRC = ROOT / "ladder" / "e2e_pickplace.ld.txt"
E2E_XGWX = ROOT / "ladder" / "e2e_pickplace.xgwx"


def encoded(text):
    """래더 원본 text를 XG5000 본문 바이트로."""
    return codec.encode(layout(ladder_src.parse(text).rungs))


def write_two_programs(path, second, first=None):
    """e2e_pickplace.xgwx 뒤에 프로그램 SECOND(본문 second)를 붙여 path에 쓴다.
    first를 주면 첫 프로그램(E2E_PICKNPLACE)의 본문도 바꾼다."""
    x = container.read(E2E_XGWX)
    m = re.search(rb"<Program\b.*?</Program>", x.xml, re.S)
    copy = m.group(0).replace(b">E2E_PICKNPLACE<Body", b">SECOND<Body")
    x.xml = x.xml[:m.end()] + copy + x.xml[m.end():]
    x = container.set_program_data(x, second, "SECOND")
    if first is not None:
        x = container.set_program_data(x, first, "E2E_PICKNPLACE")
    container.write(x, path)


class ReadProgramsTest(unittest.TestCase):
    def test_xgwx_matches_source(self):
        [(name, rungs)] = read_programs(E2E_XGWX)
        self.assertEqual(name, "E2E_PICKNPLACE")
        self.assertEqual(codec.encode(rungs), encoded(E2E_SRC.read_text(encoding="utf-8")))

    def test_bin(self):
        [(name, rungs)] = read_programs(SAMPLES / "055_main.bin")
        self.assertEqual((name, len(rungs)), ("055_main.bin", 19))

    def test_two_programs_in_xml_order(self):
        second = encoded("OUT M00001 := P00000")
        with tempfile.TemporaryDirectory() as d:
            write_two_programs(Path(d) / "two.xgwx", second)
            progs = read_programs(Path(d) / "two.xgwx")
        self.assertEqual([name for name, _ in progs], ["E2E_PICKNPLACE", "SECOND"])
        first = container.get_program_data(container.read(E2E_XGWX), "E2E_PICKNPLACE")
        self.assertEqual([codec.encode(rungs) for _, rungs in progs], [first, second])

    def test_empty_template(self):
        self.assertEqual(read_programs(SAMPLES / "e2e_template.xgwx"), [("E2E_PICKNPLACE", [])])


def decoded(*rungs):
    """요소 목록마다 렁 하나. 행 머리, 세로선 참조, 상자 부분은 encode가 채운다."""
    return codec.decode(codec.encode([RungRec(i, list(els)) for i, els in enumerate(rungs)]))


def from_text(text):
    return DiagramPlc([codec.decode(encoded(text))])


def sample_rung(name, n):
    return read_programs(SAMPLES / name)[0][1][n]


class FlowTest(unittest.TestCase):
    def test_rail_only_into_x1(self):
        p = DiagramPlc([decoded([make_hline(4, 0, 91), make_coil("M00001", 0)],     # x=1이 비었다
                                [make_hline(1, 1, 91), make_coil("M00002", 1)])])
        p.scan()
        self.assertEqual((p.bit("M00001"), p.bit("M00002")), (0, 1))

    def test_gap_breaks_flow(self):
        p = DiagramPlc([decoded(
            [make_contact("P00000", 1, 0), make_hline(7, 0, 91), make_coil("M00001", 0)],  # 칸 1이 비었다
            [make_contact("P00000", 1, 1), make_hline(4, 1, 91), make_coil("M00002", 1)])])
        p.scan({"P00000": 1})
        self.assertEqual((p.bit("M00001"), p.bit("M00002")), (0, 1))

    def test_or_branches(self):
        rung = sample_rung("555_main.bin", 10)             # M00101 | M00102 | M00103 → P00041
        for v in itertools.product((0, 1), repeat=3):
            p = DiagramPlc([[rung]])
            p.scan(dict(zip(("M00101", "M00102", "M00103"), v)))
            self.assertEqual(p.bit("P00041"), int(any(v)), v)
        p = from_text("OUT M00001 := P00000 | P00001 & P00002 | !P00003")   # 길이가 다른 가지
        for v in itertools.product((0, 1), repeat=4):
            p.scan(dict(zip(("P00000", "P00001", "P00002", "P00003"), v)))
            self.assertEqual(p.bit("M00001"), int(v[0] or (v[1] and v[2]) or not v[3]), v)

    def test_or_join_after_hlines(self):
        # 두 행 모두 가로선을 지나 칸 3의 경계(x=12)에서 합친다. 그 칸은 두 행 모두 가로선이다.
        p = DiagramPlc([decoded([make_contact("P00000", 1, 0), make_hline(4, 0, 10),
                                 *make_vline(12, 0), make_hline(13, 0, 91), make_coil("M00001", 0),
                                 make_contact("P00001", 1, 1), make_hline(4, 1, 10)])])
        for v in itertools.product((0, 1), repeat=2):
            p.scan(dict(zip(("P00000", "P00001"), v)))
            self.assertEqual(p.bit("M00001"), int(any(v)), v)

    def test_fan_out_only_through_vline(self):
        # 아래 두 행은 x=1~6이 비어 있다. 세로선으로만 첫 행의 조건을 받는다.
        p = from_text("SET M00001, RST M00002, MOV 5 D00001 := P00000 & P00001")
        p.bits["M00002"] = 1
        p.scan({"P00000": 1, "P00001": 0})
        self.assertEqual((p.bit("M00001"), p.bit("M00002"), p.word("D00001")), (0, 1, 0))
        p.scan({"P00000": 1, "P00001": 1})
        self.assertEqual((p.bit("M00001"), p.bit("M00002"), p.word("D00001")), (1, 0, 5))

    def test_nested_or_from_sample(self):
        # M0010F·P00002·M00201·(/P00005 | /P00007) → RST M00200, MOV 0 D00100, SET M00300, RST M0010F
        rung = sample_rung("08_home_main.bin", 44)
        for v in itertools.product((0, 1), repeat=5):
            p = DiagramPlc([[rung]])
            p.bits["M00200"] = 1
            p.write_word("D00100", 7)
            p.scan(dict(zip(("M0010F", "P00002", "M00201", "P00005", "P00007"), v)))
            on = int(v[0] and v[1] and v[2] and not (v[3] and v[4]))
            got = (p.bit("M00200"), p.word("D00100"), p.bit("M00300"), p.bit("M0010F"))
            self.assertEqual(got, (1 - on, 0 if on else 7, on, v[0] - on), v)

    def test_compare_boxes(self):
        p = from_text("OUT M00001 := [= D00001 5]\nOUT M00002 := [> D00001 5]\n"
                      "OUT M00003 := [< D00001 D00002]\nOUT M00004 := [>= D00001 5]\n"
                      "OUT M00005 := [<= D00001 5]")
        p.write_word("D00002", 5)
        for d in (4, 5, 6):
            p.write_word("D00001", d)
            p.scan()
            self.assertEqual([p.bit("M0000%d" % i) for i in range(1, 6)],
                             [int(d == 5), int(d > 5), int(d < 5), int(d >= 5), int(d <= 5)], d)
        p = from_text("TON T0000 100 := P00000\nOUT M00001 := [>= T0000 2]")   # 타이머 현재값
        for _ in range(19):
            p.scan({"P00000": 1})
        self.assertEqual(p.bit("M00001"), 0)               # 190 ms → 1
        p.scan({"P00000": 1})
        self.assertEqual(p.bit("M00001"), 1)               # 200 ms → 2

    def test_rising_edge_per_element(self):
        # 같은 디바이스의 상승 접점이 셋. 셋째는 전위가 없어도 매 스캔 에지 기억을 갱신한다.
        p = from_text("OUT M00001 := ^P00000\nOUT M00002 := ^P00000\nOUT M00003 := P00001 & ^P00000")
        seen = []
        for p0, p1 in ((0, 0), (1, 0), (1, 1), (0, 1), (1, 1)):
            p.scan({"P00000": p0, "P00001": p1})
            seen.append((p.bit("M00001"), p.bit("M00002"), p.bit("M00003")))
        self.assertEqual(seen, [(0, 0, 0), (1, 1, 0), (0, 0, 0), (0, 0, 0), (1, 1, 1)])

    def test_end_stops_program(self):
        p = DiagramPlc([decoded([make_hline(1, 0, 91), make_end(0)],
                                [make_hline(1, 1, 91), make_coil("M00001", 1)])])
        p.scan()
        self.assertEqual(p.bit("M00001"), 0)


class UnsupportedTest(unittest.TestCase):
    def test_unsupported_lines(self):
        rungs = decoded(
            [make_contact("P00000", 1, 0), make_hline(4, 0, 85), make_box("CTU,C0001,10", 88, 0)],
            [make_contact("P00000", 1, 1), make_hline(4, 1, 85), make_box("MOVP,1,D00001", 88, 1)],
            [Element(0x09, 1, 2, {"device": "P00000"}), make_hline(4, 2, 91), make_coil("M00001", 2)],
            [make_hline(1, 3, 85), Element(0x22, 88, 3, {"text": "ADD,D00001,1,D00002", "code": 1300})])
        want = ["미지원: 렁 0, 요소 BOX:CTU (kind=0x22, code=925)",
                "미지원: 렁 1, 요소 BOX:MOVP (kind=0x22, code=404)",
                "미지원: 렁 2, 요소 UNKNOWN_0x9 (kind=0x09, code=-)",
                "미지원: 렁 3, 요소 BOX:ADD (kind=0x22, code=1300)"]
        self.assertEqual(unsupported(rungs), want)
        with self.assertRaises(ValueError) as cm:
            DiagramPlc([rungs])
        self.assertEqual(str(cm.exception), "\n".join(want))

    def test_unsupported_operand(self):
        # 래더 원본 문법에 없는 피연산자. F는 F0009B만 뜻을 안다.
        rungs = decoded([make_contact("F00099", 1, 0), make_hline(4, 0, 91), make_coil("M00001", 0)],
                        [make_hline(1, 1, 85), make_box("MOV,h00FF,D00001", 88, 1)])
        self.assertEqual(unsupported(rungs), [
            "미지원: 렁 0, 요소 CONTACT_NO (kind=0x06, code=-), 피연산자 F00099",
            "미지원: 렁 1, 요소 BOX:MOV (kind=0x22, code=148), 피연산자 h00FF"])

    def test_fidelity_branch_contact(self):
        # A ─┬─[TON T0001]
        #    └─ T0001 ─(LAMP)      아래 행은 x=1이 비어 세로선으로만 전위를 받고, 거기에 접점이 있다.
        rungs = decoded([make_contact("P00000", 1, 0), *make_vline(3, 0), make_hline(4, 0, 85),
                         make_ton("T0001", 10, 0),
                         make_contact("T0001", 4, 1), make_hline(7, 1, 91), make_coil("P00040", 1)])
        self.assertEqual(fidelity(rungs), ["렁 0: 분기 뒤 접점 ― 실물(위→아래 실행)과 한 스캔 차이가 날 수 있음"])
        self.assertEqual(unsupported(rungs), [])            # 경고일 뿐 실행은 한다
        # A ─┬─ B ─┬─( )      출력이 하나면 경고하지 않는다 (C는 OR 묶음 안의 접점)
        #    └─ C ─┘
        one = decoded([make_contact("P00000", 1, 0), *make_vline(3, 0), make_contact("P00001", 4, 0),
                       *make_vline(6, 0), make_hline(7, 0, 91), make_coil("M00001", 0),
                       make_contact("P00002", 4, 1)])
        self.assertEqual(fidelity(one), [])

    def test_fidelity_quiet_for_reference(self):
        for src in (E2E_SRC, ROOT / "ladder" / "PICKNPLACE_FULL.ld.txt"):
            self.assertEqual(fidelity(codec.decode(encoded(src.read_text(encoding="utf-8")))), [], src.name)

    def test_box_without_text_is_one_line(self):
        # tag 1로 저장된 kind 0x22: 상자 부분처럼 text가 없다. KeyError 없이 3.5 한 줄로 거부한다.
        rungs = decoded([make_hline(1, 0, 85), Element(0x22, 88, 0, {"tag": 0x01})])
        self.assertEqual(unsupported(rungs), ["미지원: 렁 0, 요소 UNKNOWN_0x22 (kind=0x22, code=-)"])


if __name__ == "__main__":
    unittest.main()

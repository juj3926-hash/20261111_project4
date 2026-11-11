"""Rodi Script 실행기 (fake_robot/static/rodi.js, 노트 3.3)를 Node로 돌려 시험한다. node가 없으면 건너뛴다."""

import json
import shutil
import subprocess
import unittest
from pathlib import Path

from fake_robot.core import Robot

NODE = shutil.which("node")
BRIDGE = Path(__file__).parent / "fake_robot_samples" / "node_robot.js"
REFERENCE = Path(__file__).parent.parent / "robot" / "PICKNPLACE_FULL_main.js"
DT = 0.01
ZERO = [0.0] * 6
# 여기서 시작하면 참조 스크립트의 이동1(PICK [30, 0, 90, 0, 90, 0]까지)은 가장 큰 축이 45도 → 2초로 내장 스크립트의
# move_time과 같다. 그래서 LATE_STOP·LATE_PULSE의 1.9초는 양쪽 모두 이동이 끝나기 0.1초 전이다.
# 이동2(PICK → HOME)는 30도 → 1.5초다. SCENARIO는 이동2를 3초 기다리므로 양쪽 모두 끝난다
START = [0.0, 0.0, 45.0, 0.0, 45.0, 0.0]

# 합격 (a)의 DI 입력 열. PLC처럼 DO를 보고 다음 입력을 정한다(시각이 아니라 순서를 비교한다)
SCENARIO = [
    ("wait", 0.3),
    ("set", "DI3", 1), ("until", "DO1", 1), ("set", "DI3", 0), ("until", "DO1", 0),  # 쉬는 중 DI3
    ("set", "DI0", 1), ("until", "DO0", 1), ("set", "DI0", 0),                        # 이동1 요청. DO0을 보고 끈다
    ("wait", 0.5), ("set", "DI3", 1), ("until", "DO1", 1), ("wait", 0.5),             # 이동 중 즉시 정지
    ("set", "DI3", 0), ("until", "DO1", 0), ("until", "DO0", 0),                      # 재개 → 끝
    ("set", "DI1", 1), ("until", "DO0", 1), ("wait", 3.0),                            # 안 꺼지는 이동2 요청: 끝나도 DO0 유지
    ("set", "DI3", 1), ("until", "DO1", 1), ("set", "DI3", 0), ("until", "DO1", 0),  # 그동안 DI3는 DO1만 바꾼다
    ("set", "DI1", 0), ("until", "DO0", 0), ("wait", 0.3),                            # 요청이 꺼지면 DO0을 끈다
]
EXPECTED = [("DO1", 1), ("DO1", 0), ("DO0", 1), ("DO1", 1), ("DO1", 0), ("DO0", 0),
            ("DO0", 1), ("DO1", 1), ("DO1", 0), ("DO0", 0)]
# 이동이 끝나기 0.1초 전 DI3를 1초: 감속하는 동안 목표에 닿아도 DI3가 꺼질 때까지 DO0을 유지한다
LATE_STOP = [("set", "DI0", 1), ("until", "DO0", 1), ("set", "DI0", 0), ("wait", 1.9),
             ("set", "DI3", 1), ("wait", 1.0), ("set", "DI3", 0), ("until", "DO0", 0), ("wait", 0.3)]
# 이동이 끝나기 0.1초 전 DI3를 0.05초만: 목표에 닿을 때 DI3가 이미 꺼져 있어 DO0을 먼저 끈다
LATE_PULSE = [("set", "DI0", 1), ("until", "DO0", 1), ("set", "DI0", 0), ("wait", 1.9),
              ("set", "DI3", 1), ("wait", 0.05), ("set", "DI3", 0), ("wait", 0.6)]
CASES = [("기본", SCENARIO, EXPECTED),
         ("끝 직전 정지", LATE_STOP, [("DO0", 1), ("DO1", 1), ("DO1", 0), ("DO0", 0)]),
         ("끝 직전 짧은 정지", LATE_PULSE, [("DO0", 1), ("DO0", 0), ("DO1", 1), ("DO1", 0)])]


def drive(robot, scenario, limit=10.0):
    """scenario대로 robot.step을 부르고 DO 변화 순서를 돌려준다. Robot과 NodeRobot 모두 된다."""
    di = {"DI0": 0, "DI1": 0, "DI2": 0, "DI3": 0}
    do = robot.step(di, DT)
    for op, *arg in scenario:
        if op == "set":
            di[arg[0]] = arg[1]
        elif op == "wait":
            for _ in range(round(arg[0] / DT)):
                do = robot.step(di, DT)
        else:
            for _ in range(round(limit / DT)):
                if do[arg[0]] == arg[1]:
                    break
                do = robot.step(di, DT)
            else:
                robot.events.append(("시간 초과", f"{arg[0]}={arg[1]}"))
    return robot.events


class NodeRobot:
    """rodi.js를 Node 다리(node_robot.js)로 한 스텝씩 돌린다. Robot과 같은 step(di, dt)."""

    def __init__(self, source, joints=ZERO):
        self.p = subprocess.Popen([NODE, str(BRIDGE)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  text=True, encoding="utf-8")
        self.t, self.events, self.times, self.log = 0.0, [], [], []
        self._ask({"script": source, "joints": joints})

    def _ask(self, msg):
        self.p.stdin.write(json.dumps(msg) + "\n")
        self.p.stdin.flush()
        r = json.loads(self.p.stdout.readline())
        self.events += [tuple(e) for e in r["events"]]
        self.times += [self.t] * len(r["events"])
        self.log += [tuple(x) for x in r["log"]]
        self.do = {"DO0": r["do"][0], "DO1": r["do"][1]}
        self.joints, self.done, self.state = r["joints"], r["done"], r["state"]

    def step(self, di, dt):
        self.t = round(self.t + dt, 9)
        self._ask({"di": [di.get(f"DI{i}", 0) for i in range(4)], "dt": round(dt * 1000)})
        return self.do

    def run(self, seconds, di=None):
        for _ in range(round(seconds / DT)):
            self.step(di or {}, DT)

    def run_until(self, cond, di=None, limit=10.0):
        """cond(self)가 참이 될 때까지 돌고 걸린 시간(초)을 돌려준다."""
        start = self.t
        while not cond(self):
            if self.t - start > limit:
                raise AssertionError(f"{limit}초 안에 조건이 참이 되지 않음. log={self.log}")
            self.step(di or {}, DT)
        return self.t - start

    def at(self, event):
        return self.times[self.events.index(event)]

    def close(self):
        self.p.stdin.close()
        self.p.wait(timeout=5)
        self.p.stdout.close()


def do(name, value):
    return lambda r: r.do[name] == value


@unittest.skipUnless(NODE, "node가 없어 Rodi Script 실행기 시험을 건너뜀")
class RodiTestCase(unittest.TestCase):
    def robot(self, source, joints=ZERO):
        r = NodeRobot(source, joints)
        self.addCleanup(r.close)
        return r

    def assertLogged(self, r, kind, text):
        self.assertIn((kind, text), r.log)


class ApiTest(RodiTestCase):
    def test_digital_io_and_outputs_low_after_script_end(self):
        r = self.robot("while (getGeneralDigitalInput(2) === 0) { sleep(10); }\n"
                       "setGeneralDigitalOutput(0, 1);\n"
                       "setGeneralDigitalOutput(1, getGeneralDigitalOutput(0));\n"
                       "sleep(100);\n")
        r.run(0.2)
        self.assertEqual((r.events, r.state), ([], "쉼"))
        r.run_until(do("DO1", 1), {"DI2": 1})
        self.assertEqual(r.events, [("DO0", 1), ("DO1", 1)])
        r.run_until(lambda r: r.done)
        self.assertEqual(r.events[2:], [("DO0", 0), ("DO1", 0)])        # 프로그램이 안 돌면 출력 LOW (H p63)
        self.assertLogged(r, "상태", "스크립트 끝")

    def test_sleep_and_wait_are_ms_and_wait_takes_a_condition(self):
        r = self.robot("sleep(500);\n"
                       "setGeneralDigitalOutput(0, 1);\n"
                       "wait(300);\n"
                       "setGeneralDigitalOutput(1, 1);\n"
                       "wait(function () { return getGeneralDigitalInput(0) === 1; });\n"
                       "setGeneralDigitalOutput(1, 0);\n"
                       "sleep(1000);\n")
        r.run(1.5)
        self.assertAlmostEqual(r.at(("DO0", 1)), 0.51, delta=0.015)    # 첫 step(0.01초)에 시작
        self.assertAlmostEqual(r.at(("DO1", 1)) - r.at(("DO0", 1)), 0.3, delta=0.015)
        self.assertEqual(len(r.events), 2)                               # 조건이 거짓인 동안 기다린다
        self.assertAlmostEqual(r.run_until(do("DO1", 0), {"DI0": 1}), DT)
        self.assertFalse([x for x in r.log if x[0] == "경고"])

    def test_small_sleep_argument_warns_once_per_line(self):
        r = self.robot("sleep(1);\nwait(5);\nsleep(20);\nsleep(1);\nsleep(1);\n")
        r.run(0.2)
        warnings = [x[1] for x in r.log if x[0] == "경고"]
        self.assertEqual(warnings, ["sleep 인자가 작음: 초가 아니라 ms일 수 있음 (줄 1)",
                                    "wait 인자가 작음: 초가 아니라 ms일 수 있음 (줄 2)",
                                    "sleep 인자가 작음: 초가 아니라 ms일 수 있음 (줄 4)",
                                    "sleep 인자가 작음: 초가 아니라 ms일 수 있음 (줄 5)"])

    def test_move_joint_blocks_for_the_trapezoid_time(self):
        r = self.robot("moveJoint([90, 0, 0, 0, 0, 0], 30, 60);\n"
                       "setGeneralDigitalOutput(0, 1);\n"
                       "setGeneralDigitalOutput(1, getCurrentJoint()[0] === 90 ? 1 : 0);\n"
                       "sleep(1000);\n")
        # 90도 / 30 deg/s + 가속 30/60초 = 3.5초
        self.assertAlmostEqual(r.run_until(do("DO0", 1)), 3.5 + DT, delta=0.015)
        self.assertEqual(r.do["DO1"], 1)                                 # 목표에 정확히 닿음
        self.assertEqual(r.joints, [90, 0, 0, 0, 0, 0])

    def test_move_joint_default_speed_and_accel(self):
        r = self.robot("moveJoint([0, 0, 0, 0, 0, 10]);\nsetGeneralDigitalOutput(0, 1);\nsleep(1000);\n")
        # 기본 50 deg/s, 100 deg/s² (R 522). 10도는 최고 속도에 못 닿음: 2 × √(10/100)초
        self.assertAlmostEqual(r.run_until(do("DO0", 1)), 0.632 + DT, delta=0.015)

    def test_bad_arguments_are_errors(self):
        cases = {"getGeneralDigitalInput(16);": "번호는 0~15",
                 "setGeneralDigitalOutput(0, 2);": "값은 0 또는 1",
                 "moveJoint([1, 2, 3]);": "숫자 6개",
                 "moveJoint([0, 0, 0, 0, 0, 0], 50, 100, {async_mode: true});": "미지원"}
        for source, text in cases.items():
            with self.subTest(source):
                r = self.robot(source)
                r.run(0.05)
                errors = [x[1] for x in r.log if x[0] == "오류"]
                self.assertEqual(len(errors), 1)
                self.assertIn("(줄 1)", errors[0])
                self.assertIn(text, errors[0])
                self.assertTrue(r.done)

    def test_runtime_error_stops_and_turns_outputs_off(self):
        r = self.robot("setGeneralDigitalOutput(0, 1);\nsleep(50);\nvar a = null;\na.b = 1;\n")
        r.run(0.2)
        self.assertEqual(r.events, [("DO0", 1), ("DO0", 0)])
        errors = [x[1] for x in r.log if x[0] == "오류"]
        self.assertEqual(len(errors), 1)
        self.assertTrue(errors[0].startswith("메인 (줄 4) TypeError"), errors[0])


class ThreadTest(RodiTestCase):
    def test_thread_runs_while_main_is_blocked_in_move_joint(self):
        r = self.robot("Thread(function () {\n"
                       "  while (true) { setGeneralDigitalOutput(1, 1 - getGeneralDigitalOutput(1)); sleep(100); }\n"
                       "});\n"
                       "moveJoint([90, 0, 0, 0, 0, 0], 30, 60);\n"
                       "setGeneralDigitalOutput(0, 1);\n"
                       "sleep(500);\n")
        r.run_until(do("DO0", 1))
        toggles = r.events.index(("DO0", 1))
        self.assertGreaterEqual(toggles, 30)                             # 3.5초 이동 동안 0.1초마다
        self.assertLogged(r, "상태", "스레드1 시작")

    def test_thread_move_stop_ends_main_move_before_target(self):
        r = self.robot("Thread(function () {\n"
                       "  while (getGeneralDigitalInput(3) === 0) { sleep(20); }\n"
                       "  moveStop(0);\n"
                       "  setGeneralDigitalOutput(1, 1);\n"
                       "});\n"
                       "moveJoint([90, 0, 0, 0, 0, 0], 30, 60);\n"
                       "setGeneralDigitalOutput(0, 1);\n"
                       "sleep(1000);\n")
        r.run(1.0)
        self.assertEqual(r.state, "동작 중")                               # 이동 중 (화면 상태)
        took = r.run_until(lambda r: r.do == {"DO0": 1, "DO1": 1}, {"DI3": 1})
        self.assertEqual(r.state, "정지 중")                               # DO1 = 비정상 정지
        self.assertGreaterEqual(took, 0.3)                               # 자동 감속 = stop_time 0.3초
        self.assertLess(took, 0.3 + 0.02 + 2 * DT)                       # + 스레드 읽기 주기 20 ms
        self.assertEqual(r.events, [("DO0", 1), ("DO1", 1)])             # 메인이 먼저 돌아온다
        self.assertGreater(r.joints[0], 26)                              # 감속하는 동안 더 간다 (30 deg/s × 0.3초 / 2)
        self.assertLess(r.joints[0], 28)                                 # 목표 90도에 못 닿음
        self.assertLogged(r, "경고", "스레드의 moveStop은 실물 미확인 (H p184~185)")

    def test_move_stop_time_rules(self):
        # t ≥ 0.01이면 t초(최대 0.5초) 감속, 0.01 미만이면 stop_time 0.3초 (R 1247)
        for arg, seconds in [("0", 0.3), ("0.005", 0.3), ("0.1", 0.1), ("2", 0.5)]:
            with self.subTest(arg):
                r = self.robot("Thread(function () {\n"
                               "  wait(function () { return getGeneralDigitalInput(3) === 1; });\n"
                               f"  moveStop({arg});\n"
                               "});\n"
                               "moveJoint([90, 0, 0, 0, 0, 0], 30, 60);\n"
                               "setGeneralDigitalOutput(0, 1);\n"
                               "sleep(1000);\n")
                r.run(1.0)
                # DI3를 본 step에서 감속을 시작한다
                self.assertAlmostEqual(r.run_until(do("DO0", 1), {"DI3": 1}), seconds + DT, delta=0.015)
                self.assertLess(r.joints[0], 90)

    def test_thread_using_script_globals_warns_sharing(self):
        r = self.robot("var flag = 0;\n"
                       "var LIMIT = 1;\n"
                       "Thread(function () {\n"
                       "  var n = 0;\n"
                       "  while (true) { if (flag === LIMIT) { setGeneralDigitalOutput(1, 1); } n++; sleep(20); }\n"
                       "});\n"
                       "sleep(100);\n"
                       "flag = 1;\n"
                       "sleep(100);\n")
        r.run(0.3)
        self.assertEqual(r.events[0], ("DO1", 1))                        # 흉내에서는 스레드가 전역을 본다
        warnings = [x[1] for x in r.log if x[0] == "경고"]
        # 읽기만 하는 상수(LIMIT)도 경고한다. 이름마다 한 번. 스레드 안의 지역 변수(n)와 API는 아니다
        self.assertEqual(warnings, ["변수 공유는 실물 미확인: flag (스레드에서 읽거나 씀)",
                                    "변수 공유는 실물 미확인: LIMIT (스레드에서 읽거나 씀)"])


class EquivalenceTest(RodiTestCase):
    """합격 (a): 같은 DI 입력 열에 내장 스크립트와 참조 스크립트의 DO0·DO1 변화 순서가 같다."""

    def test_builtin_and_reference_script_change_do_in_the_same_order(self):
        if not REFERENCE.exists():
            self.skipTest("robot/PICKNPLACE_FULL_main.js 없음 (feature/PICKNPLACE_FULL_side 병합 전)")
        source = REFERENCE.read_text(encoding="utf-8")
        shared = "변수 공유는 실물 미확인: {} (스레드에서 읽거나 씀)"
        for name, scenario, expected in CASES:
            with self.subTest(name):
                builtin = drive(Robot(), scenario)
                script = self.robot(source, START)
                self.assertEqual(drive(script, scenario), builtin)
                self.assertEqual(builtin, expected)
                self.assertEqual(script.log, [("상태", "실행 시작"), ("상태", "스레드1 시작"),
                                              ("경고", shared.format("DI_STOP")), ("경고", shared.format("POLL_MS")),
                                              ("경고", "스레드의 moveStop은 실물 미확인 (H p184~185)"),
                                              ("경고", shared.format("DO_ABN"))])


class CheckBeforeRunTest(RodiTestCase):
    """합격 (c): ES5 밖 문법은 경고, 표 밖 API는 미지원 오류."""

    def test_let_warns_may_differ_from_real(self):
        r = self.robot("var a = 1;\nlet b = 2;\n")
        self.assertLogged(r, "경고", "실물과 다를 수 있음: let (줄 2)")
        # 경고 다음의 오류가 왜 안 도는지 말한다
        self.assertLogged(r, "오류", "문법 오류 (줄 2): ES5만 실행됨 (let·const·화살표 함수 → var·function으로 바꾸세요)")
        self.assertEqual([kind for kind, _ in r.log], ["경고", "오류"])

    def test_plain_syntax_error_keeps_the_parser_message(self):
        r = self.robot("var a = 1;\nvar b = ;\n")
        self.assertEqual(r.log, [("오류", "문법 오류 (줄 2): Unexpected token (2:8)")])

    def test_each_non_es5_syntax_warns(self):
        cases = {"const C = 1;": "const", "var f = function (x) { return x; };\nvar g = (x) => x;": "=>",
                 "var s = `a`;": "템플릿 문자열", "class A {}": "class",
                 "function f(a, b = 2) { return a; }": "기본값 인자"}
        for source, label in cases.items():
            with self.subTest(label):
                r = self.robot(source)
                line = source.count("\n") + 1
                self.assertLogged(r, "경고", f"실물과 다를 수 있음: {label} (줄 {line})")

    def test_words_in_comments_and_strings_do_not_warn(self):
        r = self.robot("// let, const, class, =>\n/* `a` */\nvar s = \"let x => 1\";\nvar t = 'class';\n"
                       "setGeneralDigitalOutput(0, 1);\nsleep(100);\n")
        r.run(0.05)
        self.assertEqual(r.events, [("DO0", 1)])
        self.assertFalse([x for x in r.log if x[0] in ("경고", "오류")])

    def test_move_linear_is_unsupported_and_nothing_runs(self):
        r = self.robot("setGeneralDigitalOutput(0, 1);\n"
                       "if (getGeneralDigitalInput(0) === 1) {\n"
                       "  moveLinear('tcp', [600, 0, 400, 180, 0, 0], 100, 1000);\n"
                       "}\n")
        r.run(0.1, {"DI0": 1})
        self.assertLogged(r, "오류", "미지원: moveLinear (줄 3)")
        self.assertTrue(r.done)
        self.assertEqual(r.events, [])

    def test_calls_outside_the_api_table_are_unsupported(self):
        r = self.robot("function f() { return 1; }\nf();\nhalt();\npopup('x', 1, 'p');\n"
                       "console.log(f());\nsetTimeout(f, 10);\nvar n = Math.abs(-1);\n")
        errors = [x[1] for x in r.log if x[0] == "오류"]
        self.assertEqual(errors, ["미지원: halt (줄 3)", "미지원: popup (줄 4)",
                                  "미지원: console (줄 5)", "미지원: setTimeout (줄 6)"])


if __name__ == "__main__":
    unittest.main()

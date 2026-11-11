"""로봇 스크립트 초안의 정적 검사. 실물 동작은 robot/README.md의 현장 확인 목록으로 본다."""
import re
import shutil
import subprocess
import unittest
from pathlib import Path

PATH = Path(__file__).resolve().parent.parent / "robot" / "PICKNPLACE_FULL_main.js"


def _code():
    src = PATH.read_text(encoding="utf-8")
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"//[^\n]*", "", src)


class RobotScriptTest(unittest.TestCase):
    def test_es5_only(self):
        code = _code()
        for bad in ("let ", "const ", "=>", "`", "class "):
            self.assertNotIn(bad, code, bad)
        self.assertIsNone(re.search(r"function\s*\w*\s*\([^)]*=[^)]*\)", code), "기본값 인자")

    def test_contract_calls(self):
        code = _code()
        for need in ("Thread(function", "moveStop(", "getCurrentJoint(",
                     "setGeneralDigitalOutput(DO_BUSY, 1)",
                     "setGeneralDigitalOutput(DO_BUSY, 0)",
                     "setGeneralDigitalOutput(DO_ABN, 1)", "waitAllOff"):
            self.assertIn(need, code, need)
        t = code.index("Thread(function")
        for off in ("setGeneralDigitalOutput(DO_BUSY, 0)", "setGeneralDigitalOutput(DO_ABN, 0)"):
            self.assertLess(code.index(off), t, "DO 끄기는 Thread 시작 전: " + off)

    def test_do0_off_only_after_requests_off(self):
        # 2026-11-10 강사 결정: 요청이 꺼질 때까지 DO0 유지. waitAllOff가 DO0 ON이나 이동보다 앞이면
        # PLC가 DO0을 못 봐서 요청을 끄지 않으므로 서로 기다리다 멈춘다
        code = _code()
        steps = ("setGeneralDigitalOutput(DO_BUSY, 1)", "moveJointSafely(", "waitAllOff()",
                 "setGeneralDigitalOutput(DO_BUSY, 0)")
        for name in ("script1", "script2"):
            body = re.search(r"function %s\(\) \{(.*?)\n\}" % name, code, re.S).group(1)
            at = [body.index(step) for step in steps]
            self.assertEqual(at, sorted(at), name)

    def test_constants(self):
        code = _code()
        for need in ("DI_MOVE1 = 0", "DI_MOVE2 = 1", "DI_STOP = 3",
                     "DO_BUSY = 0", "DO_ABN = 1", "POLL_MS = 20"):
            self.assertIn(need, code, need)

    @unittest.skipUnless(shutil.which("node"), "node 없음")
    def test_node_parses(self):
        r = subprocess.run(["node", "--check", str(PATH)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()

"""학생 스크립트 Worker(fake_robot/static/worker.js)를 브라우저 없이 Node worker_threads로 돌린다.

틀(worker_harness.js)은 브라우저 Worker가 주는 것 중 importScripts, onmessage/postMessage, 상대 주소 fetch만 채운다.
서버와 가짜 PLC는 포트 0. node가 없으면 건너뛴다.
"""

import json
import shutil
import subprocess
import threading
import unittest
from pathlib import Path

from tests.fake_robot_samples.doubles import Rig, wait_for

NODE = shutil.which("node")
HARNESS = Path(__file__).parent / "fake_robot_samples" / "worker_harness.js"
REFERENCE = Path(__file__).parent.parent / "robot" / "PICKNPLACE_FULL_main.js"
START = [0.0, 0.0, 45.0, 0.0, 45.0, 0.0]          # 참조 스크립트의 PICK까지 가장 큰 축이 45도: 2초 이동
ENDED = {"type": "ended"}


class NodeWorker:
    """worker_harness.js 프로세스. Worker가 페이지로 보낸 메시지를 모은다."""

    def __init__(self, base, source, joints):
        self.p = subprocess.Popen([NODE, str(HARNESS), base], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  text=True, encoding="utf-8")
        self.messages = []
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        self.send({"type": "start", "source": source, "joints": joints})

    def _read(self):
        for line in self.p.stdout:
            self.messages.append(json.loads(line))

    def send(self, msg):
        self.p.stdin.write(json.dumps(msg) + "\n")
        self.p.stdin.flush()

    def close(self):
        self.p.stdin.close()
        self.p.wait(5)
        self.reader.join(5)
        self.p.stdout.close()


@unittest.skipUnless(NODE, "node가 없어 Worker 시험을 건너뜀")
class WorkerTest(unittest.TestCase):
    def setUp(self):
        self.rig = Rig()
        self.addCleanup(self.rig.close)
        wait_for(lambda: self.rig.wiring.connected)
        self.rig.request("POST", "/api/mode", {"mode": "script"})

    def worker(self, source, joints=START):
        w = NodeWorker(f"http://127.0.0.1:{self.rig.port}/", source, joints)
        self.addCleanup(w.close)
        return w

    def test_reference_script_runs_through_the_server_bridge(self):
        r = self.rig
        w = self.worker(REFERENCE.read_text(encoding="utf-8"))
        wait_for(lambda: ("상태", "실행 시작") in r.texts())
        r.run(r.plc.outputs, 1)                                          # PLC: 이동1 요청
        wait_for(lambda: (1, 0) in r.plc.do_changes())                   # Worker의 DO0이 PLC에 닿았다
        r.run(r.plc.outputs)                                             # PLC는 DO0을 보고 요청을 끈다
        wait_for(lambda: r.plc.do_changes() == [(0, 0), (1, 0), (0, 0)], timeout=5)  # 2초 이동
        self.assertEqual(r.state()["joints"], [30, 0, 90, 0, 90, 0])    # 참조 스크립트의 PICK
        r.run(r.plc.outputs, 0, 0, 0, 1)                                 # 쉬는 중 즉시 정지
        wait_for(lambda: r.plc.do_changes()[-1] == (0, 1))
        r.run(r.plc.outputs)
        wait_for(lambda: r.plc.do_changes()[-1] == (0, 0))
        self.assertEqual(r.plc.do_changes(), [(0, 0), (1, 0), (0, 0), (0, 1), (0, 0)])
        texts = r.texts()
        for entry in [("상태", "스레드1 시작"), ("상태", "동작 중"), ("DO", "DO0 → 1"), ("상태", "정지 중"),
                      ("경고", "변수 공유는 실물 미확인: DI_STOP (스레드에서 읽거나 씀)"),
                      ("경고", "스레드의 moveStop은 실물 미확인 (H p184~185)")]:
            self.assertIn(entry, texts)
        w.send({"type": "stop"})
        wait_for(lambda: ENDED in w.messages)
        self.assertIn(("상태", "멈춤 (사용자)"), r.texts())

    def test_a_new_run_starts_with_outputs_low(self):
        r = self.rig
        old = NodeWorker(f"http://127.0.0.1:{r.port}/", "setGeneralDigitalOutput(0, 1);\nwhile (true) { sleep(100); }\n",
                         START)
        wait_for(lambda: r.plc.do_changes() == [(0, 0), (1, 0)])
        old.close()                                                      # 페이지의 [다시 실행]: 이전 Worker를 끝낸다
        self.worker("while (true) { sleep(100); }\n")
        wait_for(lambda: r.plc.do_changes() == [(0, 0), (1, 0), (0, 0)])  # 지난 실행이 남긴 DO0이 꺼진다

    def test_es5_syntax_error_reaches_the_run_log_after_the_warning(self):
        w = self.worker("var a = 1;\nlet b = 2;\n")
        wait_for(lambda: ENDED in w.messages)
        texts = self.rig.texts()
        warning = texts.index(("경고", "실물과 다를 수 있음: let (줄 2)"))
        error = texts.index(("오류", "문법 오류 (줄 2): ES5만 실행됨 (let·const·화살표 함수 → var·function으로 바꾸세요)"))
        self.assertLess(warning, error)
        self.assertEqual(self.rig.plc.do_changes(), [(0, 0)])


if __name__ == "__main__":
    unittest.main()

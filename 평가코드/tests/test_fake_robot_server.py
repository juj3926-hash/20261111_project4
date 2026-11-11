"""가짜 로봇 서버 (fake_robot/server.py): 실시간 루프, HTTP, 브라우저 Worker ↔ PLC 다리. 포트는 모두 0."""

import asyncio
import contextlib
import gc
import io
import json
import os
import socket
import subprocess
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
import warnings
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import fake_robot.server as server
from fake_robot.core import HOME, PICK
from fake_robot.server import arguments, fixed_loop
from tests.fake_robot_samples.doubles import J, Rig, wait_for

ROOT = Path(__file__).resolve().parent.parent


class RigTestCase(unittest.TestCase):
    def setUp(self):
        self.rig = Rig()
        self.addCleanup(self.rig.close)
        wait_for(lambda: self.rig.wiring.connected and self.rig.plc.got)


class HttpTest(RigTestCase):
    def test_only_local_host_and_origin(self):
        r = self.rig
        self.assertEqual(r.request("GET", "/api/state")[0], 200)
        self.assertEqual(r.request("GET", "/api/state", host="evil.example:80")[0], 403)
        self.assertEqual(r.request("GET", "/rodi.js", host=f"localhost:{r.port}")[0], 403)
        self.assertEqual(r.request("GET", "/api/state", headers={"Origin": "http://evil.example"})[0], 403)
        self.assertEqual(r.request("GET", "/api/state", headers={"Origin": f"http://127.0.0.1:{r.port}"})[0], 200)
        self.assertEqual(r.request("POST", "/api/mode", {"mode": "script"}, host="evil.example:80")[0], 403)
        self.assertEqual(r.state()["mode"], "builtin")

    def test_bad_requests(self):
        r = self.rig
        self.assertEqual(r.request("GET", "/nothing")[0], 404)
        self.assertEqual(r.request("POST", "/api/nothing", {})[0], 404)
        for body in [b"not json", b"[1]", b'{"mode": "fast"}', b'{"mode": "script", "x": 1}',
                     b'{"mode": "' + b"x" * 20000 + b'"}']:
            with self.subTest(body=body[:24]):
                self.assertEqual(r.request("POST", "/api/mode", body)[0], 400)
        self.assertEqual(r.request("POST", "/api/mode", b'{"mode": "script"}',
                                   headers={"Content-Type": "text/plain"})[0], 400)
        for home in [b"[0, 0, 0, 0, 0]", b'[0, 0, 0, 0, 0, "x"]', b"[0, 0, 0, 0, 0, 400]",
                     b"[0, 0, 0, 0, 0, NaN]", b"[0, 0, 0, 0, 0, true]"]:
            with self.subTest(home=home):
                body = b'{"home": ' + home + b', "pick": [0, 0, 0, 0, 0, 0]}'
                self.assertEqual(r.request("POST", "/api/joints", body)[0], 400)
        r.request("POST", "/api/mode", {"mode": "script"})
        for events in [[["DO0", 2]], [["DO0", True]], [["DI0", 1]], [["DO16", 1]]]:
            with self.subTest(events=events):
                self.assertEqual(r.io(events=events)[0], 400)
        self.assertEqual(r.io(log=[["잡담", "x"]])[0], 400)
        self.assertEqual(r.io(state="달리는 중")[0], 400)
        self.assertEqual(r.state()["home"], HOME)                        # 아무것도 바뀌지 않았다
        self.assertEqual(r.plc.do_changes(), [(0, 0)])

    @unittest.skipUnless(os.name == "nt", "SO_REUSEADDR로 같은 포트를 두 번 여는 것은 윈도우 동작이다")
    def test_second_server_on_the_same_port_fails(self):
        # 두 번째 python -m fake_robot이 조용히 떠서 요청을 나눠 받지 않고, 시작 실패 한 줄로 끝난다
        with self.assertRaises(OSError):
            server.make_server(self.rig.robot, self.rig.loop, self.rig.port).server_close()

    def test_serves_static_and_vendored_js_only(self):
        r = self.rig
        status, body, ctype = r.request("GET", "/rodi.js")
        self.assertEqual((status, ctype), (200, "text/javascript"))
        self.assertIn(b"RodiRunner", body)
        self.assertEqual(r.request("GET", "/vendor/interpreter.js")[:3:2], (200, "text/javascript"))
        for path in ["/vendor/README.md", "/../core.py", "/vendor/../server.py", "/static/rodi.js"]:
            with self.subTest(path):
                self.assertEqual(r.request("GET", path)[0], 404)


class BuiltinModeTest(RigTestCase):
    def test_runs_without_a_browser(self):
        r = self.rig
        r.run(r.plc.outputs, 1)                                          # DI0 요청
        wait_for(lambda: (1, 0) in r.plc.do_changes())
        r.run(r.plc.outputs)                                             # PLC는 DO0을 보고 요청을 끈다
        wait_for(lambda: r.plc.do_changes() == [(0, 0), (1, 0), (0, 0)])
        st = r.state()
        self.assertEqual((st["mode"], st["state"], st["do"], st["joints"]), ("builtin", "쉼", [0, 0], PICK))
        for entry in [("상태", "PLC 연결됨"), ("상태", "동작 중"), ("DO", "DO0 → 1"), ("DO", "DO0 → 0"), ("상태", "쉼")]:
            self.assertIn(entry, r.texts())

    def test_state_returns_io_and_only_newer_log(self):
        r = self.rig
        r.run(r.plc.outputs, 0, 0, 1, 1)
        wait_for(lambda: r.state()["di"] == [0, 0, 1, 1])
        wait_for(lambda: r.state()["do"] == [0, 1])                      # 쉬는 중 DI3 → 바로 DO1
        last = r.state()["log"][-1]["n"]
        r.request("POST", "/api/joints", {"home": J, "pick": PICK})
        self.assertEqual([e["text"] for e in r.state(last)["log"]], ["HOME·PICK 바꿈 (다음 이동부터)"])
        st = r.state(last + 100)                                         # 다시 켠 서버에 화면이 옛 번호로 묻는다
        self.assertEqual((st["log"], st["last"]), ([], last + 1))        # 새 줄이 없어도 마지막 번호를 알려 준다

    def test_edited_pick_used_by_the_next_move(self):
        r = self.rig
        self.assertEqual(r.request("POST", "/api/joints", {"home": J, "pick": [10] * 6})[0], 200)
        self.assertEqual((r.state()["home"], r.state()["pick"]), (J, [10.0] * 6))
        r.run(r.plc.outputs, 1)
        wait_for(lambda: (1, 0) in r.plc.do_changes())
        r.run(r.plc.outputs)
        wait_for(lambda: r.state()["joints"] == [10.0] * 6)


class ScriptModeTest(RigTestCase):
    def setUp(self):
        super().setUp()
        self.assertEqual(self.rig.request("POST", "/api/mode", {"mode": "script"})[0], 200)

    def test_each_do_change_goes_to_the_plc_in_order_and_di_comes_back(self):
        r = self.rig
        r.run(r.plc.outputs, 1, 0, 0, 1)
        wait_for(lambda: r.state()["di"] == [1, 0, 0, 1])
        status, body, _ = r.io(events=[["DO0", 1], ["DO0", 0], ["DO1", 1], ["DO5", 1]],
                               log=[["경고", "시험 경고"]], state="정지 중")
        self.assertEqual((status, body), (200, {"di": [1, 0, 0, 1], "mode": "script"}))
        wait_for(lambda: r.plc.do_changes()[-1] == (0, 1))
        self.assertEqual(r.plc.do_changes(), [(0, 0), (1, 0), (0, 0), (0, 1)])  # 짧은 펄스도 하나씩 보낸다
        st = r.state()
        self.assertEqual((st["do"], st["joints"], st["state"]), ([0, 1], J, "정지 중"))
        for entry in [("DO", "DO0 → 1"), ("DO", "DO0 → 0"), ("DO", "DO1 → 1"), ("DO", "DO5 → 1"),
                      ("경고", "시험 경고"), ("상태", "정지 중")]:
            self.assertIn(entry, r.texts())

    def test_core_does_not_drive_do_while_a_script_runs(self):
        r = self.rig
        r.run(r.plc.outputs, 1)
        time.sleep(0.5)                                                  # 내장 스크립트라면 0.05초 뒤 DO0
        self.assertEqual(r.plc.do_changes(), [(0, 0)])
        self.assertEqual(r.robot.robot.phase, "쉼")

    def test_quiet_worker_turns_do_off(self):
        r = self.rig
        r.io(events=[["DO0", 1]])
        wait_for(lambda: r.plc.do_changes()[-1] == (1, 0))
        t0 = time.monotonic()
        wait_for(lambda: r.plc.do_changes()[-1] == (0, 0))
        self.assertGreater(time.monotonic() - t0, 0.9)                   # 1초 동안 소식이 없을 때
        self.assertIn(("상태", "Worker 소식 없음: DO를 끈다 (스크립트가 안 돌면 출력 LOW)"), r.texts())

    def test_io_is_ignored_outside_script_mode(self):
        r = self.rig
        r.request("POST", "/api/mode", {"mode": "builtin"})
        self.assertEqual(r.io(events=[["DO0", 1]])[1], {"di": [0, 0, 0, 0], "mode": "builtin"})
        time.sleep(0.2)
        self.assertEqual(r.plc.do_changes(), [(0, 0)])

    def test_back_to_builtin_continues_from_the_script_joints(self):
        r = self.rig
        r.io(joints=J, state="동작 중")
        r.request("POST", "/api/mode", {"mode": "builtin"})
        st = r.state()
        self.assertEqual((st["mode"], st["joints"], st["state"], st["do"]), ("builtin", J, "쉼", [0, 0]))
        r.run(r.plc.outputs, 1)
        wait_for(lambda: (1, 0) in r.plc.do_changes())
        r.run(r.plc.outputs)
        wait_for(lambda: r.state()["joints"] == PICK)


class ShutdownTest(unittest.TestCase):
    """끝내는 중에 온 요청은 연결을 끊지 않고 503으로 답한다(PC 프로그램과 같다)."""

    def serve(self, loop):
        http = server.make_server(SimpleNamespace(snapshot=lambda since: {}), loop, 0)
        self.addCleanup(http.server_close)
        thread = threading.Thread(target=http.serve_forever, args=(0.05,))
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(http.shutdown)
        return http.server_port

    def answer(self, port, path, data=None):
        """(상태 코드, JSON 본문)"""
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read())

    def test_closed_loop_answers_503(self):
        loop = asyncio.new_event_loop()
        loop.close()
        port = self.serve(loop)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            for path, data in (("/api/state", None), ("/api/mode", b'{"mode": "script"}')):
                self.assertEqual(self.answer(port, path, data), (503, {"error": "가짜 로봇을 끝내는 중입니다"}))
            time.sleep(0.05)                                             # 요청 스레드가 코루틴을 놓을 때 나는 경고까지
            gc.collect()
        self.assertEqual([str(w.message) for w in caught], [])          # 돌지 않을 코루틴은 닫았다

    def test_no_answer_in_2_s_is_503(self):
        loop = asyncio.new_event_loop()                                  # 돌지 않는 루프 = 끝내느라 멈춘 루프
        self.addCleanup(loop.close)
        port = self.serve(loop)
        self.assertEqual(self.answer(port, "/api/state"), (503, {"error": "가짜 로봇이 응답하지 않습니다"}))
        loop.run_until_complete(asyncio.sleep(0.05))                     # 밀린 요청 코루틴을 끝까지 돌린다


class FixedLoopTest(unittest.IsolatedAsyncioTestCase):
    async def test_steps_follow_real_time(self):
        steps = []
        task = asyncio.create_task(fixed_loop(steps.append))
        t0 = time.monotonic()
        await asyncio.sleep(0.5)
        task.cancel()
        elapsed = time.monotonic() - t0
        await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(set(steps), {0.01})
        self.assertAlmostEqual(len(steps) * 0.01, elapsed, delta=0.05)

    async def test_catch_up_is_capped_after_a_stall(self):
        # 가짜 시계: 깨어날 때마다 돈 step 수를 센다. 다섯 번째 잠은 0.3초(30 step) 늦게 깬다
        clock, steps = SimpleNamespace(now=0.0), [0]

        class Stop(Exception):
            pass

        async def sleep(seconds):
            if len(steps) == 10:
                raise Stop
            clock.now += max(seconds, 0.0) + (0.3 if len(steps) == 5 else 0.0)
            steps.append(0)

        def tick(dt):
            steps[-1] += 1
        with mock.patch.object(server, "time", SimpleNamespace(monotonic=lambda: clock.now)), \
                mock.patch.object(server, "asyncio", SimpleNamespace(sleep=sleep)), self.assertRaises(Stop):
            await server.fixed_loop(tick)
        self.assertEqual(steps, [1, 1, 1, 1, 1, 10, 1, 1, 1, 1])        # 최대 10 step, 나머지 20 step은 버린다


class CliTest(unittest.TestCase):
    def test_arguments(self):
        a = arguments([])
        self.assertEqual((a.plc_host, a.wiring_port, a.http_port), ("127.0.0.1", 5021, 8879))
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            arguments(["--http-port", "70000"])

    def test_python_m_fake_robot_prints_the_address_and_serves(self):
        with socket.socket() as s:                                       # 아무도 듣지 않는 배선 포트
            s.bind(("127.0.0.1", 0))
            wiring_port = s.getsockname()[1]
        p = subprocess.Popen([sys.executable, "-m", "fake_robot", "--http-port", "0", "--wiring-port", str(wiring_port)],
                             cwd=ROOT, stdout=subprocess.PIPE, text=True, encoding="utf-8",
                             env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        try:
            first = p.stdout.readline()
            self.assertTrue(first.startswith("가짜 로봇 3D 화면: http://127.0.0.1:"), first)
            self.assertIn(f"127.0.0.1:{wiring_port}", p.stdout.readline())
            with urllib.request.urlopen(first.split(": ", 1)[1].strip() + "/rodi.js", timeout=5) as resp:
                self.assertEqual(resp.status, 200)
        finally:
            p.terminate()
            p.wait(5)
            p.stdout.close()


if __name__ == "__main__":
    unittest.main()

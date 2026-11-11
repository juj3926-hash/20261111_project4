"""PC 프로그램 웹 서버 시험 (spec 7.4). 이벤트 루프는 별도 스레드에서 돌고, 요청은 urllib로 보낸다."""

import asyncio
import contextlib
import gc
import io
import json
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import warnings
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from picknplace_full import app
from picknplace_full.app import EventLog, arguments, make_server, serve
from picknplace_full.link import Event, PlcLink
from picknplace_full.sequence import BUTTONS, Sequencer
from picknplace_full.transport import AutoTransport
from tests.picknplace_fakes import FakeTransport, until

TIME = r"\d\d:\d\d:\d\d\.\d{3}"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 환경의 프록시를 거치지 않는다


class AppTest(unittest.TestCase):
    """serve와 같은 짜임(EventLog, PlcLink, Sequencer, make_server)을 포트 0에 띄운다. 로그 파일은 임시 폴더에 쓴다."""

    def run_loop(self):
        """별도 스레드에서 도는 이벤트 루프. 시험이 끝나면 멈추고 닫는다."""
        self.loop = asyncio.new_event_loop()
        thread = threading.Thread(target=self.loop.run_forever)
        thread.start()
        self.addCleanup(self.loop.close)
        self.addCleanup(thread.join)
        self.addCleanup(self.loop.call_soon_threadsafe, self.loop.stop)
        self.addCleanup(lambda: self.on_loop(self.loop.shutdown_default_executor()))

    def start(self, target="model"):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.run_loop()
        log = EventLog()
        self.link = PlcLink(target, speed=10)
        self.link.on_event(log.write)
        self.seq = Sequencer(self.link, log=log.log)
        self.server = make_server(self.link, self.seq, self.loop, 0, log.events)
        self.addCleanup(self.server.server_close)
        self.addCleanup(log.open(Path(tmp.name)).close)
        server_thread = threading.Thread(target=self.server.serve_forever, args=(0.05,))
        server_thread.start()
        self.addCleanup(server_thread.join)
        self.addCleanup(self.server.shutdown)
        self.on_loop(self.link.start())
        self.addCleanup(lambda: self.on_loop(self.link.close()))
        self.addCleanup(lambda: self.on_loop(self.seq.close()))  # 끝내는 순서도 serve와 같다: 자동 순서, 통신, 서버

    def on_loop(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(10)

    def ready(self):
        """첫 스캔의 그리퍼 열기(3초/10)가 끝날 때까지 기다린다. 그 전의 1111은 거부된다(시험 표 9행)."""
        plant = self.link.transport.plant
        self.on_loop(until(lambda: not plant.plc.first and not plant.bit("G_OPENING"), 3.0))
        return self.until(lambda s: s["buttons"]["start"]["enabled"])

    def request(self, method, path, payload=None, data=None, **headers):
        """(상태 코드, 본문 바이트, 헤더). headers는 Host="evil:1"처럼 준다. 이름의 _는 -가 된다."""
        port = self.server.server_port
        if payload is not None:
            data = json.dumps(payload).encode()
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), data=data, method=method)
        req.add_header("Host", "127.0.0.1:%d" % port)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        for name, value in headers.items():
            req.add_header(name.replace("_", "-"), value)
        try:
            with OPENER.open(req, timeout=15) as r:
                return r.status, r.read(), r.headers
        except urllib.error.HTTPError as e:
            with e:
                return e.code, e.read(), e.headers

    def post(self, path, payload):
        """(상태 코드, message)."""
        code, body, _ = self.request("POST", path, payload)
        return code, json.loads(body)["message"]

    def status(self):
        code, body, _ = self.request("GET", "/api/status")
        self.assertEqual(code, 200)
        return json.loads(body)

    def until(self, pred, timeout=3.0):
        """pred(/api/status)가 참이 될 때까지 묻는다. 맞은 상태를 돌려준다."""
        end = time.monotonic() + timeout
        while True:
            s = self.status()
            if pred(s):
                return s
            if time.monotonic() > end:
                self.fail("%.1f초 안에 조건이 맞지 않음: %s" % (timeout, s))
            time.sleep(0.02)

    def sent(self):
        """보냄 이벤트의 코드 열."""
        return [e["text"] for e in self.status()["events"] if e["tag"] == "보냄"]

    def test_status_shape(self):
        self.start()
        s = self.until(lambda s: s["status"] is not None)
        self.assertEqual(set(s), {"status", "comm_ok", "target", "ports", "sequence", "buttons", "events"})
        self.assertEqual(set(s["status"]), {"raw", "mailbox", "robot", "gripper", "plc", "valid", "text", "at"})
        self.assertRegex(s["status"]["at"], "^%s$" % TIME)
        self.assertEqual(set(s["sequence"]), {"running", "step", "stop_requested", "auto_maybe_on", "last_result"})
        self.assertEqual(set(s["buttons"]), set(BUTTONS) | {"code"})
        for state in s["buttons"].values():
            self.assertEqual(set(state), {"enabled", "label", "hint"})
        self.assertEqual((s["target"], s["ports"]), ("model", []))
        self.assertTrue(s["events"])
        for e in s["events"]:
            self.assertEqual(set(e), {"at", "tag", "text"})
            self.assertRegex(e["at"], "^%s$" % TIME)

    def test_press_start_runs_cycle(self):
        self.start()
        self.ready()
        code, message = self.post("/api/press", {"button": "start"})
        self.assertEqual(code, 200)
        self.assertIn("시작", message)
        s = self.until(lambda s: s["sequence"]["last_result"] == "완료", 4.0)
        self.assertEqual(self.sent(), ["1111", "9991", "9992"])
        self.assertIn({"tag": "자동", "text": "한 사이클 완료"}, [{"tag": e["tag"], "text": e["text"]} for e in s["events"]])

    def test_bad_requests(self):
        self.start()
        self.assertEqual(self.request("GET", "/nope")[0], 404)
        self.assertEqual(self.request("POST", "/api/nope", data=b"")[0], 404)
        for payload in ({"button": "start", "extra": 1}, {"name": "start"}, {"button": "fly"}, {"button": 1}, ["start"]):
            self.assertEqual(self.request("POST", "/api/press", payload)[0], 400, payload)
        self.assertEqual(self.request("POST", "/api/rescan", {"x": 1})[0], 400)
        self.assertEqual(self.request("POST", "/api/press", data=b"{bad")[0], 400)
        self.assertEqual(self.request("POST", "/api/rescan", data=b"", Content_Length="2", Content_Type="text/plain")[0], 400)
        # 본문을 읽기 전에 거절하는 경우(형식 오류, 4 KB 초과)는 길이만 적고 본문은 보내지 않는다. 서버가 본문을 읽지 않고 닫아도 남는 데이터가 없다(윈도우 RST 없음)
        self.assertEqual(self.request("POST", "/api/rescan", data=b"", Content_Length="5000")[0], 400)
        self.assertEqual(self.request("GET", "/api/status", Host="evil:1")[0], 403)
        self.assertEqual(self.request("GET", "/", Origin="http://evil:1")[0], 403)
        self.assertEqual(self.request("POST", "/api/press", {"button": "estop"}, Host="evil:1")[0], 403)
        self.assertEqual(self.sent(), [])  # 403을 받은 즉시 정지도 보내지 않았다

    def test_browser_origin_accepted(self):
        """브라우저는 POST에 Origin을 붙인다. 이 서버의 주소면 받는다."""
        self.start()
        code, body, _ = self.request("POST", "/api/rescan", {}, Origin="http://127.0.0.1:%d" % self.server.server_port)
        self.assertEqual((code, json.loads(body)["message"]), (200, "포트 찾기는 --target auto일 때만 합니다"))

    def test_code_input_validation(self):
        """Review Focus 5."""
        self.start()
        self.ready()
        for code in (0, 70000, -1, "abc", 1.5, True, None):
            self.assertEqual(self.request("POST", "/api/code", {"code": code})[0], 400, code)
        self.assertEqual(self.sent(), [])
        code, message = self.post("/api/code", {"code": 2222})  # 맞는 값은 보낸다. 위의 "없음"이 헛말이 아님을 보인다
        self.assertEqual(code, 200)
        self.assertIn("2222 접수", message)
        self.assertEqual(self.sent(), ["2222"])

    def test_double_press_sends_once(self):
        """Review Focus 2. 두 요청을 동시에 보낸다. 첫 누름이 9991을 보내는 동안 두 번째는 거절된다."""
        self.start()
        self.ready()
        go, replies = threading.Barrier(2), []

        def press():
            go.wait()
            replies.append(self.post("/api/press", {"button": "script1"}))

        threads = [threading.Thread(target=press) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(15)
        self.assertEqual([code for code, _ in replies], [200, 200])
        messages = [message for _, message in replies]
        self.assertEqual(sum("보내는 중" in m for m in messages), 1, messages)
        self.assertEqual(sum("9991 접수" in m for m in messages), 1, messages)
        self.assertEqual(self.sent(), ["9991"])

    def test_no_port_found_still_serves(self):
        """Review Focus 4. 어느 포트도 응답하지 않아도 서버는 뜨고, 결과를 보여 주고, 다시 찾는다."""
        probes = []

        async def probe(port):
            probes.append(port)
            return "응답 없음"

        self.start(AutoTransport(list_ports=lambda: [SimpleNamespace(device="COM9", description="USB Serial")],
                                 probe=probe))
        s = self.until(lambda s: s["ports"])
        self.assertEqual(s["ports"], [["COM9", "응답 없음"]])
        self.assertEqual((s["status"], s["comm_ok"], s["target"]), (None, False, "auto · 포트 찾는 중"))
        self.assertEqual({name for name, state in s["buttons"].items() if state["enabled"]}, {"estop"})
        code, message = self.post("/api/rescan", {})
        self.assertEqual(code, 200)
        self.assertIn("응답하는 포트가 없습니다", message)
        self.assertEqual(probes, ["COM9", "COM9"])
        ports = [e["text"] for e in self.status()["events"] if e["tag"] == "포트"]
        self.assertEqual(ports, ["COM9 (USB Serial): 응답 없음"] * 2)

    def test_rescan_refused_while_connected(self):
        """[다시 찾기]는 포트를 못 찾았을 때 쓴다(spec 6.2). 통신 중이거나 자동 순서 중이면 쓰던 포트를 닫지 않는다."""
        probes = []

        async def probe(port):
            probes.append(port)
            return "응답 있음"

        async def open_(port):
            return FakeTransport(raw=2, respond={1111: 2, 9991: 1}.get)  # 9991 뒤 로봇은 계속 동작 중이다

        auto = AutoTransport(list_ports=lambda: [SimpleNamespace(device="COM5", description="USB")], probe=probe)
        auto._open = open_
        self.start(auto)
        self.until(lambda s: s["comm_ok"])
        self.assertEqual(self.post("/api/rescan", {}), (200, "COM5 포트로 통신 중이라 다시 찾지 않습니다"))
        self.post("/api/press", {"button": "start"})
        self.until(lambda s: s["sequence"]["step"] == "2단계 이동1")
        self.assertEqual(self.post("/api/rescan", {}), (200, "자동 순서 중에는 다시 찾지 않습니다"))
        self.assertEqual(probes, ["COM5"])  # 처음 찾기 한 번뿐이다

    def test_static_files(self):
        self.start()
        code, body, headers = self.request("GET", "/")
        self.assertEqual((code, headers.get_content_type()), (200, "text/html"))
        html = body.decode("utf-8")
        self.assertIn("PICKNPLACE_FULL", html)
        code, body, headers = self.request("GET", "/ui.js")
        self.assertEqual((code, headers.get_content_type()), (200, "text/javascript"))
        self.assertNotRegex(html + body.decode("utf-8"), r"https?://")  # 외부 자원 없음

    def test_closed_loop_answers_503(self):
        """끝내는 중이라 루프가 닫힌 뒤에 온 요청은 503이다. 돌지 않을 코루틴은 닫으므로 경고가 남지 않는다."""
        loop = asyncio.new_event_loop()
        loop.close()
        link = PlcLink(FakeTransport())
        self.server = make_server(link, Sequencer(link), loop, 0)
        self.addCleanup(self.server.server_close)
        thread = threading.Thread(target=self.server.serve_forever, args=(0.05,))
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(self.server.shutdown)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            for args in (("GET", "/api/status"), ("POST", "/api/press", {"button": "start"})):
                code, body, _ = self.request(*args)
                self.assertEqual((code, json.loads(body)["message"]), (503, "프로그램을 끝내는 중입니다"))
            time.sleep(0.05)  # 요청 스레드가 코루틴을 놓을 때 나는 경고까지 기다린다
            gc.collect()
        self.assertEqual([str(w.message) for w in caught], [])

    def test_ctrl_c_stops_cycle(self):
        """Ctrl+C(asyncio.run이 serve 작업을 취소)에 돌던 자동 순서를 먼저 멈춘다.

        로그 파일에 멈춤 한 줄이 남고, 그 뒤로 아무것도 보내지 않고, 일어나지 않은 시간 초과를 적지 않는다.
        """
        seqs, out = [], io.StringIO()

        class Seq(Sequencer):  # serve가 만든 Sequencer를 붙잡아 둔다
            def __init__(self, *args, **kw):
                super().__init__(*args, **kw)
                seqs.append(self)

        async def begin():
            return asyncio.create_task(serve(arguments(["--target", "model", "--speed", "10", "--http-port", "0"])))

        async def ctrl_c(task):  # asyncio.run이 Ctrl+C에 하는 일: 주 작업을 취소하고 끝나기를 기다린다
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.run_loop()
        with mock.patch.object(app, "Sequencer", Seq), mock.patch.object(app, "LOGS", Path(tmp.name)), \
                contextlib.redirect_stdout(out):
            task = self.on_loop(begin())
            self.on_loop(until(lambda: seqs and seqs[0].link.comm_ok, 3.0))
            seq = seqs[0]
            plant = seq.link.transport.plant
            self.on_loop(until(lambda: not plant.plc.first and not plant.bit("G_OPENING"), 3.0))
            self.on_loop(seq.press("start"))
            self.on_loop(until(lambda: seq.step.startswith("3단계"), 3.0))  # 그리퍼가 닫히면 9992를 보낼 차례다
            self.on_loop(ctrl_c(task))
        self.assertRegex(out.getvalue(), r"^http://127\.0\.0\.1:\d+ · target=model\n$")
        [path] = Path(tmp.name).glob("PICKNPLACE_FULL_*.log")
        lines = path.read_text(encoding="utf-8").splitlines()
        sent = [line.split("] ", 1)[1] for line in lines if "[보냄]" in line]
        auto = [line.split("] ", 1)[1] for line in lines if "[자동]" in line]
        self.assertEqual(sent, ["1111", "9991"])  # 멈춘 뒤로 9992를 보내지 않았다
        self.assertEqual(auto[-2:], ["3단계 그리퍼 닫힘 기다림", "프로그램 종료로 자동 순서를 멈춤"])
        self.assertEqual([line for line in lines if "시간 초과" in line], [])


class ServeTest(unittest.TestCase):
    def test_log_file_written(self):
        """serve가 쓰는 EventLog. 링크 이벤트(write)와 Sequencer 기록(log)이 같은 형식으로 파일에 간다."""
        with tempfile.TemporaryDirectory() as tmp:
            log = EventLog()
            with log.open(Path(tmp)):
                log.write(Event(time.time(), "보냄", "1111"))
                log.log("자동", "한 사이클 완료")
            [path] = Path(tmp).glob("PICKNPLACE_FULL_*.log")
            self.assertRegex(path.name, r"^PICKNPLACE_FULL_\d{8}_\d{6}\.log$")
            lines = path.read_text(encoding="utf-8").splitlines()
            log.log("자동", "닫은 뒤")  # 종료 중에 늦게 온 줄은 예외 없이 화면에만 남는다
        self.assertEqual(len(lines), 2)
        self.assertRegex(lines[0], r"^%s \[보냄\] 1111$" % TIME)
        self.assertRegex(lines[1], r"^%s \[자동\] 한 사이클 완료$" % TIME)
        self.assertEqual([e.text for e in log.events], ["1111", "한 사이클 완료", "닫은 뒤"])

    def test_busy_port_leaves_no_log_file(self):
        """화면 포트를 이미 쓰는 중이면 serve는 OSError로 끝나고 빈 로그 파일을 남기지 않는다."""
        with socket.socket() as busy, tempfile.TemporaryDirectory() as tmp, mock.patch.object(app, "LOGS", Path(tmp)):
            busy.bind(("127.0.0.1", 0))
            busy.listen()
            args = arguments(["--target", "model", "--http-port", str(busy.getsockname()[1])])
            with self.assertRaises(OSError):
                asyncio.run(serve(args))
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_arguments(self):
        a = arguments([])
        self.assertEqual((a.target, a.http_port, a.interval, a.speed), ("auto", 8877, 0.2, 1.0))
        for bad in (["--interval", "0"], ["--speed", "-1"], ["--http-port", "70000"],
                    ["--interval", "inf"], ["--speed", "nan"]):
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                arguments(bad)


if __name__ == "__main__":
    unittest.main()

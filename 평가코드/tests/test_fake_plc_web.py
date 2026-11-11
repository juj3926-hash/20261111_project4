"""가짜 PLC 모니터 UI (docs/handoff/fake-plc.md 3.8). 가짜 PLC를 포트 0으로 띄워 HTTP로 묻는다."""

import asyncio
import contextlib
import gc
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import warnings
from pathlib import Path

from fake_plc.__main__ import arguments, serve
from fake_plc.app import FakePlc
from fake_plc.engine import LATCH
from fake_plc.web import make_http
from tests.test_fake_plc_diagram import encoded, write_two_programs

ROOT = Path(__file__).resolve().parents[1]
FULL_XGWX = ROOT / "ladder" / "PICKNPLACE_FULL.xgwx"
NODE = shutil.which("node")
# monitor.html의 스크립트 앞뒤에 붙인다. DOM은 update가 쓰는 만큼만 흉내 내고, fetch는 답하지 않는다
STUB_DOM = """
const els = {};
const element = () => ({textContent: "", className: "", children: [], classList: {add() {}, remove() {}},
                        append(...xs) { this.children.push(...xs); }});
globalThis.document = {getElementById: id => (els[id] ??= element()), createElement: element};
globalThis.fetch = () => new Promise(() => {});
"""
UPDATE_ROBOTS = """
console.log(JSON.stringify([0, 1, 2].map(robot => {
  els.warn = element();
  update({running: true, scans: 1, robot, ports: {modbus: 5020, wiring: 5021}, problems: [], warnings: [],
          devices: [], forced: {}, modbus: [], on: []});
  return [els.info.textContent, els.warn.children.map(c => c.textContent)];
})));
"""
STATE_KEYS = {"running", "scans", "path", "opened", "names", "chosen", "problems", "warnings",
              "forced", "robot", "ports", "devices", "on", "modbus"}


class MonitorTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.app = FakePlc(FULL_XGWX)
        await self.app.start(modbus_port=0, wiring_port=0, http_port=0)
        self.base = "http://127.0.0.1:%d" % self.app.ports["http"]

    async def asyncTearDown(self):
        await self.app.close()

    async def http(self, path, body=None, headers=None, raw=None):
        """요청 하나를 스레드에서 보낸다(이벤트 루프는 계속 돈다). 돌려주는 값: (상태 코드, 본문, Content-Type)."""
        def go():
            data = raw if raw is not None else None if body is None else json.dumps(body).encode()
            req = urllib.request.Request(self.base + path, data=data,
                                         headers={"Content-Type": "application/json", **(headers or {})})
            try:
                with urllib.request.urlopen(req, timeout=5) as r:
                    return r.status, r.read(), r.headers["Content-Type"]
            except urllib.error.HTTPError as err:
                with err:
                    return err.code, err.read(), err.headers["Content-Type"]
        return await asyncio.to_thread(go)

    async def api(self, path, body=None):
        code, data, _ = await self.http(path, body)
        return code, json.loads(data)

    async def state(self):
        return (await self.api("/api/state"))[1]

    async def until_state(self, pred, timeout=3.0):
        end = time.monotonic() + timeout
        while not pred(s := await self.state()):
            if time.monotonic() > end:
                self.fail("%.1f초 안에 상태가 맞지 않음: %s" % (timeout, s))
            await asyncio.sleep(0.02)
        return s

    async def test_state_shape(self):
        s = await self.until_state(lambda s: dict((d[0], d[1]) for d in s["devices"]).get("D01000") == 2)
        self.assertEqual(set(s), STATE_KEYS)
        self.assertTrue(s["running"] and s["scans"] > 0)        # 시작하면 바로 RUN
        self.assertEqual((s["names"], s["chosen"], s["problems"]), (["E2E_PICKNPLACE"], ["E2E_PICKNPLACE"], []))
        self.assertIn(LATCH, s["warnings"])
        self.assertEqual(set(s["ports"]), {"modbus", "wiring", "http"})

    async def test_run_stop(self):
        self.assertEqual((await self.api("/api/stop", {}))[0], 200)
        self.assertFalse((await self.state())["running"])
        self.assertEqual((await self.api("/api/run", {}))[0], 200)
        self.assertTrue((await self.state())["running"])

    async def test_open_choose_and_errors(self):
        two = self.dir / "two.xgwx"
        write_two_programs(two, encoded("OUT M00002 := P00000"))
        await self.api("/api/open", {"path": '"%s"' % two, "programs": None})   # 탐색기 "경로로 복사"의 따옴표
        s = await self.state()
        self.assertEqual((s["names"], s["chosen"], s["running"]),
                         (["E2E_PICKNPLACE", "SECOND"], ["E2E_PICKNPLACE", "SECOND"], False))
        await self.api("/api/open", {"path": str(two), "programs": ["SECOND"]})
        self.assertEqual((await self.state())["chosen"], ["SECOND"])
        await self.api("/api/open", {"path": str(self.dir / "missing.xgwx"), "programs": None})
        self.assertTrue((await self.state())["problems"][0].startswith("열 수 없음: "))
        code, body = await self.api("/api/run", {})
        self.assertEqual(code, 400)
        self.assertIn("열 수 없음", body["error"])

    async def test_force(self):
        def p4(s):
            return dict((d[0], d[1]) for d in s["devices"])["P00004"]
        await self.api("/api/force", {"device": "P00004", "value": 1})
        s = await self.until_state(lambda s: p4(s) == 1)
        self.assertEqual(s["forced"], {"P00004": 1})
        await self.api("/api/force", {"device": "P00004", "value": None})
        s = await self.until_state(lambda s: p4(s) == 0)            # 풀면 배선 값(로봇이 없으니 0)
        self.assertEqual(s["forced"], {})
        for bad in ({"device": "D01000", "value": 1}, {"device": "P00004", "value": 2},
                    {"device": 5, "value": 1}):
            self.assertEqual((await self.api("/api/force", bad))[0], 400, bad)

    async def test_diagram_and_highlight(self):
        (self.dir / "h.ld.txt").write_text("OUT M00001 := P00000 & !P00001", encoding="utf-8")
        await self.api("/api/open", {"path": str(self.dir / "h.ld.txt"), "programs": None})
        await self.api("/api/run", {})
        _, d = await self.api("/api/diagram")
        items = {i["text"]: i for p in d["programs"] for r in p["rungs"] for i in r["items"] if i["text"]}
        self.assertEqual([(items[t]["kind"], items[t]["col"]) for t in ("P00000", "P00001", "M00001")],
                         [("NO", 0), ("NC", 1), ("OUT", 31)])
        a, b, coil = (items[t]["id"] for t in ("P00000", "P00001", "M00001"))

        def lit(want):
            return lambda s: set(s["on"]) & {a, b, coil} == want
        await self.until_state(lit({b}))                       # b접점은 디바이스가 0이면 닫혀 있다
        await self.api("/api/force", {"device": "P00000", "value": 1})
        await self.until_state(lit({a, b, coil}))               # 닫힌 접점과 켜진 코일을 강조한다
        await self.api("/api/force", {"device": "P00001", "value": 1})
        await self.until_state(lit({a}))

    async def test_bad_requests(self):
        self.assertEqual((await self.http("/nope"))[0], 404)
        self.assertEqual((await self.http("/api/run", {"x": 1}))[0], 400)            # 필드가 틀림
        self.assertEqual((await self.http("/api/run", raw=b"{" + b" " * 5000 + b"}"))[0], 400)   # 4 KB 넘음
        self.assertEqual((await self.http("/api/run", raw=b"[]"))[0], 400)           # 객체가 아님
        self.assertEqual((await self.http("/api/state", headers={"Host": "evil:1"}))[0], 403)
        self.assertEqual((await self.http("/api/stop", {}, headers={"Origin": "http://evil"}))[0], 403)
        self.assertTrue((await self.state())["running"])                             # 403 요청은 아무것도 바꾸지 않았다

    @unittest.skipUnless(os.name == "nt", "SO_REUSEADDR로 같은 포트를 두 번 여는 것은 윈도우 동작이다")
    async def test_second_monitor_on_the_same_port_fails(self):
        with self.assertRaises(OSError):
            make_http(self.app, asyncio.get_running_loop(), self.app.ports["http"]).server_close()

    async def test_page(self):
        code, page, kind = await self.http("/")
        text = page.decode("utf-8")
        self.assertEqual((code, kind), (200, "text/html; charset=utf-8"))
        self.assertIn('"Malgun Gothic"', text)
        for outside in ("http:", "https:", "<link", "src=", "@import"):          # 외부 자원 없음
            self.assertNotIn(outside, text)


class ClosedLoopTest(unittest.TestCase):
    def test_closed_loop_answers_503(self):
        """끝내는 중이라 루프가 닫힌 뒤에 온 요청은 연결을 끊지 않고 503이다. 돌지 않을 코루틴은 닫으므로 경고가 없다."""
        loop = asyncio.new_event_loop()
        loop.close()
        http = make_http(None, loop, 0)
        self.addCleanup(http.server_close)
        thread = threading.Thread(target=http.serve_forever, args=(0.05,))
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(http.shutdown)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            for path, data in (("/api/state", None), ("/api/run", b"{}")):
                req = urllib.request.Request("http://127.0.0.1:%d%s" % (http.server_port, path), data=data,
                                             headers={"Content-Type": "application/json"})
                with self.assertRaises(urllib.error.HTTPError) as caught_error:
                    urllib.request.urlopen(req, timeout=5)
                with caught_error.exception as e:
                    self.assertEqual((e.code, json.loads(e.read())), (503, {"error": "가짜 PLC를 끝내는 중입니다"}))
            time.sleep(0.05)                                    # 요청 스레드가 코루틴을 놓을 때 나는 경고까지
            gc.collect()
        self.assertEqual([str(w.message) for w in caught], [])


@unittest.skipUnless(NODE, "node가 없어 모니터 스크립트 시험을 건너뜀")
class MonitorScriptTest(unittest.TestCase):
    def test_robot_count_and_warning(self):
        page = (ROOT / "fake_plc" / "monitor.html").read_text(encoding="utf-8")
        script = re.search(r"<script>(.*)</script>", page, re.S).group(1)
        r = subprocess.run([NODE, "-"], input=STUB_DOM + script + UPDATE_ROBOTS, capture_output=True, text=True,
                           encoding="utf-8", timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        (none, w0), (one, w1), (two, w2) = json.loads(r.stdout)
        self.assertEqual([t.split(" · ")[1] for t in (none, one, two)], ["로봇 없음", "로봇 1대 연결됨", "로봇 2대 연결됨"])
        self.assertEqual((w0, w1), ([], []))
        self.assertEqual(w2, ["로봇이 2대 붙었다. 한 대만 붙인다 (가짜 로봇을 두 번 띄웠는지 본다)"])


class MainTest(unittest.IsolatedAsyncioTestCase):
    def test_arguments(self):
        a = arguments([])
        self.assertEqual((Path(a.file), a.modbus_port, a.wiring_port, a.http_port), (FULL_XGWX, 5020, 5021, 8878))
        a = arguments(["x.ld.txt", "--http-port", "0"])
        self.assertEqual((a.file, a.http_port), ("x.ld.txt", 0))
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            arguments(["--modbus-port", "70000"])

    async def test_serve_prints_addresses_and_runs(self):
        out = io.StringIO()
        args = arguments(["--modbus-port", "0", "--wiring-port", "0", "--http-port", "0"])
        with contextlib.redirect_stdout(out):
            task = asyncio.create_task(serve(args))
            end = time.monotonic() + 5
            while "파일" not in out.getvalue() and time.monotonic() < end:
                await asyncio.sleep(0.02)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        text = out.getvalue()
        self.assertRegex(text, r"가짜 PLC 모니터 +http://127\.0\.0\.1:\d+")
        self.assertRegex(text, r"Modbus TCP +127\.0\.0\.1:\d+")
        self.assertRegex(text, r"배선 JSON +127\.0\.0\.1:\d+")
        self.assertRegex(text, r"파일 +.*PICKNPLACE_FULL\.xgwx · RUN")
        self.assertEqual(len(set(re.findall(r"127\.0\.0\.1:(\d+)", text))), 3)


if __name__ == "__main__":
    unittest.main()

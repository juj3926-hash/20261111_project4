"""3D 화면 (fake_robot/static/index.html, app.js)과 넣어 둔 라이브러리. 그림 자체는 브라우저에서 사람이 본다."""

import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.fake_robot_samples.doubles import Rig, wait_for

PACKAGE = Path(__file__).resolve().parent.parent / "fake_robot"
NODE = shutil.which("node")
HARNESS = Path(__file__).resolve().parent / "fake_robot_samples" / "page_harness.mjs"


class VendorTest(unittest.TestCase):
    def test_vendored_files_match_the_recorded_hashes(self):
        rows = re.findall(r"^\| `([^`]+)` \|.*\| `([0-9a-f]{64})` \|$",
                          (PACKAGE / "vendor" / "README.md").read_text(encoding="utf-8"), re.M)
        self.assertEqual({name for name, _ in rows}, {"interpreter.js", "acorn.js", "three.module.min.js",
                                                      "three.core.min.js", "OrbitControls.js"})
        for name, digest in rows:
            with self.subTest(name):
                self.assertEqual(hashlib.sha256((PACKAGE / "vendor" / name).read_bytes()).hexdigest(), digest)


class PageTest(unittest.TestCase):
    def setUp(self):
        self.rig = Rig()
        self.addCleanup(self.rig.close)
        wait_for(lambda: self.rig.wiring.connected)

    def test_page_and_its_scripts_are_served_offline(self):
        status, page, ctype = self.rig.request("GET", "/")
        self.assertEqual((status, ctype), (200, "text/html"))
        page = page.decode("utf-8")
        for text in ["치수는 근사", "DI0", "DI1", "DI2", "DI3", "DO0", "DO1", '"Malgun Gothic"', 'accept=".js"',
                     '"three": "/vendor/three.module.min.js"', "스레드의 moveStop은 실물 미확인", "ES5만 실행됨",
                     "다시 실행 (읽어 둔 내용)", "스크립트를 고쳤으면 파일을 다시 고른다"]:
            self.assertIn(text, page)
        for path in ["/app.js", "/worker.js", "/rodi.js", "/vendor/three.module.min.js", "/vendor/three.core.min.js",
                     "/vendor/OrbitControls.js", "/vendor/acorn.js", "/vendor/interpreter.js"]:
            with self.subTest(path):
                self.assertEqual(self.rig.request("GET", path)[:3:2], (200, "text/javascript"))
        app = (PACKAGE / "static" / "app.js").read_text(encoding="utf-8")
        for name, text in [("index.html", page), ("app.js", app)]:
            self.assertNotRegex(text, r"https?://", f"{name}가 바깥 주소를 부른다")


@unittest.skipUnless(NODE, "node가 없어 화면 스크립트 문법 검사를 건너뜀")
class ScriptSyntaxTest(unittest.TestCase):
    def test_page_scripts_parse(self):
        static = PACKAGE / "static"
        with tempfile.TemporaryDirectory() as tmp:
            module = Path(tmp) / "app.mjs"                               # app.js는 ES 모듈이다
            shutil.copy(static / "app.js", module)
            for file in [module, static / "worker.js", static / "rodi.js"]:
                with self.subTest(file.name):
                    r = subprocess.run([NODE, "--check", str(file)], capture_output=True, text=True, encoding="utf-8")
                    self.assertEqual(r.returncode, 0, r.stderr)


@unittest.skipUnless(NODE, "node가 없어 화면 스크립트 시험을 건너뜀")
class PageScriptTest(unittest.TestCase):
    """app.js를 Node로 돌린다(page_harness.mjs). three, DOM, 서버는 가짜다."""

    def run_page(self, webgl):
        r = subprocess.run([NODE, str(HARNESS), webgl], capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertNotIn("error", out, out.get("error"))
        # 서버를 다시 켜서 마지막 번호가 줄면 로그를 처음부터 다시 받는다. 옛 번호로 묻기만 하면 로그가 멈춘다
        self.assertEqual(out["asked"], ["/api/state?log=0", "/api/state?log=3", "/api/state?log=0"])
        self.assertTrue(out["log"][0].endswith("다시 켠 뒤"), out["log"])
        # 읽은 파일은 고르기에서 비운다. 그래야 고친 같은 파일을 다시 고를 때 change가 나서 새로 읽는다
        self.assertEqual((out["value"], out["started"]), ("", ["A", "B", "B"]))   # 마지막은 [다시 실행]
        return out

    def test_page_draws_the_arm(self):
        out = self.run_page("1")
        self.assertEqual(out["note"], "")
        self.assertTrue(out["joints"].startswith("관절 (deg)"), out["joints"])

    def test_without_webgl_the_rest_still_works(self):
        self.assertEqual(self.run_page("0")["note"], "3D 화면을 쓸 수 없음 (WebGL 없음)")


if __name__ == "__main__":
    unittest.main()

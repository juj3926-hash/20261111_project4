"""tasks_1109 6절 통합 시험 표를 장비 없이 세 프로그램(PC 프로그램, 가짜 PLC, 가짜 로봇)으로 실제로 돈다.

    python tests/integration_all.py [--robot builtin|script]

- 고정 포트 5020(Modbus), 5021(배선), 8877(PC), 8878(가짜 PLC), 8879(가짜 로봇)를 쓴다. 하나라도 쓰이고 있으면 시작하지 않는다.
- 가짜 PLC는 참조 래더 ladder/PICKNPLACE_FULL.xgwx를 돈다.
- --robot script: 참조 스크립트 robot/PICKNPLACE_FULL_main.js를 가짜 로봇의 학생 스크립트 모드로 돌린다.
  브라우저 대신 시험용 Node 틀(tests/fake_robot_samples/worker_harness.js)이 Worker를 돌린다. node가 있어야 한다.
- 1, 2, 3, 3-1, 4~9행은 PC 프로그램의 HTTP API로만 누른다. 9행의 STOP·RUN만 가짜 PLC 모니터 API를 쓴다.
- 행마다 D01000 변화 열과 명령 결과는 PC 프로그램의 이벤트(로그 창, 로그 파일과 같은 것)에서 뽑는다.
- 세 프로그램의 출력은 logs/integration_*.txt에 남는다. 끝나면(Ctrl+C, 예외 포함) 모두 끈다.
- 이름이 test_로 시작하지 않으므로 unittest가 줍지 않는다.
"""

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tests.test_fake_robot_worker import NodeWorker  # noqa: E402  시험용 Node 틀을 띄우고 start를 보낸다

LOGS = ROOT / "logs"
PC, PLC, ROBOT = 8877, 8878, 8879
PORTS = {"Modbus": 5020, "배선": 5021, "PC 프로그램": PC, "가짜 PLC": PLC, "가짜 로봇": ROBOT}
PROGRAMS = {
    "fake_plc": [str(ROOT / "ladder" / "PICKNPLACE_FULL.xgwx"),
                 "--modbus-port", "5020", "--wiring-port", "5021", "--http-port", str(PLC)],
    "fake_robot": ["--plc-host", "127.0.0.1", "--wiring-port", "5021", "--http-port", str(ROBOT)],
    "picknplace_full": ["--target", "tcp:127.0.0.1:5020", "--http-port", str(PC)],
}
INIT_SCANS = 310  # RUN 뒤 그리퍼 열기 3초(스캔 하나 = 래더 시간 10 ms)가 끝났다


class Fail(Exception):
    pass


def until(cond, timeout, what):
    end = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > end:
            raise Fail("시간 초과: " + what)
        time.sleep(0.1)


def http(port, path, body=None):
    """(HTTP 상태, JSON). 거절한 누름(200 + message)과 잘못된 요청(400)도 예외 없이 돌려준다.
    Host는 urllib이 127.0.0.1:포트로 채운다."""
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), data, {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        with e:
            return e.code, json.loads(e.read())


def plc():
    return http(PLC, "/api/state")[1]


def robot(log=10 ** 9):
    """가짜 로봇 상태. log 번호 뒤의 실행 로그만 온다(기본: 없음)."""
    return http(ROBOT, "/api/state?log=%d" % log)[1]


class Pc:
    """PC 프로그램 HTTP. 이벤트에서 D01000 변화 열(raws)과 명령 결과(results)를 모은다."""

    def __init__(self):
        self.seen, self.raws, self.results, self.at = set(), [], [], (0, 0)

    def status(self):
        code, s = http(PC, "/api/status")
        if code != 200:
            raise Fail("PC 상태 읽기: HTTP %d %s" % (code, s.get("message")))
        for e in s["events"]:                     # 최근 200개. 겹치는 것은 (시각, 태그, 글)로 거른다
            key = (e["at"], e["tag"], e["text"])
            if key in self.seen:
                continue
            self.seen.add(key)
            m = re.search(r"→ (\d{4})", e["text"])
            if e["tag"] == "상태" and m and (not self.raws or self.raws[-1] != int(m[1])):
                self.raws.append(int(m[1]))       # 우편함만 바뀐 상태 이벤트는 빼고 D01000 변화만
            elif e["tag"] in ("접수", "거부", "미확인"):
                self.results.append("%s %s" % (e["text"].split()[0], e["tag"]))
        return s

    def now(self):
        """지금 (D01000, D01001)."""
        s = self.status()["status"]
        return s["raw"], s["mailbox"]

    def mark(self):
        self.status()
        self.at = (len(self.raws) - 1, len(self.results))

    def since(self):
        """mark 뒤 (D01000 변화 열, 명령 결과). 열의 첫 값은 mark 때의 상태다."""
        self.status()
        return self.raws[self.at[0]:], self.results[self.at[1]:]

    def send(self, name, path, body):
        """버튼이 켜질 때까지 기다렸다가 누른다. PC가 거절하면("안 됨") 실패다."""
        until(lambda: self.status()["buttons"][name]["enabled"], 10, name + " 버튼 켜짐")
        status, reply = http(PC, path, body)
        if status != 200 or " 안 됨" in reply["message"]:
            raise Fail("%s: HTTP %d %s" % (name, status, reply["message"]))

    def press(self, button):
        self.send(button, "/api/press", {"button": button})

    def code(self, code):
        self.send("code", "/api/code", {"code": code})

    def cycle_end(self):
        until(lambda: not self.status()["sequence"]["running"], 40, "자동 순서 끝")


def row1(pc):
    pc.code(2222)
    until(lambda: pc.now() == (2, 0), 5, "0002 · 우편함 0")


def row2(pc):
    pc.press("start")
    pc.cycle_end()


def row3(pc):
    pc.press("start")
    until(lambda: pc.now()[0] == 1, 5, "0001")              # 이동1 중에 정지를 누른다
    pc.press("stop")
    pc.cycle_end()
    time.sleep(1.0)                                         # 다음 이동이 없는지 더 본다


def row3_1(pc):
    pc.press("script2")
    until(lambda: pc.since()[0] == [5, 4, 5], 8, "0005 → 0004 → 0005")
    time.sleep(3.5)                                         # 자동 열기가 있다면 3초 뒤 0002가 된다


def row4(pc):
    pc.press("gripper")                                     # 4~7행은 열림에서 시작한다
    until(lambda: pc.now()[0] == 2, 6, "0002")
    pc.mark()
    pc.press("start")
    # 로봇이 실제로 움직인 뒤(DO0 ON) 누른다. 그 전이면 9995가 아직 안 받은 요청을 취소한다(spec 결정 3)
    until(lambda: robot()["do"][0] == 1, 5, "로봇 DO0")
    time.sleep(0.5)
    pc.press("estop")
    until(lambda: pc.now()[0] == 3, 5, "0003")              # 0003은 9995 접수 때부터다(DO1 또는 즉시 정지 중)
    # 로봇이 멈춘 뒤(DO1 ON) 재개한다. 감속(0.3초) 중에 재개하면 로봇이 멈춘 뒤 DO1을 잠깐 켜서 0003이 한 번 더 보인다
    until(lambda: robot()["do"][1] == 1, 5, "로봇 DO1")
    pc.press("resume")
    pc.cycle_end()


def row5(pc):
    pc.press("script1")
    until(lambda: pc.now()[0] == 1, 5, "0001")
    pc.code(3333)                                           # 그리퍼 버튼은 동작 중에 꺼져 있다. 직접 코드로 보낸다
    until(lambda: pc.now()[0] == 8, 6, "0008")              # 이동이 끝나도 PLC 이상은 남는다


def row6(pc):
    pc.press("script1")
    until(lambda: pc.since()[0] == [8, 1, 2], 8, "0008 → 0001 → 0002")


def row7(pc):
    pc.press("gripper")
    until(lambda: pc.now()[0] == 5, 6, "0005")
    pc.press("gripper")
    until(lambda: pc.now()[0] == 2, 6, "0002")


def row8(pc):
    pc.code(1234)
    until(lambda: pc.now() == (8, 0), 5, "0008 · 우편함 0")


def row9(pc):
    for path in ("/api/stop", "/api/run"):
        status, reply = http(PLC, path, {})
        if status != 200:
            raise Fail("가짜 PLC %s: HTTP %d %s" % (path, status, reply))
    until(lambda: pc.now()[0] == 2, 3, "RUN 뒤 0002")         # 첫 스캔 뒤에 보낸다. 첫 스캔은 우편함을 판정 없이 지운다
    for code in (1111, 9991, 9992, 9994, 3333):             # 시작·이동·해제·그리퍼: 처음 3초 안이라 거부
        pc.code(code)
    until(lambda: plc()["scans"] > INIT_SCANS, 5, "RUN 뒤 3초")
    pc.code(2222)
    until(lambda: pc.now()[0] == 2, 3, "0002")


ROWS = [  # (행, 할 일, 기대하는 D01000 변화 열, 기대하는 명령 결과)
    ("1", row1, [2], ["2222 접수"]),
    ("2", row2, [2, 1, 2, 5, 4, 5, 2], ["1111 접수", "9991 접수", "9992 접수"]),
    ("3", row3, [2, 1, 2, 5], ["1111 접수", "9991 접수", "9994 접수"]),
    ("3-1", row3_1, [5, 4, 5], ["9992 접수"]),
    ("4", row4, [2, 1, 3, 1, 2, 5, 4, 5, 2], ["1111 접수", "9991 접수", "9995 접수", "9993 접수", "9992 접수"]),
    ("5", row5, [2, 1, 7, 8], ["9991 접수", "3333 거부"]),
    ("6", row6, [8, 1, 2], ["9991 접수"]),
    ("7", row7, [2, 5, 2], ["3333 접수", "3333 접수"]),
    ("8", row8, [2, 8], ["1234 거부"]),
    ("9", row9, [8, 2, 8, 2], ["1111 거부", "9991 거부", "9992 거부", "9994 거부", "3333 거부", "2222 접수"]),
]


def text(raws, results):
    return " → ".join("%04d" % r for r in raws) + (" · " + ", ".join(results) if results else "")


def free(port):
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def start_script(procs):
    """가짜 로봇을 학생 스크립트 모드로 바꾸고 참조 스크립트를 Node Worker로 돌린다. 페이지처럼 지금 관절에서 시작한다."""
    http(ROBOT, "/api/mode", {"mode": "script"})
    source = (ROOT / "robot" / "PICKNPLACE_FULL_main.js").read_text(encoding="utf-8")
    worker = NodeWorker("http://127.0.0.1:%d/" % ROBOT, source, robot()["joints"])
    procs.append(worker.p)
    until(lambda: any(e["text"] == "실행 시작" for e in robot(0)["log"]), 10, "스크립트 실행 시작")


def run(mode):
    procs, env = [], {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
    before = set(LOGS.glob("PICKNPLACE_FULL_*.log"))
    try:
        for name, argv in PROGRAMS.items():                # 순서는 상관없다. 각자 상대를 다시 찾는다
            with open(LOGS / ("integration_%s.txt" % name), "w", encoding="utf-8") as out:
                procs.append(subprocess.Popen([sys.executable, "-m", name, *argv], cwd=ROOT, env=env,
                                              stdout=out, stderr=subprocess.STDOUT))
        pc = Pc()

        def ready():
            if any(p.poll() is not None for p in procs):
                raise Fail("프로그램 하나가 끝났다. logs/integration_*.txt를 보세요")
            try:
                s = pc.status()
                return bool(s["comm_ok"] and s["status"] and s["status"]["valid"]
                            and robot()["connected"] and plc()["scans"] > INIT_SCANS)
            except OSError:                                # 아직 안 떴다
                return False
        until(ready, 30, "세 프로그램 준비")
        if mode == "script":
            start_script(procs)
        rows = []
        for name, step, want, want_results in ROWS:
            pc.mark()
            note = ""
            try:
                step(pc)
            except Fail as e:
                note = str(e)
            got = pc.since()
            ok = not note and got == (want, want_results)
            rows.append((name, got, ok, "" if ok else " — 기대: %s%s" % (text(want, want_results),
                                                                         "; " + note if note else "")))
            print("%s행 %s" % (name, "PASS" if ok else "FAIL"), flush=True)
        warnings = sorted({e["text"] for e in robot(0)["log"] if e["kind"] in ("경고", "오류")})
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()
            p.wait()
    doc = (ROOT / "docs" / "tasks_1109.md").read_text(encoding="utf-8").split("### 통합 시험 표")[1]
    expected = dict(re.findall(r"^\| (\S+) \| [^|]+ \| ([^|]+?) \|", doc, re.M))
    print("\n## tasks_1109 6절 통합 시험 (로봇: %s)\n" % mode)
    print("| 행 | 기대 결과 (tasks 6절) | 관찰 (PC 상태 이벤트의 D01000 변화 · 명령 결과) | 판정 |")
    print("| --- | --- | --- | --- |")
    for name, got, ok, why in rows:
        print("| %s | %s | %s%s | %s |" % (name, expected[name], text(*got), why, "PASS" if ok else "FAIL"))
    passed = sum(ok for _, _, ok, _ in rows)
    print("\n%d/%d행 PASS" % (passed, len(rows)))
    for w in warnings:
        print("가짜 로봇 실행 로그: " + w)
    print("PC 로그 파일: %s" % ", ".join(str(p) for p in sorted(set(LOGS.glob("PICKNPLACE_FULL_*.log")) - before)))
    return 0 if passed == len(rows) else 1


def main():
    p = argparse.ArgumentParser(prog="python tests/integration_all.py", description=__doc__.splitlines()[0])
    p.add_argument("--robot", choices=("builtin", "script"), default="builtin",
                   help="가짜 로봇 모드: builtin(내장 스크립트, 기본) 또는 script(참조 스크립트를 Node Worker로)")
    args = p.parse_args()
    sys.stdout.reconfigure(errors="replace")               # 콘솔(cp949)에 없는 글자가 있어도 멈추지 않는다
    if args.robot == "script" and not shutil.which("node"):
        print("node가 없어 --robot script를 돌릴 수 없다. Node.js를 깔거나 --robot builtin으로 돈다.")
        return 2
    busy = ["%s %d" % (name, port) for name, port in PORTS.items() if not free(port)]
    if busy:
        print("포트가 이미 쓰이고 있다: %s. 그 프로그램을 끄고 다시 실행하세요." % ", ".join(busy))
        return 2
    LOGS.mkdir(exist_ok=True)
    try:
        return run(args.robot)
    except Fail as e:
        print("시작 실패: %s" % e)
        return 1
    except KeyboardInterrupt:
        print("중단 (Ctrl+C). 세 프로그램을 껐다.")
        return 130


if __name__ == "__main__":
    sys.exit(main())

"""수요일 평가 공통 시험대. 고정 커밋의 래더 모형(VirtualPlant)과 실제 통신 부품(PlcLink)에 고장을 넣는다.

- 실물에 연결하는 옵션은 없다. 모형 결과는 실물 결과가 아니다.
- 학생 코드(picknplace_full/)를 그대로 불러 쓴다. Sequencer는 기본값으로 만들므로 시간 한도는 학생 코드의 값이다.
- 통신 설정은 PC 앱(python -m picknplace_full)의 기본값과 같다: 읽기 0.2초, 명령 확인 읽기 0.05초, 확인 한도 2초.
"""

import asyncio
import hashlib
import json
import re
import time
from pathlib import Path

from picknplace_full.link import PlcLink
from picknplace_full.model import ModelTransport, VirtualPlant
from picknplace_full.sequence import Sequencer
from picknplace_full.status import TransportError

ROOT = Path(__file__).resolve().parent
LINK_ARGS = dict(interval=0.2, fast_interval=0.05, timeout=2.0)
RESULT = re.compile(r"(완료|정지|중단) \d+/\d+")  # 반복 결과 꼴 (평가 문항 2절)


def fingerprint() -> str:
    """picknplace_full/ 아래 .py 전체의 지문(SHA-256 앞 12자리). 로그와 제출 코드가 같은지 맞춰 볼 때 쓴다."""
    base = ROOT / "picknplace_full"
    h = hashlib.sha256()
    for p in sorted(base.rglob("*.py")):
        if "__pycache__" not in p.parts:
            h.update(p.relative_to(base).as_posix().encode() + b"\0" + p.read_bytes() + b"\0")
    return h.hexdigest()[:12]


class StepClock:
    """PC가 본 통신 이벤트로 사이클마다 단계별 시간(초)을 잰다. 모형과 실물의 시간을 같은 기준으로 비교할 때 쓴다.

    이동1: 9991 접수 → 로봇 정지 관측, 닫힘: → 그리퍼 닫힘 관측, 이동2: 9992 접수 → 로봇 정지 관측, 열림: → 그리퍼 열림 관측.
    즉시 정지로 멈춘 시간도 그 단계에 들어간다.
    """

    STEPS = ("이동1", "닫힘", "이동2", "열림")

    def __init__(self, now):
        self.now = now
        self.rows = []
        self._phase, self._mark = "", 0.0

    def feed(self, e, status) -> None:
        head, t = (e.text.split()[0] if e.text else ""), self.now()
        if e.tag == "접수" and head == "1111":
            self.rows.append({})
            self._phase = "start"
        elif e.tag == "접수" and head in ("9991", "9992") and self.rows:
            self._phase, self._mark = ("move1" if head == "9991" else "move2"), t
        elif e.tag == "상태" and self.rows and status is not None and status.valid:
            row = self.rows[-1]
            if self._phase in ("move1", "move2") and status.robot == 1:
                row["이동1" if self._phase == "move1" else "이동2"] = round(t - self._mark, 2)
                self._phase, self._mark = ("close" if self._phase == "move1" else "open"), t
            elif (self._phase, status.gripper) in (("close", 1), ("open", 0)):
                row["닫힘" if self._phase == "close" else "열림"] = round(t - self._mark, 2)
                self._phase = ""

    def table(self) -> list[str]:
        if not self.rows:
            return []
        out = ["사이클 | " + " | ".join(self.STEPS)]
        for k, row in enumerate(self.rows, 1):
            out.append("%6d | " % k + " | ".join("%5s" % row.get(s, "-") for s in self.STEPS))
        return out


class EvalTransport:
    """모형을 감싸 고장을 넣는다. 통신 끊김 중에는 읽기와 쓰기가 모두 실패하고 쓰기는 PLC에 닿지 않는다."""

    name = "model · 평가 장면"

    def __init__(self, plant: VirtualPlant):
        self.inner = ModelTransport(plant)
        self.fail_until = 0.0  # 이 시각(monotonic)까지 통신 끊김
        self.rewrite = None  # rewrite(순번, 코드) -> PLC에 실제로 쓸 코드. 거부를 만들 때 쓴다
        self.lose_reply = set()  # 이 순번의 쓰기는 PLC에 전달한 뒤 응답만 잃는다
        self.delivered = []  # PLC에 닿은 쓰기 (순번, 코드)
        self._n = 0

    async def connect(self):
        return await self.inner.connect()

    async def read(self):
        if time.monotonic() < self.fail_until:
            raise TransportError("평가 모형: 통신 끊김")
        return await self.inner.read()

    async def write_command(self, code):
        if time.monotonic() < self.fail_until:
            raise TransportError("평가 모형: 통신 끊김")
        n, self._n = self._n, self._n + 1
        actual = self.rewrite(n, code) if self.rewrite else code
        self.delivered.append((n, actual))
        await self.inner.write_command(actual)
        if n in self.lose_reply:
            raise TransportError("평가 모형: 전달 뒤 쓰기 응답만 유실")

    async def close(self):
        await self.inner.close()

    def lost(self):
        return False


class Bench:
    """모형 하나, 통신 하나, Sequencer 하나. 장면 실행기와 검사기가 같이 쓴다.

    cycle은 접수된 1111 수, phase는 PC가 본 진행(start, move1, close, move2, done)이다.
    hooks의 함수는 통신 이벤트마다 불린다. 상태 이벤트는 새 관측이 기다리는 쪽에 알려지기 전에 불린다.
    """

    def __init__(self, speed=1.0, echo=True):
        self.plant = VirtualPlant(speed=speed)
        self.transport = EvalTransport(self.plant)
        self.link = PlcLink(self.transport, **LINK_ARGS)
        self.echo = echo
        self.t0 = time.monotonic()
        self.lines = []
        self.sent = []  # (시각, 코드) PC가 보낸 명령
        self.commands = []  # (시각, 코드, 판정) PC 쪽 판정: 접수, 거부, 미확인
        self.cycle, self.phase, self.completed = 0, "", 0
        self.hooks = []
        self.max_gap = 0.0  # 이벤트 루프가 가장 오래 멈춘 시간(초)
        self._gripper = None
        self._beat_task = None
        self.clock = StepClock(self.now)
        self.link.on_event(self._event)
        self.seq = Sequencer(self.link, log=self.say)

    def now(self) -> float:
        return time.monotonic() - self.t0

    def say(self, tag: str, text: str) -> None:
        line = "%7.2f [%s] %s" % (self.now(), tag, text)
        self.lines.append(line)
        if self.echo:
            print(line, flush=True)

    def _event(self, e) -> None:
        self.say(e.tag, e.text)
        head = e.text.split()[0] if e.text else ""
        if e.tag == "보냄" and head.isdigit():
            self.sent.append((self.now(), int(head)))
        elif e.tag in ("접수", "거부", "미확인") and head.isdigit():
            self.commands.append((self.now(), int(head), e.tag))
            if e.tag == "접수":
                self.cycle += head == "1111"
                self.phase = {"1111": "start", "9991": "move1", "9992": "move2"}.get(head, self.phase)
        elif e.tag == "상태":
            s = self.link.status
            if s is not None and s.valid:
                if self.phase == "move1" and s.robot == 1:
                    self.phase = "close"
                if self.phase == "move2" and s.robot == 1 and self._gripper == 1 and s.gripper == 0:
                    self.phase = "done"
                    self.completed += 1
                self._gripper = s.gripper
        self.clock.feed(e, self.link.status)
        for hook in list(self.hooks):
            hook(e)

    async def _beat(self) -> None:
        last = time.monotonic()
        while True:
            await asyncio.sleep(0.02)
            now = time.monotonic()
            self.max_gap, last = max(self.max_gap, now - last), now

    async def until(self, pred, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while not pred():
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.01)
        return True

    async def start(self) -> None:
        """통신을 열고 PLC 첫 스캔의 그리퍼 열기 3초가 끝난 뒤 새 관측 하나를 받을 때까지 기다린다."""
        self._beat_task = asyncio.create_task(self._beat())
        await self.link.start()
        ready = await self.until(lambda: self.link.comm_ok and self.link.status is not None and self.link.status.valid
                                 and not self.plant.bit("G_OPENING") and not self.plant.bit("G_CLOSING"), 15)
        if not ready or await self.link.wait(lambda s: True, 2) is None:
            raise RuntimeError("모형 준비를 확인하지 못했습니다")
        self.max_gap = 0.0
        self.say("장면", "준비 완료: %s" % self.link.status.text())

    def ended(self) -> bool:
        """반복이 끝났다: repeating이 거짓이고 last_result가 '완료|정지|중단 k/N' 꼴이다."""
        return (not getattr(self.seq, "repeating", False)
                and bool(RESULT.match(str(getattr(self.seq, "last_result", "")))))

    async def finish(self, cap: float) -> bool:
        """반복이 끝날 때까지 기다린다. cap초 안에 끝나지 않으면 False.

        repeating을 언제 세우는지는 학생의 결정(D1)이라 여기서 가정하지 않는다. 2초 안에 repeating도, 1111도,
        결과도 없으면 반복이 시작되지 않은 것으로 본다(예: 시작 요청이 거절됨).
        """
        started = await self.until(lambda: getattr(self.seq, "repeating", False) or self.ended()
                                   or any(c == 1111 for _, c in self.sent), 2.0)
        return True if not started else await self.until(self.ended, cap)

    async def settle(self, cap=20.0) -> None:
        """모형이 멈출 때까지(로봇 정지, 요청 없음, 그리퍼 동작 없음) 기다리고 1초 더 본다. 뒤늦은 명령을 잡는다."""
        await self.until(lambda: not any(self.plant.bit(b) for b in ("REQ1", "REQ2", "G_CLOSING", "G_OPENING", "DO0"))
                         and self.plant.robot.state == "쉼", cap)
        await asyncio.sleep(1.0)

    async def close(self) -> None:
        """학생의 close()를 5초까지 기다린다. 끝나지 않거나 예외가 나도 통신은 닫는다(남은 대기는 None, 명령은 미확인)."""
        try:
            await asyncio.wait_for(self.seq.close(), 5)
        except Exception as e:  # TimeoutError 포함
            self.say("장면", "Sequencer.close() 문제: %s" % (type(e).__name__ if isinstance(e, TimeoutError) else repr(e)))
        finally:
            await self.link.close()
            if self._beat_task:
                self._beat_task.cancel()
                await asyncio.gather(self._beat_task, return_exceptions=True)

    def summary(self, **extra) -> dict:
        return {"fingerprint": fingerprint(),
                "commands": ["%d %s" % (c, r) for _, c, r in self.commands],
                "result": getattr(self.seq, "last_result", None),
                "done": getattr(self.seq, "repeat_done", None),
                "total": getattr(self.seq, "repeat_total", None),
                "repeating": getattr(self.seq, "repeating", None),
                "steps": self.clock.rows,
                "loop_max_gap_s": round(self.max_gap, 3), **extra}


def dumps(data) -> str:
    return json.dumps(data, ensure_ascii=False)


if __name__ == "__main__":  # python -B -X utf8 eval_harness.py → 지금 코드의 지문 (8교시 예측지에 적는다)
    print("코드 지문 %s" % fingerprint())

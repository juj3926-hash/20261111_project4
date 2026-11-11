"""자동 순서와 버튼 규칙 (spec 7.2, 7.3). 학생이 나중에 쓰는 부분이다.

- 순서는 PC가 정한다: 1111 → 9991 → 자동 닫힘 기다림 → 9992 → 자동 열림 기다림.
- 통신은 PlcLink가 맡는다. 여기서는 send로 보내고 wait로 새 읽기를 기다리기만 한다.
- 버튼은 PC 쪽 1차 거름이다. 최종 판정은 PLC가 한다.
"""

import asyncio
import time
import traceback
from typing import Callable

from picknplace_full.link import PlcLink, SendResult
from picknplace_full.status import AUTO_OFF, ESTOP, GRIPPER, MOVE1, MOVE2, RESUME, ROBOT_TEXT, START, Status

BUTTONS = ("start", "stop", "estop", "resume", "script1", "script2", "gripper")
NAMES = {"start": "시작", "stop": "정지", "estop": "즉시 정지", "resume": "재개",
         "script1": "스크립트1", "script2": "스크립트2", "gripper": "그리퍼", "code": "직접 코드"}
CODES = {"estop": ESTOP, "resume": RESUME, "script1": MOVE1, "script2": MOVE2, "gripper": GRIPPER}


class Abort(Exception):
    """자동 순서를 중단할 이유. 시간 초과, 거부, 미확인이다."""


def _result_text(r: SendResult) -> str:
    """예: "9991 접수", "9991 미확인 (PLC 처리 확인 안 됨)" """
    return "%d %s" % (r.code, r.result) + (" (%s)" % r.reason if r.reason else "")


class Sequencer:
    """자동 순서(auto_cycle)와 버튼(press, send_code, buttons)."""

    # D3: normal 측정(logs/eval_normal_20261111_105358_066_33612.log) 최대값 × 1.5. 이동은 즉시 정지 허용 30초를 더한다
    #     이동 2.24 × 1.5 ≈ 3.4 + 30 = 33.4초, 그리퍼 3.05 × 1.5 ≈ 4.6초
    def __init__(self, link: PlcLink, move_timeout=33.4, grip_timeout=4.6,
                 log: Callable[[str, str], None] | None = None):
        self.link = link
        self.move_timeout = move_timeout  # 이동 한 번의 한도. 즉시 정지·재개 시간도 들어간다
        self.grip_timeout = grip_timeout  # 그리퍼 측정 최대 3.05초 × 1.5. 준비 확인 대기 한도로도 쓴다
        self.log = log or (lambda tag, text: None)  # 태그는 "자동"과 "버튼"
        self.running = False  # 자동 순서 중
        self.step = ""  # 자동 순서 단계 (화면용)
        self.stop_requested = False  # 정지 버튼을 눌렀다. 단계 경계에서 멈춘다
        self.auto_maybe_on = False  # 중단해서 PLC 자동 운전이 켜져 있을 수 있다. 9994가 접수되면 거짓
        self.sending = False  # 버튼 명령을 보내는 중
        self.last_result = ""  # 마지막 자동 순서 결과
        self._grip_from = None  # 3333 접수 때 그리퍼 자리
        self._grip_until = 0.0  # 그리퍼 대기 추정이 끝나는 시각
        self._task = None  # 시작 버튼이 띄운 자동 순서 작업
        # ---- 수요일 평가: 반복 운전 상태 (평가 문항 2절의 계약. 이름과 뜻을 바꾸지 않는다) ----
        self.repeating = False  # 반복 운전이 끝나기 전까지 참
        self.repeat_total = 0  # 이번 반복의 횟수 N
        self.repeat_done = 0  # 5단계 열림 완료를 새 관측으로 확인한 사이클 수

    @property
    def grip_pending(self) -> bool:
        """그리퍼 대기 중 (PC 추정). 상태 코드에 그리퍼 동작 중 자리가 없어서 추정한다.

        3333이 접수되면 참이다. 그리퍼 자리가 바뀌거나 grip_timeout이 지나면 거짓이다.
        """
        s = self.link.status
        return time.monotonic() < self._grip_until and s is not None and s.gripper == self._grip_from

    # ---- 자동 순서 (spec 7.2, tasks 1.4) ----

    async def auto_cycle(self) -> str:
        """자동 운전 한 사이클. "완료", "정지", "중단: <이유>" 중 하나를 돌려준다."""
        self.running, self.last_result = True, ""  # stop_requested는 지우지 않는다. 시작 직후 누른 정지도 지킨다
        try:
            result = await self._cycle()
        except Abort as e:
            self.auto_maybe_on = True  # PLC 자동 운전이 켜져 있을 수 있다
            result = "중단: %s" % e
        except Exception as e:  # 예상 못 한 고장도 중단으로 끝낸다. 시작 버튼의 작업은 아무도 기다리지 않는다
            traceback.print_exc()
            self.auto_maybe_on = True
            result = "중단: 예외 %r" % e
        finally:
            self.running, self.stop_requested, self.step = False, False, ""
        self.last_result = result
        self._log("자동", "한 사이클 완료" if result == "완료" else result)
        return result

    async def _cycle(self) -> str:
        self._set_step("1단계 자동 운전 시작")
        await self._accept(START)
        if self.stop_requested:  # 정지 경계: 1단계 뒤(9991 전)
            return await self._stop()

        self._set_step("2단계 이동1")
        await self._accept(MOVE1)
        await self._until(lambda s: s.robot == 1, self.move_timeout)  # 로봇 정지. 접수한 순간부터 0이라 착각하지 않는다

        self._set_step("3단계 그리퍼 닫힘 기다림")
        await self._until(lambda s: s.gripper == 1, self.grip_timeout)  # PLC가 알아서 닫는다
        if self.stop_requested:  # 정지 경계: 3단계 뒤. 4단계부터는 사이클을 끝까지 간다
            return await self._stop()

        self._set_step("4단계 이동2")
        await self._accept(MOVE2)
        await self._until(lambda s: s.robot == 1, self.move_timeout)

        self._set_step("5단계 그리퍼 열림 기다림")
        await self._until(lambda s: s.gripper == 0, self.grip_timeout)  # PLC가 알아서 열고 자동 운전을 끈다
        return "완료"

    async def _stop(self) -> str:
        """단계 경계에서 정지 요청을 처리한다. 9994가 접수되면 "정지"다."""
        self._set_step("자동 운전 해제")
        await self._accept(AUTO_OFF)
        return "정지"

    def _set_step(self, step: str) -> None:
        self.step = step
        self._log("자동", step)

    async def _accept(self, code: int) -> None:
        """code를 보낸다. 접수가 아니면(거부, 미확인) 중단한다. 1111 뒤의 명령은 로봇 정지(1)를 기다렸다가 보낸다."""
        if code != START:  # 즉시 정지 중(로봇 2)에 보내면 거부된다. 그리퍼는 정지 중에도 움직이므로 재개를 여기서 기다린다
            await self._until(lambda s: s.robot == 1, self.move_timeout)
        r = await self._send(code)
        if r.result != "접수":
            raise Abort("%s에서 %s" % (self.step, _result_text(r)))

    async def _until(self, pred: Callable[[Status], bool], timeout: float) -> None:
        """pred를 만족하는 새 읽기를 기다린다. 통신이 끊기면 새 읽기가 없으므로 한도가 지나 중단한다."""
        if await self.link.wait(pred, timeout) is None:
            raise Abort("%s에서 시간 초과 (%.1f초)" % (self.step, timeout))

    # ---- 수요일 평가: 반복 운전 (학생이 결정하고 AI가 구현한다) ----

    async def start_repeat(self, count: int) -> str:
        """[반복 시작]. 화면 한 줄을 돌려준다. 계약은 평가 문항 2절이다.

        - count가 1~5 정수가 아니거나 시작할 수 없으면 아무것도 보내지 않고 이유를 돌려준다.
        - 시작하면 반복을 작업으로 띄우고 곧바로 돌려준다(press("start")처럼 끝까지 기다리지 않는다).
        - 반복이 끝나면 repeating은 거짓, last_result는 "완료 k/N", "정지 k/N", "중단 k/N: 이유" 중 하나다.
        """
        # D1: 판단부터 표시·작업 띄우기까지 await가 없다. 그 사이에 다른 버튼 코루틴이 끼어들 수 없다
        if type(count) is not int or not 1 <= count <= 5:
            return self._say("반복 시작 안 됨: 횟수는 1~5 정수입니다 (%r)" % (count,))
        why = "반복 중" if self.repeating else self._why_off("start")
        if why:
            return self._say("반복 시작 안 됨: %s" % why)
        self.repeating = self.running = True  # D1: 판단 직후 await 없이 실행 중 표시
        self.repeat_total, self.repeat_done, self.last_result = count, 0, ""
        self._task = asyncio.create_task(self._repeat(count))  # D1: 반복 전체가 작업 하나. close()가 이 작업을 끊는다
        return self._say("반복 시작: %d회" % count)

    def _ready(self, s: Status) -> bool:
        """시작 버튼의 준비 조건: 로봇 정지, 그리퍼 열림·동작 아님, 복구 불필요."""
        return s.valid and s.robot == 1 and s.gripper == 0 and not self.grip_pending and not self.auto_maybe_on

    async def _repeat(self, count: int) -> None:
        """반복 작업 본체. 사이클마다 준비 확인 → 정지 확인 → _cycle()."""
        n, result = count, None
        try:
            while self.repeat_done < n:
                if self.stop_requested:  # D2: 사이클 사이 정지 (앞 사이클 완료 관측 뒤)
                    result = "정지 %d/%d" % (self.repeat_done, n)
                    break
                self._set_step("반복 %d/%d 준비 확인" % (self.repeat_done + 1, n))
                # D4: link.status를 그대로 쓰지 않고, 이 호출 뒤에 도착한 새 정상 읽기로 준비 조건을 본다
                # D3: 준비 확인 대기의 한도는 grip_timeout
                await self._until(self._ready, self.grip_timeout)
                if self.stop_requested:  # D2: 1111 직전 확인. 이 뒤 _accept(START)까지 await가 없다
                    result = "정지 %d/%d" % (self.repeat_done, n)
                    break
                r = await self._cycle()  # auto_cycle()은 부르지 않는다: 그 finally가 running·stop_requested를 지운다
                if r == "정지":
                    result = "정지 %d/%d" % (self.repeat_done, n)
                    break
                self.repeat_done += 1  # 규칙 1: _cycle()의 "완료"는 5단계 열림을 새 읽기로 확인한 뒤에만 나온다
                self._log("자동", "반복 %d/%d 완료" % (self.repeat_done, n))
                # D2: stop_requested는 지우지 않는다. 이동2 뒤 들어온 정지가 다음 루프 맨 앞에서 반영된다
            else:
                result = "완료 %d/%d" % (self.repeat_done, n)
        except Abort as e:
            self.auto_maybe_on = True
            result = "중단 %d/%d: %s" % (self.repeat_done, n, e)
        except asyncio.CancelledError:  # close()
            result = "중단 %d/%d: 프로그램 종료" % (self.repeat_done, n)
            raise
        except Exception as e:
            traceback.print_exc()
            self.auto_maybe_on = True
            result = "중단 %d/%d: 예외 %r" % (self.repeat_done, n, e)
        finally:
            self.repeating = self.running = self.stop_requested = False  # D1: 반복이 끝날 때 표시를 내린다
            self.step = ""
            self.last_result = result or "중단 %d/%d: 알 수 없음" % (self.repeat_done, n)
            self._log("자동", "반복 결과: %s" % self.last_result)

    async def close(self) -> None:
        """프로그램을 끝낼 때 부른다. 돌고 있는 자동 순서를 멈추고 한 줄을 남긴다.

        멈추지 않으면 Ctrl+C 뒤에도 다음 명령을 보내고, 통신이 닫힌 것을 시간 초과로 적는다.
        PLC에는 아무것도 보내지 않는다.
        """
        if self._task and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._log("자동", "프로그램 종료로 자동 순서를 멈춤")

    # ---- 버튼 (spec 7.3) ----

    async def press(self, button: str) -> str:
        """버튼 하나를 누른다. 화면에 보일 한 줄을 돌려준다. 꺼진 버튼이면 보내지 않고 이유를 돌려준다."""
        if button not in BUTTONS:
            raise ValueError("모르는 버튼: %r" % (button,))
        label = self._label(button)
        if button == "estop":  # 언제나 보낸다. 다른 명령을 보내는 중이어도 link가 대기열 맨 앞에 넣는다
            r = await self._send(ESTOP)
            return self._say("%s → %s" % (label, _result_text(r)))
        why = self._why_off(button)
        if why:
            return self._say("%s 안 됨: %s" % (label, why))
        if button == "start":
            self.running = True  # 작업이 돌기 전에도 다른 버튼이 자동 순서 중으로 본다
            self._task = asyncio.create_task(self.auto_cycle())
            return self._say("시작: 자동 순서를 시작합니다")
        if button == "stop" and self.running:
            self.stop_requested = True
            return self._say("정지 요청: 단계가 끝나면 멈춥니다")
        return await self._button_send(label, AUTO_OFF if button == "stop" else CODES[button])

    async def send_code(self, code: int) -> str:
        """직접 코드 입력 (시험 8의 모르는 코드 등). 자동 순서 중에는 보내지 않는다."""
        why = self._why_off("code")
        if why:
            return self._say("직접 코드 안 됨: %s" % why)
        return await self._button_send("직접 코드", code)

    def buttons(self) -> dict[str, dict]:
        """버튼마다 {"enabled", "label", "hint"}. hint는 꺼진 이유다. "code"는 직접 코드 입력칸이다."""
        states = {}
        for name in BUTTONS + ("code",):
            why = self._why_off(name)
            states[name] = {"enabled": not why, "label": self._label(name), "hint": why}
        return states

    def _why_off(self, name: str) -> str:
        """버튼이 꺼진 이유. 켜져 있으면 ""."""
        if name == "estop":
            return ""  # 즉시 정지는 언제나 켜져 있다
        if self.sending:
            return "보내는 중"
        if name == "stop" and self.running:  # 정지 요청 표시만 남기므로 통신·상태와 상관없다
            return "정지 요청함" if self.stop_requested else ""
        s = self.link.status
        if not self.link.comm_ok or s is None:
            return "통신 안 됨"
        if not s.valid:
            return s.text()  # 예: "알 수 없는 코드 0". PLC STOP이거나 다른 래더다
        # 위의 통신·상태 검사가 먼저다. raw 0이면 s.robot이 None이라 아래 ROBOT_TEXT[s.robot]이 고장 난다
        # 교육 사본 D: 복구가 확인되기 전 개별 동작 버튼을 보류한다.
        if self.auto_maybe_on and name in ("script1", "script2", "gripper"):
            return "자동 운전 해제(9994)를 먼저 하세요"
        in_cycle = (self.running, "자동 순서 중")
        robot_busy = (s.robot != 1, "로봇 " + ROBOT_TEXT[s.robot])  # 1 = 정지
        grip_busy = (self.grip_pending, "그리퍼 동작 중")
        off_when = {  # 버튼마다 (꺼지는 조건, 이유). 앞에서부터 본다
            "start": [in_cycle, (self.auto_maybe_on, "자동 운전 해제(9994)를 먼저 하세요"),
                      robot_busy, grip_busy, (s.gripper == 1, "그리퍼를 먼저 여세요")],
            "stop": [(not self.auto_maybe_on, "자동 순서 중 아님")],  # 여기서 정지는 9994를 보낸다
            "resume": [(s.robot != 2, "즉시 정지 중 아님")],
            "script1": [in_cycle, robot_busy, grip_busy],
            "script2": [in_cycle, robot_busy, grip_busy],
            "gripper": [in_cycle, robot_busy, grip_busy],
            "code": [in_cycle],
        }
        for off, why in off_when[name]:
            if off:
                return why
        return ""

    def _label(self, name: str) -> str:
        s = self.link.status
        if name == "stop" and self.auto_maybe_on:
            return "자동 운전 해제(9994)"
        if name == "gripper" and s is not None and s.valid:
            return ("그리퍼 닫기 (지금 열림)", "그리퍼 열기 (지금 닫힘)")[s.gripper]
        return NAMES[name]

    async def _button_send(self, label: str, code: int) -> str:
        """버튼 명령 하나를 보낸다. 보내는 동안 다른 누름은 "보내는 중"으로 거절된다."""
        self.sending = True
        try:
            r = await self._send(code)
        finally:
            self.sending = False
        return self._say("%s → %s" % (label, _result_text(r)))

    def _say(self, text: str) -> str:
        """버튼 결과 한 줄을 로그에 남기고 돌려준다."""
        self._log("버튼", text)
        return text

    def _log(self, tag: str, text: str) -> None:
        """log를 부른다. 로그 쪽 고장(예: 파일 쓰기 실패)은 stderr에 남기고 자동 순서와 버튼은 계속한다."""
        try:
            self.log(tag, text)
        except Exception:
            traceback.print_exc()

    async def _send(self, code: int) -> SendResult:
        """link로 보낸다. 접수되면 PC 쪽 추정(auto_maybe_on, 그리퍼 대기)을 고친다."""
        r = await self.link.send(code)
        if r.result == "접수" and code == AUTO_OFF:
            self.auto_maybe_on = False
        if r.result == "접수" and code == GRIPPER:
            self._grip_from, self._grip_until = r.status.gripper, time.monotonic() + self.grip_timeout
        return r

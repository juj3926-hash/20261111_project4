"""자동 순서와 버튼 규칙 Sequencer 시험. tasks 6절 시험 표를 PlcLink와 가상 장비로 끝까지 돌린다."""

import asyncio
import contextlib
import io
import unittest
from types import SimpleNamespace

from picknplace_full.link import PlcLink
from picknplace_full.model import ModelTransport, VirtualPlant
from picknplace_full.sequence import BUTTONS, Sequencer
from picknplace_full.status import decode
from tests.picknplace_fakes import FakeTransport, until


class TableTest(unittest.IsolatedAsyncioTestCase):
    """시험 표 1~9행. 가상 장비가 10배속이라 그리퍼 3초는 0.3초, 로봇 이동 2.2초는 0.22초다."""

    async def asyncSetUp(self):
        self.plant = VirtualPlant(speed=10)
        self.link = PlcLink(ModelTransport(self.plant), interval=0.02, fast_interval=0.01, timeout=1.0)
        self.events = []  # (태그, 글, 그때의 raw)
        self.link.on_event(lambda e: self.events.append((e.tag, e.text, self.link.status and self.link.status.raw)))
        self.logs = []
        self.seq = Sequencer(self.link, move_timeout=3.0, grip_timeout=0.6,
                             log=lambda tag, text: self.logs.append((tag, text)))
        await self.link.start()
        self.addAsyncCleanup(self.link.close)

    async def ready(self):
        """첫 스캔의 그리퍼 열기 3초(/10)가 끝날 때까지 기다린다. 그 뒤 새 읽기로 0002를 본다."""
        await until(lambda: not self.plant.plc.first and not self.plant.bit("G_OPENING"), 2.0)
        self.assertIsNotNone(await self.link.wait(lambda s: s.raw == 2, 1.0))

    def sent(self):
        """보낸 코드 열 (link의 보냄 이벤트)."""
        return [int(text) for tag, text, _ in self.events if tag == "보냄"]

    def raws(self):
        """raw 변화 열 (link의 상태 이벤트). 우편함만 바뀐 이벤트는 raw가 같으므로 건너뛴다."""
        out = []
        for tag, _, raw in self.events:
            if tag == "상태" and (not out or out[-1] != raw):
                out.append(raw)
        return out

    def raws_at(self, tag, code):
        """code의 결과 이벤트(접수·거부·미확인)가 났을 때의 raw."""
        return [raw for t, text, raw in self.events if t == tag and text.split()[0] == str(code)]

    def assert_raws_in_order(self, expected):
        rest = iter(self.raws())
        # in은 rest를 앞에서부터 소비하므로 차례대로 들어 있는지 본다
        self.assertTrue(all(raw in rest for raw in expected), "raw 변화 %s에 %s가 차례로 없음" % (self.raws(), expected))

    async def test_row1(self):
        await self.ready()
        r = await self.seq.send_code(2222)
        self.assertIn("접수", r)
        self.assertEqual((self.link.status.raw, self.link.status.mailbox), (2, 0))
        self.assertEqual(self.logs, [("버튼", r)])

    async def test_row2_cycle(self):
        await self.ready()
        self.assertEqual(await self.seq.auto_cycle(), "완료")
        self.assert_raws_in_order([2, 1, 2, 5, 4, 5, 2])
        self.assertEqual(self.sent(), [1111, 9991, 9992])
        self.assertEqual((self.seq.running, self.seq.last_result), (False, "완료"))
        self.assertEqual({tag for tag, _ in self.logs}, {"자동"})
        self.assertEqual(self.logs[-1], ("자동", "한 사이클 완료"))

    async def test_row3_stop_and_row3_1(self):
        await self.ready()
        await self.seq.press("start")
        self.assertIsNotNone(await self.link.wait(lambda s: s.robot == 0, 2.0))  # 이동1 중
        self.assertIn("정지 요청", await self.seq.press("stop"))
        await until(lambda: not self.seq.running, 5.0)
        self.assertEqual(self.seq.last_result, "정지")
        self.assertEqual(self.sent(), [1111, 9991, 9994])  # 이동1과 자동 닫기까지 하고 멈춤
        self.assertEqual(self.raws_at("접수", 9994), [5])
        # 3-1: 개별 스크립트2. 자동 운전이 해제됐으므로 로봇만 움직이고 그리퍼는 닫힌 채다
        self.assertIn("접수", await self.seq.press("script2"))
        self.assertIsNotNone(await self.link.wait(lambda s: s.robot == 1, 2.0))
        self.assertEqual(self.link.status.raw, 5)
        self.assertIsNone(await self.link.wait(lambda s: s.raw != 5, 0.6))  # 자동 열기 없음

    async def test_stop_before_move1(self):
        await self.ready()
        await self.seq.press("start")
        self.assertIn("정지 요청", await self.seq.press("stop"))  # 1111 접수 전에 누름
        await until(lambda: not self.seq.running, 2.0)
        self.assertEqual(self.seq.last_result, "정지")
        self.assertEqual(self.sent(), [1111, 9994])  # 1단계 뒤 바로 9994. 이동 없음

    async def test_row4_estop_resume(self):
        await self.ready()
        await self.seq.press("start")
        # 로봇이 스크립트1을 시작한 뒤에 누른다. 시작 전이면 9995가 요청을 취소한다(spec 결정 3)
        await until(lambda: self.plant.robot.state == "동작", 2.0)
        self.assertIn("접수", await self.seq.press("estop"))
        self.assertEqual(self.link.status.raw, 3)
        await until(lambda: self.plant.bit("DO1"), 1.0)  # 로봇이 멈추고 비정상 정지를 알린다
        self.assertIn("접수", await self.seq.press("resume"))
        await until(lambda: not self.seq.running, 5.0)
        self.assertEqual(self.seq.last_result, "완료")
        self.assert_raws_in_order([1, 3, 1, 2, 5, 4, 5, 2])

    async def estop_in_gripper_wait(self):
        """3단계(그리퍼 닫힘 기다림)에 즉시 정지를 누르고, 그리퍼가 닫힐 때까지 기다린 뒤 재개한다.
        그리퍼 타이머는 즉시 정지와 상관없이 돌아서 정지 중에 닫힌다."""
        await until(lambda: self.seq.step.startswith("3단계"), 2.0)
        self.assertIn("접수", await self.seq.press("estop"))
        self.assertEqual(self.link.status.raw, 3)  # 즉시 정지 · 그리퍼 열림 (아직 닫는 중)
        self.assertIsNotNone(await self.link.wait(lambda s: s.raw == 6, 1.0))  # 즉시 정지 · 닫힘
        self.assertIn("접수", await self.seq.press("resume"))
        await until(lambda: not self.seq.running, 5.0)

    async def test_estop_in_gripper_wait_resumes(self):
        await self.ready()
        await self.seq.press("start")
        await self.estop_in_gripper_wait()
        self.assertEqual(self.seq.last_result, "완료")  # 정지 중에 9992를 보내지 않고 재개를 기다렸다(spec 7.2)
        self.assertEqual(self.sent(), [1111, 9991, 9995, 9993, 9992])

    async def test_stop_and_estop_in_gripper_wait(self):
        await self.ready()
        await self.seq.press("start")
        await until(lambda: self.seq.step.startswith("3단계"), 2.0)
        self.assertIn("정지 요청", await self.seq.press("stop"))
        await self.estop_in_gripper_wait()
        self.assertEqual(self.seq.last_result, "정지")  # 정지 경계의 9994도 재개를 기다렸다
        self.assertEqual(self.sent(), [1111, 9991, 9995, 9993, 9994])
        self.assertEqual(self.raws_at("접수", 9994), [5])

    async def test_row5_gripper_rejected(self):
        await self.ready()
        self.assertIn("접수", await self.seq.press("script1"))
        self.assertIn("거부", await self.seq.send_code(3333))
        self.assertEqual(self.link.status.raw, 7)

    async def test_row6_script1_only(self):
        await self.ready()
        self.assertIn("접수", await self.seq.press("script1"))
        self.assertIsNotNone(await self.link.wait(lambda s: s.robot == 1, 2.0))
        self.assertEqual((self.link.status.raw, self.link.status.gripper), (2, 0))
        self.assertIsNone(await self.link.wait(lambda s: s.raw != 2, 0.6))  # 자동 닫기 없음

    async def test_row7_gripper_twice(self):
        await self.ready()
        self.assertEqual(self.seq.buttons()["gripper"]["label"], "그리퍼 닫기 (지금 열림)")
        self.assertIn("접수", await self.seq.press("gripper"))
        states = self.seq.buttons()
        for name in ("gripper", "script1", "script2", "start"):  # 그리퍼 대기 중 (PC 추정)
            self.assertEqual((states[name]["enabled"], states[name]["hint"]), (False, "그리퍼 동작 중"), name)
        self.assertIsNotNone(await self.link.wait(lambda s: s.raw == 5, 1.0))
        gripper = self.seq.buttons()["gripper"]
        self.assertTrue(gripper["enabled"])
        self.assertIn("지금 닫힘", gripper["label"])
        self.assertIn("접수", await self.seq.press("gripper"))
        self.assertIsNotNone(await self.link.wait(lambda s: s.raw == 2, 1.0))

    async def test_row8_unknown(self):
        await self.ready()
        self.assertIn("거부", await self.seq.send_code(1234))
        self.assertEqual((self.link.status.raw, self.link.status.mailbox), (8, 0))

    async def test_row9_init(self):
        await until(lambda: self.link.status is not None and self.link.status.valid)
        self.assertIn("거부", await self.seq.send_code(1111))  # 첫 스캔 3초 안의 시작은 거부

    async def test_abort_on_robot_timeout(self):
        self.plant.robot.ignore_requests = True  # 로봇이 자동 실행 상태가 아니다
        await self.ready()
        result = await self.seq.auto_cycle()
        self.assertTrue(result.startswith("중단"), result)
        self.assertTrue(self.seq.auto_maybe_on)
        self.assertEqual(self.seq.buttons()["stop"]["label"], "자동 운전 해제(9994)")
        # 회복: 즉시 정지로 남은 요청을 취소하고, 재개한 뒤, 자동 운전을 해제한다
        self.assertIn("접수", await self.seq.press("estop"))
        self.assertIn("접수", await self.seq.press("resume"))
        self.assertIn("9994 접수", await self.seq.press("stop"))
        self.assertFalse(self.seq.auto_maybe_on)


class ButtonRuleTest(unittest.TestCase):
    """buttons()는 link.status, link.comm_ok와 Sequencer 상태만 본다. 그래서 가짜 link로 본다."""

    def states(self, raw, comm_ok=True, **flags):
        seq = Sequencer(SimpleNamespace(status=decode(raw, 0, 0.0), comm_ok=comm_ok))
        for name, value in flags.items():
            setattr(seq, name, value)
        return seq.buttons()

    def on(self, states):
        return {name for name in BUTTONS if states[name]["enabled"]}

    def test_button_rules(self):
        self.assertEqual(set(self.states(2)), set(BUTTONS) | {"code"})
        self.assertEqual(self.on(self.states(2)), {"start", "estop", "script1", "script2", "gripper"})
        self.assertFalse(self.on(self.states(1)) & {"start", "script1", "script2", "gripper"})  # 로봇 0
        self.assertEqual(self.on(self.states(3)), {"estop", "resume"})  # 로봇 2
        closed = self.states(5)  # 그리퍼 1
        self.assertFalse(closed["start"]["enabled"])
        self.assertEqual(closed["start"]["hint"], "그리퍼를 먼저 여세요")
        running = self.states(2, running=True)
        self.assertEqual(self.on(running), {"stop", "estop"})
        self.assertFalse(running["code"]["enabled"])
        self.assertTrue(self.states(2)["code"]["enabled"])
        self.assertNotIn("stop", self.on(self.states(2, running=True, stop_requested=True)))
        self.assertEqual(self.on(self.states(2, auto_maybe_on=True)), {"stop", "estop"})  # 교육 사본 D

    def test_stop_while_running_ignores_comm(self):
        # 자동 순서 중의 정지는 요청 표시만 남긴다(spec 7.3 정지 줄에 연결 조건이 없다).
        # 9994를 보내는 정지(auto_maybe_on)는 연결과 유효한 상태가 있어야 한다
        for raw, comm_ok in ((0, True), (2, False)):  # 알 수 없는 코드, 통신 끊김
            self.assertEqual(self.on(self.states(raw, comm_ok, running=True)), {"stop", "estop"})
            self.assertEqual(self.on(self.states(raw, comm_ok, auto_maybe_on=True)), {"estop"})
        self.assertNotIn("stop", self.on(self.states(2, running=True, sending=True)))  # 보내는 중이면 거절


class FakeLinkTest(unittest.IsolatedAsyncioTestCase):
    """가짜 Transport로 장비 없이 본다."""

    async def start(self, fake, **seq_args):
        link = PlcLink(fake, interval=0.02, fast_interval=0.01, timeout=1.0)
        await link.start()
        self.addAsyncCleanup(link.close)
        return link, Sequencer(link, **seq_args)

    async def test_unknown_status_disables_buttons(self):
        """Review Focus 1: PLC STOP이거나 다른 래더라서 raw가 0이다."""
        fake = FakeTransport(raw=0)
        link, seq = await self.start(fake)
        await until(lambda: link.status is not None)
        states = seq.buttons()
        self.assertEqual({name for name in states if states[name]["enabled"]}, {"estop"})
        self.assertIn("알 수 없는 코드 0", await seq.press("start"))
        self.assertFalse(seq.running)
        await asyncio.sleep(0.1)  # 뒤에서 무언가 돌았다면 쓰기 시도가 남는다
        self.assertEqual(fake.write_tries, 0)

    async def test_sending_refuses_presses_but_estop(self):
        fake = FakeTransport(raw=2, respond=lambda code: 2, process_after_reads=5)
        link, seq = await self.start(fake)
        await until(lambda: link.comm_ok)
        first = asyncio.create_task(seq.press("script1"))
        await until(lambda: fake.writes == [9991])
        self.assertTrue(seq.sending)
        self.assertIn("보내는 중", await seq.press("script2"))
        self.assertIn("9995 접수", await seq.press("estop"))  # 즉시 정지는 예외로 보낸다
        self.assertIn("9991 접수", await first)
        self.assertEqual(fake.writes, [9991, 9995])
        self.assertFalse(seq.sending)

    async def test_abort_on_reject(self):
        fake = FakeTransport(raw=2, respond={1111: 8}.get)  # PLC가 1111을 거부한다
        link, seq = await self.start(fake)
        await until(lambda: link.comm_ok)
        result = await seq.auto_cycle()
        self.assertEqual(result, "중단: 1단계 자동 운전 시작에서 1111 거부")
        self.assertEqual(fake.writes, [1111])
        self.assertTrue(seq.auto_maybe_on)

    async def test_raising_log_does_not_stop_cycle(self):
        fake = FakeTransport(raw=2, respond={1111: 2, 9991: 5, 9992: 2}.get)  # 로봇·그리퍼가 바로 끝난다
        logs = []

        def log(tag, text):  # 예: 로그 파일 쓰기 실패
            logs.append((tag, text))
            if len(logs) == 2:  # 두 번째 자동 이벤트 = 1111 접수 뒤의 "2단계 이동1"
                raise OSError("로그 파일 쓰기 실패")

        link, seq = await self.start(fake, log=log)
        await until(lambda: link.comm_ok)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            result = await asyncio.wait_for(seq.auto_cycle(), 3.0)
        self.assertEqual(result, "완료")
        self.assertEqual(fake.writes, [1111, 9991, 9992])
        self.assertEqual((logs[1], logs[-1]), (("자동", "2단계 이동1"), ("자동", "한 사이클 완료")))
        self.assertFalse(seq.auto_maybe_on)
        self.assertIn("로그 파일 쓰기 실패", err.getvalue())

    async def test_unexpected_error_aborts_cycle(self):
        fake = FakeTransport(raw=2, respond={1111: 2}.get)
        link, seq = await self.start(fake)
        await until(lambda: link.comm_ok)

        async def broken_wait(pred, timeout):
            raise RuntimeError("시험용 고장")

        link.wait = broken_wait  # 1111 접수 뒤 9991을 보내기 전에 로봇 정지를 기다리다가 예상 못 한 예외가 난다
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertIn("시작", await seq.press("start"))  # 시작 버튼: 아무도 기다리지 않는 작업
            await until(lambda: not seq.running)
        self.assertEqual(seq.last_result, "중단: 예외 RuntimeError('시험용 고장')")
        self.assertEqual(fake.writes, [1111])
        self.assertTrue(seq.auto_maybe_on)
        self.assertEqual(seq.buttons()["stop"], {"enabled": True, "label": "자동 운전 해제(9994)", "hint": ""})
        self.assertIn("시험용 고장", err.getvalue())

    async def test_comm_loss_does_not_advance(self):
        """Review Focus 3: 자동 순서 중 통신이 끊기면 다음 단계로 가지 않고, 한도가 지나면 중단한다."""
        fake = FakeTransport(raw=2, respond={1111: 2, 9991: 1}.get)
        link, seq = await self.start(fake, move_timeout=0.5)
        cycle = asyncio.create_task(seq.auto_cycle())
        await until(lambda: fake.writes == [1111, 9991] and fake.mailbox == 0)  # 9991 접수
        fake.fail_reads = 10 ** 6  # 통신이 끊긴다
        await until(lambda: not link.comm_ok)
        self.assertIn("정지 요청", await seq.press("stop"))  # 끊겨도 정지 요청은 받는다(보내지 않음)
        result = await asyncio.wait_for(cycle, 3.0)
        self.assertTrue(result.startswith("중단"), result)
        self.assertIn("시간 초과", result)
        self.assertEqual(fake.writes, [1111, 9991])  # 끊긴 동안 9992를 보내지 않았다
        self.assertTrue(seq.auto_maybe_on)


if __name__ == "__main__":
    unittest.main()

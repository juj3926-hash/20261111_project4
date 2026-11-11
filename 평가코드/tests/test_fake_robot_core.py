"""가짜 로봇 핵심의 I/O 계약 (docs/handoff/fake-robot-3d.md 3.1·3.2). 시간은 step의 dt로만 흐른다."""

import unittest

from fake_robot.core import HOME, PICK, Robot

DT = 0.01


def steps_until(robot, cond, di, limit=1000):
    """cond(robot)이 참이 될 때까지 step한 횟수."""
    for n in range(1, limit + 1):
        robot.step(di, DT)
        if cond(robot):
            return n
    raise AssertionError(f"{limit} step 안에 조건이 참이 되지 않음")


def hold(robot, seconds, di):
    for _ in range(round(seconds / DT)):
        robot.step(di, DT)


def do(name, value):
    return lambda r: r.do[name] == value


class RobotTest(unittest.TestCase):
    def test_request_starts_after_delay_and_moves_for_move_time(self):
        r = Robot()
        self.assertEqual(steps_until(r, do("DO0", 1), {"DI0": 1}), 20)   # start_delay 0.2초
        self.assertEqual(r.joints, HOME)
        self.assertEqual(steps_until(r, do("DO0", 0), {}), 200)          # move_time 2초 (PLC가 요청을 끔)
        self.assertEqual(r.joints, PICK)
        self.assertEqual(r.events, [("DO0", 1), ("DO0", 0)])

    def test_joints_interpolate_linearly_home_and_back(self):
        r = Robot()
        steps_until(r, do("DO0", 1), {"DI0": 1})
        hold(r, 1.0, {})
        for j, (a, b) in enumerate(zip(HOME, PICK)):
            self.assertAlmostEqual(r.joints[j], (a + b) / 2)
        steps_until(r, do("DO0", 0), {})
        steps_until(r, do("DO0", 1), {"DI1": 1})                         # 스크립트2: PICK → HOME
        steps_until(r, do("DO0", 0), {})
        self.assertEqual(r.joints, HOME)

    def test_di0_wins_when_both_requests_on(self):
        r = Robot()
        steps_until(r, do("DO0", 1), {"DI0": 1, "DI1": 1})
        steps_until(r, do("DO0", 0), {})
        self.assertEqual(r.joints, PICK)

    def test_request_left_on_keeps_do0_until_all_off(self):
        r = Robot()
        steps_until(r, do("DO0", 1), {"DI0": 1})
        hold(r, 5.0, {"DI0": 1})                                         # 요청이 안 꺼짐: 이동이 끝나도 DO0 유지
        self.assertEqual((r.joints, r.do["DO0"], r.state), (PICK, 1, "동작 중"))
        self.assertEqual(r.events, [("DO0", 1)])                         # 다시 시작하지도 않는다
        r.step({}, DT)                                                   # 모두 꺼짐 → DO0을 끄고 다음 요청을 받음
        self.assertEqual(r.events, [("DO0", 1), ("DO0", 0)])
        steps_until(r, do("DO0", 1), {"DI1": 1})

    def test_stop_during_move_then_resume(self):
        r = Robot()
        steps_until(r, do("DO0", 1), {"DI0": 1})
        hold(r, 0.5, {})
        self.assertEqual(steps_until(r, do("DO1", 1), {"DI3": 1}), 30)   # stop_time 0.3초 감속 뒤
        self.assertEqual(r.do["DO0"], 1)
        frozen = list(r.joints)
        hold(r, 1.0, {"DI3": 1, "DI2": 1})                               # DI2는 보지 않는다
        self.assertEqual(r.joints, frozen)
        self.assertEqual(r.state, "정지 중")
        r.step({}, DT)
        self.assertEqual(r.do, {"DO0": 1, "DO1": 0})
        self.assertEqual(r.state, "동작 중")
        # 남은 이동 = 2초 − 0.5초 − 감속 중 진행(0.15초) = 1.35초
        self.assertAlmostEqual(steps_until(r, do("DO0", 0), {}) * DT, 1.35 - DT, delta=2 * DT)
        self.assertEqual(r.joints, PICK)
        self.assertEqual(r.events, [("DO0", 1), ("DO1", 1), ("DO1", 0), ("DO0", 0)])

    def test_short_stop_pulse_still_stops_like_the_watchdog(self):
        r = Robot()
        steps_until(r, do("DO0", 1), {"DI0": 1})
        hold(r, 0.1, {"DI3": 1})                                         # stop_time보다 짧다
        steps_until(r, do("DO1", 1), {})
        r.step({}, DT)
        steps_until(r, do("DO0", 0), {})
        self.assertEqual(r.events, [("DO0", 1), ("DO1", 1), ("DO1", 0), ("DO0", 0)])

    def test_stop_near_the_end_keeps_do0_until_di3_off(self):
        r = Robot()
        steps_until(r, do("DO0", 1), {"DI0": 1})
        hold(r, 1.9, {})
        hold(r, 1.0, {"DI3": 1})                                         # 감속하는 동안 목표에 닿는다
        self.assertEqual(r.joints, PICK)
        self.assertEqual(r.do, {"DO0": 1, "DO1": 1})                     # DO0은 DI3가 꺼질 때까지 그대로
        r.step({}, DT)
        self.assertEqual(r.events, [("DO0", 1), ("DO1", 1), ("DO1", 0), ("DO0", 0)])

    def test_stop_during_start_delay_still_takes_stop_time(self):
        r = Robot()
        hold(r, 0.1, {"DI0": 1})                                         # 시작 지연 중 (DO0은 아직 0)
        self.assertEqual(steps_until(r, do("DO1", 1), {"DI0": 1, "DI3": 1}), 30)
        self.assertEqual(r.events, [("DO0", 1), ("DO1", 1)])             # 스크립트처럼 DO0이 먼저

    def test_stop_while_idle_sets_do1_at_once(self):
        r = Robot()
        # 움직이는 것이 없으면 감속 시간이 없다 (스크립트의 moveStop이 바로 돌아옴)
        self.assertEqual(steps_until(r, do("DO1", 1), {"DI3": 1}), 1)
        self.assertEqual(r.do["DO0"], 0)
        self.assertEqual(r.state, "정지 중")
        r.step({}, DT)
        self.assertEqual(r.state, "쉼")
        self.assertEqual(r.events, [("DO1", 1), ("DO1", 0)])

    def test_di2_ignored(self):
        r = Robot()
        hold(r, 1.0, {"DI2": 1})
        self.assertEqual(r.events, [])

    def test_edited_pick_used_from_next_move(self):
        r = Robot()
        steps_until(r, do("DO0", 1), {"DI0": 1})
        r.pick = [10.0] * 6                                              # 이동 중에 바꿈
        steps_until(r, do("DO0", 0), {})
        self.assertEqual(r.joints, PICK)
        steps_until(r, do("DO0", 1), {"DI1": 1})
        steps_until(r, do("DO0", 0), {})
        steps_until(r, do("DO0", 1), {"DI0": 1})
        steps_until(r, do("DO0", 0), {})
        self.assertEqual(r.joints, [10.0] * 6)

    def test_times_can_be_shortened(self):
        r = Robot(start_delay=0.05, move_time=0.1, stop_time=0.02)
        self.assertEqual(steps_until(r, do("DO0", 1), {"DI0": 1}), 5)
        self.assertEqual(steps_until(r, do("DO1", 1), {"DI3": 1}), 2)
        r.step({}, DT)
        steps_until(r, do("DO0", 0), {})
        self.assertEqual(r.joints, PICK)


if __name__ == "__main__":
    unittest.main()

"""수요일 평가 고정 규칙 검사. 교육 모형(4배속)에서 학생의 반복 운전을 실행하고 PC가 보낸 명령과 결과만 본다.

    python -B -X utf8 check_eval.py
    python -B -X utf8 check_eval.py --only 규칙4      # 이름에 '규칙4'가 든 검사만

내부 구조(작업을 몇 개 쓰는지, 정지를 무엇으로 전하는지)는 보지 않는다. 그것은 결정서의 몫이다.
시간 한도는 학생 코드의 기본값을 쓴다. 4배속이므로 이동 약 0.6초, 그리퍼 약 0.75초다.
"""

import argparse
import asyncio
import sys
import time
from types import SimpleNamespace

# 규칙 6: 학생 코드(picknplace_full/)의 time.sleep 호출을 기록한다.
# 학생 모듈을 불러오기 전에 바꿔야 `from time import sleep`으로 가져간 것도 잡힌다.
_real_sleep = time.sleep
SLEEP_CALLS = []


def _guarded_sleep(seconds):
    caller = sys._getframe(1).f_code.co_filename.replace("\\", "/")
    if "/picknplace_full/" in caller:
        SLEEP_CALLS.append((caller.rsplit("/", 1)[-1], sys._getframe(1).f_lineno, seconds))
    return _real_sleep(seconds)


time.sleep = _guarded_sleep

from eval_harness import Bench, dumps, fingerprint  # noqa: E402  (time.sleep을 바꾼 뒤에 불러온다)

SPEED = 4.0
CYCLE = ["1111 접수", "9991 접수", "9992 접수"]


def need(condition, message):
    if not condition:
        raise AssertionError(message)


def cmds(b: Bench) -> list[str]:
    return ["%d %s" % (c, r) for _, c, r in b.commands]


def sent_after(b: Bench, t: float) -> list[int]:
    return [c for at, c in b.sent if at >= t]


def result_is(b: Bench, prefix: str) -> bool:
    return str(getattr(b.seq, "last_result", "")).startswith(prefix)


def complete_hook(b: Bench, cycle: int, action):
    def hook(e):
        if e.tag == "상태" and b.phase == "done" and b.completed == cycle and hook in b.hooks:
            b.hooks.remove(hook)
            action()
    b.hooks.append(hook)


def phase_hook(b: Bench, cycle: int, phase: str, action):
    def hook(e):
        if b.cycle == cycle and b.phase == phase and hook in b.hooks:
            b.hooks.remove(hook)
            action()
    b.hooks.append(hook)


async def finished(b: Bench, cap=40.0):
    need(await b.finish(cap), "반복이 %.0f초 안에 끝나지 않았습니다(repeating이 계속 참)" % cap)
    await b.settle()


# ---- 검사 ----

async def normal(b: Bench):
    seen, ahead = {}, []
    b.hooks.append(lambda e: e.tag == "보냄" and seen.setdefault(len(b.sent), getattr(b.seq, "repeat_done", None)))

    def not_ahead(e):  # 완료 횟수는 통신이 실제로 관측한 완료 수를 앞지를 수 없다
        done = getattr(b.seq, "repeat_done", 0) or 0
        if done > b.completed:
            ahead.append("%.2f초 [%s] %s 때 repeat_done=%d, 관측한 완료 %d" % (b.now(), e.tag, e.text, done, b.completed))

    b.hooks.append(not_ahead)
    await b.seq.start_repeat(2)
    await finished(b)
    need(not ahead, "5단계 열림을 관측하기 전에 완료 횟수가 늘었습니다: %s" % (ahead[:1],))
    need(cmds(b) == CYCLE * 2, "정상 2회의 명령은 1111·9991·9992가 두 번입니다. 실제: %s" % cmds(b))
    need(result_is(b, "완료 2/2") and b.seq.repeat_done == 2, "결과는 '완료 2/2', repeat_done은 2입니다. 실제: %r, %r"
         % (b.seq.last_result, b.seq.repeat_done))
    need(seen.get(3) == 0 and seen.get(4) == 1,
         "완료 횟수는 그 사이클의 5단계 열림 관측 뒤에만 늘어야 합니다. 1회차 9992 때 %r, 2회차 1111 때 %r"
         % (seen.get(3), seen.get(4)))
    need(b.max_gap < 0.3, "이벤트 루프가 %.2f초 멈췄습니다. 동기 대기(time.sleep 등)를 쓰지 마세요" % b.max_gap)


async def bad_count(b: Bench):
    for n in (0, 6, 2.0, "2"):
        try:
            reply = await b.seq.start_repeat(n)
        except (TypeError, ValueError) as e:  # 정수가 아닌 값은 예외로 거절해도 보내지만 않으면 된다
            reply = "예외 %r" % e
        await asyncio.sleep(0.5)
        need(not b.sent and not b.seq.repeating, "횟수 %r은 거절하고 아무것도 보내지 않아야 합니다. 응답: %r" % (n, reply))


async def one_repeat(b: Bench):
    loop = asyncio.get_running_loop()
    complete_hook(b, 1, lambda: loop.create_task(b.seq.press("start")))
    await asyncio.gather(b.seq.start_repeat(2), b.seq.start_repeat(2))
    await finished(b)
    starts = [c for _, c in b.sent if c == 1111]
    need(len(starts) == 2 and result_is(b, "완료 2/2"),
         "반복은 하나만 돌아야 합니다(연속 시작 요청, 사이클 사이 [시작]). 1111 %d번, 결과 %r" % (len(starts), b.seq.last_result))


async def start_rejected(b: Bench):
    count = {"1111": 0}

    def rewrite(n, code):
        if code == 1111:
            count["1111"] += 1
            if count["1111"] == 2:
                return 1  # PLC가 모르는 코드로 바꿔 거부를 만든다
        return code

    b.transport.rewrite = rewrite
    await b.seq.start_repeat(2)
    await finished(b)
    need(cmds(b) == CYCLE + ["1111 거부"], "2회차 1111 거부 뒤 아무것도 보내지 않아야 합니다. 실제: %s" % cmds(b))
    need(result_is(b, "중단 1/2"), "결과는 '중단 1/2: 이유'입니다. 실제: %r" % b.seq.last_result)


async def start_reply_lost(b: Bench):
    b.transport.lose_reply = {3}  # 2회차 1111: PLC에는 닿고 응답만 잃는다
    await b.seq.start_repeat(2)
    await finished(b)
    need(cmds(b) == CYCLE + ["1111 미확인"], "1111 미확인 뒤 다시 보내거나 다음 명령을 보내면 안 됩니다. 실제: %s" % cmds(b))
    need(result_is(b, "중단 1/2"), "결과는 '중단 1/2: 이유'입니다. 실제: %r" % b.seq.last_result)


async def move1_rejected(b: Bench):
    b.transport.rewrite = lambda n, code: 1 if n == 1 and code == 9991 else code  # 1회차 9991을 PLC가 모르는 코드로 바꿔 거부를 만든다
    await b.seq.start_repeat(2)
    await finished(b)
    need(cmds(b) == ["1111 접수", "9991 거부"], "9991 거부 뒤 다시 보내거나 다음 명령을 보내면 안 됩니다. 실제: %s" % cmds(b))
    need(result_is(b, "중단 0/2"), "결과는 '중단 0/2: 이유'입니다. 실제: %r" % b.seq.last_result)


async def reply_lost(b: Bench):
    b.transport.lose_reply = {2}  # 1회차 9992: PLC에는 닿고 응답만 잃는다
    await b.seq.start_repeat(2)
    await finished(b)
    need(cmds(b) == ["1111 접수", "9991 접수", "9992 미확인"],
         "미확인 뒤 다시 보내거나 다음 명령을 보내면 안 됩니다. 실제: %s" % cmds(b))
    need(result_is(b, "중단 0/2"), "결과는 '중단 0/2: 이유'입니다. 실제: %r" % b.seq.last_result)


async def stop_after_start(b: Bench):
    loop = asyncio.get_running_loop()
    phase_hook(b, 2, "start", lambda: loop.create_task(b.seq.press("stop")))
    await b.seq.start_repeat(2)
    await finished(b)
    need(cmds(b) == CYCLE + ["1111 접수", "9994 접수"],
         "1111 접수 직후의 정지는 이동1 전에 9994로 멈춥니다. 실제: %s" % cmds(b))
    need(result_is(b, "정지 1/2"), "결과는 '정지 1/2'입니다. 실제: %r" % b.seq.last_result)


async def stop_between(b: Bench):
    loop = asyncio.get_running_loop()
    mark = {}

    def stop():
        mark["t"] = b.now()
        loop.create_task(b.seq.press("stop"))

    complete_hook(b, 1, stop)
    await b.seq.start_repeat(2)
    await finished(b)
    after = sent_after(b, mark.get("t", 1e9))
    need("t" in mark, "1회차 완료를 만나지 못했습니다")
    need(after in ([], [1111, 9994]), "사이클 사이 정지 뒤 이동 명령을 보내면 안 됩니다. 정지 뒤 명령: %s" % after)
    need(result_is(b, "정지 1/2"), "결과는 '정지 1/2'입니다. 실제: %r" % b.seq.last_result)


async def stop_after_move1(b: Bench):
    loop = asyncio.get_running_loop()
    phase_hook(b, 1, "close", lambda: loop.create_task(b.seq.press("stop")))
    await b.seq.start_repeat(2)
    await finished(b)
    need(cmds(b) == ["1111 접수", "9991 접수", "9994 접수"],
         "이동1 완료 뒤의 정지는 그리퍼 닫힘을 확인한 뒤 9994로 끝냅니다. 실제: %s" % cmds(b))
    need(result_is(b, "정지 0/2"), "결과는 '정지 0/2'입니다. 실제: %r" % b.seq.last_result)


async def stop_after_move2(b: Bench):
    loop = asyncio.get_running_loop()
    phase_hook(b, 1, "move2", lambda: loop.create_task(b.seq.press("stop")))
    await b.seq.start_repeat(2)
    await finished(b)
    need(cmds(b) == CYCLE, "이동2를 보낸 뒤의 정지는 그 사이클을 끝내고 멈춥니다. 실제: %s" % cmds(b))
    need(result_is(b, "정지 1/2"), "결과는 '정지 1/2'입니다. 실제: %r" % b.seq.last_result)


async def estop_resume(b: Bench):
    await b.seq.start_repeat(2)
    need(await b.until(lambda: b.cycle == 1 and b.plant.bit("RUN1") and b.plant.robot.state == "동작", 10),
         "1회차 이동1을 만나지 못했습니다")
    t = b.now()
    await b.seq.press("estop")
    estop = [at for at, c in b.sent if c == 9995]
    need(estop and estop[0] - t < 0.5, "반복 중에도 즉시 정지는 바로 나가야 합니다")
    await asyncio.sleep(1.0)
    await b.seq.press("resume")
    await finished(b)
    need("9995 접수" in cmds(b) and "9993 접수" in cmds(b), "즉시 정지와 재개가 접수되어야 합니다. 실제: %s" % cmds(b))
    need(result_is(b, "완료 2/2"), "재개 뒤 현재 사이클과 남은 횟수를 이어 갑니다. 실제: %r" % b.seq.last_result)
    need(b.max_gap < 0.3, "이벤트 루프가 %.2f초 멈췄습니다" % b.max_gap)


async def close_sends_nothing(b: Bench):
    mark = {}
    await b.seq.start_repeat(2)
    need(await b.until(lambda: b.cycle == 1 and b.phase == "move1", 10), "1회차 이동1을 만나지 못했습니다")
    mark["t"] = b.now()
    await b.seq.close()
    await asyncio.sleep(2.0)
    need(not sent_after(b, mark["t"]), "close() 뒤에는 아무것도 보내지 않습니다. 보낸 명령: %s" % sent_after(b, mark["t"]))
    need(not b.seq.repeating, "close() 뒤에는 반복이 끝나 있어야 합니다(repeating 거짓)")


async def no_sleep(b: Bench):
    await b.seq.start_repeat(1)
    await finished(b)
    need(not SLEEP_CALLS, "이벤트 루프를 막는 time.sleep을 picknplace_full에서 불렀습니다(이 실행까지의 모든 검사 포함): %s"
         % (SLEEP_CALLS[:3],))


CHECKS = [
    ("규칙1·2·6 정상 2회, 완료 횟수, 루프 막지 않음", normal),
    ("규칙2 횟수가 1~5 정수가 아니면 거절", bad_count),
    ("규칙2 반복은 하나만", one_repeat),
    ("규칙3 사이클 사이 1111 거부", start_rejected),
    ("규칙3 1111 응답 유실, 재전송 없음", start_reply_lost),
    ("규칙3 9991 거부, 재전송 없음", move1_rejected),
    ("규칙3 9992 응답 유실, 재전송 없음", reply_lost),
    ("규칙4 1111 접수 직후 정지", stop_after_start),
    ("규칙4 사이클 사이 정지", stop_between),
    ("규칙4 이동1 완료 뒤 정지", stop_after_move1),
    ("규칙4 이동2 뒤 정지", stop_after_move2),
    ("규칙5 즉시 정지와 재개", estop_resume),
    ("규칙6 close 뒤 명령 없음", close_sends_nothing),
    ("규칙6 time.sleep 없음", no_sleep),  # 맨 마지막: 앞선 검사에서 부른 것까지 본다
]


async def one(name, check) -> bool:
    b = Bench(speed=SPEED, echo=False)
    try:
        await b.start()
        await asyncio.wait_for(check(b), timeout=60)
    except Exception as e:
        reason = "시간 초과" if isinstance(e, TimeoutError) else "%s: %s" % (type(e).__name__, e)
        print("FAIL %s — %s" % (name, reason), flush=True)
        for line in b.lines[-12:]:
            print("      " + line)
        return False
    finally:
        await b.close()
    print("PASS %s" % name, flush=True)
    return True


async def run(args) -> int:
    print("코드 지문 %s · 교육 모형 %.0f배속" % (fingerprint(), SPEED), flush=True)
    chosen = [(n, c) for n, c in CHECKS if not args.only or args.only in n]
    passed = 0
    for name, check in chosen:
        passed += await one(name, check)
    total = len(chosen)
    if not args.only:
        import check_student  # 기존 A~D 검사 (규칙6)
        print("---- 기존 A~D 검사 ----", flush=True)
        ok = await check_student.run(SimpleNamespace(stage="all", part="all")) == 0
        print("%s 규칙6 기존 A~D 검사" % ("PASS" if ok else "FAIL"), flush=True)
        passed, total = passed + ok, total + 1
    print(dumps({"mode": "eval-check", "fingerprint": fingerprint(), "passed": passed, "total": total}), flush=True)
    return 0 if passed == total else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--only", default="", help="이름에 이 글자가 든 검사만")
    raise SystemExit(asyncio.run(run(parser.parse_args())))

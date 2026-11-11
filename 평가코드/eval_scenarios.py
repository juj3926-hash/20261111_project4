"""수요일 평가 장면 실행기. 교육 모형 전용이며 실물에 연결하지 않는다.

    python -B -X utf8 eval_scenarios.py normal --count 2
    python -B -X utf8 eval_scenarios.py s1 --order repeat-first
    python -B -X utf8 eval_scenarios.py s2 --cycle 1 --delay 0.1
    python -B -X utf8 eval_scenarios.py s3 --cycle 1 --move 1 --hold 20
    python -B -X utf8 eval_scenarios.py s4 --cycle 1 --drop 3

장면
- normal  반복 N회. 단계별 시간을 잴 때 쓴다.
- s1      반복 시작 요청과 개별 [스크립트1] 요청이 같은 순간 들어온다. --order로 어느 쪽이 먼저 도는지 정한다.
- s2      사이클 k의 완료(5단계 열림)를 PC가 관측한 뒤 --delay초에 [정지] 요청이 들어온다.
- s3      사이클 k의 이동(--move 1 또는 2)이 실제로 움직이는 중에 [즉시 정지], --hold초 뒤 [재개].
- s4      사이클 k의 완료를 PC가 관측한 직후 통신이 --drop초 끊긴다(읽기·쓰기 모두 실패).

모형은 1배속이다(이동 약 2.3초, 그리퍼 3초). 장면마다 logs/eval_<장면>_<시각>.log를 남긴다.
맨 위의 코드 지문은 picknplace_full/의 지문이다. 제출한 코드와 로그가 같은 판인지 이것으로 맞춰 본다.
"""

import argparse
import asyncio
import os
import time
from datetime import datetime

from eval_harness import ROOT, Bench, dumps, fingerprint


async def press(b: Bench, button: str) -> str:
    """버튼 하나를 누르고 응답 한 줄을 로그에 남긴다."""
    b.say("장면", "[%s] 누름" % button)
    reply = await b.seq.press(button)
    b.say("장면", "[%s] 응답: %s" % (button, reply))
    return reply


async def start_repeat(b: Bench, count: int) -> str:
    b.say("장면", "반복 시작 요청 %d회" % count)
    reply = await b.seq.start_repeat(count)
    b.say("장면", "반복 시작 응답: %s" % reply)
    return reply


def on_complete(b: Bench, cycle: int, action) -> None:
    """사이클 cycle의 완료를 PC가 관측하는 순간 action()을 한 번 부른다(기다리는 쪽이 깨어나기 전)."""
    def hook(e):
        if e.tag == "상태" and b.phase == "done" and b.completed == cycle and hook in b.hooks:
            b.hooks.remove(hook)
            action()
    b.hooks.append(hook)


async def normal(b: Bench, a) -> dict:
    reply = await start_repeat(b, a.count)
    return {"start_reply": reply}


async def s1(b: Bench, a) -> dict:
    calls = [("repeat", start_repeat(b, a.count)), ("script1", press(b, "script1"))]
    if a.order == "script-first":
        calls.reverse()
    b.say("장면", "같은 순간 요청: %s" % " → ".join(name for name, _ in calls))
    replies = await asyncio.gather(*(c for _, c in calls))
    return {"replies": dict(zip((name for name, _ in calls), replies))}


async def s2(b: Bench, a) -> dict:
    loop = asyncio.get_running_loop()
    marks = {}

    def later():
        marks["stop_at"] = round(b.now(), 2)
        loop.create_task(press(b, "stop"))

    on_complete(b, a.cycle, lambda: loop.call_later(a.delay, later))
    reply = await start_repeat(b, a.count)
    return {"start_reply": reply, **marks}


async def s3(b: Bench, a) -> dict:
    reply = await start_repeat(b, a.count)
    run_bit = "RUN1" if a.move == 1 else "RUN2"
    moving = await b.until(lambda: b.cycle == a.cycle and b.plant.bit(run_bit) and b.plant.robot.state == "동작",
                           a.cycle * 30 + 30)
    if not moving:
        return {"start_reply": reply, "note": "사이클 %d 이동%d을 만나지 못함" % (a.cycle, a.move)}
    estop = await press(b, "estop")
    b.say("장면", "%.1f초 기다린 뒤 재개" % a.hold)
    await asyncio.sleep(a.hold)
    resume = await press(b, "resume")
    return {"start_reply": reply, "estop_reply": estop, "resume_reply": resume}


async def s4(b: Bench, a) -> dict:
    def drop():
        b.transport.fail_until = time.monotonic() + a.drop
        b.say("장면", "통신 끊김 %.1f초 시작" % a.drop)

    on_complete(b, a.cycle, drop)
    reply = await start_repeat(b, a.count)
    return {"start_reply": reply}


CASES = {"normal": normal, "s1": s1, "s2": s2, "s3": s3, "s4": s4}


async def run(a) -> dict:
    b = Bench(speed=1.0)
    params = {k: v for k, v in vars(a).items() if k != "case"}
    b.say("장면", "교육 모형 전용 / 실물 연결 없음 / 코드 지문 %s / 장면 %s %s" % (fingerprint(), a.case, dumps(params)))
    extra = {}
    try:
        await b.start()
        try:
            extra = await CASES[a.case](b, a)
        except NotImplementedError as e:
            extra = {"note": "구현 안 됨: %s" % e}
        except Exception as e:  # 학생 코드의 예외도 요약에 남기고 장면은 끝까지 정리한다
            extra = {"note": "예외 %s: %s" % (type(e).__name__, e)}
            b.say("장면", extra["note"])
        hold = getattr(a, "hold", 0) + getattr(a, "drop", 0)
        if not await b.finish(a.count * 40 + hold + 30):
            extra["note"] = "반복이 끝나지 않음"
        await b.settle()
    finally:
        await b.close()
    summary = b.summary(case=a.case, params=params, **extra)
    print("---- 요약 ----")
    print("코드 지문   %s" % summary["fingerprint"])
    print("PC 명령     %s" % (" → ".join(summary["commands"]) or "없음"))
    print("반복 결과   %s (완료 %s / %s)" % (summary["result"] or "없음", summary["done"], summary["total"]))
    for line in b.clock.table():  # 단계별 시간(초). 실물 확인(eval_real.py)의 표와 같은 기준이다
        print("단계 시간   " + line)
    print("SUMMARY " + dumps(summary), flush=True)
    logs = ROOT / "logs"
    logs.mkdir(exist_ok=True)
    path = logs / ("eval_%s_%s_%d.log" % (a.case, datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3], os.getpid()))
    path.write_text("\n".join(b.lines + ["SUMMARY " + dumps(summary)]) + "\n", encoding="utf-8")
    print("로그 파일   %s" % path.relative_to(ROOT), flush=True)
    return summary


def arguments(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="case", required=True)
    for name in CASES:
        q = sub.add_parser(name)
        q.add_argument("--count", type=int, default=3 if name == "s2" else 2, help="반복 횟수 N")
        if name in ("s2", "s3", "s4"):
            q.add_argument("--cycle", type=int, default=1, help="몇 번째 사이클에서 일으킬지")
    sub.choices["s1"].add_argument("--order", choices=("repeat-first", "script-first"), default="repeat-first")
    sub.choices["s2"].add_argument("--delay", type=float, default=0.1, help="완료 관측 뒤 정지 요청까지(초)")
    sub.choices["s3"].add_argument("--move", type=int, choices=(1, 2), default=1)
    sub.choices["s3"].add_argument("--hold", type=float, default=20.0, help="즉시 정지 뒤 재개까지(초)")
    sub.choices["s4"].add_argument("--drop", type=float, default=3.0, help="통신 끊김 길이(초)")
    a = p.parse_args(argv)
    if a.count < 1:
        p.error("--count는 1 이상")
    if hasattr(a, "cycle") and not 1 <= a.cycle <= a.count:
        p.error("--cycle은 1~count")
    return a


if __name__ == "__main__":
    asyncio.run(run(arguments()))

"""실물 확인 실행기 (7교시, 강사 입회). 자기 picknplace_full의 반복 운전을 장치에서 한 번 돌린다.

    python -B -X utf8 eval_real.py --target auto --count 2                # 실물 (COM 포트 자동)
    python -B -X utf8 eval_real.py --target serial:COM3 --count 2         # 실물 (포트 지정)
    python -B -X utf8 eval_real.py --target tcp:127.0.0.1:5020 --count 2  # 리허설: fake_plc + fake_robot
    python -B -X utf8 eval_real.py --target model --count 2 --yes         # 리허설: 프로세스 안 모형

- 시작 전에 Enter를 눌러야 연결한다(--yes로 생략). 연결 뒤 시작 조건([시작] 버튼과 같은 조건)이 아니면 아무것도 보내지 않는다.
- 실행 중 로봇을 멈추려면 장비의 비상정지를 누른다. Ctrl+C는 PC 순서만 멈추고 PLC에는 아무것도 보내지 않는다.
- logs/eval_real_<시각>.log를 남긴다. 맨 위에 코드 지문과 대상이 찍히고, 끝에 단계별 시간 표가 나온다.
  표는 eval_scenarios.py normal의 표와 같은 기준이다(모형과 실물 비교).
"""

import argparse
import asyncio
import os
import time
from datetime import datetime

from eval_harness import LINK_ARGS, RESULT, ROOT, StepClock, dumps, fingerprint
from picknplace_full.link import PlcLink
from picknplace_full.sequence import Sequencer


async def until(pred, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while not pred():
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(0.05)
    return True


async def run(a, lines: list, out: dict) -> None:
    t0 = time.monotonic()

    def say(tag, text):
        line = "%7.2f [%s] %s" % (time.monotonic() - t0, tag, text)
        lines.append(line)
        print(line, flush=True)

    link = PlcLink(a.target, **LINK_ARGS)
    clock = StepClock(lambda: time.monotonic() - t0)
    commands = []

    def on_event(e):
        say(e.tag, e.text)
        clock.feed(e, link.status)
        head = e.text.split()[0] if e.text else ""
        if e.tag in ("접수", "거부", "미확인") and head.isdigit():
            commands.append("%s %s" % (head, e.tag))

    link.on_event(on_event)
    seq = Sequencer(link, log=say)
    out.update(commands=commands, clock=clock, seq=seq)
    say("실물", "코드 지문 %s · 대상 %s · 반복 %d회 · %s" % (fingerprint(), a.target, a.count,
                                                     datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    await link.start()
    try:
        if not await until(lambda: link.comm_ok and link.status is not None and link.status.valid, 20):
            out["note"] = "20초 안에 정상 상태를 읽지 못함(연결·포트·PLC RUN 확인)"
            return
        # PLC RUN 직후 첫 스캔의 그리퍼 열기 3초 동안은 상태가 '정지·열림'이어도 1111이 거부된다(상태 코드에 안 보임).
        # 연결 뒤 3.5초 지켜본 다음 시작한다. 실물 PLC가 계속 RUN이었다면 영향이 없다.
        say("실물", "연결됨. 3.5초 동안 상태를 지켜본 뒤 시작 조건을 본다")
        await asyncio.sleep(3.5)
        await link.wait(lambda s: True, 3)  # 새 관측 하나
        why = seq.buttons()["start"]["hint"]
        if why:
            out["note"] = "시작 조건 아님: %s (장비를 로봇 정지·그리퍼 열림·자동 운전 해제 상태로 둔 뒤 다시)" % why
            return
        say("실물", "시작 조건 확인: %s" % link.status.text())
        reply = await seq.start_repeat(a.count)
        say("실물", "반복 시작 응답: %s" % reply)

        def ended():
            return not getattr(seq, "repeating", False) and bool(RESULT.match(str(getattr(seq, "last_result", ""))))

        started = await until(lambda: getattr(seq, "repeating", False) or ended() or commands, 2.0)
        if started and not await until(ended, a.count * 180):
            out["note"] = "반복이 %d초 안에 끝나지 않음" % (a.count * 180)
    finally:
        try:
            await asyncio.wait_for(seq.close(), 5)
        except Exception as e:  # TimeoutError 포함. 통신은 닫는다
            say("실물", "Sequencer.close() 문제: %r" % e)
        await link.close()


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--target", required=True, help="auto, serial:COMn, tcp:HOST:PORT, model")
    p.add_argument("--count", type=int, default=2, help="반복 횟수 (기본 2)")
    p.add_argument("--yes", action="store_true", help="시작 전 확인을 묻지 않는다(리허설용)")
    a = p.parse_args()
    if not a.yes:
        input("로봇 주변이 비었고 비상정지에 손이 닿는지 확인했으면 Enter (취소 Ctrl+C) ")
    lines, out = [], {}
    try:
        asyncio.run(run(a, lines, out))
    except KeyboardInterrupt:
        out["note"] = "Ctrl+C로 멈춤(PC 순서만 멈췄다. 장비 상태를 확인할 것)"
    except Exception as e:  # 학생 코드의 예외(미구현 포함)도 요약과 로그를 남긴다
        out["note"] = "예외 %s: %s" % (type(e).__name__, e)
    seq, clock = out.get("seq"), out.get("clock")
    summary = {"fingerprint": fingerprint(), "target": a.target, "count": a.count,
               "commands": out.get("commands", []),
               "result": getattr(seq, "last_result", None), "done": getattr(seq, "repeat_done", None),
               "steps": clock.rows if clock else [], "note": out.get("note", "")}
    report = ["---- 실물 확인 요약 ----",
              "코드 지문   %s" % summary["fingerprint"],
              "대상        %s" % a.target,
              "PC 명령     %s" % (" → ".join(summary["commands"]) or "없음"),
              "반복 결과   %s (완료 %s / %s)" % (summary["result"] or "없음", summary["done"], a.count)]
    report += ["단계 시간   " + line for line in (clock.table() if clock else [])]
    if summary["note"]:
        report.append("메모        %s" % summary["note"])
    print("\n".join(report))
    print("SUMMARY " + dumps(summary), flush=True)
    logs = ROOT / "logs"
    logs.mkdir(exist_ok=True)
    path = logs / ("eval_real_%s_%d.log" % (datetime.now().strftime("%Y%m%d_%H%M%S"), os.getpid()))
    path.write_text("\n".join(lines + report + ["SUMMARY " + dumps(summary)]) + "\n", encoding="utf-8")
    print("로그 파일   %s" % path.relative_to(ROOT), flush=True)


if __name__ == "__main__":
    main()

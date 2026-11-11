# 수요일 평가 코드

화요일 A~D 완성본에 반복 운전의 자리(`Sequencer.start_repeat`와 상태 속성)를 만든 사본이다. 과제와 채점은 평가 문항 문서를 따른다.

- 수정은 `picknplace_full/sequence.py`에서 하고, 화면을 연결한다면 `app.py`와 `web/`도 고친다. 통신·모형 부품(`link.py`, `model.py`, `status.py`, `transport.py`)과 아래 평가 도구는 고치지 않는다. 평가자는 이것들을 배포 원본으로 바꿔 채점한다.
- 장비에는 연결하지 않는다. 모든 도구는 교육 모형(고정 커밋의 래더 모형과 고장 재현)에서 돈다.

| 명령 | 하는 일 |
|---|---|
| `python -B -X utf8 check_eval.py` | 고정 규칙 검사(모형 4배속) + 기존 A~D 검사. `--only 규칙4`처럼 일부만 돌릴 수 있다 |
| `python -B -X utf8 eval_scenarios.py normal --count 2` | 반복 N회. 단계별 시간을 잴 때 |
| `python -B -X utf8 eval_scenarios.py s1 --order repeat-first` | 반복 시작과 [스크립트1]이 같은 순간 (`script-first`도 있음) |
| `python -B -X utf8 eval_scenarios.py s2 --cycle 1 --delay 0.1` | 사이클 완료 관측 뒤 지정한 초에 [정지] |
| `python -B -X utf8 eval_scenarios.py s3 --cycle 1 --move 1 --hold 20` | 이동 중 [즉시 정지], 지정한 초 뒤 [재개] |
| `python -B -X utf8 eval_scenarios.py s4 --cycle 1 --drop 3` | 사이클 완료 관측 직후 통신이 지정한 초 동안 끊김 |
| `python -B -X utf8 eval_harness.py` | 지금 코드의 지문 |
| `python -B -X utf8 eval_real.py --target tcp:127.0.0.1:5020 --count 2` | 실물 확인 리허설. 가짜 PLC(`-m fake_plc`)와 가짜 로봇(`-m fake_robot`)을 먼저 띄운다. `--target model --yes`는 가짜 장치 없이 프로세스 안 모형으로 돈다 |
| `python -B -X utf8 eval_real.py --target auto --count 2` | 7교시 실물 확인(강사 입회). 시작 전에 Enter로 확인하고, 연결 뒤 3.5초 지켜본 다음 시작 조건을 본다. 로봇을 멈추려면 장비의 비상정지를 누른다(Ctrl+C는 PC 순서만 멈춘다) |
| `python -B -X utf8 -m picknplace_full --target model` | 화면에서 모형으로 실행(화면 연결을 했다면) |

장면 실행기는 끝에 `---- 요약 ----`과 `SUMMARY` 줄을 찍고 `logs/eval_<장면>_<시각>.log`를 남긴다. 로그 맨 위 줄의 코드 지문으로 어느 판의 코드에서 나온 로그인지 알 수 있다.

모든 장면에 `--count N`(반복 횟수)을 줄 수 있다. 출발 코드 그대로 `check_eval.py`를 돌리면 규칙 검사 14개가 `NotImplementedError`로 실패하고 기존 A~D 검사만 통과한다. 이것이 정상이다. 검사기는 대표 장면만 본다. 통과가 고정 규칙을 모두 지켰다는 보증은 아니다.

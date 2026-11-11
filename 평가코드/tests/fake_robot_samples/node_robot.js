// 시험용 다리: fake_robot/static/rodi.js를 Node에서 한 스텝씩 돌린다 (tests/test_fake_robot_rodi.py).
// 표준 입력 한 줄 = JSON 한 개, 답도 한 줄.
//   첫 줄  {"script": 소스, "joints": [관절 6개]}  → 실행 준비 (실행 전 검사)
//   다음 줄 {"di": [DI0, DI1, DI2, DI3], "dt": ms}  → step
//   답     {"do": [DO0, DO1], "events": [...], "log": [...], "joints": [...], "done": 참/거짓, "state": 화면 상태}
var fs = require('fs');
var path = require('path');
var vm = require('vm');
var readline = require('readline');

var root = path.join(__dirname, '..', '..', 'fake_robot');
['vendor/acorn.js', 'vendor/interpreter.js', 'static/rodi.js'].forEach(function (name) {
  var file = path.join(root, name);
  vm.runInThisContext(fs.readFileSync(file, 'utf8'), {filename: file});  // Worker의 importScripts와 같다
});

var run = null;
readline.createInterface({input: process.stdin}).on('line', function (line) {
  var msg = JSON.parse(line);
  if (run === null) {
    run = new RodiRunner(msg.script, msg.joints);
  } else {
    run.step(msg.di, msg.dt);
  }
  process.stdout.write(JSON.stringify({
    do: run.do.slice(0, 2), events: run.events.splice(0), log: run.log.splice(0),
    joints: run.joints, done: run.done, state: run.state()
  }) + '\n');
});

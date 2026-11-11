// 시험용 틀: fake_robot/static/worker.js를 브라우저 없이 Node worker_threads로 그대로 돌린다.
// 브라우저 Worker가 주는 것 중 importScripts, onmessage/postMessage, 상대 주소 fetch만 채운다.
//   node worker_harness.js <서버 주소, 예: http://127.0.0.1:8879/>
// 표준 입력의 JSON 한 줄 = 페이지가 Worker로 보내는 메시지. Worker가 페이지로 보내는 메시지는 표준 출력에 한 줄씩.
var fs = require('fs');
var path = require('path');
var vm = require('vm');
var readline = require('readline');
var wt = require('worker_threads');

var STATIC = path.join(__dirname, '..', '..', 'fake_robot', 'static');

function load(file) {
  vm.runInThisContext(fs.readFileSync(file, 'utf8'), {filename: file});
}

if (wt.isMainThread) {
  var worker = new wt.Worker(__filename, {workerData: process.argv[2]});
  worker.on('message', function (m) { process.stdout.write(JSON.stringify(m) + '\n'); });
  worker.on('error', function (e) {
    process.stdout.write(JSON.stringify({type: 'error', text: String(e && e.stack || e)}) + '\n');
  });
  readline.createInterface({input: process.stdin})
    .on('line', function (line) { worker.postMessage(JSON.parse(line)); })
    .on('close', function () { worker.terminate(); });
} else {
  var base = wt.workerData;
  // 브라우저처럼 worker.js 기준 상대 주소다. 서버와 같은 길: vendor/… → fake_robot/vendor, 나머지 → fake_robot/static
  globalThis.importScripts = function () {
    Array.prototype.forEach.call(arguments, function (name) {
      load(path.join(STATIC, name.indexOf('vendor/') === 0 ? '..' : '.', name));
    });
  };
  var nodeFetch = globalThis.fetch;
  globalThis.fetch = function (url, options) { return nodeFetch(new URL(url, base), options); };
  globalThis.postMessage = function (m) { wt.parentPort.postMessage(m); };
  globalThis.onmessage = null;
  wt.parentPort.on('message', function (m) { globalThis.onmessage({data: m}); });
  load(path.join(STATIC, 'worker.js'));
}

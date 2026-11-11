// 시험용 틀: fake_robot/static/app.js(3D 화면)를 브라우저 없이 Node로 한 번 돌린다.
//   node page_harness.mjs 1|0   → 결과 JSON 한 줄. 0이면 WebGL이 없는 PC처럼 three의 렌더러가 예외를 낸다
// three와 OrbitControls는 가짜다. DOM, fetch, Worker, 타이머는 app.js가 쓰는 만큼만 흉내 낸다.
// 순서: 서버 읽기 셋(로그 1~3 → 서버를 다시 켬 → 다시 켠 뒤 로그) → 파일 열기 → 고친 같은 파일 다시 열기 → [다시 실행]
import { readFileSync } from 'node:fs';

const asModule = (code) => 'data:text/javascript,' + encodeURIComponent(code);
const ANY = 'const any = () => new Proxy(function () {}, { get: (t, k) => (k === Symbol.toPrimitive ? () => 0 : any()),'
  + ' set: () => true, apply: () => any(), construct: () => any() });\n';   // 무엇을 부르든 받아 주는 가짜
const THREE = ANY + ['Scene', 'Color', 'PerspectiveCamera', 'HemisphereLight', 'DirectionalLight', 'GridHelper',
  'MeshStandardMaterial', 'Mesh', 'CylinderGeometry', 'Group', 'BoxGeometry', 'MathUtils']
  .map((name) => `export const ${name} = any();\n`).join('')
  + (process.argv[2] === '1' ? 'export const WebGLRenderer = any();'
    : "export class WebGLRenderer { constructor() { throw new Error('Error creating WebGL context.'); } }");
const ORBIT = ANY + 'export const OrbitControls = any();';

const elements = {};
const element = () => ({
  textContent: '', className: '', value: '', title: '', disabled: false, checked: false, files: [], style: {},
  clientWidth: 800, clientHeight: 600, children: [], classList: { toggle() {}, add() {}, remove() {} },
  prepend(x) { this.children.unshift(x); }, append(...xs) { this.children.push(...xs); },
  setAttribute() {}, addEventListener() {},
  get childElementCount() { return this.children.length; },
});
const $ = (id) => (elements[id] ??= element());
globalThis.document = { getElementById: $, createElement: element, querySelector: element, querySelectorAll: () => [] };
globalThis.window = { addEventListener() {} };
let frames = 0;
globalThis.requestAnimationFrame = (fn) => { if (frames++ < 2) fn(performance.now()); };

// 서버: 로그 줄은 번호(n)가 since보다 큰 것만 준다. last는 서버의 마지막 번호다
const line = (n, text) => ({ n, t: '12:00:00.000', kind: '상태', text });
const ROBOT = { connected: true, plc: '127.0.0.1:5021', state: '쉼', di: [0, 0, 0, 0], do: [0, 0], mode: 'builtin',
  joints: [0, 0, 0, 0, 0, 0], home: [0, 0, 0, 0, 0, 0], pick: [0, 0, 0, 0, 0, 0] };
let serverLog = [line(1, '하나'), line(2, '둘'), line(3, '셋')];
const asked = [];
globalThis.fetch = async (url) => {
  let body = { message: '받음' };
  if (url.startsWith('/api/state')) {
    asked.push(url);
    const since = Number(new URL(url, 'http://127.0.0.1').searchParams.get('log'));
    body = { ...ROBOT, log: serverLog.filter((x) => x.n > since), last: serverLog.at(-1).n };
  }
  return { ok: true, status: 200, json: async () => body };
};
let poll = null;
globalThis.setTimeout = (fn) => { poll = fn; };   // 다음 읽기는 이 틀이 부른다
const settle = () => new Promise((resolve) => setImmediate(resolve));
const started = [];
globalThis.Worker = class { postMessage(m) { if (m.type === 'start') started.push(m.source); } terminate() {} };

const app = readFileSync(new URL('../../fake_robot/static/app.js', import.meta.url), 'utf8')
  .replace("from 'three'", `from "${asModule(THREE)}"`)
  .replace("from '/vendor/OrbitControls.js'", `from "${asModule(ORBIT)}"`);
const out = {};
try {
  await import(asModule(app));
  await settle();                                  // 첫 읽기: 로그 1~3
  serverLog = [line(1, '다시 켠 뒤')];             // 서버를 다시 켰다. 번호가 1부터 다시 간다
  poll(); await settle();
  poll(); await settle();
  const file = $('file');
  for (const text of ['A', 'B']) {                 // 같은 파일을 고쳐서 다시 고른다
    file.files = [{ name: 'main.js', text: async () => text }];
    file.value = 'C:\\fakepath\\main.js';
    await file.onchange();
    out.value = file.value;
  }
  await $('rerun').onclick();
} catch (e) {
  out.error = String(e);                           // 스택에는 data: 주소에 든 app.js 전체가 찍힌다
}
Object.assign(out, { note: $('note').textContent, joints: $('joints').textContent, asked, started,
  log: $('log').children.map((x) => x.textContent) });
console.log(JSON.stringify(out));

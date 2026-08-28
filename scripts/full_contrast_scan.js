const { spawn } = require('child_process');
const http = require('http');
const WS = globalThis.WebSocket;

function startChrome(port, udd) {
  const chrome = spawn('"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe"', [
    '--headless=new',
    '--disable-gpu',
    '--remote-debugging-port=' + port,
    '--remote-allow-origins=*',
    '--user-data-dir=' + udd,
    'about:blank'
  ], { shell: true });
  return chrome;
}

function waitDebug(port, retries = 20) {
  for (let i = 0; i < retries; i++) {
    try {
      const data = httpRequestSync('GET', 'http://127.0.0.1:' + port + '/json/version');
    } catch (e) {}
  }
  return httpRequest('GET', 'http://127.0.0.1:' + port + '/json/version').then(() => true).catch(() => false);
}

function httpRequestSync(method, url) {
  let done = false; let ok = false;
  const req = http.request(url, { method }, () => { done = true; });
  req.on('error', () => { done = true; });
  req.end();
  // sync-ish wait (not ideal but simple)
  const start = Date.now();
  while (!done && Date.now() - start < 500) {}
  return true;
}

function httpRequest(method, urlPath) {
  return new Promise((res, rej) => {
    const options = { method };
    const req = http.request(urlPath, options, x => {
      let d = '';
      x.on('data', c => d += c);
      x.on('end', () => { try { res(JSON.parse(d)); } catch (e) { res({}); } });
    }).on('error', rej);
    req.end();
  });
}

function lum(r) { return r <= 0.03928 ? r / 12.92 : Math.pow((r + 0.055) / 1.055, 2.4); }
function rel(rgb) {
  const [r, g, b] = rgb.map(v => { v /= 255; return lum(v); });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}
function parseRGB(s) {
  const m = (s || '').match(/(\d+),\s*(\d+),\s*(\d+)/);
  return m ? [+m[1], +m[2], +m[3]] : null;
}
function contrast(fg, bg) {
  const a = Math.max(rel(fg), rel(bg)), b = Math.min(rel(fg), rel(bg));
  return (a + 0.05) / (b + 0.05);
}

async function runThemeCheck(theme) {
  const tab = await httpRequest('PUT', 'http://127.0.0.1:9227/json/new?http://localhost:8099/?v=' + Date.now());
  const ws = new WS(tab.webSocketDebuggerUrl);
  let id = 0; const pend = {};
  const send = (method, params = {}) => new Promise(r => {
    const i = ++id; pend[i] = r; ws.send(JSON.stringify({ id: i, method, params }));
  });
  ws.addEventListener('message', m => {
    const o = JSON.parse(m.data);
    if (o.id && pend[o.id]) { pend[o.id](o); delete pend[o.id]; }
  });
  await new Promise(r => ws.addEventListener('open', r));
  await send('Page.enable');
  await send('Runtime.enable');
  await new Promise(r => setTimeout(r, 6000));
  if (theme === 'dark') await send('Runtime.evaluate', { expression: "document.documentElement.classList.add('dark')" });
  else await send('Runtime.evaluate', { expression: "document.documentElement.classList.remove('dark')" });
  await new Promise(r => setTimeout(r, 800));

  const expr = `(() => {
    function effBg(el){
      let e = el;
      while(e){
        const b = getComputedStyle(e).backgroundColor;
        if(b && b !== 'rgba(0, 0, 0, 0)' && b !== 'transparent') return b;
        e = e.parentElement;
      }
      return 'rgb(255,255,255)';
    }
    const res = [];
    const seen = new Set();
    document.querySelectorAll('body *').forEach(el => {
      const cs = getComputedStyle(el);
      if(cs.display === 'none' || cs.visibility === 'hidden') return;
      const txt = (el.innerText || '').trim();
      if(!txt || txt.length > 40 || el.children.length > 0) return;
      const key = el.getBoundingClientRect().top.toFixed(0) + '_' + txt.slice(0,10);
      if(seen.has(key)) return;
      seen.add(key);
      res.push({t: txt.slice(0,24), c: cs.color, bg: effBg(el)});
    });
    return res;
  })()`;
  const r = await send('Runtime.evaluate', { expression: expr, returnByValue: true });
  ws.close();
  return r.result.result.value || [];
}

function analyze(items, label) {
  const bad = [];
  for (const it of items) {
    const fg = parseRGB(it.c), bg = parseRGB(it.bg);
    if (!fg || !bg) continue;
    const cr = contrast(fg, bg);
    if (cr < 4.5) {
      bad.push({ text: it.t, fg: it.c, bg: it.bg, ratio: +cr.toFixed(2) });
    }
  }
  console.log('=== ' + label + ' ===');
  console.log('TOTAL TEXT NODES:', items.length);
  console.log('LOW-CONTRAST (fail <4.5):', bad.length);
  if (bad.length > 0) console.log(JSON.stringify(bad.slice(0, 30), null, 1));
  return bad;
}

async function main() {
  const chrome = startChrome(9227, process.env.LOCALAPPDATA + '/hermes/cd7');
  await new Promise(r => setTimeout(r, 3000));
  let ok = false;
  for (let i = 0; i < 15; i++) {
    ok = await waitDebug(9227);
    if (ok) break;
    await new Promise(r => setTimeout(r, 1000));
  }
  if (!ok) { console.log('Chrome failed to start'); process.exit(1); }
  const light = await runThemeCheck('light');
  const dark = await runThemeCheck('dark');
  analyze(light, 'LIGHT MODE');
  analyze(dark, 'DARK MODE');
  chrome.kill();
  process.exit(0);
}

main().catch(e => { console.error(e); process.exit(1); });

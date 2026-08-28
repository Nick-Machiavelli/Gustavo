const PORT = process.argv[2] || 9222;
const BASE = 'http://127.0.0.1:'+PORT;
const http = require('http');

// Node 21+ has global WebSocket built in.
const WS = globalThis.WebSocket;

function httpReq(method, path){
  return new Promise((res,rej)=>{
    const req = http.request(BASE+path, {method}, r=>{
      let d=''; r.on('data',c=>d+=c); r.on('end',()=>{ try{res(JSON.parse(d));}catch(e){res({});} });
    }).on('error',rej);
    req.end();
  });
}

async function main(){
  // open a fresh tab on the dashboard
  const tab = await httpReq('PUT', '/json/new?https://nick-machiavelli.github.io/Gustavo/');
  const wsUrl = tab.webSocketDebuggerUrl;
  const ws = new WS(wsUrl);
  let id=0; const pending={};
  function send(method,params={}){
    return new Promise((res)=>{
      const mid=++id; pending[mid]=res;
      ws.send(JSON.stringify({id:mid,method,params}));
    });
  }
  ws.addEventListener('message',m=>{
    const o=JSON.parse(m.data);
    if(o.id && pending[o.id]){ pending[o.id](o); delete pending[o.id]; }
  });
  await new Promise(r=>ws.addEventListener('open',r));
  await send('Page.enable');
  await send('Runtime.enable');
  // wait for load
  await new Promise(r=>setTimeout(r,9000));
  // force LIGHT mode
  await send('Runtime.evaluate',{expression:"document.documentElement.classList.remove('dark'); 'ok'"});
  await new Promise(r=>setTimeout(r,800));
  // read computed styles of key elements
  const expr = `(() => {
    const out = [];
    const pick = (sel, label) => {
      const el = document.querySelector(sel);
      if(!el) return out.push({label, found:false});
      const cs = getComputedStyle(el);
      out.push({label, found:true, color: cs.color, bg: cs.backgroundColor, text: (el.innerText||'').trim().slice(0,30)});
    };
    pick('.text-amber-300', 'amber-300 (BTC price header)');
    pick('#price-btc', 'price-btc');
    pick('#github-sync-status', 'github sync status (emerald-300)');
    pick('.text-gray-400', 'gray-400');
    pick('.price-currency', 'price-currency');
    pick('#ticker-content', 'ticker-content');
    pick('a[onclick*="openDailySummaryModal"]', 'daily bulletin link (amber-300)');
    return out;
  })()`;
  const r = await send('Runtime.evaluate',{expression:expr, returnByValue:true});
  console.log("RAW:", JSON.stringify(r).slice(0,500));
  const val = r && r.result && r.result.result ? r.result.result.value : null;
  console.log("VAL:", JSON.stringify(val, null, 2));
  ws.close();
  process.exit(0);
}
main().catch(e=>{console.error(e); process.exit(1);});

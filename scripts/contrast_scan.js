const http = require('http');
const WS = globalThis.WebSocket;

function httpReq(method, path){
  return new Promise((res,rej)=>{
    const req = http.request('http://127.0.0.1:9222'+path, {method}, r=>{
      let d=''; r.on('data',c=>d+=c); r.on('end',()=>{ try{res(JSON.parse(d));}catch(e){res({});} });
    }).on('error',rej);
    req.end();
  });
}

async function main(){
  const tab = await httpReq('PUT', '/json/new?https://nick-machiavelli.github.io/Gustavo/');
  const ws = new WS(tab.webSocketDebuggerUrl);
  let id=0; const pending={};
  function send(method,params={}){
    return new Promise((res)=>{ const mid=++id; pending[mid]=res; ws.send(JSON.stringify({id:mid,method,params})); });
  }
  ws.addEventListener('message',m=>{ const o=JSON.parse(m.data); if(o.id&&pending[o.id]){pending[o.id](o);delete pending[o.id];} });
  await new Promise(r=>ws.addEventListener('open',r));
  await send('Page.enable'); await send('Runtime.enable');
  await new Promise(r=>setTimeout(r,6000));
  await send('Runtime.evaluate',{expression:"document.documentElement.classList.remove('dark'); 'ok'"});
  await new Promise(r=>setTimeout(r,800));

  const expr = `(() => {
    function lum(rgb){
      const m = rgb.match(/\\\\d+/g).map(Number);
      const a = m.slice(0,3).map(v=>{ v/=255; return v<=0.03928? v/12.92 : Math.pow((v+0.055)/1.055,2.4); });
      return 0.2126*a[0]+0.7152*a[1]+0.0722*a[2];
    }
    function contrast(c1,c2){ const L1=lum(c1),L2=lum(c2); const hi=Math.max(L1,L2),lo=Math.min(L1,L2); return (hi+0.05)/(lo+0.05); }
    function effBg(el){
      let e=el;
      while(e){
        const cs=getComputedStyle(e);
        const bg=cs.backgroundColor;
        if(bg && bg!=='rgba(0, 0, 0, 0)' && bg!=='transparent') return bg;
        e=e.parentElement;
      }
      return 'rgb(255,255,255)';
    }
    const bad=[];
    const seen=new Set();
    document.querySelectorAll('*').forEach(el=>{
      const txt=(el.innerText||'').trim();
      if(!txt || txt.length<2) return;
      // only leaf-ish text nodes
      if(el.children.length>0 && [...el.childNodes].some(n=>n.nodeType===3 && n.textContent.trim().length>1)) {}
      const cs=getComputedStyle(el);
      if(cs.visibility==='hidden'||cs.display==='none') return;
      const color=cs.color, bg=effBg(el);
      const ratio=contrast(color,bg);
      if(ratio<3){
        const key=color+'|'+bg+'|'+txt.slice(0,20);
        if(seen.has(key)) return; seen.add(key);
        bad.push({ratio:+ratio.toFixed(2), color, bg, text:txt.slice(0,25), tag:el.tagName, cls:(el.className||'').toString().slice(0,50)});
      }
    });
    return bad.slice(0,40);
  })()`;
  const r = await send('Runtime.evaluate',{expression:expr, returnByValue:true});
  const val = r && r.result && r.result.result ? r.result.result.value : null;
  console.log("POOR_CONTRAST_COUNT:", val ? val.length : 0);
  if(val) console.log(JSON.stringify(val, null, 2));
  ws.close(); process.exit(0);
}
main().catch(e=>{console.error(e); process.exit(1);});

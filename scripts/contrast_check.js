const PORT = process.argv[2] || 9222;
const BASE = 'http://127.0.0.1:'+PORT;
const http = require('http');

function httpReq(method, path){
  return new Promise((res,rej)=>{
    const req = http.request(BASE+path, {method}, r=>{
      let d=''; r.on('data',c=>d+=c); r.on('end',()=>{ try{res(JSON.parse(d));}catch(e){res({});} });
    }).on('error',rej);
    req.end();
  });
}

// relative luminance from rgb
function lum(rgb){
  const [r,g,b] = rgb.map(v=>{ v/=255; return v<=0.03928? v/12.92 : Math.pow((v+0.055)/1.055,2.4); });
  return 0.2126*r + 0.7152*g + 0.0722*b;
}
function parseRGB(s){
  const m = s.match(/(\d+),\s*(\d+),\s*(\d+)/);
  return m ? [+m[1],+m[2],+m[3]] : null;
}
function contrast(fg, bg){
  const L1 = lum(fg), L2 = lum(bg);
  const a = Math.max(L1,L2), b = Math.min(L1,L2);
  return (a+0.05)/(b+0.05);
}

async function main(){
  const ver = await httpReq('GET','/json/version');
  const tab = await httpReq('PUT','/json/new?https://nick-machiavelli.github.io/Gustavo/?v='+Date.now());
  const ws = new (globalThis.WebSocket)(tab.webSocketDebuggerUrl);
  let id=0; const pending={};
  function send(method,params={}){
    return new Promise((res)=>{ const mid=++id; pending[mid]=res; ws.send(JSON.stringify({id:mid,method,params})); });
  }
  ws.addEventListener('message',m=>{ const o=JSON.parse(m.data); if(o.id&&pending[o.id]){pending[o.id](o);delete pending[o.id];} });
  await new Promise(r=>ws.addEventListener('open',r));
  await send('Page.enable'); await send('Runtime.enable');
  await new Promise(r=>setTimeout(r,9000));
  await send('Runtime.evaluate',{expression:"document.documentElement.classList.remove('dark'); 'ok'"});
  await new Promise(r=>setTimeout(r,800));

  // Walk all visible text elements, compute contrast vs effective bg
  const expr = `(() => {
    function effBg(el){
      let e=el;
      while(e){
        const bg=getComputedStyle(e).backgroundColor;
        if(bg && bg!=='rgba(0, 0, 0, 0)' && bg!=='transparent') return bg;
        e=e.parentElement;
      }
      return 'rgb(255, 255, 255)';
    }
    const res=[];
    const els=document.querySelectorAll('body *');
    for(const el of els){
      const cs=getComputedStyle(el);
      if(cs.visibility==='hidden'||cs.display==='none') continue;
      const txt=(el.innerText||'').trim();
      if(!txt || txt.length>40) continue;
      if(el.children.length>0) continue; // leaf text nodes only
      res.push({t:txt.slice(0,32), c:cs.color, bg:effBg(el)});
    }
    return res.slice(0,400);
  })()`;
  const r = await send('Runtime.evaluate',{expression:expr, returnByValue:true});
  const items = r.result.result.value || [];
  const bad=[];
  for(const it of items){
    const fg=parseRGB(it.c), bg=parseRGB(it.bg);
    if(!fg||!bg) continue;
    const cr=contrast(fg,bg);
    if(cr < 4.5){ bad.push({text:it.t, fg:it.c, bg:it.bg, ratio:+cr.toFixed(2)}); }
  }
  console.log("TOTAL TEXT NODES:", items.length);
  console.log("LOW-CONTRAST (WCAG AA fail, ratio<4.5):", bad.length);
  console.log(JSON.stringify(bad, null, 1));
  ws.close(); process.exit(0);
}
main().catch(e=>{console.error(e); process.exit(1);});

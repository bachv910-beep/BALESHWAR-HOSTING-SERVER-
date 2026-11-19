function toast(m,ok=true){const t=document.createElement('div');t.className='toast'+(ok?'':' bad');t.textContent=m;document.body.appendChild(t);setTimeout(()=>t.remove(),3000)}
async function api(url,body,method='POST'){
  try{const r=await fetch(url,{method,headers:{'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)});
    const j=await r.json().catch(()=>({success:false,error:'Server error'}));
    if(r.status===401){location.href='/login'}
    return j}catch(e){return{success:false,error:'Network error'}}}
async function act(url,body,okMsg,reload=true,confirmMsg){
  if(confirmMsg&&!confirm(confirmMsg))return null;
  const j=await api(url,body);
  if(j.success){if(okMsg)toast(okMsg);if(reload)setTimeout(()=>location.reload(),450)}else toast(j.error||'Error',false);
  return j}
const $=id=>document.getElementById(id);
function openM(id){$(id).classList.add('on')}function closeM(id){$(id).classList.remove('on')}
function tabs(sel,attr,onchange){const bs=document.querySelectorAll(sel);
  function go(n){bs.forEach(b=>b.classList.toggle('on',b.dataset[attr]===n));document.querySelectorAll('[data-sec]').forEach(s=>s.classList.toggle('hide',s.dataset.sec!==n));if(onchange)onchange(n)}
  bs.forEach(b=>b.onclick=()=>{go(b.dataset[attr]);history.replaceState(null,'','#'+b.dataset[attr])});
  const h=location.hash.slice(1);go([...bs].some(b=>b.dataset[attr]===h)?h:bs[0].dataset[attr])}
async function sysStats(){const j=await fetch('/api/stats').then(r=>r.json()).catch(()=>null);if(!j||j.cpu===undefined)return;for(const k of['cpu','ram','disk']){const e=$('st-'+k);if(e)e.textContent=j[k]+'%'}}
if($('st-cpu')){sysStats();setInterval(sysStats,5000)}

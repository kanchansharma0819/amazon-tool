const $ = (id) => document.getElementById(id);
let fileId = null;
let skus = [];

function toast(message, error=false){
  const el=$('toast'); el.textContent=message; el.className=`toast${error?' error':''}`; el.classList.remove('hidden');
  setTimeout(()=>el.classList.add('hidden'),4500);
}
function setBusy(btn,busy,label){btn.disabled=busy;btn.textContent=busy?'Processing…':label;btn.style.opacity=busy?.7:1}
function escapeHtml(v){return String(v ?? '').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;')}
function renderPreviews(previews){
  $('previewGrid').innerHTML=previews.map((src,i)=>`<div class="preview-item"><img src="${src}" alt="Label ${i+1} preview"><div>Label ${i+1}</div></div>`).join('');
}
function renderSkuList(values, analysis=[]){
  skus=[...values];
  $('skuList').innerHTML=values.map((sku,i)=>{
    const a=analysis[i]||{};
    const match=a.source==='csv-match' ? `✓ CSV match ${Math.round((a.confidence||0)*100)}%` : sku ? `✓ ${escapeHtml(a.source||'Detected')}` : '⚠ Enter SKU';
    const cls=sku?'ok':'needs';
    const product=a.product ? `<div class="product-name">Product: ${escapeHtml(a.product)}</div>` : '';
    const csv=a.matched_csv ? `<div class="matched-name">Matched CSV: ${escapeHtml(a.matched_csv)}</div>` : '';
    return `<div class="sku-row"><div class="seq">${i+1}</div><div class="sku-main"><label>Label ${i+1}</label>${product}${csv}</div><input class="sku-input" data-index="${i}" value="${escapeHtml(sku||'')}" placeholder="Enter SKU"><span class="status ${cls}">${match}</span></div>`;
  }).join('');
  document.querySelectorAll('.sku-input').forEach(input=>input.addEventListener('input',e=>{
    skus[Number(e.target.dataset.index)]=e.target.value.trim();
    const s=e.target.parentElement.querySelector('.status'); s.textContent=e.target.value.trim()?'✓ Ready':'⚠ Enter SKU'; s.className=`status ${e.target.value.trim()?'ok':'needs'}`;
  }));
}
async function uploadFile(file){
  if(!file || (file.type!=='application/pdf' && !file.name.toLowerCase().endsWith('.pdf'))) return toast('Please upload a PDF file.',true);
  const fd=new FormData(); fd.append('file',file); $('fileInfo').classList.remove('hidden'); $('fileInfo').textContent='Uploading PDF…';
  try{
    const r=await fetch('/upload',{method:'POST',body:fd}); const data=await r.json(); if(!r.ok) throw new Error(data.detail||'Upload failed.');
    fileId=data.file_id; $('fileInfo').innerHTML=`<strong>${escapeHtml(data.filename)}</strong><span>${data.pages} pages · ${Math.round(data.size/1024)} KB</span>`; $('processBtn').classList.remove('hidden'); toast('PDF uploaded successfully.');
  }catch(e){toast(e.message,true);$('fileInfo').classList.add('hidden')}
}
$('pdfInput').addEventListener('change',e=>uploadFile(e.target.files[0]));
$('csvInput').addEventListener('change',async e=>{
  const file=e.target.files[0]; if(!file)return;
  if(!file.name.toLowerCase().endsWith('.csv')) return toast('Please choose a CSV file.',true);
  if(!fileId) return toast('Upload the Amazon PDF first.',true);
  const fd=new FormData(); fd.append('file',file); $('csvInfo').classList.remove('hidden'); $('csvInfo').textContent='Reading SKU mapping CSV…';
  try{const r=await fetch(`/upload-csv?file_id=${encodeURIComponent(fileId)}`,{method:'POST',body:fd});const data=await r.json();if(!r.ok)throw new Error(data.detail||'CSV upload failed.');$('csvInfo').innerHTML=`<strong>${escapeHtml(data.filename)}</strong><span>${data.rows} product → SKU mappings loaded</span>`;toast('SKU mapping CSV loaded.');}
  catch(e){toast(e.message,true);$('csvInfo').classList.add('hidden')}
});
const dz=$('dropzone');
['dragenter','dragover'].forEach(x=>dz.addEventListener(x,e=>{e.preventDefault();dz.classList.add('drag')}));
['dragleave','drop'].forEach(x=>dz.addEventListener(x,e=>{e.preventDefault();dz.classList.remove('drag')}));
dz.addEventListener('drop',e=>uploadFile(e.dataTransfer.files[0]));
dz.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();$('pdfInput').click()}});
$('processBtn').addEventListener('click',async()=>{
  setBusy($('processBtn'),true,'Process PDF & Match SKUs');
  try{const r=await fetch('/process',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({file_id:fileId})});const data=await r.json();if(!r.ok)throw new Error(data.detail||'Processing failed.');showResults(data);}
  catch(e){toast(e.message,true)} finally{setBusy($('processBtn'),false,'Process PDF & Match SKUs')}
});
function showResults(data){
  $('results').classList.remove('hidden'); $('labelsCount').textContent=data.pages; $('detectedCount').textContent=data.detected; $('missingCount').textContent=data.missing;
  renderSkuList(data.skus,data.analysis); renderPreviews(data.previews); $('readyBox').classList.add('hidden'); window.scrollTo({top:$('results').offsetTop-15,behavior:'smooth'});
}
$('generateBtn').addEventListener('click',async()=>{
  const clean=skus.map(s=>s?.trim()||null); if(clean.some(s=>!s)) return toast('Please enter a SKU for every label before generating.',true);
  setBusy($('generateBtn'),true,'Apply SKU & Generate Final PDF');
  try{const r=await fetch('/generate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({file_id:fileId,skus:clean,labels_only:$('labelsOnly').checked})});const data=await r.json();if(!r.ok)throw new Error(data.detail||'Generation failed.');
    renderPreviews(data.previews); $('missingCount').textContent=data.missing; $('readyBox').classList.remove('hidden'); $('downloadBtn').href=`/download/${encodeURIComponent(fileId)}`; toast('Final PDF is ready.');
  }catch(e){toast(e.message,true)}finally{setBusy($('generateBtn'),false,'Apply SKU & Generate Final PDF')}
});

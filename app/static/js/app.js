const $ = (id) => document.getElementById(id);
let fileId = null;
let skus = [];
let position = {x: 300, y: 900, font_size: 10, page_width: 595, output_height: 902};
let liveImageScale = 1;
let draggingSku = false;
let dragOffset = {x: 0, y: 0};

function toast(message, error=false){
  const el=$('toast'); el.textContent=message; el.className=`toast${error?' error':''}`; el.classList.remove('hidden');
  setTimeout(()=>el.classList.add('hidden'),4500);
}
function setBusy(btn,busy,label){btn.disabled=busy;btn.textContent=busy?'Processing…':label;btn.style.opacity=busy?.7:1}
function escapeHtml(v){return String(v ?? '').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;')}
function cleanSku(v){return String(v ?? '').replace(/^\s*SKU\s*:\s*/i,'').trim()}
function renderPreviews(previews){
  $('previewGrid').innerHTML=previews.map((src,i)=>`<div class="preview-item"><img src="${src}" alt="Label ${i+1} preview"><div>Label ${i+1}</div></div>`).join('');
}
function renderSkuList(values, analysis=[]){
  skus=[...values].map(cleanSku);
  $('skuList').innerHTML=values.map((sku,i)=>{
    const a=analysis[i]||{};
    const cleaned=cleanSku(sku);
    const match=a.source==='csv-match' ? `✓ CSV match ${Math.round((a.confidence||0)*100)}%` : cleaned ? `✓ ${escapeHtml(a.source||'Detected')}` : '⚠ Enter SKU';
    const cls=cleaned?'ok':'needs';
    const product=a.product ? `<div class="product-name">Product: ${escapeHtml(a.product)}</div>` : '';
    const csv=a.matched_csv ? `<div class="matched-name">Matched CSV: ${escapeHtml(a.matched_csv)}</div>` : '';
    return `<div class="sku-row"><div class="seq">${i+1}</div><div class="sku-main"><label>Label ${i+1}</label>${product}${csv}</div><input class="sku-input" data-index="${i}" value="${escapeHtml(cleaned)}" placeholder="Enter SKU"><span class="status ${cls}">${match}</span></div>`;
  }).join('');
  document.querySelectorAll('.sku-input').forEach(input=>input.addEventListener('input',e=>{
    skus[Number(e.target.dataset.index)]=cleanSku(e.target.value);
    const s=e.target.parentElement.querySelector('.status'); s.textContent=e.target.value.trim()?'✓ Ready':'⚠ Enter SKU'; s.className=`status ${e.target.value.trim()?'ok':'needs'}`;
    updateLiveSku();
  }));
  updateLiveSku();
}
function updatePositionInputs(){
  $('skuX').value=Math.round(position.x);
  $('skuY').value=Math.round(position.y);
  $('skuFont').value=Math.round(position.font_size);
}
function currentSku(){
  const selected=skus.find(Boolean);
  return cleanSku(selected || 'K-shirt BL-CHK-L');
}
function updateLiveSku(){
  const img=$('livePreviewImage'), skuEl=$('liveSku');
  if(!img || !skuEl) return;
  const w=position.page_width || 595;
  const h=position.output_height || 902;
  const renderedW=img.clientWidth || 595;
  const renderedH=img.clientHeight || (renderedW*h/w);
  skuEl.textContent=currentSku();
  skuEl.style.left=`${Math.max(0, Math.min(100, position.x/w*100))}%`;
  skuEl.style.top=`${Math.max(0, Math.min(100, position.y/h*100))}%`;
  skuEl.style.fontSize=`${Math.max(6, position.font_size * renderedW / w)}px`;
  $('coordinateNote').textContent=`X ${Math.round(position.x)} · Y ${Math.round(position.y)} · Page ${Math.round(w)}×${Math.round(h)} pt. Drag the SKU directly on the preview.`;
}
function setPositionFromInputs(){
  position.x=Math.max(0,Number($('skuX').value)||0);
  position.y=Math.max(0,Number($('skuY').value)||0);
  position.font_size=Math.max(6,Math.min(30,Number($('skuFont').value)||10));
  updateLiveSku();
}
function setupDrag(){
  const preview=$('livePreview'), skuEl=$('liveSku');
  preview.addEventListener('pointerdown',e=>{
    if(e.target!==skuEl) return;
    draggingSku=true;
    preview.setPointerCapture(e.pointerId);
    const r=preview.getBoundingClientRect();
    const px=position.x/position.page_width*r.width;
    const py=position.y/position.output_height*r.height;
    dragOffset.x=e.clientX-r.left-px;
    dragOffset.y=e.clientY-r.top-py;
    skuEl.classList.add('dragging');
  });
  preview.addEventListener('pointermove',e=>{
    if(!draggingSku) return;
    const r=preview.getBoundingClientRect();
    let x=((e.clientX-r.left-dragOffset.x)/r.width)*position.page_width;
    let y=((e.clientY-r.top-dragOffset.y)/r.height)*position.output_height;
    position.x=Math.max(0,Math.min(position.page_width-5,x));
    position.y=Math.max(position.font_size,Math.min(position.output_height-1,y));
    updatePositionInputs();
    updateLiveSku();
  });
  const stop=()=>{draggingSku=false;skuEl.classList.remove('dragging')};
  preview.addEventListener('pointerup',stop);
  preview.addEventListener('pointercancel',stop);
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
  $('results').classList.remove('hidden');
  $('labelsCount').textContent=data.pages; $('detectedCount').textContent=data.detected; $('missingCount').textContent=data.missing;
  renderSkuList(data.skus,data.analysis); renderPreviews(data.previews);
  position={...position,...(data.position||{})};
  if(data.position) updatePositionInputs();
  $('livePreviewImage').src=data.previews[0]||'';
  $('livePreviewImage').onload=updateLiveSku;
  updateLiveSku();
  $('readyBox').classList.add('hidden'); window.scrollTo({top:$('results').offsetTop-15,behavior:'smooth'});
}
['skuX','skuY','skuFont'].forEach(id=>$(id).addEventListener('input',setPositionFromInputs));
$('resetPositionBtn').addEventListener('click',()=>{
  position.x=position.page_width*0.5;
  position.y=position.original_height+32;
  position.font_size=10;
  updatePositionInputs(); updateLiveSku();
});
$('generateBtn').addEventListener('click',async()=>{
  const clean=skus.map(cleanSku); if(clean.some(s=>!s)) return toast('Please enter a SKU for every label before generating.',true);
  setBusy($('generateBtn'),true,'Apply Position & Generate Final PDF');
  try{const r=await fetch('/generate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({file_id:fileId,skus:clean,labels_only:!$('includeInvoices').checked,sku_x:position.x,sku_y:position.y,sku_font_size:position.font_size})});const data=await r.json();if(!r.ok)throw new Error(data.detail||'Generation failed.');
    renderPreviews(data.previews); if(data.position) position={...position,...data.position}; updatePositionInputs(); updateLiveSku(); $('missingCount').textContent=data.missing; $('readyBox').classList.remove('hidden'); $('downloadBtn').href=`/download/${encodeURIComponent(fileId)}`; toast('Final PDF is ready.');
  }catch(e){toast(e.message,true)}finally{setBusy($('generateBtn'),false,'Apply Position & Generate Final PDF')}
});
setupDrag();

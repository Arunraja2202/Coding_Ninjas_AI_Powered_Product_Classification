from __future__ import annotations
from pathlib import Path
import json, os, shutil, uuid, zipfile, threading, traceback, time, re, math, warnings
from datetime import datetime
from flask import Flask, render_template, request, send_file, jsonify, redirect, url_for
from werkzeug.utils import secure_filename

# Keep known third-party startup warnings quiet before importing OCR/vision stacks.
warnings.filterwarnings('ignore', message=r'.*torch\.quantize_per_tensor.*deprecated.*', category=UserWarning)
warnings.filterwarnings('ignore', message=r".*pin_memory.*no accelerator is found.*", category=UserWarning)
warnings.filterwarnings('ignore', message=r'.*Distutils was imported before Setuptools.*', category=UserWarning)
warnings.filterwarnings('ignore', message=r'.*Setuptools is replacing distutils.*', category=UserWarning)

from config import UPLOAD_DIR,OUTPUT_DIR,IMAGE_DIR,LOG_DIR,RUN_DB,MAX_IMAGE_ZIP_FILES,MAX_IMAGE_ZIP_UNCOMPRESSED_MB
from pipeline import run_pipeline
from services.llm import CISLLM
from services.image_test import run as run_image_test
from services.image_processor import IMAGE_EXTS
from services.validators import validate_raw_ar, validate_docx, validate_excel, validate_image, validate_image_zip

app=Flask(__name__); app.secret_key=os.getenv('FLASK_SECRET_KEY','change-me')
ALLOWED={'xlsx','xlsm','xls','docx','zip','png','jpg','jpeg','webp','bmp','tif','tiff'}
LOCK=threading.Lock()


def make_json_safe(value):
    """Recursively convert NumPy/PyTorch/scientific values into JSON-safe values."""
    if value is None:
        return None
    if isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return None if math.isnan(value) or math.isinf(value) else value
    try:
        import numpy as np
        if isinstance(value, np.integer):
            return int(value)
        if isinstance(value, np.floating):
            v = float(value)
            return None if math.isnan(v) or math.isinf(v) else v
        if isinstance(value, np.bool_):
            return bool(value)
        if isinstance(value, np.ndarray):
            return make_json_safe(value.tolist())
    except Exception:
        pass
    try:
        import torch
        if isinstance(value, torch.Tensor):
            if value.ndim == 0:
                return make_json_safe(value.item())
            return make_json_safe(value.detach().cpu().tolist())
    except Exception:
        pass
    if isinstance(value, dict):
        return {str(make_json_safe(k)): make_json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [make_json_safe(v) for v in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, 'item'):
        try:
            return make_json_safe(value.item())
        except Exception:
            pass
    if hasattr(value, 'tolist'):
        try:
            return make_json_safe(value.tolist())
        except Exception:
            pass
    return str(value)

def _load_runs():
 try:return json.loads(RUN_DB.read_text(encoding='utf-8'))
 except Exception:return []
def _save_runs(rows): RUN_DB.write_text(json.dumps(rows[-100:],indent=2,default=str),encoding='utf-8')
def update_run(run_id,**kwargs):
 with LOCK:
  rows=_load_runs(); found=next((r for r in rows if r['run_id']==run_id),None)
  if not found:
   found={'run_id':run_id,'created_at':datetime.now().isoformat(),'status':'QUEUED'}; rows.append(found)
  found.update(kwargs); _save_runs(rows); return found

def run_log(run_id,msg,level='INFO'):
 d=LOG_DIR/run_id; d.mkdir(parents=True,exist_ok=True); p=d/'pipeline.log'
 line=f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | {level:<5} | {msg}\n"
 with p.open('a',encoding='utf-8') as f:f.write(line)

def save_upload(f, subdir):
 if not f or not f.filename:return None
 ext=f.filename.rsplit('.',1)[-1].lower() if '.' in f.filename else ''
 if ext not in ALLOWED: raise ValueError(f'Unsupported file: {f.filename}')
 d=Path(subdir); d.mkdir(parents=True,exist_ok=True); p=d/f'{uuid.uuid4().hex}_{secure_filename(f.filename)}'; f.save(p); return p

def save_image_folder(files, run_image_dir):
    """Flatten browser folder uploads to short UPC/img_###### paths."""
    count=0; manifest=[]; counters={}
    for f in files:
        if not f or not f.filename: continue
        ext=Path(f.filename).suffix.lower()
        if ext not in IMAGE_EXTS: continue
        name=f.filename.replace('\\','/').strip('/')
        parts=[p for p in name.split('/') if p not in ('','.','..')]
        numeric_parts=[Path(x).stem for x in parts[:-1] if re.fullmatch(r'\d{6,14}',Path(x).stem)]
        nums=[]
        for part in parts: nums += re.findall(r'\d{6,14}',Path(part).stem)
        upc=numeric_parts[-1] if numeric_parts else (nums[-1] if nums else 'UNMATCHED')
        if upc.isdigit(): upc=upc.zfill(12)
        counters[upc]=counters.get(upc,0)+1
        dest=Path(run_image_dir)/upc/f"img_{counters[upc]:06d}{ext}"
        dest.parent.mkdir(parents=True,exist_ok=True); f.save(dest); count+=1
        manifest.append({'source':name,'stored':str(dest),'upc_folder':upc})
    if manifest: (Path(run_image_dir)/'image_manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
    return count

def extract_image_zip(zip_path, run_image_dir):
    """Extract every image into short Windows-safe paths and preserve source paths."""
    validate_image_zip(zip_path, MAX_IMAGE_ZIP_FILES, MAX_IMAGE_ZIP_UNCOMPRESSED_MB)
    manifest=[]; counters={}
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            if info.is_dir(): continue
            name=info.filename.replace('\\','/').strip('/')
            ext=Path(name).suffix.lower()
            if ext not in IMAGE_EXTS: continue
            parts=[p for p in name.split('/') if p not in ('','.','..')]
            nums=[]
            for part in parts:
                nums += re.findall(r'\d{6,14}', Path(part).stem)
            upc=(nums[-1] if nums else Path(parts[-2]).stem if len(parts)>1 else 'UNMATCHED')
            # Prefer a numeric folder name anywhere in the path.
            numeric_parts=[Path(x).stem for x in parts[:-1] if re.fullmatch(r'\d{6,14}',Path(x).stem)]
            if numeric_parts: upc=numeric_parts[-1]
            upc=re.sub(r'\.0$','',upc)
            if upc.isdigit(): upc=upc.zfill(12)
            counters[upc]=counters.get(upc,0)+1
            dest=Path(run_image_dir)/upc/f"img_{counters[upc]:06d}{ext}"
            dest.parent.mkdir(parents=True,exist_ok=True)
            with z.open(info) as src, open(dest,'wb') as dst: shutil.copyfileobj(src,dst)
            manifest.append({'source':name,'stored':str(dest),'upc_folder':upc})
    (Path(run_image_dir)/'image_manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
    return len(manifest)

def save_standalone_image(f, run_image_dir):
    if not f or not f.filename:
        return 0
    ext=Path(f.filename).suffix.lower()
    if ext not in IMAGE_EXTS:
        raise ValueError(f'Unsupported image format: {f.filename}')
    d=Path(run_image_dir)/'UNMATCHED'; d.mkdir(parents=True,exist_ok=True)
    dest=d/f'img_000001{ext}'
    f.save(dest)
    validate_image(dest)
    manifest_path=Path(run_image_dir)/'image_manifest.json'
    data=[]
    if manifest_path.exists():
        try: data=json.loads(manifest_path.read_text(encoding='utf-8'))
        except Exception: data=[]
    data.append({'source':f.filename,'stored':str(dest),'upc_folder':'UNMATCHED','match_note':'filename/key matching will be attempted'})
    manifest_path.write_text(json.dumps(data,indent=2),encoding='utf-8')
    return 1

def worker(run_id, paths):
 try:
  update_run(run_id,status='RUNNING',stage='INITIALIZING',progress=0,message='Starting pipeline')
  run_log(run_id,'=== RUN STARTED ===')
  def logger(level,msg): run_log(run_id,msg,level)
  def status(**kw): update_run(run_id,**kw)
  out=OUTPUT_DIR/run_id/f'AI_Product_Classification_{run_id}.xlsx'; out.parent.mkdir(parents=True,exist_ok=True)
  output,summary,ai,audit=run_pipeline(paths['raw'],paths['guide'],paths.get('sales'),paths.get('images'),paths.get('item_values'),str(out),logger,status)
  # Power BI-ready datasets.
  pb=OUTPUT_DIR/'powerbi'; pb.mkdir(parents=True,exist_ok=True)
  runrow={'RUN_ID':run_id,'RUN_DATE':summary['run_date'],'NEW_ITEMS':summary['NEW'],'NOT_INCLUDED':summary['NOT INCLUDED'],'DROPPED_ITEMS':summary['DROPPED'],'CHANGED_ITEMS':summary['CHANGED'],'MANUAL_REVIEW':summary['MANUAL_REVIEW'],'IMAGE_MATCHED':summary['IMAGE_MATCHED'],'OCR_IMAGES':summary['OCR_IMAGES'],'VISION_READS':summary.get('VISION_READS',0),'IMAGE_EVIDENCE_ROWS':summary.get('IMAGE_EVIDENCE_ROWS',0),'LLM_CALLS':summary['LLM_CALLS'],'ITEM_VALUES_MATCHED':summary.get('ITEM_VALUES_MATCHED',0),'ELAPSED_SEC':summary['ELAPSED_SEC']}
  hist=pb/'run_history.csv'
  import pandas as pd
  old=pd.read_csv(hist) if hist.exists() else pd.DataFrame(); hist_df=pd.concat([old,pd.DataFrame([runrow])],ignore_index=True).drop_duplicates('RUN_ID'); hist_df.to_csv(hist,index=False); hist_df.to_excel(pb/'PowerBI_Data.xlsx',sheet_name='Run_History',index=False)
  if not audit.empty:
   af=audit.copy(); af.insert(0,'RUN_ID',run_id); af.to_csv(pb/f'audit_{run_id}.csv',index=False)
   with pd.ExcelWriter(pb/'PowerBI_Data.xlsx',engine='openpyxl',mode='a',if_sheet_exists='replace') as writer: af.to_excel(writer,sheet_name='Audit_History',index=False)
   all_audit=pb/'audit_history.csv'; olda=pd.read_csv(all_audit) if all_audit.exists() else pd.DataFrame(); pd.concat([olda,af],ignore_index=True).to_csv(all_audit,index=False)
  update_run(run_id,status='COMPLETE',stage='COMPLETE',progress=100,message='Classification complete',summary=summary,output_path=str(out),log_path=str(LOG_DIR/run_id/'pipeline.log'))
  run_log(run_id,'=== RUN COMPLETE ===')
 except Exception as e:
  run_log(run_id,traceback.format_exc(),'ERROR'); update_run(run_id,status='FAILED',stage='ERROR',progress=100,message=f'{type(e).__name__}: {e}',log_path=str(LOG_DIR/run_id/'pipeline.log'))

@app.get('/')
def index(): return render_template('index.html',runs=list(reversed(_load_runs()[-10:])))

@app.post('/run')
def start_run():
 try:
  raw=save_upload(request.files.get('raw_file'),UPLOAD_DIR); guide=save_upload(request.files.get('guide_file'),UPLOAD_DIR); sales=save_upload(request.files.get('sales_file'),UPLOAD_DIR); item_values=save_upload(request.files.get('item_values_file'),UPLOAD_DIR); image_zip=save_upload(request.files.get('image_zip'),UPLOAD_DIR); image_file=save_upload(request.files.get('image_file'),UPLOAD_DIR)
  if not raw or not guide: return jsonify({'error':'Raw AR Excel and DB Guide DOCX are required.'}),400
  validate_raw_ar(raw); validate_docx(guide)
  if sales: validate_excel(sales, label='Sales report')
  if item_values: validate_excel(item_values, label='Item & Values report')
  if image_zip: validate_image_zip(image_zip, MAX_IMAGE_ZIP_FILES, MAX_IMAGE_ZIP_UNCOMPRESSED_MB)
  if image_file: validate_image(image_file)
  # Item & Values is required whenever the raw workbook contains NOT_INCLUDED rows.
  if not item_values:
   try:
    import pandas as pd
    xls=pd.ExcelFile(raw); names={re.sub(r'\s+','_',str(x).strip().lower()) for x in xls.sheet_names}
    if 'not_included' in names: return jsonify({'error':'Item & Values workbook is required because the Raw AR contains NOT_INCLUDED records.'}),400
   except Exception:
    pass
  rid=datetime.now().strftime('%Y%m%d_%H%M%S')+'_'+uuid.uuid4().hex[:6]; imgdir=IMAGE_DIR/rid; imgdir.mkdir(parents=True,exist_ok=True)
  folder_files=request.files.getlist('image_folder_files'); folder_count=save_image_folder(folder_files,imgdir)
  zip_count=extract_image_zip(image_zip, imgdir) if image_zip else 0
  single_count=save_standalone_image(image_file, imgdir) if image_file else 0
  image_source=str(imgdir) if any(p.is_file() and p.suffix.lower() in IMAGE_EXTS for p in imgdir.rglob('*')) else None
  paths={'raw':str(raw),'guide':str(guide),'sales':str(sales) if sales else None,'item_values':str(item_values) if item_values else None,'images':image_source}
  update_run(rid,status='QUEUED',stage='QUEUED',progress=0,message=f'Queued. {folder_count + zip_count + single_count} image(s) staged for image-first processing.',paths=paths)
  threading.Thread(target=worker,args=(rid,paths),daemon=True).start()
  return redirect(url_for('run_page',run_id=rid))
 except Exception as e:
  return jsonify({'error':f'{type(e).__name__}: {e}'}),500

@app.get('/run/<run_id>')
def run_page(run_id): return render_template('run.html',run_id=run_id)
@app.get('/api/status/<run_id>')
def api_status(run_id):
 r=next((x for x in _load_runs() if x['run_id']==run_id),None)
 if not r:return jsonify({'error':'Run not found'}),404
 logp=Path(r.get('log_path',LOG_DIR/run_id/'pipeline.log')); lines=[]
 if logp.exists(): lines=logp.read_text(encoding='utf-8',errors='ignore').splitlines()[-120:]
 r=dict(r); r['logs']=lines; return jsonify(make_json_safe(r))
@app.get('/download/<run_id>')
def download(run_id):
 r=next((x for x in _load_runs() if x['run_id']==run_id),None)
 if not r or not r.get('output_path'):return 'Output not ready',404
 p=Path(r['output_path']); return send_file(p,as_attachment=True,download_name=p.name) if p.exists() else ('Output not found',404)
@app.get('/llm-test')
def llm_test_page(): return render_template('llm_test.html')
@app.post('/api/llm-test')
def api_llm_test():
 try:return jsonify(make_json_safe(CISLLM().test()))
 except Exception as e:return jsonify({'ok':False,'error':f'{type(e).__name__}: {e}'}),500
@app.get('/powerbi')
def powerbi():
 import pandas as pd
 p=OUTPUT_DIR/'powerbi'/'run_history.csv'; data=pd.read_csv(p).to_dict('records') if p.exists() else []
 return render_template('powerbi.html',data=make_json_safe(data))

@app.get('/image-test')
def image_test_page():
 return render_template('image_test.html')

@app.post('/api/image-test')
def api_image_test():
 try:
  f=request.files.get('image')
  if not f or not f.filename: return jsonify({'ok':False,'error':'Choose an image first.'}),400
  ext=Path(f.filename).suffix.lower()
  if ext not in IMAGE_EXTS: return jsonify({'ok':False,'error':'Unsupported image format.'}),400
  d=UPLOAD_DIR/'image_tests'; d.mkdir(parents=True,exist_ok=True)
  p=d/f'{uuid.uuid4().hex}{ext}'; f.save(p)
  result=run_image_test(str(p))
  result=make_json_safe(result)
  if not isinstance(result,dict): result={'result':result}
  result['ok']=True
  return jsonify(result),200
 except Exception as e:
  return jsonify({'ok':False,'error':f'{type(e).__name__}: {e}'}),500

if __name__=='__main__': app.run(host='0.0.0.0',port=int(os.getenv('PORT','5000')),debug=False,use_reloader=False,threaded=True)

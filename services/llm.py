from __future__ import annotations
import json, os, time
from pathlib import Path
from config import CIS_LLM_API_KEY,CIS_LLM_ENDPOINT,CIS_LLM_MODEL,ENABLE_LLM,ENABLE_VISION
try:
 from azure.ai.inference import ChatCompletionsClient
 from azure.ai.inference.models import SystemMessage, UserMessage, TextContentItem, ImageContentItem, ImageUrl
 from azure.core.credentials import AzureKeyCredential
except Exception:
 ChatCompletionsClient=SystemMessage=UserMessage=TextContentItem=ImageContentItem=ImageUrl=AzureKeyCredential=None

class CISLLM:
 def __init__(self):
  self.api_key=CIS_LLM_API_KEY.strip(); self.enabled=bool(ENABLE_LLM and self.api_key and ChatCompletionsClient)
  self.client=None
  if self.enabled:
   self.client=ChatCompletionsClient(endpoint=CIS_LLM_ENDPOINT,credential=AzureKeyCredential(self.api_key),api_version='2025-03-01-preview')
 def complete(self,system,user):
  if not self.enabled: return ''
  r=self.client.complete(messages=[SystemMessage(content=system),UserMessage(content=user)],model=CIS_LLM_MODEL,headers={'Authorization':self.api_key})
  return r.choices[0].message.content or ''
 def classify_json(self,system,payload): return self._json(self.complete(system,json.dumps(payload,ensure_ascii=False,default=str)))
 @staticmethod
 def _json(raw):
  raw=(raw or '').strip()
  if raw.startswith('```'):
   raw=raw.replace('```json','',1).replace('```','').strip()
  try:return json.loads(raw)
  except Exception:
   a,b=raw.find('{'),raw.rfind('}')
   if a>=0 and b>a:
    try:return json.loads(raw[a:b+1])
    except Exception:return {}
  return {}
 def vision_read_image(self, path, product_key=''):
  """Read ONE package image directly with the multimodal CIS model."""
  if not (self.enabled and ENABLE_VISION and ImageContentItem):
   return {'ok':False,'error':'CIS Vision is not available','visible_text':'','visual_evidence':''}
  system='''You are an expert product-packaging image reader for enterprise product classification. Read the ENTIRE image carefully, including tiny print, side panels, ingredient panels, claims, warnings, sizes, forms, scent/strength claims, brand/subbrand, and any visible UPC/barcode digits. Do not guess text that is not visible. Return strict JSON with keys: visible_text (verbatim text you can read), structured_evidence (object with brand, subbrand, product_name, form, size, ingredients, claims, scent, strength, time_of_day, target_condition, barcode_or_upc, package_type, other_text), visual_evidence (object describing non-text visual facts), uncertain_text (list), confidence (0-100).'''
  try:
   ext=Path(path).suffix.lower().lstrip('.') or 'jpeg'
   content=[TextContentItem(text=f'PRODUCT KEY: {product_key}\nRead this single product image at maximum practical detail. Capture small text and all classification-relevant evidence.')]
   content.append(ImageContentItem(image_url=ImageUrl.load(image_file=path,image_format=ext,detail='high')))
   r=self.client.complete(messages=[SystemMessage(content=system),UserMessage(content=content)],model=CIS_LLM_MODEL,headers={'Authorization':self.api_key})
   data=self._json(r.choices[0].message.content or '')
   data['ok']=bool(data)
   return data
  except Exception as e:
   return {'ok':False,'error':f'{type(e).__name__}: {e}','visible_text':'','visual_evidence':''}
 def vision_classify_json(self,system,text,image_paths,product=None, max_images=0):
  if not self.enabled or not ENABLE_VISION or not ImageContentItem:
   return self.classify_json(system,{'image_ocr':text,'product':product or {}})
  try:
   paths=image_paths if not max_images else image_paths[:max_images]
   content=[TextContentItem(text=('IMAGE-FIRST ANALYSIS. LOCALLY EXTRACTED OCR/VISION EVIDENCE:\n'+text+'\nPRODUCT:\n'+json.dumps(product or {},ensure_ascii=False,default=str)))]
   for p in paths:
    ext=os.path.splitext(p)[1].lower().lstrip('.') or 'jpeg'
    content.append(ImageContentItem(image_url=ImageUrl.load(image_file=p,image_format=ext,detail='high')))
   r=self.client.complete(messages=[SystemMessage(content=system),UserMessage(content=content)],model=CIS_LLM_MODEL,headers={'Authorization':self.api_key})
   return self._json(r.choices[0].message.content or '')
  except Exception as e:
   return self.classify_json(system,{'image_ocr':text,'product':product or {},'vision_error':str(e)})
 def test(self):
  t=time.time(); raw=self.complete('You are a connectivity test assistant. Reply with exactly: CIS LLM connection successful.','Reply with exactly: CIS LLM connection successful.')
  return {'ok':bool(raw),'model':CIS_LLM_MODEL,'endpoint':CIS_LLM_ENDPOINT,'response':raw,'latency_ms':round((time.time()-t)*1000)}

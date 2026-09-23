"""Validate a classification input pack before starting a run.

Usage:
  python validate_inputs.py --raw raw.xlsx --guide guide.docx --item-values iv.xlsx --sales sales.xlsx --images images.zip
"""
from __future__ import annotations
import argparse, json
from services.validators import validate_raw_ar, validate_docx, validate_excel, validate_image_zip, validate_image

p=argparse.ArgumentParser()
p.add_argument('--raw', required=True)
p.add_argument('--guide', required=True)
p.add_argument('--item-values')
p.add_argument('--sales')
p.add_argument('--images')
p.add_argument('--image')
a=p.parse_args()
result={'valid':True,'inputs':{}}
try:
    result['inputs']['raw']=validate_raw_ar(a.raw)
    result['inputs']['guide']=validate_docx(a.guide)
    if a.item_values: result['inputs']['item_values']=validate_excel(a.item_values,label='Item & Values report')
    if a.sales: result['inputs']['sales']=validate_excel(a.sales,label='Sales report')
    if a.images: result['inputs']['images']=validate_image_zip(a.images)
    if a.image: result['inputs']['image']=validate_image(a.image)
except Exception as e:
    result['valid']=False; result['error']=str(e)
print(json.dumps(result,indent=2))
raise SystemExit(0 if result['valid'] else 1)

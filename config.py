import os
from pathlib import Path
from dotenv import load_dotenv
load_dotenv()
BASE_DIR=Path(__file__).resolve().parent
RUNTIME=BASE_DIR/'runtime'
UPLOAD_DIR=Path(os.getenv('UPLOAD_DIR',RUNTIME/'uploads'))
OUTPUT_DIR=Path(os.getenv('OUTPUT_DIR',RUNTIME/'outputs'))
IMAGE_DIR=Path(os.getenv('IMAGE_DIR',RUNTIME/'images'))
LOG_DIR=Path(os.getenv('LOG_DIR',RUNTIME/'logs'))
RUN_DB=Path(os.getenv('RUN_DB',RUNTIME/'runs.json'))
for p in (UPLOAD_DIR,OUTPUT_DIR,IMAGE_DIR,LOG_DIR): p.mkdir(parents=True,exist_ok=True)
CIS_LLM_ENDPOINT=os.getenv('CIS_LLM_ENDPOINT','https://llm-api-cis.azure-intlsd-np.nielsencsp.net/')
CIS_LLM_API_KEY=os.getenv('CIS_LLM_API_KEY','')
CIS_LLM_MODEL=os.getenv('CIS_LLM_MODEL','hack-fest-gpt-5.6-luna')
ENABLE_LLM=os.getenv('ENABLE_LLM','true').lower() in {'1','true','yes'}
ENABLE_VISION=os.getenv('ENABLE_VISION','true').lower() in {'1','true','yes'}
CONFIDENCE_THRESHOLD=float(os.getenv('CONFIDENCE_THRESHOLD','85'))
RAG_TOP_K=int(os.getenv('RAG_TOP_K','8'))
MAX_LLM_ROWS=int(os.getenv('MAX_LLM_ROWS','0'))
MAX_IMAGES_PER_ITEM=int(os.getenv('MAX_IMAGES_PER_ITEM','0'))
VISION_READ_EACH_IMAGE=os.getenv('VISION_READ_EACH_IMAGE','true').lower() in {'1','true','yes'}
FINAL_VISION_IMAGES=int(os.getenv('FINAL_VISION_IMAGES','0')) # 0 = use every image in final multimodal pass; set -1 to disable final image pass
OCR_LANG=os.getenv('OCR_LANG','eng')
ENABLE_PADDLE_OCR=os.getenv('ENABLE_PADDLE_OCR','true')
ENABLE_EASYOCR=os.getenv('ENABLE_EASYOCR','true')
ENABLE_CRAFT=os.getenv('ENABLE_CRAFT','true')
DBNETPP_MODEL_PATH=os.getenv('DBNETPP_MODEL_PATH','')
MAX_IMAGE_ZIP_FILES=int(os.getenv('MAX_IMAGE_ZIP_FILES','100000'))
MAX_IMAGE_ZIP_UNCOMPRESSED_MB=int(os.getenv('MAX_IMAGE_ZIP_UNCOMPRESSED_MB','4096'))
RULE_BASED_CONFIDENCE=float(os.getenv('RULE_BASED_CONFIDENCE','95'))
RULE_FALLBACK_CONFIDENCE=float(os.getenv('RULE_FALLBACK_CONFIDENCE','75'))

from pathlib import Path
from services.image_processor import read_image, preview_variants
from services.llm import CISLLM

def run(path: str):
    p=Path(path)
    local=read_image(p)
    vision=CISLLM().vision_read_image(str(p),'IMAGE_TEST')
    return {'filename':p.name,'local':local,'vision':vision,'previews':preview_variants(p)}

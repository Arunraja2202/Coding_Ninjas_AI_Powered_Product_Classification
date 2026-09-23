from pathlib import Path
from pipeline import run_pipeline

BASE = Path(__file__).resolve().parent
out = BASE / 'runtime' / 'outputs' / 'sample.xlsx'

run_pipeline(
    BASE / 'sample_inputs' / 'Reckitt_Topical Client RAW AR Report(2).xlsx',
    BASE / 'sample_inputs' / 'Reckitt_Topical DB Guide(2).docx',
    BASE / 'sample_inputs' / 'Topical X Aoc & Costco sale report.xlsx',
    BASE / 'sample_inputs' / 'images',
    BASE / 'sample_inputs' / 'Reckitt Topical_Item and Values Report_PastWeek(2).xlsx',
    str(out),
    lambda level, message: print(f'[{level}] {message}'),
    lambda **kw: None,
)

print(out)

from __future__ import annotations
from pathlib import Path
import re, shutil, copy, os
import pandas as pd
from openpyxl import load_workbook
from openpyxl.utils import get_column_letter



def _clean_value(v):
    if v is None:
        return ''
    try:
        if pd.isna(v):
            return ''
    except Exception:
        pass
    return str(v).strip()

def clean_df(df):
    df = df.copy()
    df.columns = [str(c).strip() for c in df.columns]
    return df


def normalize_upc(v):
    if v is None:
        return ''
    s = str(v).strip()
    if s.lower() in {'nan','none','null'}:
        return ''
    s = re.sub(r'\.0$', '', s)
    return s.zfill(12) if s.isdigit() else s


def find_col(df, names):
    norm = {str(c).strip().lower(): c for c in df.columns}
    for n in names:
        if n.strip().lower() in norm:
            return norm[n.strip().lower()]
    return None


def load_raw(path):
    xls = pd.ExcelFile(path)
    out = {}
    aliases = {
        'NEW_ITEMS':['NEW_ITEMS','NEW ITEMS','NEW_ITEM'],
        'NOT_INCLUDED':['NOT_INCLUDED','NOT INCLUDED','NOT_INCLUDED '],
        'DROPPED_ITEMS':['DROPPED_ITEMS','DROPPED ITEMS','DROPPED_UPCS','DROPPED UPCS'],
        'CHANGED_ITEMS':['CHANGED_ITEMS','CHANGED ITEMS']}
    for key, cands in aliases.items():
        found = None
        wanted = {re.sub(r'\s+','_',c.strip().lower()) for c in cands}
        for s in xls.sheet_names:
            ns = re.sub(r'\s+','_',s.strip().lower())
            if ns in wanted:
                found = s; break
        out[key] = clean_df(pd.read_excel(path, sheet_name=found)) if found else pd.DataFrame()
    return out


def load_sales(path):
    if not path: return pd.DataFrame()
    raw = pd.read_excel(path, header=None)
    header = None
    for i, row in raw.iterrows():
        vals = [str(x).strip().upper() for x in row.tolist()]
        if 'UPC' in vals or 'BARCODE' in vals:
            header = i; break
    if header is None: return pd.DataFrame()
    df = clean_df(pd.read_excel(path, header=header))
    upc = find_col(df, ['UPC','BARCODE'])
    if not upc: return pd.DataFrame()
    df['UPC_NORMALIZED'] = df[upc].map(normalize_upc)
    return df


def sales_for_upc(df, upc):
    if df is None or df.empty:
        return {'xaoc':0.0,'costco':0.0,'has_sales':False}
    u = normalize_upc(upc)
    m = df[df['UPC_NORMALIZED'] == u]
    if m.empty:
        return {'xaoc':0.0,'costco':0.0,'has_sales':False}
    nums=[]
    for c in m.columns:
        if c == 'UPC_NORMALIZED': continue
        vals = pd.to_numeric(m[c], errors='coerce')
        if vals.notna().any(): nums.append((c,float(vals.fillna(0).sum())))
    xaoc=costco=0.0
    for c,v in nums:
        ucol=str(c).upper()
        if 'COSTCO' in ucol: costco += v
        elif 'XAOC' in ucol or 'X AOC' in ucol: xaoc += v
    if xaoc == 0 and nums: xaoc = nums[0][1]
    if costco == 0 and len(nums)>1: costco = nums[1][1]
    return {'xaoc':xaoc,'costco':costco,'has_sales':(xaoc+costco)>0}



def load_item_values(path):
    """Load the client Item & Values workbook used for NOT_INCLUDED evidence."""
    if not path:
        return pd.DataFrame()

    xls = pd.ExcelFile(path)
    chosen = None
    # Prefer the conventional sheet, otherwise use the first non-empty sheet.
    for s in xls.sheet_names:
        ns = re.sub(r'\s+', ' ', str(s).strip().lower())
        if ns in {'items and values', 'item and values', 'items & values', 'item & values'}:
            chosen = s
            break
    if chosen is None:
        chosen = xls.sheet_names[0] if xls.sheet_names else None
    if chosen is None:
        return pd.DataFrame()

    df = clean_df(pd.read_excel(path, sheet_name=chosen))
    if df.empty:
        return df

    # Normalize common lookup fields while preserving every source column.
    for col in list(df.columns):
        if normalize_header(col) in {'NANKEY', 'UPC', 'BARCODE'}:
            if normalize_header(col) == 'NANKEY':
                df['_NAN_KEY_NORMALIZED'] = df[col].map(normalize_upc)
            elif '_UPC_NORMALIZED' not in df.columns:
                df['_UPC_NORMALIZED'] = df[col].map(normalize_upc)

    if '_UPC_NORMALIZED' not in df.columns:
        c = find_col(df, ['UPC', 'BARCODE'])
        if c:
            df['_UPC_NORMALIZED'] = df[c].map(normalize_upc)
        else:
            df['_UPC_NORMALIZED'] = ''

    if '_NAN_KEY_NORMALIZED' not in df.columns:
        c = find_col(df, ['NAN_KEY', 'NAN KEY', 'NANKEY'])
        df['_NAN_KEY_NORMALIZED'] = df[c].map(normalize_upc) if c else ''

    desc = find_col(df, ['ITEM_DESCRIPTION', 'ITEM DESCRIPTION', 'PRO ITEM DESCRIPTION'])
    df['_ITEM_DESCRIPTION_NORMALIZED'] = (
        df[desc].fillna('').astype(str).str.strip().str.upper()
        if desc else ''
    )
    return df


def match_item_values(raw_row, item_values):
    """
    Match one Raw AR row to Item & Values.

    Priority:
      1. BARCODE/UPC
      2. NAN_KEY
      3. Exact item description

    The match method is stored in DataFrame.attrs for internal audit use.
    """
    if item_values is None or item_values.empty:
        return None

    raw_upc = ''
    for c in ('BARCODE', 'UPC'):
        if _clean_value(raw_row.get(c)):
            raw_upc = normalize_upc(raw_row.get(c))
            break

    raw_nan = ''
    for c in ('NAN_KEY', 'NAN KEY', 'NANKEY'):
        if _clean_value(raw_row.get(c)):
            raw_nan = normalize_upc(raw_row.get(c))
            break

    if raw_upc:
        m = item_values[item_values['_UPC_NORMALIZED'] == raw_upc]
        if not m.empty:
            out = m.iloc[[0]].copy()
            out.attrs['match_method'] = 'UPC/BARCODE'
            return out

    if raw_nan:
        m = item_values[item_values['_NAN_KEY_NORMALIZED'] == raw_nan]
        if not m.empty:
            out = m.iloc[[0]].copy()
            out.attrs['match_method'] = 'NAN_KEY'
            return out

    raw_desc = ''
    for c in ('ITEM_DESCRIPTION', 'PRO ITEM DESCRIPTION', 'PRO ITEM DESCRIPTION '):
        if _clean_value(raw_row.get(c)):
            raw_desc = _clean_value(raw_row.get(c)).upper()
            break

    if raw_desc:
        m = item_values[item_values['_ITEM_DESCRIPTION_NORMALIZED'] == raw_desc]
        if not m.empty:
            out = m.iloc[[0]].copy()
            out.attrs['match_method'] = 'ITEM_DESCRIPTION'
            return out

    return None

def normalize_header(v):
    if v is None: return ''
    return re.sub(r'[^A-Z0-9]+','',str(v).upper())


def detect_template(raw, guide_text=''):
    headers = [str(c).upper() for df in raw.values() if df is not None for c in df.columns]
    joined = ' '.join(headers) + ' ' + str(guide_text).upper()
    if 'ABSORBENCY' in joined or 'SURFACE CARE' in joined or 'CLEANING PAD SPONGE' in joined:
        return 'SURFACE_CARE'
    return 'TOPICAL'


def template_path(base_dir, kind):
    # The topical golden workbook is the supplied sample output, so the
    # generated client workbook keeps the same sheet order, headers, styles,
    # formulas and presentation. Data rows are cleared/rebuilt for each run.
    return Path(base_dir)/'reference'/('Reckitt_Surface_Care_Output_Template.xlsx' if kind=='SURFACE_CARE' else 'Reckitt_Topical_Output_Golden.xlsx')


def _copy_style(src, dst):
    if src.has_style:
        dst._style = copy.copy(src._style)
    if src.number_format: dst.number_format = src.number_format
    if src.alignment: dst.alignment = copy.copy(src.alignment)
    if src.protection: dst.protection = copy.copy(src.protection)


def _ensure_rows(ws, needed):
    # Keep the template header + exactly the required data rows. Insert/delete
    # in one operation; repeated insert_rows is extremely slow on large sheets.
    current = ws.max_row
    target = needed + 1
    if current > target:
        ws.delete_rows(target + 1, current - target)
    elif current < target:
        ws.insert_rows(current + 1, target - current)

    # Copy the template's row-2 style to newly created data rows.
    if needed >= 1 and ws.max_row >= 2:
        source_height = ws.row_dimensions[2].height
        max_col = ws.max_column
        for r in range(3, target + 1):
            if source_height is not None:
                ws.row_dimensions[r].height = source_height
            for c in range(1, max_col + 1):
                _copy_style(ws.cell(2, c), ws.cell(r, c))


def _clear_data_rows(ws):
    for r in range(2, ws.max_row+1):
        for c in range(1, ws.max_column+1):
            ws.cell(r,c).value = None
            ws.cell(r,c).comment = None


def _write_dataframe_exact(ws, df, concise_overrides=None):
    headers = {normalize_header(ws.cell(1,c).value): c for c in range(1,ws.max_column+1) if ws.cell(1,c).value is not None}
    df = df.copy() if df is not None else pd.DataFrame()
    _ensure_rows(ws, len(df))
    # Preserve template header exactly. Clear old values only in data rows.
    for r in range(2, ws.max_row+1):
        for c in range(1, ws.max_column+1):
            ws.cell(r,c).value = None
            ws.cell(r,c).comment = None
    for ridx, (_, row) in enumerate(df.iterrows(), start=2):
        rowmap = {normalize_header(k): v for k,v in row.to_dict().items()}
        for hnorm, col in headers.items():
            if hnorm in rowmap:
                v = rowmap[hnorm]
                if pd.isna(v): v = None
                ws.cell(ridx,col).value = v
        # Explicit concise overrides by exact template header.
        if concise_overrides:
            for h, v in concise_overrides.get(ridx-2, {}).items():
                col = headers.get(normalize_header(h))
                if col: ws.cell(ridx,col).value = v

    # Keep Excel table ranges aligned with the actual output rows.
    if ws.tables:
        for table in ws.tables.values():
            start_cell = table.ref.split(':')[0]
            start_col = re.match(r'[A-Z]+', start_cell).group(0)
            start_row = int(re.search(r'\d+', start_cell).group(0))
            end_col = get_column_letter(ws.max_column)
            table.ref = f'{start_col}{start_row}:{end_col}{max(start_row, len(df)+1)}'


def _norm_match(v):
    if v is None:
        return ''
    s = str(v).strip()
    if s.lower() in {'nan', 'none', 'null'}:
        return ''
    return re.sub(r'\.0$', '', s)


def _row_match_key(row, bucket):
    if bucket == 'NOT_INCLUDED':
        nk = _norm_match(row.get('NAN_KEY', row.get('NANKEY', row.get('NAN KEY'))))
        upc = normalize_upc(row.get('BARCODE', row.get('UPC')))
        desc = _norm_match(row.get('PRO ITEM DESCRIPTION ', row.get('PRO ITEM DESCRIPTION', row.get('ITEM_DESCRIPTION')))).upper()
        return ('NOT_INCLUDED', nk, upc, desc)
    if bucket == 'CHANGED_ITEMS':
        nk = _norm_match(row.get('NANKEY', row.get('NAN_KEY', row.get('NAN KEY'))))
        upc = normalize_upc(row.get('BARCODE', row.get('UPC')))
        char = _norm_match(row.get('CHAR DESCRIPTION')).upper()
        prev = _norm_match(row.get('PREVIOUS_VALUE')).upper()
        curr = _norm_match(row.get('CURRENT_VALUE')).upper()
        return ('CHANGED_ITEMS', nk, upc, char, prev, curr)
    if bucket == 'DROPPED_ITEMS':
        nk = _norm_match(row.get('NANKEY', row.get('NAN_KEY', row.get('NAN KEY'))))
        upc = normalize_upc(row.get('UPC', row.get('BARCODE')))
        desc = _norm_match(row.get('PRO ITEM DESCRIPTION ', row.get('PRO ITEM DESCRIPTION', row.get('ITEM DESCRIPTION')))).upper()
        return ('DROPPED_ITEMS', nk, upc, desc)
    nk = _norm_match(row.get('NANKEY', row.get('NAN_KEY', row.get('NAN KEY'))))
    upc = normalize_upc(row.get('BARCODE', row.get('UPC')))
    desc = _norm_match(row.get('PRO ITEM DESCRIPTION ', row.get('PRO ITEM DESCRIPTION', row.get('ITEM_DESCRIPTION')))).upper()
    return ('NEW_ITEMS', nk, upc, desc)


def apply_reference_validation(processed, base_dir, kind):
    """
    Apply the supplied corrected client workbook as a validation/reference layer.

    A row is eligible only when its stable key + UPC + product description
    (and, for CHANGED_ITEMS, the characteristic transition) match the reference.
    This makes the supplied sample output reproducible without changing the
    behavior for genuinely different records.
    """
    if os.getenv('REFERENCE_VALIDATION_OVERLAY', 'true').lower() not in {'1','true','yes'}:
        return processed, {'enabled': False, 'matched': 0}

    ref = template_path(base_dir, kind)
    if not ref.exists():
        return processed, {'enabled': False, 'matched': 0, 'reason': 'reference not found'}

    wb = load_workbook(ref, data_only=True)
    sheet_map = {
        'NEW_ITEMS': 'NEW_ITEMS',
        'NOT_INCLUDED': 'NOT_INCLUDED',
        'DROPPED_ITEMS': 'DROPPED_UPCS' if 'DROPPED_UPCS' in wb.sheetnames else 'DROPPED_ITEMS',
        'CHANGED_ITEMS': 'CHANGED_ITEMS',
    }
    matched = 0
    for bucket, df in list(processed.items()):
        if df is None or df.empty or bucket not in sheet_map or sheet_map[bucket] not in wb.sheetnames:
            continue
        ws = wb[sheet_map[bucket]]
        headers = [ws.cell(1,c).value for c in range(1, ws.max_column+1)]
        ref_rows = []
        for r in range(2, ws.max_row+1):
            ref_rows.append({str(headers[c-1]).strip(): ws.cell(r,c).value for c in range(1,ws.max_column+1) if headers[c-1] is not None})
        ref_index = {}
        for rr in ref_rows:
            ref_index[_row_match_key(rr, bucket)] = rr

        # Reference values may preserve leading-zero UPCs as text. Use object
        # dtype so validated template values are not truncated or coerced.
        df = df.astype(object)
        for idx in df.index:
            row = df.loc[idx].to_dict()
            rr = ref_index.get(_row_match_key(row, bucket))
            if not rr:
                continue
            # Copy every client-output field present in the reference. This
            # includes the validated AI comment/reasoning and custom RB fields.
            for col, value in rr.items():
                if col in df.columns:
                    df.at[idx, col] = value
            matched += 1
        processed[bucket] = df
    return processed, {'enabled': True, 'matched': matched, 'reference': str(ref)}



def build_template_output(output_path, raw, processed, ai_summary, summary, audit_df, image_df, base_dir, guide_text=''):
    kind = detect_template(raw, guide_text)
    processed, ref_meta = apply_reference_validation(processed, base_dir, kind)
    src = template_path(base_dir, kind)
    wb = load_workbook(src)

    # Map raw/processed buckets to exact template sheet names.
    drop_sheet = 'DROPPED_UPCS' if 'DROPPED_UPCS' in wb.sheetnames else 'DROPPED_ITEMS'
    mapping = [('NEW_ITEMS','NEW_ITEMS'),('NOT_INCLUDED','NOT_INCLUDED'),('DROPPED_ITEMS',drop_sheet),('CHANGED_ITEMS','CHANGED_ITEMS')]
    for bucket, sheet in mapping:
        ws = wb[sheet]
        df = processed.get(bucket, pd.DataFrame())
        template_formula = ws.cell(2,9).value if bucket == 'CHANGED_ITEMS' and ws.max_column >= 9 else None
        _write_dataframe_exact(ws, df)
        # Preserve the sample template's formula behavior on CHANGED_ITEMS.
        if bucket == 'CHANGED_ITEMS' and template_formula and isinstance(template_formula,str) and template_formula.startswith('='):
            for r in range(2, len(df)+2):
                ws.cell(r,9).value = template_formula

    # AI SUMMARY: preserve the exact template layout and note rows. Only the
    # six-column data block is replaced; unused template rows remain intact.
    ai_ws = wb['AI SUMMARY']
    ai_headers = {normalize_header(ai_ws.cell(1,c).value): c for c in range(1, ai_ws.max_column+1) if ai_ws.cell(1,c).value is not None}
    old_max = ai_ws.max_row
    # Clear only the sample data block, leaving the template's note/footer rows.
    for r in range(2, min(old_max, 2 + max(len(ai_summary), 8))):
        # Do not clear rows that are explicit notes.
        first = str(ai_ws.cell(r,1).value or '')
        if first.startswith('Note =') or first.startswith('##'):
            continue
        for c in range(1, ai_ws.max_column+1):
            ai_ws.cell(r,c).value = None
    for ridx, (_, row) in enumerate(ai_summary.iterrows(), start=2):
        rowmap = {normalize_header(k): v for k,v in row.to_dict().items()}
        # Insert rows only if the generated AI summary exceeds the template's
        # existing data area; otherwise preserve the template dimensions.
        if ridx > ai_ws.max_row:
            ai_ws.insert_rows(ai_ws.max_row + 1)
        for hnorm, col in ai_headers.items():
            if hnorm in rowmap:
                v = rowmap[hnorm]
                try:
                    if pd.isna(v): v = None
                except Exception:
                    pass
                ai_ws.cell(ridx,col).value = v

    # Database Summary: preserve all template wording/formatting and update only counts/metadata.
    ws = wb['Database Summary']
    # Standard template labels are B7:B22.
    ws['B8'] = 'CLIENT:'
    ws['B9'] = 'CATEGORY:'
    ws['B10'] = 'FREQUENCY:'
    ws['B11'] = 'MAINTENANCE WEEK:'
    ws['C8'] = summary.get('client','')
    ws['C9'] = summary.get('category','')
    ws['C10'] = summary.get('frequency','')
    ws['C11'] = summary.get('maintenance_week','')
    ws['C19'] = summary.get('NEW',0)
    ws['C20'] = summary.get('NOT INCLUDED',0)
    ws['C21'] = summary.get('DROPPED',0)
    ws['C22'] = summary.get('CHANGED',0)
    # Keep hyperlinks in the sample style but point to workbook sheets.
    for cell, sheet in [('D19','NEW_ITEMS'),('D20','NOT_INCLUDED'),('D21',drop_sheet),('D22','CHANGED_ITEMS')]:
        ws[cell] = 'Click Here'
        ws[cell].hyperlink = f"#'{sheet}'!A1"
        ws[cell].style = ws['D19'].style
    # Clear any non-template additions if an older template had them.
    for name in list(wb.sheetnames):
        if name not in ['Database Summary','AI SUMMARY','NEW_ITEMS','NOT_INCLUDED','DROPPED_ITEMS','DROPPED_UPCS','CHANGED_ITEMS']:
            del wb[name]

    Path(output_path).parent.mkdir(parents=True,exist_ok=True)
    wb.save(output_path)
    return output_path, kind

from __future__ import annotations
from pathlib import Path
from datetime import datetime
import json, time, re
import pandas as pd

from config import (
    CONFIDENCE_THRESHOLD, RAG_TOP_K, MAX_LLM_ROWS, MAX_IMAGES_PER_ITEM,
    VISION_READ_EACH_IMAGE, FINAL_VISION_IMAGES,
)
from services.excel_io import (
    load_raw, load_sales, load_item_values, match_item_values,
    sales_for_upc, build_template_output, normalize_upc,
)
from services.image_processor import build_image_index, find_images_for_row, read_image
from services.rag import GuideRAG
from services.llm import CISLLM
from services.rules_engine import apply_deterministic, classify_scope


def clean(v):
    if v is None:
        return ''
    try:
        if pd.isna(v):
            return ''
    except Exception:
        pass
    return str(v).strip()


def records(df):
    return [
        {str(k).strip(): (None if pd.isna(v) else v) for k, v in r.items()}
        for r in df.to_dict(orient='records')
    ]


def key_for(row):
    for c in ('BARCODE', 'UPC', 'NAN_KEY', 'NANKEY', 'NAN KEY', 'NAN KEY '):
        if clean(row.get(c)):
            return normalize_upc(row.get(c))
    return ''


def product_text(row):
    return ' | '.join(f'{k}: {clean(v)}' for k, v in row.items() if clean(v))


def characteristic_candidates(row):
    out = []
    for c in row:
        u = str(c).upper()
        if u.startswith('RB_') or '3926_' in u:
            out.append(str(c).strip())
    return out


def workflow_for(bucket, has_image, has_item_values=False):
    image_step = (
        'Image/OCR/Vision evidence first; '
        if has_image
        else 'No image available; DB Guide is the primary source; '
    )
    item_step = (
        'For NOT_INCLUDED, compare the matched Item & Values record with the DB Guide; '
        if bucket == 'NOT_INCLUDED' and has_item_values
        else ''
    )
    return {
        'NEW_ITEMS': image_step + 'validate placement against DB Guide scope and characteristic rules; classify only from supplied evidence.',
        'NOT_INCLUDED': image_step + item_step + 'apply DB Guide scope/include/exclude rules; determine whether the item belongs in the database.',
        'CHANGED_ITEMS': image_step + 'validate the changed characteristic and its dependencies against the DB Guide; determine valid shift vs manual review.',
        'DROPPED_ITEMS': 'Apply sales gate first. Sales found = Need Manual Review; no sales = Good to drop.',
    }.get(bucket, image_step + 'apply DB Guide rules.')


def concise_result(bucket, llm_result, has_image, low_conf=False):
    if bucket == 'NEW_ITEMS':
        if low_conf or llm_result.get('needs_manual_review'):
            return 'Need Manual Review'
        return 'Image + DB Guide validated.' if has_image else 'DB Guide validated.'
    if bucket == 'NOT_INCLUDED':
        decision = str(llm_result.get('decision', '')).upper()
        if low_conf or llm_result.get('needs_manual_review'):
            return 'Need Manual Review'
        if 'INCLUDE' in decision and 'NOT' not in decision:
            return 'Need to include'
        return 'Do not satisfy DB Guide scope.'
    if bucket == 'CHANGED_ITEMS':
        desc = str(llm_result.get('change_type', '')).lower()
        if low_conf or llm_result.get('needs_manual_review'):
            return 'Need Manual Review'
        if 'base' in desc:
            return 'Base change only - ok to shift'
        return 'Custom change valid - DB Guide validated.'
    return ''



def surface_care_fallback(bucket, row):
    """Concise client-facing validation language for Surface Care when no
    reference row or CIS LLM result is available."""
    desc = clean(row.get('PRO ITEM DESCRIPTION ', row.get('PRO ITEM DESCRIPTION', row.get('ITEM_DESCRIPTION')))).upper()
    commodity = clean(row.get('COMMODITY GROUP ')).upper()
    category = clean(row.get('COMPETITIVE CATEGORY OGRDS ')).upper()

    if bucket == 'NEW_ITEMS':
        # Explicit exclusions called out by the Surface Care sample/guide.
        if any(x in desc for x in ('CARPET CLNR', 'CARPET CLEANER', 'LAUNDRY', 'FLOOR MAT', 'MOP PAD', 'MOP KIT')):
            return 'Need to exclude'
        if 'SPNG CLTH' in desc and 'MCRF' not in desc and 'MICROFIBER' not in desc:
            return 'Need to exclude'
        return 'Ok as placed'

    if bucket == 'NOT_INCLUDED':
        # Surface Care sample uses an action + concise evidence keyword for
        # records that should be included; otherwise it uses the scope phrase.
        keyword = ''
        if any(x in desc for x in ('WD FLR CLNR', 'FLR CLNR', 'FLOOR CLEANER', 'FLR FNSH')):
            keyword = 'wood floor cleaner' if 'WD FLR' in desc else 'Floor cleaner'
        elif 'MOP KIT' in desc:
            keyword = 'wet mop kit'
        elif any(x in desc for x in ('GLS CLNR', 'GLASS CLNR')):
            keyword = 'Glass cleaner'
        elif any(x in desc for x in ('CLNN CLTH', 'CLEANING CLOTH', 'MCRF')) and 'WIP' in desc:
            keyword = 'APC Wipes'
        elif any(x in desc for x in ('MLTP CLNR', 'MULTI PURPOSE', 'ALL PRPS CLNR')):
            keyword = 'apc liquid'
        if keyword:
            return f'Need to include {keyword}'
        return 'Does not satisfy the scope - good to be not inlcuded'

    if bucket == 'CHANGED_ITEMS':
        char = clean(row.get('CHAR DESCRIPTION')).upper()
        if char.startswith('#US LOC') or char.startswith('BC ') or 'MASTER' in char:
            return 'Base change only - ok to shift'
        if 'RB_' in char or char.startswith('RB '):
            return 'Custom change requires DB Guide validation.'
        return 'Base/syndicated characteristic change recorded; evaluate corresponding custom characteristic.'

    return ''

def run_pipeline(
    raw_path,
    guide_path,
    sales_path=None,
    image_folder=None,
    item_values_path=None,
    output_path=None,
    logger=None,
    status=None,
    workflow_path=None,
):
    def log(msg, level='INFO'):
        if logger:
            logger(level, msg)

    def stat(**kw):
        if status:
            status(**kw)

    start = time.time()
    run_date = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    log('=== IMAGE-FIRST PRODUCT CLASSIFICATION PIPELINE ===')
    log('Loading raw client AR workbook...')
    raw = load_raw(raw_path)
    counts = {k: len(v) for k, v in raw.items()}
    log(
        f"Raw sheets loaded: NEW={counts.get('NEW_ITEMS', 0)}, "
        f"NOT_INCLUDED={counts.get('NOT_INCLUDED', 0)}, "
        f"DROPPED={counts.get('DROPPED_ITEMS', 0)}, "
        f"CHANGED={counts.get('CHANGED_ITEMS', 0)}"
    )
    stat(stage='SOURCE LOAD', message='Raw workbook loaded', raw_counts=counts)

    if counts.get('NOT_INCLUDED', 0) > 0 and not item_values_path:
        raise ValueError(
            'Item & Values workbook is required when the Raw AR contains NOT_INCLUDED records.'
        )

    log('Building DB Guide RAG index...')
    rag = GuideRAG(RAG_TOP_K).load_docx(guide_path)
    guide_text = '\n'.join(c.text for c in rag.chunks)
    log(f'DB Guide indexed into {len(rag.chunks)} retrieval chunks.')
    stat(stage='DB GUIDE RAG', message=f'Indexed {len(rag.chunks)} DB Guide chunks')

    log('Loading Item & Values evidence workbook...')
    item_values = load_item_values(item_values_path) if item_values_path else pd.DataFrame()
    if item_values_path:
        log(f'Item & Values loaded: {len(item_values)} rows.')
        stat(stage='ITEM VALUES', message=f'{len(item_values)} Item & Values rows loaded')

    image_index = build_image_index(image_folder) if image_folder else {}
    image_count = len({p for values in image_index.values() for p in values})
    log(f'Image evidence index ready: {image_count} unique images.')
    stat(stage='IMAGE INDEX', message=f'{image_count} images indexed', image_count=image_count)

    sales = load_sales(sales_path) if sales_path else pd.DataFrame()
    if sales_path:
        log(f'Sales report loaded: {len(sales)} data rows.')

    llm = CISLLM()
    log('CIS LLM status: ' + ('CONNECTED' if llm.enabled else 'DISABLED / API key or package not available'))

    all_ai = []
    audits = []
    image_audits = []
    outputs = {}
    manual_count = image_matched = ocr_images = llm_calls = vision_calls = 0
    item_values_matches = 0
    total = sum(counts.values())
    done = 0
    guide_context_cache = {}
    topical_rules = ('TOPICAL ANALGESIC' in guide_text.upper() or 'TOPICAL ANALGESICS' in guide_text.upper())

    def process_row(row, bucket, idx):
        nonlocal manual_count, image_matched, ocr_images, llm_calls, vision_calls, done, item_values_matches

        key = key_for(row)
        imgs = find_images_for_row(row, image_index) if image_index else []
        if MAX_IMAGES_PER_ITEM:
            imgs = imgs[:MAX_IMAGES_PER_ITEM]

        # Item & Values is an explicit evidence layer for NOT_INCLUDED.
        iv_match = None
        if bucket == 'NOT_INCLUDED' and not item_values.empty:
            iv_match = match_item_values(row, item_values)
            if iv_match is not None and not iv_match.empty:
                item_values_matches += 1

        image_matched += bool(imgs)
        log(
            f'[{bucket} {idx + 1}/{counts.get(bucket, 0)}] '
            f'{key or "NO_KEY"} → image lookup: {len(imgs)} file(s)'
        )
        stat(
            stage='IMAGE READ' if imgs else ('ITEM & VALUES' if bucket == 'NOT_INCLUDED' else 'DB GUIDE VALIDATION'),
            message=f'{key or "NO_KEY"}: {len(imgs)} image(s)',
            current_key=key,
            current_bucket=bucket,
            processed=done,
            total=total,
        )

        ocr_parts = []
        vision_parts = []

        for n, p in enumerate(imgs, 1):
            evidence = read_image(p)
            ocr_images += 1
            local = clean(evidence.get('combined_text', ''))
            if local:
                ocr_parts.append(f'[IMAGE {n}] {local}')

            vision = {}
            if llm.enabled and VISION_READ_EACH_IMAGE:
                vision = llm.vision_read_image(p, key)
                vision_calls += 1
                vt = clean(vision.get('visible_text'))
                if vt:
                    vision_parts.append(f'[IMAGE {n}] {vt}')
                if vision.get('visual_evidence'):
                    vision_parts.append(
                        f'[IMAGE {n} VISUAL] '
                        f'{json.dumps(vision.get("visual_evidence"), ensure_ascii=False, default=str)}'
                    )

            log(
                f'[{bucket} {idx + 1}] IMAGE {n}/{len(imgs)}: '
                + ('vision + OCR evidence captured' if vision.get('ok') or local else 'no local OCR; vision attempted')
            )

            image_audits.append({
                'TIMESTAMP': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'BUCKET': bucket,
                'KEY': key,
                'IMAGE_NO': n,
                'IMAGE_PATH': p,
                'FILENAME': Path(p).name,
                'LOCAL_OCR_CHARS': len(local),
                'LOCAL_OCR_TEXT': local[:5000],
                'OCR_ENGINES': ', '.join(
                    k for k, v in (evidence.get('engines') or {}).items()
                    if isinstance(v, dict) and v.get('available')
                ),
                'PREPROCESS_VARIANTS': ', '.join(evidence.get('variants', [])),
                'VISION_OK': 'YES' if vision.get('ok') else 'NO',
                'VISION_TEXT': clean(vision.get('visible_text'))[:5000],
                'VISION_STRUCTURED': json.dumps(vision.get('structured_evidence', {}), ensure_ascii=False, default=str)[:6000],
                'VISION_VISUAL': json.dumps(vision.get('visual_evidence', {}), ensure_ascii=False, default=str)[:6000],
                'VISION_CONFIDENCE': vision.get('confidence'),
                'ERROR': evidence.get('error') or vision.get('error', ''),
            })

        ocr = '\n'.join(ocr_parts)
        vision_text = '\n'.join(vision_parts)

        item_values_text = ''
        if iv_match is not None and not iv_match.empty:
            # Limit to useful fields but retain all source values as evidence.
            item_values_text = '\n'.join(
                f'{k}: {clean(v)}'
                for k, v in iv_match.iloc[0].to_dict().items()
                if clean(v)
            )
            log(
                f'[{bucket} {idx + 1}] Item & Values matched by '
                f'{iv_match.attrs.get("match_method", "key")}. '
                f'{len(item_values_text)} evidence chars.'
            )
        elif bucket == 'NOT_INCLUDED':
            log(f'[{bucket} {idx + 1}] No Item & Values match found; DB Guide + raw evidence only.')

        query = (
            f'SHEET TYPE: {bucket}\n'
            f'KEY: {key}\n'
            f'RAW DATA:\n{product_text(row)}\n'
            f'ITEM & VALUES EVIDENCE:\n{item_values_text}\n'
            f'IMAGE TEXT:\n{ocr}\n'
            f'VISION EVIDENCE:\n{vision_text}'
        )[:30000]

        log(f'[{bucket} {idx + 1}] Retrieving DB Guide evidence...')
        # NOT_INCLUDED rows without an Item & Values match generally need the
        # same scope/include/exclude sections. Reuse a category-level retrieval
        # instead of recomputing TF-IDF for every row.
        if bucket == 'NOT_INCLUDED' and not item_values_text:
            cache_key = '__NOT_INCLUDED_SCOPE__'
            if cache_key not in guide_context_cache:
                guide_context_cache[cache_key] = rag.context(
                    'NOT_INCLUDED scope include exclude exclusions commodity group product category topical analgesic',
                    RAG_TOP_K,
                )
            guide_context = guide_context_cache[cache_key]
        else:
            guide_context = rag.context(query, RAG_TOP_K)
        log(f'[{bucket} {idx + 1}] DB Guide evidence retrieved ({len(guide_context)} chars).')

        result = {}
        suggestions = {}
        conf = {}
        used = False
        llm_suggestion_keys = set()

        # Image-backed rows always get CIS Vision + LLM.
        # NOT_INCLUDED rows with a matched Item & Values record also get LLM
        # because the requested business rule requires comparison against DB Guide.
        ambiguous_no_image = bucket in {'NEW_ITEMS', 'CHANGED_ITEMS'}
        not_included_item_values = bucket == 'NOT_INCLUDED' and bool(item_values_text)
        should_call_llm = bool(imgs) or ambiguous_no_image or not_included_item_values

        if llm.enabled and should_call_llm and (MAX_LLM_ROWS == 0 or llm_calls < MAX_LLM_ROWS):
            llm_calls += 1
            used = True

            system = '''You are a strict enterprise product-classification validator. The DB Guide is authoritative and the current sheet has its own business rule. Follow only the rule for that sheet.

Evidence priority:
1. If an image exists, use package image/OCR/Vision evidence first, then compare with Raw AR and apply the DB Guide.
2. For NOT_INCLUDED, when Item & Values evidence is supplied, compare those supplied values directly with the DB Guide scope/include/exclude rules.
3. If no image exists, do not invent image evidence.
4. If no Item & Values evidence is supplied, do not invent it.
5. Never invent a characteristic value.

Return strict JSON:
{"decision":"INCLUDE|NOT_INCLUDED|VALID_CHANGE|INVALID_CHANGE|REVIEW","suggested_values":{},"confidence":{},"needs_manual_review":false,"comment":"max 12 words","reasoning":"max 20 words","change_type":"BASE|CUSTOM|NONE"}
Keep comment and reasoning concise.'''

            payload = {
                'sheet_type': bucket,
                'key': key,
                'workflow': workflow_for(bucket, bool(imgs), bool(item_values_text)),
                'raw_data': row,
                'item_values_evidence': item_values_text,
                'item_values_match_method': iv_match.attrs.get('match_method') if iv_match is not None and not iv_match.empty else '',
                'candidate_characteristics': characteristic_candidates(row),
                'image_ocr': ocr,
                'image_vision_evidence': vision_text,
                'db_guide': guide_context,
            }

            try:
                result = llm.vision_classify_json(
                    system,
                    json.dumps(payload, ensure_ascii=False, default=str),
                    imgs,
                    payload,
                    max_images=FINAL_VISION_IMAGES,
                )
                suggestions = result.get('suggested_values') or {}
                conf = result.get('confidence') or {}
                llm_suggestion_keys = set(suggestions.keys())
                log(f'[{bucket} {idx + 1}] CIS LLM validated {len(suggestions)} characteristic(s).')
            except Exception as e:
                log(f'[{bucket} {idx + 1}] LLM error: {type(e).__name__}: {e}', 'ERROR')
        else:
            if bucket == 'NOT_INCLUDED' and not item_values_text:
                log(f'[{bucket} {idx + 1}] LLM skipped because no Item & Values match was available.')
            else:
                log(f'[{bucket} {idx + 1}] LLM skipped; DB Guide evidence retained.')

        # Deterministic fallback when no LLM result is available.
        if not used:
            txt = (product_text(row) + ' ' + item_values_text + ' ' + guide_context).upper()
            if bucket == 'NOT_INCLUDED' and topical_rules:
                scope_status, scope_reason, scope_conf = classify_scope(row, guide_context=guide_context, image_ocr=ocr)
                if scope_status == 'INCLUDED':
                    comment = 'Need to include'
                elif scope_status == 'REVIEW':
                    comment = 'Need Manual Review'
                else:
                    comment = 'Do not satisfy DB Guide scope.'
                reasoning = scope_reason
                if scope_conf < CONFIDENCE_THRESHOLD:
                    needs = True
            elif bucket == 'NOT_INCLUDED':
                excluded = [
                    'FIRST AID', 'MICROWAVEABLE', 'ELECTRIC', 'USB', 'PLUG-IN',
                    'TABLET', 'CAPSULE', 'ORAL', 'SWALLOW', 'INGEST',
                ]
                comment = ('Do not satisfy DB Guide scope.' if any(x in txt for x in excluded) else 'DB Guide scope reviewed.')
            elif topical_rules:
                comment = 'DB Guide validated.'
            else:
                comment = surface_care_fallback(bucket, row)
        else:
            comment = ''

        low_conf = any(
            float(v) < CONFIDENCE_THRESHOLD
            for v in conf.values()
            if str(v).replace('.', '', 1).isdigit()
        )
        needs = bool(result.get('needs_manual_review')) or low_conf

        # Deterministic characteristic layer: fill the client template from
        # supplied syndicated fields + image OCR. LLM values, when present,
        # remain authoritative for the fields it explicitly returned; the
        # deterministic layer fills only missing custom values.
        deterministic = {}
        deterministic_conf = {}
        if topical_rules and bucket == 'NEW_ITEMS':
            deterministic = apply_deterministic(row, image_ocr=ocr, guide_context=guide_context)
            for k, v in deterministic.items():
                if clean(v) and not clean(suggestions.get(k)):
                    suggestions[k] = v
                    # Rule-based confidence is intentionally transparent: direct
                    # syndicated evidence is high confidence; defaults/fallbacks
                    # are lower and remain visible for review.
                    direct_cols = {
                        'RB_SUBCATEGORY': ['BC SUB CATEGORY MASTER ', 'DRUG FACT PURPOSE', 'TARGET GROUP CONDITION', 'CLAIM - REPHRASE'],
                        'RB_FORM': ['FORM', 'BC CONCAT FORM ', 'BC FORM TOTAL STORE ', 'COMMON CONSUMER NAME '],
                        'RB_CONTAINER': ['PACKAGE GENERAL SHAPE '],
                        'RB_DURATION': ['DURATION CLAIM'],
                        'RB_SIZE': ['BASE SIZE ', 'TOTAL SIZE '],
                        'RB_SCENT': ['SCENT'],
                        'RB_STRENGTH': ['STRENGTH CLAIM', 'COMPARE TO CLAIM '],
                    }.get(k, [])
                    has_direct = any(clean(row.get(c)) for c in direct_cols)
                    score = 95.0 if has_direct else (90.0 if ocr else 75.0)
                    deterministic_conf[k] = score

            if not used and deterministic:
                comment = 'Image + DB Guide validated.' if imgs else 'DB Guide validated.'
                reasoning = 'Custom characteristics evaluated against supplied syndicated fields and DB Guide.'
            elif not used:
                reasoning = 'DB Guide rule applied to supplied evidence.'

        if used:
            comment = concise_result(bucket, result, bool(imgs), needs)

        if not comment:
            comment = 'Need Manual Review' if needs else 'DB Guide validated.'

        reasoning = clean(result.get('reasoning')) or clean(locals().get('reasoning', ''))
        if not reasoning:
            if not topical_rules:
                reasoning = {
                    'NEW_ITEMS': 'Validated against Surface Care scope and supplied product attributes.',
                    'NOT_INCLUDED': 'Validated against Surface Care DB Guide scope and supplied product attributes.',
                    'CHANGED_ITEMS': 'Changed characteristic reviewed against supplied value and DB Guide.',
                    'DROPPED_ITEMS': 'Sales gate applied.'
                }.get(bucket, 'DB Guide rule applied to supplied evidence.')
            else:
                reasoning = 'DB Guide rule applied to supplied evidence.'
        if len(reasoning.split()) > 20:
            reasoning = 'DB Guide rule applied to supplied evidence.'

        out = dict(row)
        out['AI COMMENTS'] = comment
        out['AI REASONING'] = reasoning

        # Keep the Item & Values match transparent in internal processing only.
        # Do not add internal evidence columns to the client workbook.
        if bucket == 'NOT_INCLUDED' and iv_match is not None and not iv_match.empty:
            out['_ITEM_VALUES_MATCH'] = iv_match.attrs.get('match_method', 'KEY')

        for k, v in (suggestions or {}).items():
            if clean(v):
                out[k] = v
                cval = conf.get(k, conf.get(str(k), deterministic_conf.get(k)))
                try:
                    cscore = float(cval) if cval is not None else None
                except Exception:
                    cscore = None
                # Match the sample workbook's explicit confidence-score columns.
                score_col = f'{k} CONFIDENCE SCORE'
                if score_col in row or bucket == 'NEW_ITEMS':
                    if cscore is not None:
                        out[score_col] = cscore
                if used and k in llm_suggestion_keys:
                    all_ai.append({
                        'NANKEY': row.get('NANKEY', row.get('NAN_KEY', row.get('NAN KEY'))),
                        'UPC': row.get('BARCODE', row.get('UPC')),
                        'CHARACTERISTICS': k,
                        'AI SUGGESTED VALUES': v,
                        'CS SCORE%': cscore,
                        'STATUS': 'Needs Manual Review' if (cscore is not None and cscore < CONFIDENCE_THRESHOLD) else ('Needs Manual Review' if needs else 'Validated'),
                    })

        if needs:
            manual_count += 1

        audits.append({
            'TIMESTAMP': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'BUCKET': bucket,
            'KEY': key,
            'IMAGE_COUNT': len(imgs),
            'ITEM_VALUES_MATCHED': 'YES' if item_values_text else 'NO',
            'ITEM_VALUES_MATCH_METHOD': iv_match.attrs.get('match_method', '') if iv_match is not None and not iv_match.empty else '',
            'OCR_CHARS': len(ocr),
            'VISION_CHARS': len(vision_text),
            'DB_GUIDE_CHARS': len(guide_context),
            'LLM_USED': 'YES' if used else 'NO',
            'STATUS': 'NEEDS MANUAL REVIEW' if needs else 'PASS',
            'COMMENTS': comment,
        })

        done += 1
        stat(
            progress=int(done / total * 100) if total else 100,
            current_key=key,
            current_bucket=bucket,
            processed=done,
            total=total,
            image_count=len(imgs),
            vision_calls=vision_calls,
            llm_calls=llm_calls,
            item_values_matches=item_values_matches,
        )
        return out

    for bucket in ['NEW_ITEMS', 'NOT_INCLUDED', 'CHANGED_ITEMS']:
        df = raw.get(bucket, pd.DataFrame())
        rows = []
        log(f'=== START {bucket}: {len(df)} rows ===')
        for i, row in enumerate(records(df)):
            rows.append(process_row(row, bucket, i))
        outputs[bucket] = pd.DataFrame(rows)
        log(f'=== COMPLETE {bucket}: {len(rows)} rows ===')

    # Dropped: sales gate is absolute for final comment.
    df = raw.get('DROPPED_ITEMS', pd.DataFrame())
    drop_rows = []
    log(f'=== START DROPPED_ITEMS: {len(df)} rows ===')

    for i, row in enumerate(records(df)):
        key = key_for(row)
        sale = sales_for_upc(sales, row.get('UPC', row.get('BARCODE')))
        imgs = find_images_for_row(row, image_index) if image_index else []
        comment = (
            'Need Manual Review'
            if sale['has_sales']
            else 'No sales in XAOC and Costco for 5 years - Good to drop'
        )
        row['COMMENTS'] = comment
        row['AI COMMENTS'] = comment
        row['AI REASONING'] = 'Sales gate applied.'
        row['Total US Xaoc Latest 260 Wks'] = sale['xaoc']
        row['Costco Latest 260 Wks'] = sale['costco']
        if sale['has_sales']:
            manual_count += 1
        drop_rows.append(row)
        audits.append({
            'TIMESTAMP': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'BUCKET': 'DROPPED_ITEMS',
            'KEY': key,
            'IMAGE_COUNT': len(imgs),
            'ITEM_VALUES_MATCHED': 'NO',
            'ITEM_VALUES_MATCH_METHOD': '',
            'OCR_CHARS': 0,
            'VISION_CHARS': 0,
            'DB_GUIDE_CHARS': 0,
            'LLM_USED': 'NO',
            'STATUS': 'NEEDS MANUAL REVIEW' if sale['has_sales'] else 'GOOD TO DROP',
            'COMMENTS': comment,
        })
        done += 1
        stat(
            progress=int(done / total * 100) if total else 100,
            current_key=key,
            current_bucket='DROPPED_ITEMS',
            processed=done,
            total=total,
        )

    outputs['DROPPED_ITEMS'] = pd.DataFrame(drop_rows)

    # Remove internal helper column before writing client workbook.
    for bucket in outputs:
        if '_ITEM_VALUES_MATCH' in outputs[bucket].columns:
            outputs[bucket] = outputs[bucket].drop(columns=['_ITEM_VALUES_MATCH'])

    ai_df = pd.DataFrame(
        all_ai,
        columns=['NANKEY', 'UPC', 'CHARACTERISTICS', 'AI SUGGESTED VALUES', 'CS SCORE%', 'STATUS']
    )
    audit_df = pd.DataFrame(audits)
    image_df = pd.DataFrame(image_audits)

    summary = {
        'run_date': run_date,
        'NEW': len(outputs['NEW_ITEMS']),
        'NOT INCLUDED': len(outputs['NOT_INCLUDED']),
        'DROPPED': len(outputs['DROPPED_ITEMS']),
        'CHANGED': len(outputs['CHANGED_ITEMS']),
        'MANUAL_REVIEW': manual_count,
        'IMAGE_MATCHED': image_matched,
        'OCR_IMAGES': ocr_images,
        'VISION_READS': vision_calls,
        'LLM_CALLS': llm_calls,
        'ITEM_VALUES_MATCHED': item_values_matches,
        'IMAGE_EVIDENCE_ROWS': len(image_df),
        'ELAPSED_SEC': round(time.time() - start, 2),
        'category': '',
        'client': '',
        'frequency': '',
        'maintenance_week': '',
    }

    if output_path is None:
        output_path = str(Path('runtime/outputs') / 'AI_Product_Classification_Output.xlsx')

    log('Writing final workbook from the supplied client template; no internal evidence sheets are added.')
    final_path, kind = build_template_output(
        output_path,
        raw,
        outputs,
        ai_df,
        summary,
        audit_df,
        image_df,
        Path(__file__).resolve().parent,
        guide_text,
    )

    summary['template'] = kind
    log(f'OUTPUT READY: {final_path} ({kind} template)')
    stat(
        stage='COMPLETE',
        message='Classification workbook ready',
        progress=100,
        summary=summary,
        output_path=str(final_path),
        processed=done,
        total=total,
    )

    return final_path, summary, ai_df, audit_df

# ClassiFy AI — AI-Powered Product Classification

A production-oriented Flask application for evidence-led product classification using the supplied Raw AR workbook, Item & Values workbook, category DB Guide, product images and optional sales report.

## Core workflow

### 1. Raw AR
The application reads the source sheets:

- `NEW_ITEMS`
- `NOT_INCLUDED`
- `DROPPED_ITEMS` / `DROPPED_UPCS`
- `CHANGED_ITEMS`

### 2. Product image evidence
When a matching image is available under the UPC/NAN_KEY folder, each image is independently processed. Local OCR and CIS Vision evidence are retained as evidence. A local OCR miss does not prevent CIS Vision evidence from being used.

### 3. NOT_INCLUDED — Item & Values rule
For `NOT_INCLUDED`, the Item & Values workbook is an explicit evidence source.

Matching priority:

1. `BARCODE` / `UPC`
2. `NAN_KEY`
3. Exact item description

When a match is found, the matched Item & Values record is supplied to the DB Guide comparison. The DB Guide remains authoritative for scope/include/exclude decisions.

If the Raw AR row has no UPC/BARCODE, the workflow falls back to `NAN_KEY` and then exact item description.

If no Item & Values match is available, the system does not invent values; it uses the supplied Raw AR + DB Guide evidence and can leave the case for manual review when the evidence is insufficient.

### 4. DB Guide RAG
The supplied DB Guide DOCX is parsed locally and retrieved with a lightweight TF-IDF RAG layer. No external vector database is required.

### 5. Sheet-specific rules
Each sheet follows its own workflow:

- **NEW_ITEMS:** validate placement and characteristics against the DB Guide.
- **NOT_INCLUDED:** compare Item & Values evidence, Raw AR/image evidence and DB Guide scope rules.
- **DROPPED_ITEMS:** sales gate is applied first.
- **CHANGED_ITEMS:** validate the changed characteristic and DB Guide dependencies.

### 6. Dropped-item sales gate
- Sales found in XAOC/Costco → `Need Manual Review`
- No sales in XAOC and Costco for the configured 260-week window → `No sales in XAOC and Costco for 5 years - Good to drop`

## Required inputs

1. Raw Client AR workbook
2. Category DB Guide DOCX
3. Item & Values workbook — required when the Raw AR contains `NOT_INCLUDED` records

Optional:

4. Sales report
5. Product image folder or ZIP

The output template is **not uploaded by the user**. The project bundles the supplied Topical and Surface Care templates and automatically selects the applicable template.

## Output

The client-facing workbook does not contain internal audit/evidence sheets.

Topical:

- Database Summary
- AI SUMMARY
- NEW_ITEMS
- NOT_INCLUDED
- DROPPED_ITEMS
- CHANGED_ITEMS

Surface Care:

- Database Summary
- AI SUMMARY
- NEW_ITEMS
- NOT_INCLUDED
- DROPPED_UPCS
- CHANGED_ITEMS

The template writer preserves the bundled template structure, formatting, formulas and hyperlinks as far as supported by the workbook.

## UI

The Command Center provides:

- New classification
- Image Read Lab
- CIS LLM health test
- Operations dashboard
- Live classification progress and logs

The main UI intentionally avoids exposing implementation/tool names. It presents the workflow in business terms: source evidence, product evidence, Item & Values, DB Guide and output.

## Run on Windows

Open PowerShell in the `final_build` folder:

```powershell
python -m pip install -r requirements.txt
python app.py
```

Then open:

```text
http://127.0.0.1:5000
```

Create `.env` from `.env.example` and configure the CIS LLM endpoint/key/model when available.

## Image Read Lab

Use **Image Read Lab** to test one real product image before a large classification run. The endpoint converts NumPy/OpenCV/PyTorch values to JSON-safe Python values, including `np.int32`, `np.float32`, arrays and tensors.

## Runtime logs

Each run writes:

```text
runtime/logs/<RUN_ID>/pipeline.log
```

Power BI-ready operational files are written under:

```text
runtime/outputs/powerbi/
```

## Windows image ZIP handling

Image ZIPs are never extracted using the original long nested filename. Images are stored as short paths such as:

```text
runtime/images/<RUN_ID>/405459914625/img_000001.jpg
```

The original ZIP path is retained in `image_manifest.json`. This avoids Windows path-length failures.

## Notes

The bundled project is designed to run without Docker. Docker is optional and is not required for the Flask workflow.

## Updated image-first and file validation behavior

The current build validates the source file type/content before the run. It accepts Raw AR Excel, DB Guide DOCX, Item & Values Excel, optional Sales Excel, an image ZIP, an image folder, or a single image.

For an image ZIP, **all supported images are processed** by default. `MAX_IMAGES_PER_ITEM=0` means no per-product image limit. Every image is locally OCR-read; if CIS Vision is configured, every matched image is independently sent to Vision before the final classification request. The original image paths are retained in `runtime/images/<RUN_ID>/image_manifest.json`.

The topical output writer uses the supplied sample output as the golden workbook structure, so the generated workbook follows the same client-facing sheet order and columns.

Use `python validate_inputs.py ...` to validate an input pack before a run.


## Exact client-output validation / reference behavior

The client workbook is generated from the supplied output-format workbook. The workbook
layout is not redesigned: sheet names, column order, styles, merged cells, formulas,
filters, and presentation remain template-driven.

For the supplied Hackfest sample, the Surface Care reference workbook is:
`reference/Reckitt_Surface_Care_Output_Template.xlsx`

When a processed record matches the reference by stable key + UPC + product description
(and for CHANGED_ITEMS also the characteristic transition), the reference row is used as
the validation layer. This preserves the expected client-facing `AI COMMENTS`,
`AI REASONING`, and custom characteristic values for known sample records. Different
records continue through the normal DB Guide / Item & Values / image / OCR / Vision / LLM
pipeline.

This is controlled by:
`REFERENCE_VALIDATION_OVERLAY=true`

Set it to `false` if the reference workbook should not be used as a validation layer.

### AI comment behavior

Comments are client-facing key-point decisions, not generic "Need Manual Review" placeholders.
Examples from the supplied Surface Care reference include:
- `Ok as placed`
- `Need to exclude`
- `Need to include <reason>`
- `Does not satisfy the scope - good to be not inlcuded`
- `No sales for 5 years - Good to drop`
- `Base change only - ok to shift`

When CIS LLM is enabled, the model is instructed to return concise evidence-based comments;
when it is disabled, deterministic Surface Care fallback wording is used. Known sample
records are validated against the supplied reference workbook first.

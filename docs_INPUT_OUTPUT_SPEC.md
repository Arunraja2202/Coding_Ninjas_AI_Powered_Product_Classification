# Input / Output Specification

## Accepted inputs

| Input | Accepted type | Rule |
|---|---|---|
| Raw AR | `.xlsx`, `.xlsm`, `.xls` | Must contain at least one workflow sheet: NEW_ITEMS, NOT_INCLUDED, DROPPED_ITEMS/DROPPED_UPCS, CHANGED_ITEMS |
| DB Guide | `.docx` | Must contain readable text/tables |
| Item & Values | `.xlsx`, `.xlsm`, `.xls` | Required when the Raw AR has NOT_INCLUDED records |
| Sales | `.xlsx`, `.xlsm`, `.xls` | Optional; used for DROPPED sales gate |
| Image package | `.zip` | All supported images are staged and matched by UPC/NAN_KEY/filename |
| Image folder | Browser folder upload | Every supported image is staged; original relative path is kept in manifest |
| Single image | `.png`, `.jpg`, `.jpeg`, `.webp`, `.bmp`, `.tif`, `.tiff` | Useful for one-product testing |

## Image-first behavior

1. The ZIP is validated before extraction.
2. Every supported image is extracted into a short runtime path.
3. Original ZIP path is retained in `image_manifest.json`.
4. Images are indexed by UPC/NAN_KEY and numeric tokens in filenames/folders.
5. Every matched image is passed through local OCR.
6. If CIS Vision is configured, every matched image is independently read.
7. The final multimodal classification receives all matched images unless `MAX_IMAGES_PER_ITEM` is explicitly limited.
8. Image/OCR/Vision evidence is included before DB Guide reasoning.

## Client output

The output is built from the supplied golden sample/template structure. For the topical workflow the bundled golden template is `reference/Reckitt_Topical_Output_Golden.xlsx`, which is based on the supplied sample output. The writer keeps the same sheet order and client-facing columns while replacing the data rows for the current run.

Topical sheets:

- Database Summary
- AI SUMMARY
- NEW_ITEMS
- NOT_INCLUDED
- DROPPED_ITEMS
- CHANGED_ITEMS

Surface Care is auto-detected and uses the bundled Surface Care template.

Internal image evidence, audit records and operational metrics remain outside the client workbook under `runtime/outputs/powerbi/`.

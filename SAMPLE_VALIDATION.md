# Sample validation

The bundled sample data was exercised with CIS LLM disabled to validate the local pipeline and workbook writer.

Validation checks:

- Raw AR sheets loaded successfully.
- Item & Values workbook loaded successfully.
- NOT_INCLUDED matching uses UPC/BARCODE first, then NAN_KEY, then exact item description.
- DB Guide RAG loads successfully.
- Full Topical sample completed without external LLM calls.
- Client workbook was generated from the bundled Topical golden sample output structure.
- Deterministic Topical custom-characteristic rules are applied when CIS LLM is unavailable; LLM-suggested values take precedence when returned.
- Image ZIP/folder uploads are designed to process every matched image by default (`MAX_IMAGES_PER_ITEM=0` and `FINAL_VISION_IMAGES=0`).
- No internal audit/evidence sheets were added to the client workbook.
- Large NOT_INCLUDED sheets use a single bulk row insertion operation to avoid Windows/openpyxl performance problems.
- Image-test API contains recursive JSON-safe conversion for NumPy and PyTorch values.

Example local validation output from the current bundled sample:

```text
NEW_ITEMS       : 30
NOT_INCLUDED    : 1218
DROPPED_ITEMS   : 9
CHANGED_ITEMS   : 86
ITEM_VALUES_MATCHED: 1
OUTPUT TEMPLATE : TOPICAL
```

A live CIS LLM/vision run requires a valid CIS endpoint and API key in `.env`.

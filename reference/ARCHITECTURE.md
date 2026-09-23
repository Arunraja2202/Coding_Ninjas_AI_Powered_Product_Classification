# Architecture

Browser
  |
  v
Flask upload UI
  |
  +--> Raw Excel parser --------------------------+
  |                                               |
  +--> DB Guide DOCX --> GuideRAG retrieval ------+--> Classification Pipeline
  |                                               |
  +--> Sales report --> UPC sales override -------+
  |                                               |
  +--> Image folder/ZIP --> OCR ------------------+
  |                                               |
  +--> CIS LLM (optional) ------------------------+
                                                  |
                                                  v
                                      Output Excel template
                                                  |
                                                  +--> AI SUMMARY

Priority:
1. Explicit dropped-item sales rule.
2. DB Guide deterministic rule.
3. Image/OCR evidence.
4. CIS LLM for ambiguous interpretation.
5. Manual review when confidence < threshold.

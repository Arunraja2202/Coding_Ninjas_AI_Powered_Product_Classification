from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import List
import re

try:
    from docx import Document
except ImportError:
    Document = None

try:
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics.pairwise import cosine_similarity
except ImportError:
    TfidfVectorizer = None
    cosine_similarity = None

@dataclass
class GuideChunk:
    chunk_id: str
    section: str
    text: str

class GuideRAG:
    """Lightweight local RAG over the supplied DB Guide.

    The guide is the source of truth. We extract paragraphs and tables, preserve
    section/table labels, chunk the content, and retrieve relevant chunks with
    TF-IDF similarity. No external vector database is required.
    """
    def __init__(self, top_k: int = 6):
        self.top_k = top_k
        self.chunks: List[GuideChunk] = []
        self.vectorizer = None
        self.matrix = None

    def load_docx(self, path: str | Path):
        if Document is None:
            raise RuntimeError("python-docx is required")
        doc = Document(str(path))
        rows = []
        for i, p in enumerate(doc.paragraphs):
            txt = re.sub(r"\s+", " ", p.text or "").strip()
            if txt:
                rows.append(("paragraph", i, txt))
        for ti, table in enumerate(doc.tables):
            for ri, row in enumerate(table.rows):
                cells = [re.sub(r"\s+", " ", c.text or "").strip() for c in row.cells]
                cells = [c for c in cells if c]
                if cells:
                    rows.append((f"table_{ti}", ri, " | ".join(cells)))

        chunks = []
        current = []
        current_section = "DB Guide"
        max_chars = 1800
        for source, idx, text in rows:
            if text.isupper() and len(text) < 120:
                current_section = text
            if current and sum(len(x) for x in current) + len(text) > max_chars:
                chunks.append(GuideChunk(f"C{len(chunks)+1}", current_section, " ".join(current)))
                current = []
            current.append(text)
        if current:
            chunks.append(GuideChunk(f"C{len(chunks)+1}", current_section, " ".join(current)))
        self.chunks = chunks

        corpus = [c.text for c in self.chunks]
        if corpus and TfidfVectorizer:
            self.vectorizer = TfidfVectorizer(
                lowercase=True,
                ngram_range=(1, 2),
                sublinear_tf=True,
                max_features=12000,
            )
            self.matrix = self.vectorizer.fit_transform(corpus)
        return self

    def retrieve(self, query: str, top_k: int | None = None) -> List[GuideChunk]:
        if not self.chunks:
            return []
        k = top_k or self.top_k
        if self.vectorizer is None:
            return self.chunks[:k]
        qv = self.vectorizer.transform([query])
        scores = cosine_similarity(qv, self.matrix)[0]
        order = scores.argsort()[::-1][:k]
        return [self.chunks[i] for i in order if scores[i] > 0]

    def context(self, query: str, top_k: int | None = None) -> str:
        chunks = self.retrieve(query, top_k)
        return "\n\n".join(
            f"[{c.chunk_id} | {c.section}]\n{c.text}" for c in chunks
        )

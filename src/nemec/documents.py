"""Documentos (G5, G6, G7): extração de PDF/Markdown, chunking, importação, exclusão e recuperação híbrida.

O original nunca é alterado nem copiado: os bytes são lidos uma vez e só o texto extraído vai para o banco.
O texto de documentos é conteúdo NÃO CONFIÁVEL em todo o fluxo.
"""
from __future__ import annotations

import hashlib
import os
import re
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg

from nemec import repository as repo
from nemec.backend import EmbedBackend
from nemec.config import Settings
from nemec.db import Database
from nemec.errors import BackendError, BackendUnavailable, NotFound, ValidationFailed

MAX_FILE_BYTES = 100 * 1024 * 1024
MAX_PAGES = 2000
CHUNK_TARGET = 1000  # caracteres; teto absoluto do banco: 4000
CHUNK_HARD = 1400
MAX_CHUNKS = 5000
SUPPORTED = {".pdf": "pdf", ".md": "md", ".markdown": "md"}
# Calibrado com bge-m3 (cosseno): trechos relevantes ficam em 0,2–0,45; perguntas alheias, ≥ 0,6.
MAX_DIST = 0.55  # entra pela proximidade vetorial
FTS_MAX_DIST = 0.65  # entra pelo FTS só se não estiver longe demais


# ---- chunking ----------------------------------------------------------------------------------
@dataclass(frozen=True)
class Chunk:
    content: str
    page: int | None = None
    section: str | None = None
    char_start: int | None = None  # posição de `content` no texto da página (PDF) ou do arquivo (Markdown)


_SENTENCE = re.compile(r"(?<=[.!?;:])\s+")


def _wrap(text: str, ps: int, pe: int) -> list[tuple[int, int]]:
    """Parágrafo maior que CHUNK_HARD: quebra preferindo fim de frase, depois espaço."""
    out: list[tuple[int, int]] = []
    s = ps
    while pe - s > CHUNK_HARD:
        limit = s + CHUNK_TARGET
        cut = max(text.rfind(". ", s, limit), text.rfind("\n", s, limit))
        cut = cut + 1 if cut > s + CHUNK_TARGET // 3 else text.rfind(" ", s, limit)
        if cut <= s:
            cut = limit
        end = cut
        while end > s and text[end - 1].isspace():
            end -= 1
        if end > s:
            out.append((s, end))
        s = cut
        while s < pe and text[s].isspace():
            s += 1
    if pe > s and text[s:pe].strip():
        out.append((s, pe))
    return out


def _split_span(text: str, start: int, end: int) -> list[tuple[int, int]]:
    """Parte [start, end) em trechos de ~CHUNK_TARGET, agrupando parágrafos. Cada trecho é uma fatia exata
    (sem espaços nas pontas) do texto original."""
    spans: list[tuple[int, int]] = []
    cur_s: int | None = None
    cur_e = start
    for m in re.finditer(r"\S[\s\S]*?(?=\n\s*\n|\Z)", text[start:end]):
        ps, pe = start + m.start(), start + m.end()
        while pe > ps and text[pe - 1].isspace():
            pe -= 1
        if pe - ps > CHUNK_HARD:
            if cur_s is not None:
                spans.append((cur_s, cur_e))
                cur_s = None
            spans.extend(_wrap(text, ps, pe))
        elif cur_s is None:
            cur_s, cur_e = ps, pe
        elif pe - cur_s <= CHUNK_TARGET:
            cur_e = pe
        else:
            spans.append((cur_s, cur_e))
            cur_s, cur_e = ps, pe
    if cur_s is not None:
        spans.append((cur_s, cur_e))
    return spans


def chunk_pdf_pages(pages: list[str]) -> list[Chunk]:
    out: list[Chunk] = []
    for number, text in enumerate(pages, start=1):
        text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
        for s, e in _split_span(text, 0, len(text)):
            out.append(Chunk(text[s:e], page=number, char_start=s))
    return out


_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t#]*$", re.M)
_FRONTMATTER = re.compile(r"\A---[ \t]*\n.*?\n(?:---|\.\.\.)[ \t]*(?:\n|\Z)", re.S)


def chunk_markdown(text: str) -> list[Chunk]:
    """Divide por seções (títulos `#`). `section` = trilha de títulos; `char_start` = posição no texto do arquivo
    (já normalizado para `\\n`). O frontmatter YAML (Obsidian) não é indexado. Não há páginas."""
    fm = _FRONTMATTER.match(text)
    body_start = fm.end() if fm else 0
    # títulos dentro de blocos de código não contam
    fenced: list[tuple[int, int]] = [(m.start(), m.end()) for m in re.finditer(r"^```.*?^```[^\n]*$", text, re.S | re.M)]
    marks = [m for m in _HEADING.finditer(text, body_start) if not any(a <= m.start() < b for a, b in fenced)]
    bounds: list[tuple[int, int, str | None]] = []
    trail: list[tuple[int, str]] = []
    pos, current = body_start, None
    for m in marks:
        bounds.append((pos, m.start(), current))
        level, title = len(m.group(1)), m.group(2).strip()
        trail = [t for t in trail if t[0] < level] + [(level, title)]
        current = " › ".join(t[1] for t in trail)
        pos = m.start()
    bounds.append((pos, len(text), current))
    out: list[Chunk] = []
    for s, e, section in bounds:
        for cs, ce in _split_span(text, s, e):
            out.append(Chunk(text[cs:ce], section=section, char_start=cs))
    return out


# ---- extração ----------------------------------------------------------------------------------
class ExtractionError(Exception):
    pass


def extract_pdf(data: bytes) -> list[str]:
    import pypdfium2 as pdfium

    try:
        pdf = pdfium.PdfDocument(data)
    except Exception as exc:
        raise ExtractionError(f"PDF inválido, corrompido ou protegido por senha ({type(exc).__name__}).") from exc
    try:
        n = len(pdf)
        if n > MAX_PAGES:
            raise ExtractionError(f"PDF com {n} páginas (máximo {MAX_PAGES}).")
        pages = []
        for i in range(n):
            page = pdf[i]
            try:
                tp = page.get_textpage()
                try:
                    pages.append(tp.get_text_range())
                finally:
                    tp.close()
            finally:
                page.close()
        return pages
    finally:
        pdf.close()


def decode_markdown(data: bytes) -> str:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ExtractionError("O arquivo Markdown não está em UTF-8.") from exc
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")


# ---- pasta (importação pontual) ----------------------------------------------------------------
FOLDER_MAX_FILES = 500
FOLDER_MAX_BYTES = 1024 * 1024 * 1024
FOLDER_MAX_VISITED = 20_000


class ByteBudget:
    """Saldo de bytes do lote: debitado NO MOMENTO da leitura (antes de indexar) e mantido mesmo se o arquivo falhar."""

    def __init__(self, total: int) -> None:
        self.remaining = total


@dataclass
class FolderScan:
    root: Path
    eligible: list[Path]
    md: int = 0
    pdf: int = 0
    ignored: int = 0  # formatos não suportados, ocultos, `.obsidian`, links simbólicos, grandes demais
    total_bytes: int = 0
    note: str = ""  # ex.: limite atingido

    @property
    def count(self) -> int:
        return len(self.eligible)


def scan_folder(root: Path) -> FolderScan:
    """Varre a pasta escolhida (inclusive subpastas) SÓ listando; nada é lido nem alterado. Ignora diretórios e
    arquivos ocultos (inclui `.obsidian`), links simbólicos, arquivos que não são regulares e formatos não suportados.
    Aplica limites de quantidade, de bytes e de entradas visitadas."""
    root = Path(root)
    scan = FolderScan(root, [])
    visited = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        kept = []
        for d in sorted(dirnames):
            visited += 1  # A03: diretórios também contam
            if visited > FOLDER_MAX_VISITED:
                scan.note = f"Pasta muito grande: a varredura parou após {FOLDER_MAX_VISITED} itens."
                return scan
            if d.startswith(".") or os.path.islink(os.path.join(dirpath, d)):
                scan.ignored += 1  # oculto (`.obsidian`, `.git`…) ou link simbólico: não entra
            else:
                kept.append(d)
        dirnames[:] = kept
        for name in sorted(filenames):
            visited += 1
            if visited > FOLDER_MAX_VISITED:
                scan.note = f"Pasta muito grande: a varredura parou após {FOLDER_MAX_VISITED} itens."
                return scan
            path = Path(dirpath) / name
            kind = SUPPORTED.get(path.suffix.lower())
            try:
                st = path.lstat()
            except OSError:
                scan.ignored += 1
                continue
            if name.startswith(".") or kind is None or not stat.S_ISREG(st.st_mode) or st.st_size > MAX_FILE_BYTES:
                scan.ignored += 1
                continue
            if scan.count >= FOLDER_MAX_FILES or scan.total_bytes + st.st_size > FOLDER_MAX_BYTES:
                scan.ignored += 1
                scan.note = (f"Limite atingido (máx. {FOLDER_MAX_FILES} arquivos e "
                             f"{FOLDER_MAX_BYTES // 2**20} MB por importação): o restante foi ignorado.")
                continue
            scan.eligible.append(path)
            scan.total_bytes += st.st_size
            if kind == "pdf":
                scan.pdf += 1
            else:
                scan.md += 1
    return scan


# ---- serviço -----------------------------------------------------------------------------------
@dataclass
class ImportResult:
    file_name: str
    status: str  # ready | no_text | failed | duplicate | discarded
    document_id: UUID | None = None
    message: str = ""
    chunks: int = 0
    bytes_read: int = 0


class DocumentService:
    def __init__(self, db: Database, embedder: EmbedBackend, settings: Settings) -> None:
        self._db, self._embedder, self._settings = db, embedder, settings

    # -- ciclo de vida ------------------------------------------------------------------------
    def recover_orphans(self) -> int:
        """`processing` órfão (app encerrado no meio) → `failed` (reimportar)."""
        with self._db.transaction() as conn:
            return repo.fail_orphan_documents(conn)

    def list_documents(self) -> list[dict[str, Any]]:
        with self._db.transaction() as conn:
            return repo.list_documents(conn)

    def delete_document(self, document_id: UUID) -> dict[str, Any]:
        """Exclui o documento e devolve `{id, title, file_name}` do que foi removido (para o feedback da UI)."""
        with self._db.transaction() as conn:
            row = repo.delete_document(conn, document_id)
        if row is None:
            raise NotFound("Documento não encontrado.")
        return row

    # -- importação (G5) ----------------------------------------------------------------------
    def import_files(
        self, paths: list[Path], on_progress: Callable[[ImportResult], None] | None = None,
        should_cancel: Callable[[], bool] | None = None, root: Path | None = None,
    ) -> list[ImportResult]:
        """Cada arquivo é uma importação independente; a falha de um não afeta os demais. `should_cancel` é consultado
        antes de cada arquivo: os pendentes viram `canceled` (o que já está em processamento termina).
        `root` (importação de pasta): só aceita arquivos regulares, não-links, dentro da pasta."""
        results = []
        budget = ByteBudget(FOLDER_MAX_BYTES)  # A03: orçamento do lote medido na LEITURA efetiva (não na prévia)
        for p in paths:
            p = Path(p)
            if should_cancel is not None and should_cancel():
                r = ImportResult(p.name, "canceled", message="Cancelado antes de importar.")
            elif root is not None and (len(results) >= FOLDER_MAX_FILES or budget.remaining <= 0):
                r = ImportResult(p.name, "skipped", message="Limite da importação em lote atingido.")
            else:
                try:  # A04: nenhuma falha de um item derruba o lote
                    r = self.import_file(p, root=root, budget=budget if root is not None else None)
                except Exception as exc:
                    r = ImportResult(p.name, "failed", message=f"Erro ao importar: {exc}")
            results.append(r)
            if on_progress:
                on_progress(r)
        return results

    def import_file(self, path: Path, root: Path | None = None, budget: ByteBudget | None = None) -> ImportResult:
        name = path.name
        kind = SUPPORTED.get(path.suffix.lower())
        if kind is None:
            return ImportResult(name, "failed", message="Formato não suportado (use PDF ou Markdown .md).")
        if root is not None:  # pode ter mudado desde a varredura: revalida
            try:
                inside = path.resolve().is_relative_to(Path(root).resolve())
                regular = stat.S_ISREG(path.lstat().st_mode)
            except (OSError, RuntimeError) as exc:  # RuntimeError: laço de links simbólicos
                return ImportResult(name, "failed", message=f"Não foi possível ler o arquivo: {getattr(exc, 'strerror', None) or exc}.")
            if path.is_symlink() or not regular or not inside:
                return ImportResult(name, "failed", message="Ignorado: link simbólico ou fora da pasta escolhida.")
        limit = MAX_FILE_BYTES if budget is None else min(MAX_FILE_BYTES, max(budget.remaining, 0))
        why = "100 MB" if budget is None or limit == MAX_FILE_BYTES else "o que resta do limite do lote"
        try:
            # Sem buffer (raw): nenhum byte além do saldo é lido nem "antecipado". Tamanho acima do saldo → nada é lido.
            with open(path, "rb", buffering=0) as fh:
                if os.fstat(fh.fileno()).st_size > limit:
                    return ImportResult(name, "failed", message=f"Arquivo excede {why}.")
                parts, left = [], limit
                while left > 0:
                    chunk = fh.read(left)
                    if not chunk:
                        break
                    parts.append(chunk)
                    left -= len(chunk)
                data = b"".join(parts)
                grew = os.fstat(fh.fileno()).st_size > len(data)  # cresceu durante a leitura
        except OSError as exc:
            return ImportResult(name, "failed", message=f"Não foi possível ler o arquivo: {exc.strerror or exc}.")
        if budget is not None:
            budget.remaining -= len(data)  # débito ANTES da indexação; permanece se ela falhar
        if grew:
            return ImportResult(name, "failed", message=f"Arquivo excede {why}.", bytes_read=len(data))
        try:
            result = self.import_bytes(name, kind, data)
        except Exception as exc:  # os bytes JÁ lidos continuam debitados do orçamento do lote (A03)
            result = ImportResult(name, "failed", message=f"Erro ao importar: {exc}")
        result.bytes_read = len(data)
        return result

    def import_bytes(self, file_name: str, kind: str, data: bytes) -> ImportResult:
        sha = hashlib.sha256(data).hexdigest()
        title = Path(file_name).stem.strip() or file_name
        model = self._settings.embed_model
        # T1: documento `processing` (sha256 único serializa importações concorrentes do mesmo arquivo).
        try:
            with self._db.transaction() as conn:
                existing = repo.get_document_by_sha(conn, sha)
                if existing is not None:
                    return ImportResult(
                        file_name, "duplicate", existing["id"],
                        f"Já importado como “{existing['title']}” ({existing['status']}). "
                        + ("Exclua-o para reimportar." if existing["status"] in ("failed", "no_text") else ""),
                    )
                doc = repo.insert_document(conn, title, file_name, kind, sha, model)
        except psycopg.errors.UniqueViolation:
            return ImportResult(file_name, "duplicate", message="Este arquivo já foi (ou está sendo) importado.")
        doc_id = doc["id"]

        # Fora de transação: extração, chunking e embeddings.
        try:
            if kind == "pdf":
                pages = extract_pdf(data)
                chunks, page_count = chunk_pdf_pages(pages), max(len(pages), 1)
            else:
                chunks, page_count = chunk_markdown(decode_markdown(data)), 1
            if len(chunks) > MAX_CHUNKS:
                raise ExtractionError(f"Documento grande demais ({len(chunks)} trechos; máximo {MAX_CHUNKS}).")
            vectors = self._embedder.embed([c.content for c in chunks]) if chunks else []
            if len(vectors) != len(chunks):
                raise BackendError("Quantidade de embeddings diferente da de trechos.")
        except (ExtractionError, BackendError, BackendUnavailable) as exc:
            return self._fail(doc_id, file_name, str(exc))
        except Exception as exc:  # defensivo: o documento nunca fica preso em `processing`
            return self._fail(doc_id, file_name, f"Erro inesperado: {exc}")

        # T2 (publicação): trava a linha; se o documento foi excluído, descarta.
        try:
            with self._db.transaction() as conn:
                if repo.lock_processing_document(conn, doc_id) is None:
                    return ImportResult(file_name, "discarded", message="O documento foi excluído durante a importação.")
                if not chunks:
                    repo.finish_document(conn, doc_id, "no_text", page_count, "PDF sem texto extraível (OCR não suportado).")
                    return ImportResult(file_name, "no_text", doc_id, "Sem texto extraível (PDF escaneado? OCR não é suportado).")
                for i, (c, v) in enumerate(zip(chunks, vectors), start=1):
                    repo.insert_chunk(conn, doc_id, i, c, v)
                repo.finish_document(conn, doc_id, "ready", page_count, None)
        except Exception as exc:
            return self._fail(doc_id, file_name, f"Falha ao gravar o documento: {exc}")
        return ImportResult(file_name, "ready", doc_id, "Importado.", chunks=len(chunks))

    def _fail(self, doc_id: UUID, file_name: str, error: str) -> ImportResult:
        try:
            with self._db.transaction() as conn:
                repo.fail_document(conn, doc_id, error)
        except Exception:
            pass
        return ImportResult(file_name, "failed", doc_id, error)


# ---- recuperação (G7) --------------------------------------------------------------------------
@dataclass(frozen=True)
class Passage:
    n: int  # número no prompt [1..k]
    chunk_id: UUID
    document_id: UUID
    title: str
    file_name: str
    source_type: str
    page: int | None
    section: str | None
    char_start: int | None
    content: str

    @property
    def location(self) -> str:
        return location_label(self.source_type, self.page, self.section, self.char_start)


def location_label(source_type: str | None, page: int | None, section: str | None, char_start: int | None) -> str:
    if page is not None:
        return f"p. {page}"
    parts = []
    if section:
        parts.append(f"seção “{section}”")
    if char_start is not None:
        parts.append(f"posição {char_start}")
    return " · ".join(parts) or "início"


class Retriever:
    def __init__(self, db: Database, embedder: EmbedBackend, settings: Settings) -> None:
        self._db, self._embedder, self._settings = db, embedder, settings

    def has_documents(self) -> bool:
        with self._db.transaction() as conn:
            return repo.has_ready_documents(conn, self._settings.embed_model)

    def catalog(self) -> list[dict[str, Any]]:
        """Catálogo REAL da biblioteca (documentos e status), lido do banco, não inferido da busca."""
        with self._db.transaction() as conn:
            return repo.list_documents(conn)

    def overview(self, limit: int = 8) -> list[Passage]:
        """Trecho de abertura de cada documento pronto (visão geral para perguntas genéricas)."""
        with self._db.transaction() as conn:
            rows = repo.overview_chunks(conn, self._settings.embed_model, limit)
        return [
            Passage(i, r["chunk_id"], r["document_id"], r["title"], r["file_name"], r["source_type"],
                    r["page"], r["section"], r["char_start"], r["content"])
            for i, r in enumerate(rows, start=1)
        ]

    def retrieve(self, query: str, k: int | None = None) -> list[Passage]:
        """Embedding da pergunta (fora de transação) + UMA consulta SQL (FTS + vetor + RRF)."""
        if not query.strip():
            return []
        k = k or self._settings.retrieval_k
        vector = self._embedder.embed([query[:2000]])[0]
        with self._db.transaction() as conn:
            rows = repo.retrieve(conn, query, vector, self._settings.embed_model, k=k, pool=max(20, k * 4), max_dist=MAX_DIST, fts_dist=FTS_MAX_DIST)
        return [
            Passage(i, r["chunk_id"], r["document_id"], r["title"], r["file_name"], r["source_type"],
                    r["page"], r["section"], r["char_start"], r["content"])
            for i, r in enumerate(rows, start=1)
        ]


# ---- citações (G7, pós-processamento) ----------------------------------------------------------
_CITE = re.compile(r"\[(\d{1,3})\]")
_ADJACENT_QUOTE = re.compile(r"[“\"«]([^“”\"«»\n]{12,300})[”\"»][ \t]*$")


@dataclass(frozen=True)
class CitationPlan:
    ordinal: int
    chunk_id: UUID
    page: int | None
    quote_start: int | None
    quote_end: int | None


def plan_citations(text: str, passages: list[Passage]) -> tuple[str, list[CitationPlan]]:
    """Só `[n]` dentro de 1..k vira citação; os demais marcadores são removidos do texto. Um trecho entre aspas
    imediatamente antes de `[n]` só recebe offsets se for substring EXATA do chunk citado."""
    by_n = {p.n: p for p in passages}
    plans: dict[int, CitationPlan] = {}

    def repl(m: re.Match[str]) -> str:
        n = int(m.group(1))
        p = by_n.get(n)
        if p is None:
            return ""  # fora do intervalo: descartado
        if n not in plans:
            start = end = None
            q = _ADJACENT_QUOTE.search(text[max(0, m.start() - 320) : m.start()])  # só o literal colado ao marcador
            idx = p.content.find(q.group(1)) if q else -1
            if idx >= 0:  # não é substring exata → citação válida, sem destaque
                start, end = idx, idx + len(q.group(1))
            plans[n] = CitationPlan(n, p.chunk_id, p.page, start, end)
        return m.group(0)

    cleaned = _CITE.sub(repl, text)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r" +([.,;:!?])", r"\1", cleaned)
    return cleaned, [plans[n] for n in sorted(plans)]

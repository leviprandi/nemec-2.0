"""Widgets reutilizáveis da interface: mensagens, visão de chat, avisos, seções recolhíveis, cartões."""
from __future__ import annotations

import re
from typing import Any

from PySide6.QtCore import QPoint, QTimer, Qt, Signal
from PySide6.QtGui import QColor, QKeyEvent, QPalette, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStyledItemDelegate,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from nemec.documents import location_label
from nemec.tools import MEMORY_MAX_CHARS, TASK_NOTES_MAX, TASK_TITLE_MAX, CreateTaskArgs
from nemec.ui.render import message_html

CONTENT_MAX_WIDTH = 780
STATUS_CHIPS = {"canceled": ("cancelada", "warn"), "failed": ("falhou", "error")}
ACTION_LABEL = {"created": "Memória guardada", "updated": "Memória atualizada"}


def _prop(widget: QWidget, name: str, value: str) -> None:
    widget.setProperty(name, value)
    widget.style().unpolish(widget)
    widget.style().polish(widget)


DOTS_W = 30


class _DotsDelegate(QStyledItemDelegate):
    """Desenha o `⋯` à direita do item quando ele está sob o mouse ou selecionado; o texto é cortado antes dele."""

    def paint(self, painter, option, index) -> None:  # noqa: N802
        from PySide6.QtWidgets import QStyle

        shown = bool(option.state & (QStyle.State_MouseOver | QStyle.State_Selected))
        if not shown:
            return super().paint(painter, option, index)
        opt = type(option)(option)
        opt.rect.setRight(opt.rect.right() - DOTS_W)
        super().paint(painter, opt, index)
        painter.save()
        painter.setPen(option.palette.color(QPalette.Text))
        painter.drawText(option.rect.adjusted(option.rect.width() - DOTS_W, 0, -6, 0), Qt.AlignCenter, "⋯")
        painter.restore()


class ActionList(QListWidget):
    """Lista cujos itens têm um menu de ações: clique no `⋯`, botão direito ou tecla de menu (Shift+F10).
    Emite `menu_requested(item, global_pos)` para o item sob o ponteiro (não o selecionado antes)."""

    menu_requested = Signal(object, object)

    def __init__(self) -> None:
        super().__init__()
        self.setMouseTracking(True)
        self.setItemDelegate(_DotsDelegate(self))

    def mousePressEvent(self, event) -> None:  # noqa: N802
        item = self.itemAt(event.position().toPoint())
        if item is not None and event.button() == Qt.LeftButton and event.position().x() >= self.viewport().width() - DOTS_W:
            rect = self.visualItemRect(item)
            self.setCurrentItem(item)
            self.menu_requested.emit(item, self.viewport().mapToGlobal(QPoint(rect.right() - DOTS_W, rect.bottom())))
            return
        super().mousePressEvent(event)

    def contextMenuEvent(self, event) -> None:  # noqa: N802
        from PySide6.QtGui import QContextMenuEvent

        if event.reason() == QContextMenuEvent.Keyboard:
            item = self.currentItem()
            pos = self.viewport().mapToGlobal(self.visualItemRect(item).bottomLeft()) if item is not None else None
        else:
            item, pos = self.itemAt(event.pos()), event.globalPos()
            if item is not None:
                self.setCurrentItem(item)
        if item is not None:
            self.menu_requested.emit(item, pos)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() in (Qt.Key_Menu, Qt.Key_F10) and (event.key() == Qt.Key_Menu or event.modifiers() & Qt.ShiftModifier):
            item = self.currentItem()
            if item is not None:
                self.menu_requested.emit(item, self.viewport().mapToGlobal(self.visualItemRect(item).bottomLeft()))
            return
        super().keyPressEvent(event)


class InputBox(QPlainTextEdit):
    """Enter envia; Shift+Enter quebra linha. Cresce com o texto (1 a 6 linhas)."""

    submit = Signal()
    focus_changed = Signal(bool)
    MAX_LINES = 6

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("input")
        self.setFrameShape(QFrame.NoFrame)
        self.setTabChangesFocus(True)
        self.textChanged.connect(self._fit)
        self._fit()

    def _fit(self) -> None:
        line = self.fontMetrics().lineSpacing()
        lines = max(1, min(int(self.document().size().height()), self.MAX_LINES))
        self.setFixedHeight(lines * line + 14)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if event.key() in (Qt.Key_Return, Qt.Key_Enter) and not (event.modifiers() & Qt.ShiftModifier):
            self.submit.emit()
            return
        super().keyPressEvent(event)

    def focusInEvent(self, event) -> None:  # noqa: N802
        super().focusInEvent(event)
        self.focus_changed.emit(True)

    def focusOutEvent(self, event) -> None:  # noqa: N802
        super().focusOutEvent(event)
        self.focus_changed.emit(False)


class Banner(QFrame):
    """Aviso discreto: erro (fixo até fechar), sucesso/info (some sozinho)."""

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("banner")
        self._label = QLabel()
        self._label.setWordWrap(True)
        self._label.setTextFormat(Qt.PlainText)
        self._close = QToolButton()
        self._close.setText("✕")
        self._close.setToolTip("Fechar")
        self._close.clicked.connect(self.hide)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 8, 6, 8)
        lay.addWidget(self._label, 1)
        lay.addWidget(self._close, 0, Qt.AlignTop)
        self._timer = QTimer(self, singleShot=True)
        self._timer.timeout.connect(self.hide)
        self.hide()

    def text(self) -> str:
        return self._label.text()

    def setText(self, text: str) -> None:  # noqa: N802
        self._label.setText(text)

    def show_message(self, text: str, kind: str = "error", timeout_ms: int = 0) -> None:
        self._label.setText(text)
        _prop(self, "kind", kind)
        self.show()
        self._timer.stop()
        if timeout_ms:
            self._timer.start(timeout_ms)


class MessageWidget(QWidget):
    """Uma mensagem. Usuário: balão à direita. Assistente: avatar + texto, sem balão."""

    cite_clicked = Signal(object)  # linha de citação (dict)

    def __init__(self, role: str, text: str, status: str = "complete", notes: list[dict[str, Any]] | None = None,
                 citations: list[dict[str, Any]] | None = None) -> None:
        super().__init__()
        self.role = role
        self._text = ""
        self._citations = {c["ordinal"]: c for c in citations or []}
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 6, 0, 6)
        if role == "user":
            self.body = QLabel()
            self.body.setTextFormat(Qt.PlainText)
            self.body.setWordWrap(True)
            self.body.setTextInteractionFlags(Qt.TextSelectableByMouse)
            bubble = QFrame()
            bubble.setObjectName("userBubble")
            bl = QVBoxLayout(bubble)
            bl.setContentsMargins(14, 9, 14, 9)
            bl.addWidget(self.body)
            bubble.setMaximumWidth(int(CONTENT_MAX_WIDTH * 0.85))
            outer.addStretch(1)
            outer.addWidget(bubble, 0, Qt.AlignTop)
        else:
            avatar = QLabel("N")
            avatar.setObjectName("avatar")
            avatar.setFixedSize(26, 26)
            avatar.setAlignment(Qt.AlignCenter)
            self.body = QLabel()
            self.body.setObjectName("assistantText")
            self.body.setTextFormat(Qt.RichText)
            self.body.setWordWrap(True)
            self.body.setOpenExternalLinks(False)  # nada abre fora do app; só `cite:N` (gerado pelo app) é tratado
            self.body.setTextInteractionFlags(Qt.TextSelectableByMouse)  # links só quando há citações (set_text)
            self.body.linkActivated.connect(self._on_link)
            self.body.setAlignment(Qt.AlignTop | Qt.AlignLeft)
            self.body.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
            column = QVBoxLayout()
            column.setSpacing(6)
            column.addWidget(self.body)
            self.chip = QLabel()
            self.chip.setObjectName("statusChip")
            self.chip.hide()
            chip_row = QHBoxLayout()
            chip_row.addWidget(self.chip)
            chip_row.addStretch()
            column.addLayout(chip_row)
            self.notes_box = QVBoxLayout()
            self.notes_box.setSpacing(4)
            column.addLayout(self.notes_box)
            self.sources_box = QHBoxLayout()
            self.sources_box.setSpacing(6)
            column.addLayout(self.sources_box)
            outer.addWidget(avatar, 0, Qt.AlignTop)
            outer.addSpacing(8)
            outer.addLayout(column, 1)
            for n in notes or []:
                self.add_note(n)
            self._add_source_chips()
        self.set_text(text, status)

    def set_text(self, text: str, status: str = "complete") -> None:
        self._text = text
        if self.role == "user":
            self.body.setText(text)
            return
        if status == "generating" and not text:
            self.body.setText("<p style='margin:0'><i>Pensando…</i></p>")
        elif not text:
            self.body.setText("<p style='margin:0'><i>(sem texto)</i></p>")
        else:
            self.body.setText(message_html(text, frozenset(self._citations)))
            if self._citations:
                self.body.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.LinksAccessibleByMouse)
        chip = STATUS_CHIPS.get(status)
        if chip:
            self.chip.setText(chip[0])
            _prop(self.chip, "kind", chip[1])
            self.chip.show()
        else:
            self.chip.hide()

    def _add_source_chips(self) -> None:
        """Fontes da resposta: um botão discreto por citação (arquivo e página/seção); fonte removida → tombstone."""
        for ordinal, c in sorted(self._citations.items()):
            if c.get("content") is None:
                label = f"[{ordinal}] Fonte removida"
            else:
                label = f"[{ordinal}] {c['file_name']} · {location_label(c['source_type'], c.get('page'), c.get('section'), c.get('char_start'))}"
            btn = QPushButton(label if len(label) <= 48 else label[:47] + "…")
            btn.setObjectName("sourceChip")
            btn.setToolTip(label)
            btn.setCursor(Qt.PointingHandCursor)
            btn.clicked.connect(lambda _=False, row=c: self.cite_clicked.emit(row))
            self.sources_box.addWidget(btn)
        self.sources_box.addStretch(1)

    def _on_link(self, href: str) -> None:
        m = re.fullmatch(r"cite:(\d{1,3})", href)
        if m and int(m.group(1)) in self._citations:
            self.cite_clicked.emit(self._citations[int(m.group(1))])

    def add_note(self, event: dict[str, Any]) -> None:
        label = ACTION_LABEL.get(event.get("action", ""), "Memória")
        note = QLabel(f"{label} · {event.get('content', '')}")
        note.setObjectName("memoryNote")
        note.setTextFormat(Qt.PlainText)
        note.setWordWrap(True)
        note.setToolTip("Veja, edite ou exclua no painel Memórias." + (f"\nAntes: {event['previous']}" if event.get("previous") else ""))
        row = QHBoxLayout()
        row.addWidget(note)
        row.addStretch()
        self.notes_box.addLayout(row)

    def plain_text(self) -> str:
        suffix = f" [{self.chip.text()}]" if self.role != "user" and not self.chip.isHidden() else ""
        return self._text + suffix


class ChatView(QScrollArea):
    """Lista rolável de mensagens, com largura de leitura limitada e rolagem automática no fim."""

    citation_clicked = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        page = QWidget()
        page.setObjectName("root")
        row = QHBoxLayout(page)
        row.setContentsMargins(16, 8, 16, 8)
        column = QWidget()
        column.setMaximumWidth(CONTENT_MAX_WIDTH)
        self._col = QVBoxLayout(column)
        self._col.setContentsMargins(0, 0, 0, 0)
        self._col.setSpacing(2)
        self._welcome = QWidget()
        wl = QVBoxLayout(self._welcome)
        wl.setAlignment(Qt.AlignCenter)
        title = QLabel("Como posso ajudar?")
        title.setObjectName("welcomeTitle")
        title.setAlignment(Qt.AlignCenter)
        text = QLabel("Converse, peça ideias ou conte algo sobre você.\nTudo roda localmente neste computador.")
        text.setObjectName("welcomeText")
        text.setAlignment(Qt.AlignCenter)
        wl.addStretch(2)
        wl.addWidget(title)
        wl.addWidget(text)
        wl.addStretch(3)
        self._col.addWidget(self._welcome, 1)
        self._msgs = QWidget()
        self._msgs_layout = QVBoxLayout(self._msgs)
        self._msgs_layout.setContentsMargins(0, 0, 0, 0)
        self._msgs_layout.setSpacing(2)
        self._msgs_layout.addStretch(1)  # mantém as mensagens coladas ao topo (sem espaço extra entre elas)
        self._msgs.hide()
        self._col.addWidget(self._msgs, 1)
        row.addStretch(1)
        row.addWidget(column, 100)
        row.addStretch(1)
        self.setWidget(page)
        self._messages: list[MessageWidget] = []
        self._stick = True
        bar = self.verticalScrollBar()
        bar.rangeChanged.connect(lambda _a, mx: bar.setValue(mx) if self._stick else None)
        bar.valueChanged.connect(lambda v: setattr(self, "_stick", v >= bar.maximum() - 6))

    def clear(self) -> None:
        for m in self._messages:
            m.setParent(None)
            m.deleteLater()
        self._messages = []
        self._msgs.hide()
        self._welcome.show()
        self._stick = True

    def add_message(self, role: str, text: str, status: str = "complete", notes: list[dict[str, Any]] | None = None,
                    citations: list[dict[str, Any]] | None = None) -> MessageWidget:
        self._welcome.hide()
        self._msgs.show()
        w = MessageWidget(role, text, status, notes, citations)
        w.cite_clicked.connect(self.citation_clicked)
        self._msgs_layout.insertWidget(self._msgs_layout.count() - 1, w)
        self._messages.append(w)
        self._stick = True
        return w

    def set_messages(self, messages: list[dict[str, Any]], events_by_message: dict[Any, list[dict[str, Any]]] | None = None,
                     citations_by_message: dict[Any, list[dict[str, Any]]] | None = None) -> None:
        self.clear()
        events_by_message = events_by_message or {}
        citations_by_message = citations_by_message or {}
        for m in messages:
            self.add_message(m["role"], m["content"], m["status"], events_by_message.get(m["id"]), citations_by_message.get(m["id"]))

    def plain_text(self) -> str:
        return "\n".join(m.plain_text() for m in self._messages)

    @property
    def message_widgets(self) -> list[MessageWidget]:
        return list(self._messages)


class CollapsibleSection(QWidget):
    """Seção com cabeçalho clicável (▾/▸) e conteúdo recolhível."""

    def __init__(self, title: str, expanded: bool = True) -> None:
        super().__init__()
        self._title = title
        self.header = QPushButton()
        self.header.setObjectName("sectionHeader")
        self.header.setCursor(Qt.PointingHandCursor)
        self.header.setCheckable(True)
        self.body = QFrame()
        self.body.setObjectName("sectionBody")
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(4, 0, 4, 8)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self.header)
        lay.addWidget(self.body)
        self.header.toggled.connect(self._apply)
        self.header.setChecked(expanded)
        self._apply(expanded)

    def set_count(self, n: int) -> None:
        self._count = n
        self._apply(self.header.isChecked())

    def _apply(self, expanded: bool) -> None:
        count = getattr(self, "_count", None)
        suffix = f"  ({count})" if count else ""
        self.header.setText(f"{'▾' if expanded else '▸'}  {self._title}{suffix}")
        self.body.setVisible(expanded)

    def is_expanded(self) -> bool:
        return self.header.isChecked()


class ProposalCard(QFrame):
    """Cartão discreto de proposta pendente (confirmação humana, G3): memória ou tarefa, editável antes de confirmar."""

    confirmed = Signal(object, object)  # proposal_id, argumentos (possivelmente editados)
    rejected = Signal(object)

    def __init__(self, proposal: dict[str, Any]) -> None:
        super().__init__()
        self.proposal_id = proposal["id"]
        self.kind = proposal["kind"]
        args = proposal["arguments"]
        self.setObjectName("proposalCard")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 10)
        if self.kind == "create_task":
            title = QLabel("Sugestão: criar esta tarefa (edite se quiser)")
            title.setObjectName("muted")
            lay.addWidget(title)
            self.title_edit = QLineEdit(args.get("title", ""))
            self.title_edit.setPlaceholderText("Título da tarefa")
            self.notes_edit = QPlainTextEdit(args.get("notes") or "")
            self.notes_edit.setPlaceholderText("Notas (opcional)")
            self.notes_edit.setFixedHeight(48)
            self.due_edit = QLineEdit(args.get("due_at") or "")
            self.due_edit.setPlaceholderText("Prazo opcional: AAAA-MM-DD HH:MM")
            for w in (self.title_edit, self.notes_edit, self.due_edit):
                lay.addWidget(w)
            self.edit = self.title_edit
            fields = (self.title_edit.textChanged, self.notes_edit.textChanged, self.due_edit.textChanged)
        else:
            title = QLabel("Sugestão: guardar esta memória (edite se quiser)")
            title.setObjectName("muted")
            lay.addWidget(title)
            self.edit = QPlainTextEdit(args.get("content", ""))
            self.edit.setFixedHeight(60)
            lay.addWidget(self.edit)
            fields = (self.edit.textChanged,)
        self.error = QLabel("")
        self.error.setObjectName("muted")
        self.error.hide()
        lay.addWidget(self.error)
        row = QHBoxLayout()
        self.confirm_btn = QPushButton("Confirmar")
        self.confirm_btn.setObjectName("confirmBtn")
        self.reject_btn = QPushButton("Rejeitar")
        self.reject_btn.setObjectName("flat")
        row.addStretch()
        row.addWidget(self.reject_btn)
        row.addWidget(self.confirm_btn)
        lay.addLayout(row)
        self.confirm_btn.clicked.connect(lambda: self.confirmed.emit(self.proposal_id, self.arguments()))
        self.reject_btn.clicked.connect(lambda: self.rejected.emit(self.proposal_id))
        for sig in fields:
            sig.connect(self._sync)
        self._sync()

    def arguments(self) -> dict[str, Any]:
        if self.kind == "create_task":
            return {"title": self.title_edit.text(), "notes": self.notes_edit.toPlainText().strip() or None,
                    "due_at": self.due_edit.text().strip() or None}
        return {"content": self.edit.toPlainText()}

    def _sync(self) -> None:
        if self.kind == "create_task":
            try:
                CreateTaskArgs.model_validate(self.arguments())
                ok, msg = True, ""
            except Exception:
                ok, msg = False, f"Informe um título (até {TASK_TITLE_MAX}), notas até {TASK_NOTES_MAX} e prazo em AAAA-MM-DD HH:MM."
            self.error.setText(msg)
            self.error.setVisible(bool(msg) and bool(self.title_edit.text().strip() or self.due_edit.text().strip()))
            self.confirm_btn.setEnabled(ok)
            return
        text = self.edit.toPlainText().strip()
        self.confirm_btn.setEnabled(bool(text) and len(text) <= MEMORY_MAX_CHARS)


class SourceDialog(QDialog):
    """Mostra a fonte de uma citação: arquivo, página/seção e o trecho original (com o literal citado destacado)."""

    def __init__(self, citation: dict[str, Any], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setMinimumSize(460, 320)
        lay = QVBoxLayout(self)
        n = citation["ordinal"]
        if citation.get("content") is None:
            self.setWindowTitle(f"Fonte [{n}]")
            self.header = QLabel("Fonte removida")
            self.body = None
            note = QLabel("O documento citado foi excluído; a citação não pode mais ser verificada.")
            note.setObjectName("muted")
            note.setWordWrap(True)
            lay.addWidget(self.header)
            lay.addWidget(note)
            lay.addStretch(1)
            return
        where = location_label(citation["source_type"], citation.get("page"), citation.get("section"), citation.get("char_start"))
        self.setWindowTitle(f"Fonte [{n}] · {citation['file_name']}")
        self.header = QLabel(f"{citation['file_name']} · {where}")
        self.header.setObjectName("title")
        self.header.setWordWrap(True)
        self.header.setTextFormat(Qt.PlainText)
        self.body = QPlainTextEdit(citation["content"])
        self.body.setReadOnly(True)
        qs, qe = citation.get("quote_start"), citation.get("quote_end")
        if qs is not None and qe is not None:
            cur = QTextCursor(self.body.document())
            cur.setPosition(qs)
            cur.setPosition(qe, QTextCursor.KeepAnchor)
            fmt = QTextCharFormat()
            fmt.setBackground(QColor(255, 214, 90, 150))
            sel = QPlainTextEdit.ExtraSelection()
            sel.cursor, sel.format = cur, fmt
            self.body.setExtraSelections([sel])
            cur.setPosition(qs)
            self.body.setTextCursor(cur)
        hint = QLabel("Trecho original do documento" + (" · o trecho citado está destacado" if qs is not None else ""))
        hint.setObjectName("hint")
        lay.addWidget(self.header)
        lay.addWidget(hint)
        lay.addWidget(self.body, 1)

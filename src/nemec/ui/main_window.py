"""Janela principal: barra lateral de conversas, chat com streaming/cancelamento e painel lateral discreto
(Memórias, Documentos, Tarefas)."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from PySide6.QtCore import QObject, QSettings, Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox,
    QMenu,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from nemec.backend import CancelToken
import threading

from nemec.documents import DocumentService, ImportResult, scan_folder
from nemec.errors import NemecError
from nemec.services import ChatService, MemoryService, TaskService, TurnResult
from nemec.ui import theme
from nemec.ui.widgets import (
    CONTENT_MAX_WIDTH,
    ActionList,
    Banner,
    ChatView,
    CollapsibleSection,
    InputBox,
    MessageWidget,
    ProposalCard,
    SourceDialog,
    _prop,
)

__all__ = ["MainWindow", "ProposalCard", "InputBox", "TurnWorker"]

NARROW_PANEL = 1040  # abaixo disso o painel lateral recolhe sozinho
NARROW_SIDEBAR = 760  # abaixo disso a barra lateral também


class TurnWorker(QObject):
    """Executa um turno de chat numa QThread. O cancelamento interrompe o stream (CancelToken)."""

    delta = Signal(str)
    finished = Signal(object)  # TurnResult
    failed = Signal(str)

    def __init__(self, chat: ChatService, conversation_id: UUID | None, text: str, auto_memory: bool = True) -> None:
        super().__init__()
        self._chat, self._conversation_id, self._text, self._auto = chat, conversation_id, text, auto_memory
        self.token = CancelToken()

    @Slot()
    def run(self) -> None:
        try:
            self.finished.emit(
                self._chat.run_turn(self._conversation_id, self._text, self.token, self.delta.emit, auto_memory=self._auto)
            )
        except Exception as exc:  # erro fora da geração (ex.: banco indisponível)
            self.failed.emit(str(exc))

    def cancel(self) -> None:
        self.token.cancel()


class ImportWorker(QObject):
    """Importa arquivos (PDF/Markdown) numa QThread, um de cada vez; a falha de um não afeta os demais."""

    progress = Signal(object)  # ImportResult
    finished = Signal(object)  # list[ImportResult]

    def __init__(self, documents: DocumentService, paths: list[Path], root: Path | None = None) -> None:
        super().__init__()
        self._documents, self._paths, self._root = documents, paths, root
        self.cancel_event = threading.Event()

    @Slot()
    def run(self) -> None:
        try:
            results = self._documents.import_files(
                self._paths, self.progress.emit, self.cancel_event.is_set, self._root)
        except Exception as exc:  # defensivo: nunca derrubar a thread
            results = [ImportResult("(importação)", "failed", message=f"Erro inesperado: {exc}")]
        self.finished.emit(results)


DOC_STATUS = {"processing": "importando…", "failed": "falhou", "no_text": "sem texto"}


class MainWindow(QMainWindow):
    RENDER_INTERVAL_MS = 60

    def __init__(self, chat: ChatService, memories: MemoryService, model_name: str, prefs: QSettings | None = None,
                 documents: DocumentService | None = None, tasks: TaskService | None = None) -> None:
        super().__init__()
        self.chat, self.memories, self.documents, self.tasks = chat, memories, documents, tasks
        self._import_thread: QThread | None = None
        self._import_worker: ImportWorker | None = None
        self._import_total = self._import_done = self._import_ignored = 0
        self._source_dialog: SourceDialog | None = None
        self.prefs = prefs if prefs is not None else QSettings("Nemec", "Nemec2")
        self.setWindowTitle("Nemec")
        self.resize(1240, 780)
        self.setMinimumSize(560, 480)
        self.conversation_id: UUID | None = None
        self.messages: list[dict[str, Any]] = []
        self._thread: QThread | None = None
        self._worker: TurnWorker | None = None
        self._stream_widget: MessageWidget | None = None
        self._partial = ""
        self._dirty = False
        self._auto_hidden: set[str] = set()
        self._build(model_name)
        self._apply_theme()
        QGuiApplication.styleHints().colorSchemeChanged.connect(lambda *_: self._apply_theme())
        self._timer = QTimer(self, interval=self.RENDER_INTERVAL_MS)
        self._timer.timeout.connect(self._flush_render)
        self.refresh_conversations()
        self.refresh_memories()
        self.refresh_documents()
        self.refresh_tasks()
        self.open_conversation(None)

    # -- construção ----------------------------------------------------------------------------
    def _build(self, model_name: str) -> None:
        self.sidebar = self._build_sidebar()
        self.center = self._build_center(model_name)
        self.side_panel = self._build_side_panel()
        self.split = QSplitter()
        self.split.setChildrenCollapsible(False)
        for w in (self.sidebar, self.center, self.side_panel):
            self.split.addWidget(w)
        self.split.setStretchFactor(0, 0)
        self.split.setStretchFactor(1, 1)
        self.split.setStretchFactor(2, 0)
        self.split.setSizes([260, 720, 300])
        self.setCentralWidget(self.split)
        self.statusBar().hide()  # o estado aparece no chat (Pensando…, avisos); o modelo, no cabeçalho

    def _build_sidebar(self) -> QWidget:
        w = QWidget()
        w.setObjectName("sidebar")
        w.setMinimumWidth(210)
        w.setMaximumWidth(340)
        lay = QVBoxLayout(w)
        lay.setContentsMargins(10, 12, 10, 10)
        brand = QLabel("Nemec")
        brand.setObjectName("brand")
        self.new_btn = QPushButton("＋  Nova conversa")
        self.new_btn.setObjectName("newChat")
        self.new_btn.setToolTip("Começar uma nova conversa")
        recents = QLabel("RECENTES")
        recents.setObjectName("sectionTitle")
        self.conv_list = ActionList()
        self.conv_list.setTextElideMode(Qt.ElideRight)
        self.conv_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.conv_list.setWordWrap(False)
        self.rename_conv_btn = QPushButton("Renomear")
        self.rename_conv_btn.setObjectName("flat")
        self.del_conv_btn = QPushButton("Excluir conversa")
        self.del_conv_btn.setObjectName("flat")
        conv_actions = QHBoxLayout()
        conv_actions.addWidget(self.rename_conv_btn)
        conv_actions.addStretch(1)
        conv_actions.addWidget(self.del_conv_btn)
        lay.addWidget(brand)
        lay.addSpacing(6)
        lay.addWidget(self.new_btn)
        lay.addWidget(recents)
        lay.addWidget(self.conv_list, 1)
        lay.addLayout(conv_actions)
        self.new_btn.clicked.connect(self.new_conversation)
        self.rename_conv_btn.clicked.connect(lambda: self.rename_conversation())
        self.conv_list.itemDoubleClicked.connect(lambda i: self.rename_conversation(i.data(Qt.UserRole)))
        self.conv_list.menu_requested.connect(lambda i, p: self._exec_menu(self.build_conv_menu(i), p))
        self.conv_list.itemSelectionChanged.connect(self._on_conv_selected)
        self.del_conv_btn.clicked.connect(lambda: self.delete_conversation())
        return w

    def _build_center(self, model_name: str) -> QWidget:
        w = QWidget()
        w.setObjectName("center")
        w.setMinimumWidth(360)
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        header = QWidget()
        header.setObjectName("header")
        hl = QHBoxLayout(header)
        hl.setContentsMargins(10, 8, 12, 8)
        self.toggle_sidebar_btn = QToolButton()
        self.toggle_sidebar_btn.setText("☰")
        self.toggle_sidebar_btn.setToolTip("Mostrar/ocultar conversas")
        self.title_label = QLabel("Nova conversa")
        self.title_label.setObjectName("title")
        self.title_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        chip = QLabel(f"{model_name} · local")
        chip.setObjectName("chip")
        self.toggle_panel_btn = QToolButton()
        self.toggle_panel_btn.setText("Painel")
        self.toggle_panel_btn.setToolTip("Mostrar/ocultar o painel de memórias, documentos e tarefas")
        self.toggle_panel_btn.setToolButtonStyle(Qt.ToolButtonTextOnly)
        hl.addWidget(self.toggle_sidebar_btn)
        hl.addWidget(self.title_label, 1)
        hl.addWidget(chip)
        hl.addWidget(self.toggle_panel_btn)

        self.view = ChatView()
        self.banner = Banner()  # erros (ficam até serem fechados)
        self.notice = Banner()  # operações concluídas / informações (somem sozinhas)
        self.proposals_box = QVBoxLayout()

        composer = QFrame()
        composer.setObjectName("composer")
        self.composer = composer
        cl = QHBoxLayout(composer)
        cl.setContentsMargins(8, 6, 8, 6)
        self.attach_btn = QToolButton()
        self.attach_btn.setText("＋")
        self.attach_btn.setToolTip("Importar arquivos ou uma pasta (PDF e Markdown)")
        self.attach_btn.setEnabled(self.documents is not None)
        self.input = InputBox()
        self.input.setPlaceholderText("Escreva sua mensagem…")
        self.send_btn = QToolButton()
        self.send_btn.setObjectName("send")
        self.send_btn.setText("↑")
        self.send_btn.setToolTip("Enviar (Enter)")
        self.send_btn.setEnabled(False)
        self.cancel_btn = QToolButton()
        self.cancel_btn.setObjectName("cancel")
        self.cancel_btn.setText("■")
        self.cancel_btn.setToolTip("Cancelar a geração")
        self.cancel_btn.hide()
        cl.addWidget(self.attach_btn, 0, Qt.AlignBottom)
        cl.addWidget(self.input, 1)
        cl.addWidget(self.send_btn, 0, Qt.AlignBottom)
        cl.addWidget(self.cancel_btn, 0, Qt.AlignBottom)
        hint = QLabel("Enter envia · Shift+Enter quebra linha · tudo roda localmente")
        hint.setObjectName("hint")
        hint.setAlignment(Qt.AlignCenter)

        bottom = QWidget()
        bottom.setObjectName("root")
        bl = QHBoxLayout(bottom)
        bl.setContentsMargins(16, 6, 16, 8)
        col = QWidget()
        col.setMaximumWidth(CONTENT_MAX_WIDTH)
        cv = QVBoxLayout(col)
        cv.setContentsMargins(0, 0, 0, 0)
        cv.setSpacing(6)
        cv.addWidget(self.banner)
        cv.addWidget(self.notice)
        cv.addLayout(self.proposals_box)
        cv.addWidget(composer)
        cv.addWidget(hint)
        bl.addStretch(1)
        bl.addWidget(col, 100)
        bl.addStretch(1)

        lay.addWidget(header)
        lay.addWidget(self.view, 1)
        lay.addWidget(bottom)

        self.toggle_sidebar_btn.clicked.connect(lambda: self._toggle("sidebar"))
        self.toggle_panel_btn.clicked.connect(lambda: self._toggle("panel"))
        self.send_btn.clicked.connect(self.send)
        self.input.submit.connect(self.send)
        self.input.textChanged.connect(self._sync_send)
        self.input.focus_changed.connect(lambda f: _prop(self.composer, "focused", "true" if f else "false"))
        self.cancel_btn.clicked.connect(self.cancel)
        self.attach_menu = QMenu(self.attach_btn)  # mesmos diálogos e serviços da seção Documentos
        self.attach_menu.addAction("Importar arquivos…", self.import_documents)
        self.attach_menu.addAction("Importar pasta…", self.import_folder)
        self.attach_btn.setMenu(self.attach_menu)
        self.attach_btn.setPopupMode(QToolButton.InstantPopup)
        self.view.citation_clicked.connect(self.show_source)
        return w

    def _build_side_panel(self) -> QWidget:
        w = QWidget()
        w.setObjectName("sidePanel")
        w.setMinimumWidth(240)
        w.setMaximumWidth(380)
        outer = QVBoxLayout(w)
        outer.setContentsMargins(8, 10, 8, 8)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        inner = QWidget()
        inner.setObjectName("sidePanel")
        self.panel_layout = QVBoxLayout(inner)
        self.panel_layout.setContentsMargins(0, 0, 0, 0)

        self.mem_section = CollapsibleSection("Memórias", expanded=True)
        self.auto_mem_check = QCheckBox("Memória automática")
        self.auto_mem_check.setToolTip("O Nemec guarda sozinho fatos úteis que você disser. Desligue para parar de guardar.")
        self.auto_mem_check.setChecked(self._auto_memory_pref())
        self.auto_mem_check.toggled.connect(self._on_auto_memory_toggled)
        self.mem_hint = QLabel("")
        self.mem_hint.setObjectName("hint")
        self.mem_hint.setWordWrap(True)
        self.mem_list = ActionList()
        self.mem_list.setObjectName("memList")
        self.mem_list.setWordWrap(True)
        self.mem_list.setMinimumHeight(90)
        self.mem_edit_btn = QPushButton("Editar")
        self.mem_edit_btn.setObjectName("flat")
        self.mem_del_btn = QPushButton("Excluir")
        self.mem_del_btn.setObjectName("danger")
        mrow = QHBoxLayout()
        mrow.addStretch()
        mrow.addWidget(self.mem_edit_btn)
        mrow.addWidget(self.mem_del_btn)
        bl = self.mem_section.body_layout
        bl.addWidget(self.auto_mem_check)
        bl.addWidget(self.mem_hint)
        bl.addWidget(self.mem_list, 1)
        bl.addLayout(mrow)
        self.panel_layout.addWidget(self.mem_section)
        self._build_documents_section()
        self._build_tasks_section()
        self.panel_layout.addStretch(1)
        scroll.setWidget(inner)
        outer.addWidget(scroll)
        self.mem_edit_btn.clicked.connect(lambda: self.edit_memory())
        self.mem_del_btn.clicked.connect(lambda: self.delete_memory())
        self.mem_list.itemDoubleClicked.connect(self.edit_memory)
        self.mem_list.menu_requested.connect(lambda i, p: self._exec_menu(self.build_memory_menu(i), p))
        return w

    def _build_documents_section(self) -> None:
        self.docs_section = CollapsibleSection("Documentos", expanded=True)
        hint = QLabel("PDFs com texto e arquivos Markdown (.md) que você escolher. Os originais não são alterados.")
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        self.doc_list = ActionList()
        self.doc_list.setObjectName("docList")
        self.doc_list.setWordWrap(True)
        self.doc_list.setMinimumHeight(90)
        self.doc_import_btn = QPushButton("Importar…")
        self.doc_import_btn.setObjectName("flat")
        self.doc_folder_btn = QPushButton("Importar pasta…")
        self.doc_folder_btn.setObjectName("flat")
        self.doc_cancel_btn = QPushButton("Cancelar importação")
        self.doc_cancel_btn.setObjectName("danger")
        self.doc_cancel_btn.hide()
        self.doc_del_btn = QPushButton("Excluir")
        self.doc_del_btn.setObjectName("danger")
        row = QHBoxLayout()
        row.addWidget(self.doc_import_btn)
        row.addWidget(self.doc_folder_btn)
        row.addStretch()
        row.addWidget(self.doc_del_btn)
        bl = self.docs_section.body_layout
        bl.addWidget(hint)
        bl.addWidget(self.doc_list, 1)
        bl.addLayout(row)
        bl.addWidget(self.doc_cancel_btn)
        self.panel_layout.addWidget(self.docs_section)
        self.docs_section.setVisible(self.documents is not None)
        self.doc_import_btn.clicked.connect(self.import_documents)
        self.doc_folder_btn.clicked.connect(self.import_folder)
        self.doc_cancel_btn.clicked.connect(self.cancel_import)
        self.doc_del_btn.clicked.connect(lambda: self.delete_document())
        self.doc_list.itemDoubleClicked.connect(lambda i: self.show_document_details(i.data(Qt.UserRole)))
        self.doc_list.menu_requested.connect(lambda i, p: self._exec_menu(self.build_document_menu(i), p))

    def _build_tasks_section(self) -> None:
        self.tasks_section = CollapsibleSection("Tarefas", expanded=True)
        hint = QLabel("O Nemec só cria tarefas depois da sua confirmação na conversa.")
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        self.task_list = ActionList()
        self.task_list.setObjectName("taskList")
        self.task_list.setWordWrap(True)
        self.task_list.setMinimumHeight(90)
        self.task_done_btn = QPushButton("Concluir")
        self.task_done_btn.setObjectName("flat")
        self.task_del_btn = QPushButton("Excluir")
        self.task_del_btn.setObjectName("danger")
        row = QHBoxLayout()
        row.addWidget(self.task_done_btn)
        row.addStretch()
        row.addWidget(self.task_del_btn)
        bl = self.tasks_section.body_layout
        bl.addWidget(hint)
        bl.addWidget(self.task_list, 1)
        bl.addLayout(row)
        self.panel_layout.addWidget(self.tasks_section)
        self.tasks_section.setVisible(self.tasks is not None)
        self.task_done_btn.clicked.connect(lambda: self.complete_task())
        self.task_del_btn.clicked.connect(lambda: self.delete_task())
        self.task_list.menu_requested.connect(lambda i, p: self._exec_menu(self.build_task_menu(i), p))

    # -- tema e layout responsivo --------------------------------------------------------------
    def _apply_theme(self) -> None:
        self.setStyleSheet(theme.build_qss(theme.tokens(theme.is_dark(QGuiApplication.instance()))))

    def _toggle(self, which: str) -> None:
        target = self.sidebar if which == "sidebar" else self.side_panel
        target.setVisible(not target.isVisible())
        self._auto_hidden.discard(which)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        width = event.size().width()
        for name, widget, limit in (("panel", self.side_panel, NARROW_PANEL), ("sidebar", self.sidebar, NARROW_SIDEBAR)):
            if width < limit and widget.isVisible() and name not in self._auto_hidden:
                widget.hide()
                self._auto_hidden.add(name)
            elif width >= limit + 40 and name in self._auto_hidden:
                widget.show()
                self._auto_hidden.discard(name)

    # -- util ----------------------------------------------------------------------------------
    @property
    def busy(self) -> bool:
        return self._thread is not None

    def show_error(self, text: str) -> None:
        self.banner.show_message(text, "error")

    def clear_error(self) -> None:
        self.banner.hide()

    def notify(self, text: str, kind: str = "success", ms: int = 5000) -> None:
        self.notice.show_message(text, kind, ms)

    def _guard(self, fn, *args):
        """Executa operação de serviço exibindo erros do domínio no banner."""
        try:
            return fn(*args)
        except NemecError as exc:
            self.show_error(str(exc))
        except Exception as exc:
            self.show_error(f"Erro inesperado: {exc}")
        return None

    def _sync_send(self) -> None:
        self.send_btn.setEnabled(bool(self.input.toPlainText().strip()) and not self.busy)

    def _set_busy(self, busy: bool) -> None:
        self.send_btn.setVisible(not busy)
        self.cancel_btn.setVisible(busy)
        self.input.setEnabled(not busy)
        for w in (self.new_btn, self.conv_list, self.del_conv_btn, self.rename_conv_btn):
            w.setEnabled(not busy)
        self._sync_send()

    # -- conversas -----------------------------------------------------------------------------
    def refresh_conversations(self) -> None:
        convs = self._guard(self.chat.list_conversations) or []
        self.conv_list.blockSignals(True)
        self.conv_list.clear()
        for c in convs:
            item = QListWidgetItem(("📌  " if c["is_pinned"] else "") + c["title"])
            item.setData(Qt.UserRole, c["id"])
            item.setData(Qt.UserRole + 1, c["is_pinned"])
            item.setData(Qt.UserRole + 2, c["title"])
            item.setToolTip(c["title"])
            self.conv_list.addItem(item)
            if c["id"] == self.conversation_id:
                self.conv_list.setCurrentItem(item)
        self.conv_list.blockSignals(False)

    def _on_conv_selected(self) -> None:
        item = self.conv_list.currentItem()
        if item is not None and not self.busy:
            self.open_conversation(item.data(Qt.UserRole))

    def open_conversation(self, conversation_id: UUID | None) -> None:
        self.conversation_id = conversation_id
        self.clear_error()
        self.messages, events, cites = [], {}, {}
        if conversation_id:
            self.messages = self._guard(self.chat.get_messages, conversation_id) or []
            for e in self._guard(self.chat.get_memory_events, conversation_id) or []:
                events.setdefault(e["message_id"], []).append(e)
            cites = self._guard(self.chat.get_citations, conversation_id) or {}
        self.view.set_messages(self.messages, events, cites)
        item = self.conv_list.currentItem()
        self.title_label.setText(item.data(Qt.UserRole + 2) if item is not None and conversation_id else "Nova conversa")
        self.refresh_proposals()

    def new_conversation(self) -> None:
        self.conv_list.clearSelection()
        self.conv_list.setCurrentItem(None)
        self.open_conversation(None)
        self.input.setFocus()

    def _conv_item(self, conversation_id: UUID) -> QListWidgetItem | None:
        for i in range(self.conv_list.count()):
            if self.conv_list.item(i).data(Qt.UserRole) == conversation_id:
                return self.conv_list.item(i)
        return None

    def _exec_menu(self, menu: QMenu, pos) -> None:
        if pos is not None:
            menu.exec(pos)

    def build_conv_menu(self, item: QListWidgetItem) -> QMenu:
        """Menu da conversa do ITEM clicado (id capturado agora, não o selecionado depois)."""
        cid, pinned = item.data(Qt.UserRole), bool(item.data(Qt.UserRole + 1))
        menu = QMenu(self)
        menu.addAction("Desafixar" if pinned else "Fixar", lambda: self.set_pinned(cid, not pinned))
        menu.addAction("Renomear", lambda: self.rename_conversation(cid))
        menu.addSeparator()
        menu.addAction("Excluir", lambda: self.delete_conversation(cid))
        menu.setEnabled(not self.busy)
        return menu

    def set_pinned(self, conversation_id: UUID, pinned: bool) -> None:
        if self.busy:
            return
        self.clear_error()
        if self._guard(self.chat.set_pinned, conversation_id, pinned) is None and not self.banner.isHidden():
            return
        self.refresh_conversations()
        self.notify("Conversa fixada." if pinned else "Conversa desafixada.")

    def rename_conversation(self, conversation_id: UUID | None = None) -> None:
        conversation_id = conversation_id or self.conversation_id
        if self.busy or conversation_id is None:
            return
        item = self._conv_item(conversation_id)
        current = item.data(Qt.UserRole + 2) if item is not None else self.title_label.text()
        text, ok = QInputDialog.getText(self, "Renomear conversa", "Título:", text=current)
        if not ok:
            return
        self.clear_error()
        title = self._guard(self.chat.rename_conversation, conversation_id, text)
        if title is not None:
            self.refresh_conversations()
            if conversation_id == self.conversation_id:
                self.title_label.setText(title)
            self.notify("Conversa renomeada.")

    def delete_conversation(self, conversation_id: UUID | None = None) -> None:
        conversation_id = conversation_id or self.conversation_id
        if self.busy or conversation_id is None:
            return
        item = self._conv_item(conversation_id)
        name = item.data(Qt.UserRole + 2) if item is not None else "esta conversa"
        ans = QMessageBox.question(
            self, "Excluir conversa",
            f"Excluir a conversa “{name}” e suas mensagens?\nAs memórias já guardadas são mantidas.")
        if ans != QMessageBox.Yes:
            return
        if self._guard(self.chat.delete_conversation, conversation_id) is None and not self.banner.isHidden():
            return
        was_open = conversation_id == self.conversation_id
        if was_open:
            self.conversation_id = None
        self.refresh_conversations()
        if was_open:
            self.open_conversation(None)
        self.refresh_memories()
        self.notify(f"Conversa “{name}” excluída.")

    # -- streaming -----------------------------------------------------------------------------
    def _flush_render(self) -> None:
        if self._dirty and self._stream_widget is not None:
            self._dirty = False
            self._stream_widget.set_text(self._partial, "generating")

    def send(self) -> None:
        text = self.input.toPlainText().strip()
        if self.busy or not text:
            return
        self.clear_error()
        self.notice.hide()
        self.input.clear()
        # eco otimista; a persistência real vem do serviço e a conversa é recarregada ao final
        self.view.add_message("user", text)
        self._stream_widget = self.view.add_message("assistant", "", "generating")
        self._partial = ""
        self._thread = QThread(self)
        self._worker = TurnWorker(self.chat, self.conversation_id, text, self.auto_memory)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.delta.connect(self._on_delta)
        self._worker.finished.connect(self._on_finished)
        self._worker.failed.connect(self._on_failed)
        self._set_busy(True)
        self._timer.start()
        self._thread.start()

    @Slot(str)
    def _on_delta(self, text: str) -> None:
        self._partial += text
        self._dirty = True

    def cancel(self) -> None:
        if self._worker is not None:
            self.cancel_btn.setEnabled(False)
            self._worker.cancel()

    def _end_thread(self) -> None:
        self._timer.stop()
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(5000)
            self._thread.deleteLater()
        self._thread = self._worker = self._stream_widget = None
        self._partial = ""
        self.cancel_btn.setEnabled(True)
        self._set_busy(False)

    @Slot(object)
    def _on_finished(self, result: TurnResult) -> None:
        self._end_thread()
        if result.deleted:
            self.conversation_id = None
        else:
            self.conversation_id = result.conversation_id
        self.refresh_conversations()
        self.open_conversation(self.conversation_id)
        if result.deleted:
            self.show_error("A conversa foi excluída durante a geração.")
            return
        if result.status == "failed":
            self.show_error(result.error or "A geração falhou.")
        elif result.status == "canceled":
            self.notify("Geração cancelada; o texto parcial foi mantido.", "info")
        self.refresh_memories()
        for w in result.warnings:
            self.notify(w, "info", 9000)
        if result.proposals and result.status == "complete":
            self.notify("Tarefa proposta: confirme ou rejeite no cartão acima do campo de mensagem.", "info", 8000)
        elif result.memory_events:
            kinds = {e["action"] for e in result.memory_events}
            self.notify("Memória atualizada." if kinds == {"updated"} else "Memória guardada.", "success")
        elif result.memory_refused:
            self.notify(f"Não guardei como memória: {result.memory_refused[0]}.", "info", 8000)
        elif result.rejected_tool_calls and result.status == "complete":
            self.notify("Uma chamada de ferramenta inválida do modelo foi ignorada.", "info", 6000)

    @Slot(str)
    def _on_failed(self, message: str) -> None:
        self._end_thread()
        self.refresh_conversations()
        self.open_conversation(self.conversation_id)
        self.show_error(f"Erro: {message}")

    # -- propostas pendentes (confirmação humana, G3) ------------------------------------------
    def refresh_proposals(self) -> None:
        while self.proposals_box.count():
            item = self.proposals_box.takeAt(0)
            if item.widget():
                item.widget().setParent(None)
                item.widget().deleteLater()
        if self.conversation_id is None:
            return
        for p in self._guard(self.memories.list_pending, self.conversation_id) or []:
            card = ProposalCard(p)
            card.confirmed.connect(self._confirm)
            card.rejected.connect(self._reject)
            self.proposals_box.addWidget(card)

    def _confirm(self, proposal_id: UUID, arguments: dict) -> None:
        self.clear_error()
        res = self._guard(self.memories.confirm_proposal, proposal_id, arguments)
        if res is not None:
            self.notify("Tarefa criada." if res.task_id else "Memória guardada.")
        self.refresh_proposals()
        self.refresh_memories()
        self.refresh_tasks()

    def _reject(self, proposal_id: UUID) -> None:
        self.clear_error()
        self._guard(self.memories.reject_proposal, proposal_id)
        self.refresh_proposals()

    # -- memórias ------------------------------------------------------------------------------
    @property
    def auto_memory(self) -> bool:
        return self.auto_mem_check.isChecked()

    def _auto_memory_pref(self) -> bool:
        return str(self.prefs.value("auto_memory", "true")).lower() != "false"

    def _on_auto_memory_toggled(self, on: bool) -> None:
        self.prefs.setValue("auto_memory", "true" if on else "false")
        self._update_mem_hint()
        self.notify("Memória automática ligada." if on else "Memória automática desligada: nada novo será guardado.", "info", 4000)

    def _update_mem_hint(self) -> None:
        self.mem_hint.setText(
            "Fatos úteis que você disser são guardados sozinhos. Edite ou exclua quando quiser."
            if self.auto_memory else "Desligada: o Nemec usa as memórias abaixo, mas não guarda novas.")

    def refresh_memories(self) -> None:
        rows = self._guard(self.memories.list_memories) or []
        self.mem_list.clear()
        for m in rows:
            item = QListWidgetItem(m["content"])
            item.setData(Qt.UserRole, m["id"])
            item.setToolTip(m["content"])
            self.mem_list.addItem(item)
        self.mem_section.set_count(len(rows))
        self._update_mem_hint()

    def build_memory_menu(self, item: QListWidgetItem) -> QMenu:
        menu = QMenu(self)
        menu.addAction("Editar", lambda: self.edit_memory(item))
        menu.addSeparator()
        menu.addAction("Excluir", lambda: self.delete_memory(item))
        return menu

    def edit_memory(self, item: QListWidgetItem | None = None) -> None:
        item = item or self.mem_list.currentItem()
        if item is None:
            return
        text, ok = QInputDialog.getMultiLineText(self, "Editar memória", "Conteúdo:", item.text())
        if ok:
            self.clear_error()
            if self._guard(self.memories.update_memory, item.data(Qt.UserRole), text) is not None:
                self.notify("Memória atualizada.")
            self.refresh_memories()

    def delete_memory(self, item: QListWidgetItem | None = None) -> None:
        item = item or self.mem_list.currentItem()
        if item is None:
            return
        if QMessageBox.question(self, "Excluir memória", f"Excluir esta memória?\n\n“{item.text()[:120]}”") == QMessageBox.Yes:
            self.clear_error()
            self._guard(self.memories.delete_memory, item.data(Qt.UserRole))
            self.refresh_memories()
            self.notify("Memória excluída.")

    # -- documentos ----------------------------------------------------------------------------
    def refresh_documents(self) -> None:
        if self.documents is None:
            return
        rows = self._guard(self.documents.list_documents) or []
        self.doc_list.clear()
        for d in rows:
            meta = DOC_STATUS.get(d["status"]) or (
                f"{d['chunk_count']} trechos" + (f" · {d['page_count']} págs." if d["source_type"] == "pdf" else " · Markdown"))
            item = QListWidgetItem(f"{d['title']}\n{meta}")
            item.setData(Qt.UserRole, d["id"])
            item.setData(Qt.UserRole + 1, d)
            item.setToolTip(f"{d['file_name']}" + (f"\n{d['error']}" if d.get("error") else ""))
            self.doc_list.addItem(item)
        self.docs_section.set_count(len(rows))

    def build_document_menu(self, item: QListWidgetItem) -> QMenu:
        doc_id = item.data(Qt.UserRole)
        menu = QMenu(self)
        menu.addAction("Visualizar detalhes", lambda: self.show_document_details(doc_id))
        menu.addSeparator()
        menu.addAction("Excluir", lambda: self.delete_document(doc_id))
        return menu

    def _doc_row(self, doc_id: UUID) -> dict | None:
        for i in range(self.doc_list.count()):
            it = self.doc_list.item(i)
            if it.data(Qt.UserRole) == doc_id:
                return it.data(Qt.UserRole + 1)
        return None

    def show_document_details(self, doc_id: UUID) -> None:
        d = self._doc_row(doc_id)
        if d is None:
            return
        kind = "PDF" if d["source_type"] == "pdf" else "Markdown"
        size = f"{d['page_count']} página(s)" if d["source_type"] == "pdf" else "sem páginas"
        lines = [f"Arquivo: {d['file_name']}", f"Tipo: {kind} ({size})", f"Situação: {DOC_STATUS.get(d['status'], 'pronto')}",
                 f"Trechos indexados: {d['chunk_count']}", f"Modelo de embeddings: {d['embedding_model']}",
                 f"Importado em: {d['imported_at'].astimezone().strftime('%d/%m/%Y %H:%M')}", f"SHA-256: {d['sha256'][:16]}…"]
        if d.get("error"):
            lines.append(f"Erro: {d['error']}")
        QMessageBox.information(self, d["title"], "\n".join(lines))

    def import_documents(self) -> None:
        """Só por diálogo de arquivo escolhido pelo usuário (G5); aceita vários arquivos."""
        if self.documents is None or self._import_thread is not None:
            return
        names, _ = QFileDialog.getOpenFileNames(
            self, "Importar documentos", "", "Documentos (*.pdf *.md *.markdown);;PDF (*.pdf);;Markdown (*.md *.markdown)")
        if names:
            self.start_import([Path(n) for n in names])

    def import_folder(self) -> None:
        """Importação pontual de uma pasta (diálogo nativo): prévia → confirmação → pipeline existente."""
        if self.documents is None or self._import_thread is not None:
            return
        chosen = QFileDialog.getExistingDirectory(self, "Importar pasta", "")
        if chosen:
            self.start_folder_import(Path(chosen))

    def start_folder_import(self, root: Path) -> None:
        scan = scan_folder(root)
        if not scan.count:
            self.notify(f"Nenhum arquivo .md ou .pdf compatível em “{root.name}”.", "info", 8000)
            return
        text = (f"Encontrei {scan.count} arquivo(s) compatível(is) em “{root.name}”: {scan.md} Markdown e {scan.pdf} PDF "
                f"(~{max(scan.total_bytes // 2**20, 1)} MB, subpastas incluídas).\n"
                f"Ignorados (ocultos, .obsidian, links, outros formatos): {scan.ignored}.\n"
                + (f"{scan.note}\n" if scan.note else "")
                + "\nImportar agora? Os arquivos originais não serão alterados; duplicados são detectados.")
        if QMessageBox.question(self, "Importar pasta", text) == QMessageBox.Yes:
            self.start_import(scan.eligible, ignored=scan.ignored, root=root)

    def start_import(self, paths: list[Path], ignored: int = 0, root: Path | None = None) -> None:
        if self.documents is None or self._import_thread is not None or not paths:
            return
        self._import_total, self._import_done, self._import_ignored = len(paths), 0, ignored
        self.notify(f"Importando 0/{len(paths)}…", "info", 0)
        for w in (self.doc_import_btn, self.doc_folder_btn, self.attach_btn):
            w.setEnabled(False)
        self.doc_cancel_btn.setEnabled(True)
        self.doc_cancel_btn.show()
        self._import_thread = QThread(self)
        self._import_worker = ImportWorker(self.documents, paths, root)
        self._import_worker.moveToThread(self._import_thread)
        self._import_thread.started.connect(self._import_worker.run)
        self._import_worker.progress.connect(self._on_import_progress)
        self._import_worker.finished.connect(self._on_import_finished)
        self._import_thread.start()

    def cancel_import(self) -> None:
        if self._import_worker is not None:
            self._import_worker.cancel_event.set()
            self.doc_cancel_btn.setEnabled(False)
            self.notify("Cancelando: o arquivo em andamento termina e os pendentes são descartados…", "info", 0)

    @Slot(object)
    def _on_import_progress(self, _r: ImportResult) -> None:
        self._import_done += 1
        self.refresh_documents()
        if self._import_worker is not None and not self._import_worker.cancel_event.is_set():
            self.notice.show_message(f"Importando {self._import_done}/{self._import_total}…", "info", 0)

    @Slot(object)
    def _on_import_finished(self, results: list[ImportResult]) -> None:
        if self._import_thread is not None:
            self._import_thread.quit()
            self._import_thread.wait(5000)
            self._import_thread.deleteLater()
        self._import_thread = self._import_worker = None
        for w in (self.doc_import_btn, self.doc_folder_btn, self.attach_btn):
            w.setEnabled(True)
        self.doc_cancel_btn.hide()
        self.refresh_documents()
        count = lambda *st: sum(1 for r in results if r.status in st)  # noqa: E731
        parts = [f"{count('ready')} importado(s)"]
        for label, n in (("duplicado(s)", count("duplicate")), ("sem texto", count("no_text")),
                         ("com erro", count("failed", "discarded")), ("cancelado(s)", count("canceled")),
                         ("ignorado(s)", self._import_ignored + count("skipped"))):
            if n:
                parts.append(f"{n} {label}")
        problems = [r for r in results if r.status not in ("ready", "canceled", "skipped")]
        if len(results) > 1 or self._import_ignored or count("canceled", "skipped"):
            self.notify("Importação concluída: " + " · ".join(parts) + ".", "success" if not problems else "info", 15000)
        elif count("ready"):
            self.notify("1 documento importado.", "success")
        else:
            self.notice.hide()
        if problems:
            shown = [f"{r.file_name}: {r.message or r.status}" for r in problems[:5]]
            if len(problems) > 5:
                shown.append(f"…e mais {len(problems) - 5}.")
            self.show_error("\n".join(shown))

    def delete_document(self, doc_id: UUID | None = None) -> None:
        if self.documents is None:
            return
        if doc_id is None:
            item = self.doc_list.currentItem()
            doc_id = item.data(Qt.UserRole) if item is not None else None
        row = self._doc_row(doc_id) if doc_id is not None else None
        if row is None:
            return
        name = f"“{row['title']}” ({row['file_name']})"
        ans = QMessageBox.question(
            self, "Excluir documento",
            f"Excluir {name} e seus dados derivados (trechos e embeddings)?\n"
            "As citações já feitas nas conversas passam a mostrar “Fonte removida”. O arquivo original não é tocado.")
        if ans != QMessageBox.Yes:
            return
        self.clear_error()
        error = removed = None
        try:
            removed = self.documents.delete_document(doc_id)
        except NemecError as exc:
            error = f"Não foi possível excluir {name}: {exc}"
        except Exception as exc:
            error = f"Não foi possível excluir {name}: erro inesperado ({exc})."
        self.refresh_documents()  # atualiza já; as citações antigas passam a "Fonte removida"
        self.open_conversation(self.conversation_id)
        if error:
            self.show_error(error)
        else:
            self.notify(f"Documento “{removed['title']}” excluído.")

    def show_source(self, citation: dict) -> None:
        self._source_dialog = SourceDialog(citation, self)
        self._source_dialog.show()

    # -- tarefas -------------------------------------------------------------------------------
    def refresh_tasks(self) -> None:
        if self.tasks is None:
            return
        rows = self._guard(self.tasks.list_tasks) or []
        self.task_list.clear()
        for t in rows:
            done = t["status"] == "done"
            due = f" · vence {t['due_at'].astimezone().strftime('%d/%m %H:%M')}" if t["due_at"] and not done else ""
            item = QListWidgetItem(f"{'☑' if done else '☐'}  {t['title']}{due}")
            item.setData(Qt.UserRole, t["id"])
            item.setData(Qt.UserRole + 1, t["status"])
            item.setToolTip(t["notes"] or t["title"])
            if done:
                font = item.font()
                font.setStrikeOut(True)
                item.setFont(font)
            self.task_list.addItem(item)
        self.tasks_section.set_count(sum(1 for t in rows if t["status"] == "open"))

    def build_task_menu(self, item: QListWidgetItem) -> QMenu:
        tid, done = item.data(Qt.UserRole), item.data(Qt.UserRole + 1) == "done"
        menu = QMenu(self)
        menu.addAction("Reabrir" if done else "Concluir", lambda: self.reopen_task(tid) if done else self.complete_task(tid))
        menu.addSeparator()
        menu.addAction("Excluir", lambda: self.delete_task(tid))
        return menu

    def _task_id(self, task_id: UUID | None) -> UUID | None:
        if task_id is not None:
            return task_id
        item = self.task_list.currentItem()
        return item.data(Qt.UserRole) if item is not None else None

    def complete_task(self, task_id: UUID | None = None) -> None:
        task_id = self._task_id(task_id)
        if task_id is not None and self.tasks is not None:
            self.clear_error()
            self._guard(self.tasks.complete_task, task_id)
            self.refresh_tasks()

    def reopen_task(self, task_id: UUID | None = None) -> None:
        task_id = self._task_id(task_id)
        if task_id is not None and self.tasks is not None:
            self.clear_error()
            self._guard(self.tasks.reopen_task, task_id)
            self.refresh_tasks()

    def delete_task(self, task_id: UUID | None = None) -> None:
        task_id = self._task_id(task_id)
        if task_id is None or self.tasks is None:
            return
        if QMessageBox.question(self, "Excluir tarefa", "Excluir esta tarefa?") == QMessageBox.Yes:
            self.clear_error()
            self._guard(self.tasks.delete_task, task_id)
            self.refresh_tasks()
            self.notify("Tarefa excluída.")

    # -- encerramento --------------------------------------------------------------------------
    def closeEvent(self, event) -> None:  # noqa: N802
        if self._worker is not None:
            self._worker.cancel()
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(8000)
        if self._import_thread is not None:
            self._import_thread.quit()
            self._import_thread.wait(8000)
        super().closeEvent(event)

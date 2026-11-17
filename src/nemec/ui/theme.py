"""Tema visual (QSS). Dois conjuntos de tokens (claro/escuro, seguindo o sistema); nenhum recurso externo."""
from __future__ import annotations

LIGHT = {
    "bg": "#ffffff", "sidebar": "#f6f6f8", "panel": "#fafafb", "border": "#e4e4e8", "text": "#1c1c1f",
    "muted": "#6b6b75", "accent": "#4a63e0", "accent_hover": "#3a50c4", "accent_text": "#ffffff",
    "hover": "#ececf1", "selected": "#e4e7fa", "user_bubble": "#eceefb", "input_bg": "#ffffff",
    "code_bg": "#f1f1f5", "error_bg": "#fdecea", "error_fg": "#a52a22", "ok_bg": "#e7f5ee", "ok_fg": "#1d6a44",
    "info_bg": "#eef1fb", "info_fg": "#3a4bb0", "warn_bg": "#fff4e0", "warn_fg": "#8a5a00", "disabled": "#b8b8c2",
}
DARK = {
    "bg": "#1a1b1e", "sidebar": "#141517", "panel": "#1f2023", "border": "#2e2f34", "text": "#ececf0",
    "muted": "#9b9ba6", "accent": "#7d92ff", "accent_hover": "#97a8ff", "accent_text": "#10131f",
    "hover": "#2a2b30", "selected": "#2d3250", "user_bubble": "#2a2e45", "input_bg": "#232428",
    "code_bg": "#26272c", "error_bg": "#3a1f1d", "error_fg": "#ff9d94", "ok_bg": "#1b3328", "ok_fg": "#7fd6a6",
    "info_bg": "#242a45", "info_fg": "#a9b6ff", "warn_bg": "#3a2e14", "warn_fg": "#f0c674", "disabled": "#55565e",
}


def tokens(dark: bool) -> dict[str, str]:
    return DARK if dark else LIGHT


def is_dark(app) -> bool:
    from PySide6.QtCore import Qt

    try:
        return app.styleHints().colorScheme() == Qt.ColorScheme.Dark
    except Exception:
        return False


def build_qss(t: dict[str, str]) -> str:
    return f"""
* {{ outline: none; }}
QMainWindow, QWidget#root, QWidget#center, QScrollArea, QScrollArea > QWidget > QWidget {{ background: {t['bg']}; color: {t['text']}; }}
QLabel {{ color: {t['text']}; background: transparent; }}
QLabel#muted, QLabel#hint, QLabel#sectionTitle {{ color: {t['muted']}; }}
QLabel#sectionTitle {{ font-size: 11px; font-weight: 600; padding: 10px 4px 4px 4px; }}
QLabel#hint {{ font-size: 11px; }}
QLabel#welcomeTitle {{ font-size: 26px; font-weight: 600; }}
QLabel#welcomeText {{ color: {t['muted']}; font-size: 14px; }}
QToolTip {{ background: {t['panel']}; color: {t['text']}; border: 1px solid {t['border']}; padding: 4px 6px; }}

/* barra lateral e painel */
QWidget#sidebar {{ background: {t['sidebar']}; border-right: 1px solid {t['border']}; }}
QWidget#sidePanel {{ background: {t['panel']}; border-left: 1px solid {t['border']}; }}
QWidget#sidebar QLabel, QWidget#sidePanel QLabel {{ background: transparent; }}
QLabel#brand {{ font-size: 15px; font-weight: 700; padding: 2px 4px; }}
QListWidget {{ background: transparent; border: none; color: {t['text']}; }}
QListWidget::item {{ padding: 8px 10px; border-radius: 8px; margin: 1px 0; }}
QListWidget::item:hover {{ background: {t['hover']}; }}
QListWidget::item:selected {{ background: {t['selected']}; color: {t['text']}; }}
QListWidget#memList::item, QListWidget#docList::item, QListWidget#taskList::item {{ padding: 7px 8px; font-size: 12px; }}

/* botões */
QPushButton {{ background: transparent; color: {t['text']}; border: 1px solid {t['border']}; border-radius: 8px; padding: 7px 12px; }}
QPushButton:hover {{ background: {t['hover']}; }}
QPushButton:disabled {{ color: {t['disabled']}; }}
QPushButton#newChat {{ text-align: left; font-weight: 600; padding: 9px 12px; }}
QPushButton#flat, QPushButton#danger, QToolButton#flat {{ border: none; color: {t['muted']}; padding: 5px 8px; }}
QPushButton#flat:hover, QPushButton#danger:hover, QToolButton#flat:hover {{ color: {t['text']}; background: {t['hover']}; }}
QPushButton#danger:hover {{ color: {t['error_fg']}; background: {t['error_bg']}; }}
QPushButton#primary, QPushButton#confirmBtn {{ background: {t['accent']}; color: {t['accent_text']}; border: none; font-weight: 600; }}
QPushButton#primary:hover, QPushButton#confirmBtn:hover {{ background: {t['accent_hover']}; }}
QPushButton#primary:disabled, QPushButton#confirmBtn:disabled {{ background: {t['border']}; color: {t['disabled']}; }}
QToolButton {{ border: none; border-radius: 8px; padding: 6px; color: {t['muted']}; background: transparent; font-size: 15px; }}
QToolButton::menu-indicator {{ image: none; width: 0; }}
QToolButton:hover {{ background: {t['hover']}; color: {t['text']}; }}
QToolButton:disabled {{ color: {t['disabled']}; }}
QToolButton#send {{ background: {t['accent']}; color: {t['accent_text']}; border-radius: 15px; min-width: 30px; max-width: 30px; min-height: 30px; max-height: 30px; font-weight: 700; }}
QToolButton#send:hover {{ background: {t['accent_hover']}; }}
QToolButton#send:disabled {{ background: {t['border']}; color: {t['disabled']}; }}
QToolButton#cancel {{ background: {t['text']}; color: {t['bg']}; border-radius: 15px; min-width: 30px; max-width: 30px; min-height: 30px; max-height: 30px; }}
QToolButton#cancel:disabled {{ background: {t['border']}; }}

/* cabeçalho */
QWidget#header {{ background: {t['bg']}; border-bottom: 1px solid {t['border']}; }}
QLabel#title {{ font-size: 14px; font-weight: 600; }}
QLabel#chip {{ background: {t['hover']}; color: {t['muted']}; border-radius: 9px; padding: 2px 9px; font-size: 11px; }}

/* mensagens */
QFrame#userBubble {{ background: {t['user_bubble']}; border-radius: 16px; }}
QFrame#userBubble QLabel {{ font-size: 14px; }}
QLabel#assistantText {{ font-size: 14px; }}
QLabel#avatar {{ background: {t['accent']}; color: {t['accent_text']}; border-radius: 13px; font-weight: 700; font-size: 12px; }}
QLabel#memoryNote {{ color: {t['info_fg']}; background: {t['info_bg']}; border-radius: 9px; padding: 3px 10px; font-size: 12px; }}
QLabel#statusChip {{ border-radius: 9px; padding: 2px 9px; font-size: 11px; }}
QLabel#statusChip[kind="error"] {{ background: {t['error_bg']}; color: {t['error_fg']}; }}
QLabel#statusChip[kind="warn"] {{ background: {t['warn_bg']}; color: {t['warn_fg']}; }}
QLabel#statusChip[kind="info"] {{ background: {t['info_bg']}; color: {t['info_fg']}; }}

/* avisos */
QFrame#banner {{ border-radius: 10px; }}
QFrame#banner[kind="error"] {{ background: {t['error_bg']}; }}
QFrame#banner[kind="success"] {{ background: {t['ok_bg']}; }}
QFrame#banner[kind="info"] {{ background: {t['info_bg']}; }}
QFrame#banner[kind="error"] QLabel {{ color: {t['error_fg']}; }}
QFrame#banner[kind="success"] QLabel {{ color: {t['ok_fg']}; }}
QFrame#banner[kind="info"] QLabel {{ color: {t['info_fg']}; }}
QFrame#banner QToolButton {{ color: inherit; padding: 2px 6px; font-size: 12px; }}

/* campo de mensagem */
QFrame#composer {{ background: {t['input_bg']}; border: 1px solid {t['border']}; border-radius: 22px; }}
QFrame#composer[focused="true"] {{ border-color: {t['accent']}; }}
QPlainTextEdit#input {{ background: transparent; border: none; color: {t['text']}; font-size: 14px; selection-background-color: {t['selected']}; }}

/* menus de ações */
QMenu {{ background: {t['panel']}; color: {t['text']}; border: 1px solid {t['border']}; border-radius: 8px; padding: 4px; }}
QMenu::item {{ padding: 6px 18px; border-radius: 6px; }}
QMenu::item:selected {{ background: {t['selected']}; }}
QMenu::item:disabled {{ color: {t['disabled']}; }}
QMenu::separator {{ height: 1px; background: {t['border']}; margin: 4px 6px; }}

/* fontes citadas e diálogo de fonte */
QPushButton#sourceChip {{ background: {t['info_bg']}; color: {t['info_fg']}; border: none; border-radius: 9px; padding: 2px 10px; font-size: 11px; }}
QPushButton#sourceChip:hover {{ background: {t['selected']}; }}
QDialog {{ background: {t['bg']}; color: {t['text']}; }}
QDialog QPlainTextEdit {{ background: {t['input_bg']}; border: 1px solid {t['border']}; border-radius: 8px; color: {t['text']}; padding: 6px; }}
QLineEdit {{ background: {t['input_bg']}; border: 1px solid {t['border']}; border-radius: 8px; color: {t['text']}; padding: 6px 8px; }}
QLineEdit:focus {{ border-color: {t['accent']}; }}

/* cartões de proposta */
QFrame#proposalCard {{ background: {t['panel']}; border: 1px solid {t['border']}; border-radius: 12px; }}
QFrame#proposalCard QPlainTextEdit {{ background: {t['input_bg']}; border: 1px solid {t['border']}; border-radius: 8px; color: {t['text']}; }}

/* seções recolhíveis */
QPushButton#sectionHeader {{ text-align: left; font-weight: 600; font-size: 12px; color: {t['text']}; padding: 8px 6px; border: none; border-radius: 8px; }}
QPushButton#sectionHeader:hover {{ background: {t['hover']}; }}
QFrame#sectionBody {{ background: transparent; border: none; }}
QCheckBox {{ color: {t['text']}; spacing: 8px; font-size: 12px; }}
QCheckBox::indicator {{ width: 16px; height: 16px; border: 1px solid {t['muted']}; border-radius: 4px; background: {t['input_bg']}; }}
QCheckBox::indicator:checked {{ background: {t['accent']}; border-color: {t['accent']}; }}

/* rolagem e divisores */
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {t['border']}; border-radius: 4px; min-height: 28px; }}
QScrollBar::handle:vertical:hover {{ background: {t['muted']}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QSplitter::handle {{ background: transparent; width: 1px; }}
QStatusBar {{ background: {t['bg']}; color: {t['muted']}; border-top: 1px solid {t['border']}; font-size: 11px; }}
QStatusBar::item {{ border: none; }}
"""

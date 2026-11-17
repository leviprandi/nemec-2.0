"""Política da memória automática: segredos, ancoragem na fala do usuário e duplicidade.

Funções puras, sem banco. O LLM só *propõe* o texto (ferramenta `save_memory`); quem decide se grava,
atualiza ou ignora é este módulo + o serviço, dentro de uma transação.
"""
from __future__ import annotations

import re
import unicodedata

_STOPWORDS = frozenset(
    "que com para por uma uns umas dos das nos nas seu sua seus suas meu minha meus minhas tem tenho sao sou "
    "esta este isso isto como mais muito mas the and usuario gosta prefere quer".split()
)

# Padrões de credenciais/segredos e identificadores sensíveis: nunca viram memória automática.
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("senha ou credencial", re.compile(r"\b(senha|password|passwd|passphrase|pin)\b", re.I)),
    ("token ou chave de API", re.compile(r"\b(api[ _-]?key|apikey|secret|token|bearer|chave (de )?(api|privada|secreta))\b", re.I)),
    ("chave privada", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----", re.I)),
    ("chave de serviço", re.compile(r"\b(sk|pk|ghp|gho|xox[bap]|AKIA)[-_][A-Za-z0-9_-]{12,}|\bAKIA[0-9A-Z]{12,}\b")),
    ("sequência longa tipo segredo", re.compile(r"\b[A-Za-z0-9+/_-]{32,}\b")),
    ("CPF", re.compile(r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b")),
    ("número de cartão", re.compile(r"\b(?:\d[ -]?){13,19}\b")),
)


def secret_reason(text: str) -> str | None:
    """Motivo (legível) se o texto parece conter credencial/segredo; senão None."""
    for reason, pattern in _SECRET_PATTERNS:
        if pattern.search(text):
            return reason
    return None


def normalize(text: str) -> str:
    """minúsculas, sem acentos, sem pontuação, espaços colapsados."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    plain = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return " ".join(re.sub(r"[^\w\s]", " ", plain).split())


def tokens(text: str) -> set[str]:
    return {t for t in normalize(text).split() if len(t) >= 3 and t not in _STOPWORDS}


_EDGE = " \t\n.,;:!?\"'“”‘’()[]{}«»…-"


def literal_span(content: str, user_text: str) -> str | None:
    """Fronteira de origem (G2a): o modelo só SELECIONA um trecho; o Python exige que ele apareça, palavra por
    palavra (sem diferenciar maiúsculas nem pontuação das bordas), na mensagem ATUAL do usuário e devolve o trecho
    exato dessa mensagem, que é o que se persiste. Paráfrases, títulos, catálogo, histórico, trechos e resultados de
    ferramentas não passam: o que não está literalmente na mensagem não vira memória."""
    found = _locate(content, user_text)
    if found is None:
        return None
    start, end = found
    return user_text[start:end] if _attributed_to_user(user_text, start, end) else None


_QUOTED = re.compile(r"“[^”]*”|«[^»]*»|\"[^\"]*\"|‘[^’]*’")
_ATTRIBUTION = re.compile(
    r"\b(diz|disse|dizem|falou|fala|escreveu|escreve|afirma|afirmou|alega|segundo|conforme|citou|cita|menciona|contou|conta|"
    r"acha|acredita|ele|ela|eles|elas|personagem|autor|autora|documento|arquivo|texto|apostila|t[ií]tulo|professor|"
    r"professora|colega|amigo|amiga|chefe|alguem|alguém|pessoa)\b")
_FIRST_PERSON = frozenset("eu me mim comigo nos nós".split())  # possessivo NÃO é evidência de autoria (A01)
_POSSESSIVES = frozenset("meu minha meus minhas nosso nossa nossos nossas".split())
# Atributos pessoais claros: "meu/minha <substantivo>" só conta como sobre o usuário se o núcleo for um destes.
_SELF_NOUNS = frozenset(
    "nome sobrenome apelido idade aniversario aniversário cidade profissao profissão trabalho emprego curso faculdade "
    "time hobby objetivo meta".split())
_FILLERS = frozenset("na verdade agora também tambem sempre só so atualmente hoje ainda bem então entao mas e ah olha".split())
_NOT_VERB = frozenset("o a os as um uma uns umas ao do da dos das no".split())


def _attributed_to_user(text: str, start: int, end: int) -> bool:
    """Política conservadora (A01): só vira memória o que é CLARAMENTE afirmação do próprio usuário. Recusa trecho
    dentro de aspas/citação, em frase com verbo de atribuição ou sujeito de terceiro antes do trecho, e trecho sem
    marca de 1ª pessoa (pronome/possessivo ou verbo na 1ª pessoa do singular (-o/-ei; "-ou" é 3ª pessoa do pretérito e NÃO conta), depois de advérbios de ligação).
    Heurística lexical e deliberadamente restritiva: na dúvida, não grava. Não prova semântica."""
    if any(m.start() < end and start < m.end() for m in _QUOTED.finditer(text)):
        return False
    sent_start = max((text.rfind(c, 0, start) for c in ".!?\n"), default=-1) + 1
    if _ATTRIBUTION.search(text[sent_start:start].casefold()):
        return False
    words = [w.strip(_EDGE).casefold() for w in text[start:end].split()]
    words = [w for w in words if w]
    before = [w.strip(_EDGE).casefold() for w in text[sent_start:start].split()]
    if _third_party_possessive(before) or _third_party_possessive(words):
        return False  # "minha vizinha…", "meu irmão…": a informação principal é sobre outra pessoa/coisa
    if _FIRST_PERSON & set(words) or any(w in _POSSESSIVES for w in words):  # (possessivos já validados acima)
        return True
    core = [w for w in words if w not in _FILLERS]
    return len(core) >= 2 and core[0] not in _NOT_VERB and bool(re.fullmatch(r"[a-zà-ú]{3,}(o|ei)", core[0]))


def _third_party_possessive(words: list[str]) -> bool:
    """Possessivo seguido de algo que não é um atributo pessoal claro (ou sem núcleo) → ambíguo/terceiro."""
    for i, w in enumerate(words):
        if w in _POSSESSIVES:
            nxt = next((x for x in words[i + 1 :] if x), "")
            if nxt not in _SELF_NOUNS and not (nxt and any(x in ("favorito", "favorita", "favoritos", "favoritas") for x in words[i + 1 : i + 4])):
                return True
    return False


def _locate(content: str, user_text: str) -> tuple[int, int] | None:
    needle = [w.strip(_EDGE).casefold() for w in content.split()]
    needle = [w for w in needle if w]
    if len(needle) == 0 or sum(len(w) for w in needle) < 3:
        return None
    spans = [(m.start(), m.end(), m.group().strip(_EDGE).casefold()) for m in re.finditer(r"\S+", user_text)]
    spans = [s for s in spans if s[2]]
    for i in range(len(spans) - len(needle) + 1):
        if [s[2] for s in spans[i : i + len(needle)]] == needle:
            s, e = spans[i][0], spans[i + len(needle) - 1][1]
            while e > s and user_text[e - 1] in _EDGE:
                e -= 1
            while s < e and user_text[s] in _EDGE:
                s += 1
            return s, e
    return None


SIMILARITY_THRESHOLD = 0.75


def similarity(a: str, b: str) -> float:
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def find_duplicate(content: str, existing: list[dict]) -> tuple[dict | None, bool]:
    """(memória existente equivalente, idêntica?) — idêntica após normalização, ou muito parecida."""
    norm = normalize(content)
    best, best_score = None, 0.0
    for mem in existing:
        if normalize(mem["content"]) == norm:
            return mem, True
        score = similarity(content, mem["content"])
        if score > best_score:
            best, best_score = mem, score
    if best is not None and best_score >= SIMILARITY_THRESHOLD:
        return best, False
    return None, False

"""Estado local em SQLite: chave geral, idempotência, orçamentos, pendências e log de chamadas."""
import json
import sqlite3
import threading
import time
import uuid


class Estado:
    def __init__(self, caminho: str):
        self._db = sqlite3.connect(caminho, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS chave (nome TEXT PRIMARY KEY, valor TEXT);
                CREATE TABLE IF NOT EXISTS idempotencia (chave TEXT PRIMARY KEY, resposta TEXT, criado REAL);
                CREATE TABLE IF NOT EXISTS orcamento (id TEXT PRIMARY KEY, conversa TEXT, cliente INTEGER,
                    dados TEXT, criado REAL);
                CREATE TABLE IF NOT EXISTS pendencia (id TEXT PRIMARY KEY, conversa TEXT, tipo TEXT, resumo TEXT, criado REAL);
                CREATE TABLE IF NOT EXISTS mensagem (id INTEGER PRIMARY KEY AUTOINCREMENT, conversa TEXT, papel TEXT,
                    conteudo TEXT, criado REAL);
                CREATE INDEX IF NOT EXISTS idx_mensagem_conversa ON mensagem (conversa, id);
                CREATE TABLE IF NOT EXISTS uso_agente (dia TEXT PRIMARY KEY, chamadas INTEGER);
                CREATE TABLE IF NOT EXISTS chamada (id INTEGER PRIMARY KEY AUTOINCREMENT, acao TEXT, conversa TEXT,
                    ms INTEGER, ok INTEGER, codigo TEXT, criado REAL);
                """
            )

    def _exec(self, sql: str, args: tuple = ()):
        with self._lock:
            cur = self._db.execute(sql, args)
            self._db.commit()
            return cur

    def _um(self, sql: str, args: tuple = ()):
        with self._lock:
            return self._db.execute(sql, args).fetchone()

    # chave geral
    def desligado(self) -> bool:
        r = self._um("SELECT valor FROM chave WHERE nome='desligado'")
        return bool(r and r[0] == "1")

    def definir_desligado(self, v: bool) -> None:
        self._exec("INSERT OR REPLACE INTO chave VALUES ('desligado', ?)", ("1" if v else "0",))

    # idempotência
    def resposta_salva(self, chave: str):
        r = self._um("SELECT resposta FROM idempotencia WHERE chave=?", (chave,))
        return json.loads(r[0]) if r else None

    def salvar_resposta(self, chave: str, resposta: dict) -> None:
        self._exec("INSERT OR REPLACE INTO idempotencia VALUES (?,?,?)", (chave, json.dumps(resposta), time.time()))

    # orçamento
    def salvar_orcamento(self, conversa: str, cliente: int, dados: dict) -> str:
        oid = "ORC-" + uuid.uuid4().hex[:10].upper()
        self._exec("INSERT INTO orcamento VALUES (?,?,?,?,?)", (oid, conversa, cliente, json.dumps(dados), time.time()))
        return oid

    def obter_orcamento(self, oid: str):
        r = self._um("SELECT conversa, cliente, dados, criado FROM orcamento WHERE id=?", (oid,))
        if not r:
            return None
        return {"conversa": r[0], "cliente": r[1], "dados": json.loads(r[2]), "criado": r[3]}

    # pendência
    def salvar_pendencia(self, conversa: str, tipo: str, resumo: str) -> str:
        pid = "PEN-" + uuid.uuid4().hex[:10].upper()
        self._exec("INSERT INTO pendencia VALUES (?,?,?,?,?)", (pid, conversa, tipo, resumo[:2000], time.time()))
        return pid

    # log (sem segredos, sem dados pessoais)
    def registrar_chamada(self, acao: str, conversa: str, ms: int, ok: bool, codigo: str) -> None:
        self._exec("INSERT INTO chamada (acao, conversa, ms, ok, codigo, criado) VALUES (?,?,?,?,?,?)",
                   (acao, conversa, ms, 1 if ok else 0, codigo, time.time()))

    # chave genérica (ex.: cliente identificado numa conversa)
    def ler_chave(self, nome: str):
        r = self._um("SELECT valor FROM chave WHERE nome=?", (nome,))
        return r[0] if r else None

    def gravar_chave(self, nome: str, valor: str) -> None:
        self._exec("INSERT OR REPLACE INTO chave VALUES (?, ?)", (nome, valor))

    # histórico de conversa do agente
    def historico(self, conversa: str, desde: float, limite: int = 40) -> list[dict]:
        with self._lock:
            linhas = self._db.execute(
                "SELECT papel, conteudo FROM mensagem WHERE conversa=? AND criado>=? ORDER BY id DESC LIMIT ?",
                (conversa, desde, limite)).fetchall()
        msgs = [{"role": p, "content": json.loads(c)} for p, c in reversed(linhas)]
        # começa sempre numa fala de texto do cliente (nunca no meio de uma chamada de ferramenta)
        while msgs and not (msgs[0]["role"] == "user" and isinstance(msgs[0]["content"], str)):
            msgs.pop(0)
        return msgs

    def adicionar_mensagens(self, conversa: str, msgs: list[dict]) -> None:
        agora = time.time()
        with self._lock:
            self._db.executemany("INSERT INTO mensagem (conversa, papel, conteudo, criado) VALUES (?,?,?,?)",
                                 [(conversa, m["role"], json.dumps(m["content"], ensure_ascii=False), agora) for m in msgs])
            self._db.commit()

    # limite diário de chamadas ao modelo
    def uso_do_dia(self, dia: str) -> int:
        r = self._um("SELECT chamadas FROM uso_agente WHERE dia=?", (dia,))
        return r[0] if r else 0

    def somar_uso(self, dia: str) -> None:
        self._exec("INSERT INTO uso_agente VALUES (?, 1) ON CONFLICT(dia) DO UPDATE SET chamadas = chamadas + 1", (dia,))

    def reiniciar_conversa(self, conversa: str) -> int:
        """Apaga só a memória local do agente para esta conversa (histórico e cliente identificado)."""
        with self._lock:
            n = self._db.execute("DELETE FROM mensagem WHERE conversa=?", (conversa,)).rowcount
            self._db.execute("DELETE FROM chave WHERE nome=?", (f"cli:{conversa}",))
            self._db.commit()
        return n

    # consulta administrativa (acompanhar testes e ajustar o agente)
    def conversa_completa(self, conversa: str, limite: int = 100) -> list[dict]:
        with self._lock:
            linhas = self._db.execute(
                "SELECT papel, conteudo, criado FROM mensagem WHERE conversa=? ORDER BY id DESC LIMIT ?",
                (conversa, limite)).fetchall()
        return [{"papel": p, "conteudo": json.loads(c), "criado": t} for p, c, t in reversed(linhas)]

    def pendencias_recentes(self, limite: int = 30) -> list[dict]:
        with self._lock:
            linhas = self._db.execute(
                "SELECT id, conversa, tipo, resumo, criado FROM pendencia ORDER BY criado DESC LIMIT ?", (limite,)).fetchall()
        return [{"id": i, "conversa": c, "tipo": t, "resumo": r, "criado": d} for i, c, t, r, d in linhas]

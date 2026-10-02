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

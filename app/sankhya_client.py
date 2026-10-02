"""Cliente mínimo para o API Gateway do Sankhya Om (somente biblioteca padrão).

Estado: o fluxo de autenticação, a URL e a forma do corpo de requisição seguem a documentação oficial.
A forma da RESPOSTA do loadRecords (`entities.metadata.fields.field` / `entities.entity` com f0, f1...)
é uma suposição a confirmar no sandbox. Ver references/consulta-e-gravacao.md.

Variáveis de ambiente: SANKHYA_CLIENT_ID, SANKHYA_CLIENT_SECRET, SANKHYA_X_TOKEN,
SANKHYA_ENV (sandbox | production; padrão sandbox).
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

# Serviços permitidos no modo somente leitura (padrão).
READ_ONLY_SERVICES = {
    "CRUDServiceProvider.loadRecords",
    "CRUDServiceProvider.loadRecord",
}

BASES = {
    "production": "https://api.sankhya.com.br",
    "sandbox": "https://api.sandbox.sankhya.com.br",
}


class SankhyaError(Exception):
    """Erro de regra de negócio (HTTP 200 com status != 1) ou falha HTTP."""

    def __init__(self, message: str, *, http_status: int | None = None, body: Any = None):
        super().__init__(message)
        self.http_status = http_status
        self.body = body


class SankhyaClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        x_token: str,
        env: str = "sandbox",
        timeout: float = 30.0,
        refresh_skew: float = 60.0,
        transport: Callable[..., tuple[int, bytes]] | None = None,
        clock: Callable[[], float] = time.time,
        read_only: bool = True,
        max_pages_without_approval: int = 5,
    ):
        if env not in BASES:
            raise ValueError("env deve ser 'sandbox' ou 'production'")
        self.base = BASES[env]
        self.env = env
        self._cid, self._secret, self._xtoken = client_id, client_secret, x_token
        self.timeout = timeout
        self.refresh_skew = refresh_skew
        self._transport = transport or self._http
        self._clock = clock
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = threading.Lock()
        # Padrão seguro: só leitura. Escrever exige read_only=False de forma explícita.
        self.read_only = read_only
        self.max_pages_without_approval = max_pages_without_approval

    @classmethod
    def from_env(cls, **kw: Any) -> "SankhyaClient":
        env = os.environ.get("SANKHYA_ENV", "sandbox")
        return cls(
            os.environ.get("SANKHYA_CLIENT_ID", ""),
            os.environ.get("SANKHYA_CLIENT_SECRET", ""),
            os.environ.get("SANKHYA_X_TOKEN", ""),
            env=env,
            **kw,
        )

    # ---------- HTTP ----------
    def _http(self, method: str, url: str, headers: dict, data: bytes | None) -> tuple[int, bytes]:
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    # ---------- autenticação ----------
    def _authenticate(self) -> None:
        form = urllib.parse.urlencode(
            {
                "client_id": self._cid,
                "client_secret": self._secret,
                "grant_type": "client_credentials",
            }
        ).encode()
        status, raw = self._transport(
            "POST",
            f"{self.base}/authenticate",
            {"Content-Type": "application/x-www-form-urlencoded", "X-Token": self._xtoken},
            form,
        )
        if status != 200:
            # nunca incluir credenciais na mensagem
            raise SankhyaError(f"Falha na autenticação (HTTP {status})", http_status=status)
        body = json.loads(raw)
        self._token = body["access_token"]
        self._expires_at = self._clock() + float(body.get("expires_in", 300))

    def token(self) -> str:
        with self._lock:
            if not self._token or self._clock() >= self._expires_at - self.refresh_skew:
                self._authenticate()
            assert self._token
            return self._token

    # ---------- chamada de serviço ----------
    def call(
        self,
        service: str,
        request_body: dict,
        module: str = "mge",
        retry_reads: bool = False,
        max_attempts: int = 3,
        _allow_select: bool = False,
    ) -> dict:
        if self.read_only and service not in READ_ONLY_SERVICES and not _allow_select:
            raise SankhyaError(f"Modo somente leitura: serviço '{service}' bloqueado")
        url = f"{self.base}/gateway/v1/{module}/service.sbr?" + urllib.parse.urlencode(
            {"serviceName": service, "outputType": "json"}
        )
        payload = json.dumps({"serviceName": service, "requestBody": request_body}).encode()
        attempts = max_attempts if retry_reads else 1
        renewed = False
        attempt = 0
        while attempt < attempts:
            headers = {
                "Authorization": f"Bearer {self.token()}",
                "Content-Type": "application/json",
            }
            status, raw = self._transport("POST", url, headers, payload)
            if status in (401, 403) and not renewed:
                # token expirado ou revogado: renova uma vez e repete sem gastar tentativa.
                # Seguro também para escrita: 401/403 vêm do Gateway, antes de executar o serviço.
                renewed = True
                with self._lock:
                    self._token = None
                continue
            attempt += 1
            if status >= 500 and attempt < attempts:
                time.sleep(min(2 ** attempt, 8))
                continue
            if status != 200:
                raise SankhyaError(f"HTTP {status} em {service}", http_status=status, body=raw[:500])
            body = json.loads(raw)
            # HTTP 200 não garante sucesso: olhar o corpo
            if str(body.get("status")) != "1":
                msg = body.get("statusMessage") or body.get("tsiStatusMessage") or "erro sem mensagem"
                raise SankhyaError(f"{service}: {msg}", http_status=200, body=body)
            return body
        raise SankhyaError(f"Sem resposta válida de {service}")

    # ---------- consulta SQL somente SELECT (código controlado, nunca texto de usuário) ----------
    _FORBIDDEN = re.compile(
        r"\b(insert|update|delete|merge|drop|alter|create|truncate|exec|execute|grant|revoke|call)\b|;|--|/\*",
        re.IGNORECASE,
    )

    def query_select(self, sql: str) -> dict:
        """Executa um SELECT fixo escrito no código. Recusa qualquer outra coisa.
        Uso restrito a investigação e configuração. Nunca passar texto vindo de cliente final."""
        if not re.match(r"^\s*select\b", sql, re.IGNORECASE) or self._FORBIDDEN.search(sql):
            raise SankhyaError("query_select aceita apenas SELECT simples")
        return self.call("DbExplorerSP.executeQuery", {"sql": sql}, retry_reads=True, _allow_select=True)

    def select_fixo(self, sql: str) -> list[dict]:
        """SELECT montado em código com termos já validados (somente [A-Z0-9 ]). Devolve lista de dicts."""
        if not re.match(r"^\s*select\b", sql, re.IGNORECASE) or re.search(r";|--|/\*", sql):
            raise SankhyaError("select_fixo aceita apenas SELECT simples")
        body = self.call("DbExplorerSP.executeQuery", {"sql": sql}, retry_reads=True, _allow_select=True)
        rb = body.get("responseBody") or {}
        nomes = [m.get("name") for m in rb.get("fieldsMetadata", [])]
        return [dict(zip(nomes, linha)) for linha in rb.get("rows", [])]

    # ---------- REST (somente GET, sempre leitura) ----------
    def rest_get(self, path: str, query: dict | None = None) -> dict:
        """GET em endpoint REST (ex.: /v1/estoque/produtos/4599). Somente leitura."""
        url = f"{self.base}{path}"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        for renewed in (False, True):
            headers = {"Authorization": f"Bearer {self.token()}", "Accept": "application/json"}
            status, raw = self._transport("GET", url, headers, None)
            if status in (401, 403) and not renewed:
                with self._lock:
                    self._token = None
                continue
            break
        if status != 200:
            raise SankhyaError(f"HTTP {status} em GET {path}", http_status=status, body=raw[:300])
        return json.loads(raw)

    # POSTs REST que são apenas CONSULTA (cálculo de preço). Qualquer outro caminho é recusado.
    REST_POST_LEITURA = {"/v1/precos/contextualizado"}

    def rest_post_leitura(self, path: str, body: dict) -> dict:
        if path not in self.REST_POST_LEITURA:
            raise SankhyaError(f"POST REST '{path}' não está na lista de consultas permitidas")
        url = f"{self.base}{path}"
        payload = json.dumps(body).encode()
        for renewed in (False, True):
            headers = {
                "Authorization": f"Bearer {self.token()}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
            status, raw = self._transport("POST", url, headers, payload)
            if status in (401, 403) and not renewed:
                with self._lock:
                    self._token = None
                continue
            break
        if status != 200:
            raise SankhyaError(f"HTTP {status} em POST {path}", http_status=status, body=raw[:300])
        return json.loads(raw)

    # ---------- leitura ----------
    @staticmethod
    def _params(values: list[tuple[str, Any]] | None) -> list[dict]:
        return [{"$": str(v), "type": t} for t, v in (values or [])]

    def load_records(
        self,
        entity: str,
        fields: list[str],
        expression: str | None = None,
        params: list[tuple[str, Any]] | None = None,
        max_pages: int = 5,
        approved_heavy: bool = False,
    ) -> list[dict]:
        """Lê todas as páginas de uma entidade e devolve lista de dicts {campo: valor}.

        params: lista de (tipo, valor) com tipo em D, H, F, I, S. Um por '?' na expressão.
        """
        if max_pages > self.max_pages_without_approval and not approved_heavy:
            raise SankhyaError(
                f"Consulta pesada ({max_pages} páginas): peça permissão ao usuário "
                "e repita com approved_heavy=True"
            )
        rows: list[dict] = []
        for page in range(max_pages):
            dataset: dict[str, Any] = {
                "rootEntity": entity,
                "includePresentationFields": "N",
                "offsetPage": str(page),
                "entity": {"fieldset": {"list": ", ".join(fields)}},
            }
            if expression:
                dataset["criteria"] = {
                    "expression": {"$": expression},
                    "parameter": self._params(params),
                }
            body = self.call(
                "CRUDServiceProvider.loadRecords", {"dataSet": dataset}, retry_reads=True
            )
            chunk = self.parse_entities(body, fields)
            rows.extend(chunk)
            if not chunk:
                break
        return rows

    @staticmethod
    def parse_entities(body: dict, fields: list[str]) -> list[dict]:
        """[A CONFIRMAR] Forma assumida: responseBody.entities.entity = dict ou lista,
        com cada campo em f0, f1... como {"$": valor}, na ordem de metadata.fields.field."""
        ents = (body.get("responseBody") or {}).get("entities") or {}
        entity = ents.get("entity")
        if entity is None:
            return []
        if isinstance(entity, dict):
            entity = [entity]
        meta = (ents.get("metadata") or {}).get("fields", {}).get("field", [])
        if isinstance(meta, dict):
            meta = [meta]
        names = [m.get("name") for m in meta] or fields
        out = []
        for e in entity:
            row = {}
            for i, name in enumerate(names):
                cell = e.get(f"f{i}")
                row[name] = cell.get("$") if isinstance(cell, dict) else cell
            out.append(row)
        return out

    # ---------- gravação ----------
    def save(
        self,
        entity: str,
        fields: list[str],
        values: dict[str, Any],
        pk: dict[str, Any] | None = None,
        foreign_key: dict[str, Any] | None = None,
    ) -> dict:
        """DatasetSP.save de um registro. Com pk: UPDATE. Sem pk: INSERT.

        Escrita NÃO é repetida automaticamente. Em timeout, consulte antes de tentar de novo.
        """
        idx = {f: i for i, f in enumerate(fields)}
        unknown = [k for k in values if k not in idx]
        if unknown:
            raise ValueError(f"campos fora de 'fields': {unknown}")
        record: dict[str, Any] = {"values": {str(idx[k]): str(v) for k, v in values.items()}}
        if pk:
            record["pk"] = {k: str(v) for k, v in pk.items()}
        if foreign_key:
            record["foreignKey"] = {k: str(v) for k, v in foreign_key.items()}
        return self.call(
            "DatasetSP.save",
            {"entityName": entity, "standAlone": False, "fields": fields, "records": [record]},
            retry_reads=False,
        )

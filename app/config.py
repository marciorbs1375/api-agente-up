"""Configuração só por variáveis de ambiente. Nenhum segredo no código."""
import os
from dataclasses import dataclass


def _bool(v: str | None, padrao: bool = False) -> bool:
    return padrao if v is None else v.strip().lower() in ("1", "true", "sim", "s", "yes")


def _int(v: str | None) -> int | None:
    return int(v) if v not in (None, "") else None


@dataclass(frozen=True)
class Config:
    api_key_agente: str
    api_key_admin: str
    escrita_habilitada: bool = False
    empresa_padrao: int = 2
    local_estoque_padrao: int = 101000
    top_orcamento: int = 37
    top_pedido: int = 3100
    codvend_agente: int | None = None
    tipneg_padrao: int | None = None
    valor_max_pedido: float = 2000.0
    max_itens: int = 20
    quantidade_max_item: float = 999.0
    db_path: str = "/data/agente.db"
    anthropic_api_key: str = ""
    agente_modelo: str = "claude-sonnet-5-5"
    agente_limite_diario: int = 3000
    versao: str = "0.2.0"

    @classmethod
    def from_env(cls) -> "Config":
        e = os.environ
        agente, admin = e.get("API_KEY_AGENTE", ""), e.get("API_KEY_ADMIN", "")
        if len(agente) < 24 or len(admin) < 24 or agente == admin:
            raise RuntimeError("API_KEY_AGENTE e API_KEY_ADMIN: obrigatórias, diferentes, com 24+ caracteres")
        return cls(
            api_key_agente=agente,
            api_key_admin=admin,
            escrita_habilitada=_bool(e.get("ESCRITA_HABILITADA")),
            empresa_padrao=int(e.get("EMPRESA_PADRAO", 2)),
            local_estoque_padrao=int(e.get("LOCAL_ESTOQUE_PADRAO", 101000)),
            top_orcamento=int(e.get("TOP_ORCAMENTO", 37)),
            top_pedido=int(e.get("TOP_PEDIDO", 3100)),
            codvend_agente=_int(e.get("CODVEND_AGENTE")),
            tipneg_padrao=_int(e.get("TIPNEG_PADRAO")),
            valor_max_pedido=float(e.get("VALOR_MAX_PEDIDO", 2000)),
            max_itens=int(e.get("MAX_ITENS", 20)),
            db_path=e.get("DB_PATH", "/data/agente.db"),
            anthropic_api_key=e.get("ANTHROPIC_API_KEY", ""),
            agente_modelo=e.get("AGENTE_MODELO", "claude-sonnet-5-5") or "claude-sonnet-5-5",
            agente_limite_diario=int(e.get("AGENTE_LIMITE_DIARIO", 3000) or 3000),
        )

"""API do agente vendedor. Contrato: references/api-agente.md"""
import hmac
import time
from typing import Callable

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from .config import Config
from .estado import Estado
from .regras import (ErroNegocio, Item, conferir_precos, validar_cliente, validar_estoque, validar_itens,
                     validar_total)

VALIDADE_ORCAMENTO_DIAS = 10


class ItemIn(BaseModel):
    codigo: int
    quantidade: float


class BuscarIn(BaseModel):
    conversa_id: str
    texto: str = Field(min_length=2, max_length=80)
    limite: int = Field(default=5, ge=1, le=5)


class EstoqueIn(BaseModel):
    conversa_id: str
    codigo_produto: int
    empresa: int | None = None


class ClienteIn(BaseModel):
    conversa_id: str
    telefone: str | None = None
    documento: str | None = None


class PrecosIn(BaseModel):
    conversa_id: str
    codigo_cliente: int
    itens: list[ItemIn]


class OrcamentoIn(BaseModel):
    conversa_id: str
    codigo_cliente: int
    itens: list[ItemIn]


class PedidoIn(BaseModel):
    conversa_id: str
    orcamento_id: str
    confirmacao_cliente: bool
    texto_confirmacao: str = Field(default="", max_length=500)


class PendenciaIn(BaseModel):
    conversa_id: str
    tipo: str = Field(max_length=40)
    resumo: str = Field(max_length=2000)


def create_app(cfg: Config, ops, estado: Estado, agora: Callable[[], float] = time.time) -> FastAPI:
    app = FastAPI(title="Agente vendedor UP", version=cfg.versao)

    def exige(chave_certa: str):
        def dep(authorization: str = Header(default="")):
            token = authorization.removeprefix("Bearer ").strip()
            if not token or not hmac.compare_digest(token, chave_certa):
                raise HTTPException(status_code=401, detail="NAO_AUTORIZADO")
        return dep

    agente = Depends(exige(cfg.api_key_agente))
    admin = Depends(exige(cfg.api_key_admin))

    def ok(dados: dict, **extra) -> dict:
        return {"ok": True, "dados": dados, "erro": None, "consultado_em": agora(), **extra}

    def erro(codigo: str, msg: str) -> dict:
        return {"ok": False, "dados": None, "erro": {"codigo": codigo, "mensagem": msg}, "consultado_em": agora()}

    def executa(acao: str, conversa: str, fn: Callable[[], dict]) -> dict:
        t0 = time.perf_counter()
        if estado.desligado():
            r = erro("DESLIGADO", "O atendimento automático está pausado. Vou registrar para a equipe da UP.")
        else:
            try:
                r = ok(fn())
            except ErroNegocio as e:
                r = erro(e.codigo, e.mensagem)
        estado.registrar_chamada(acao, conversa, int((time.perf_counter() - t0) * 1000), r["ok"],
                                 (r["erro"] or {}).get("codigo", "OK"))
        return r

    @app.get("/saude")
    def saude():
        return {"ok": True, "versao": cfg.versao, "modo": "escrita" if cfg.escrita_habilitada else "somente_leitura",
                "desligado": estado.desligado()}

    @app.post("/admin/desligar")
    def desligar(_=admin):
        estado.definir_desligado(True)
        return {"ok": True, "desligado": True}

    @app.post("/admin/ligar")
    def ligar(_=admin):
        estado.definir_desligado(False)
        return {"ok": True, "desligado": False}

    @app.get("/admin/diagnostico")
    def diagnostico(_=admin):
        return {"ok": True, "passos": ops.diagnostico_busca()}

    @app.get("/admin/opcoes")
    def opcoes(_=admin):
        return {"ok": True, "opcoes": ops.opcoes_config()}

    @app.get("/admin/testar-negociacoes")
    def testar_neg(cliente: int, produto: int, top: int | None = None, _=admin):
        return {"ok": True, "resultado": ops.testa_negociacoes(cliente, produto, top)}

    @app.get("/admin/preco-bruto")
    def preco_bruto(cliente: int, produto: int, tipneg: int, top: int, data: str | None = None, _=admin):
        return {"ok": True, "resposta": ops.preco_bruto(cliente, produto, tipneg, top, data)}

    @app.get("/admin/preco-tabela")
    def preco_tabela(produto: int, tabela: int, _=admin):
        return {"ok": True, "resposta": ops.preco_tabela(produto, tabela)}

    @app.post("/v1/produtos/buscar")
    def buscar(b: BuscarIn, _=agente):
        def f():
            itens = ops.buscar_produtos(b.texto, b.limite)
            if not itens:
                raise ErroNegocio("PRODUTO_NAO_ENCONTRADO", "Não encontrei esse produto. Tente outro nome ou o código.")
            return {"produtos": itens}
        return executa("produtos.buscar", b.conversa_id, f)

    @app.post("/v1/estoque/consultar")
    def estoque(b: EstoqueIn, _=agente):
        def f():
            r = ops.estoque(b.codigo_produto, b.empresa or cfg.empresa_padrao)
            r["ao_vivo"] = True
            return r
        return executa("estoque.consultar", b.conversa_id, f)

    @app.post("/v1/clientes/identificar")
    def cliente(b: ClienteIn, _=agente):
        return executa("clientes.identificar", b.conversa_id, lambda: ops.identificar_cliente(b.telefone, b.documento))

    @app.post("/v1/precos/consultar")
    def precos(b: PrecosIn, _=agente):
        def f():
            itens = [Item(i.codigo, i.quantidade) for i in b.itens]
            validar_itens(itens, cfg.max_itens, cfg.quantidade_max_item)
            p = ops.precos(b.codigo_cliente, [(i.codigo, i.quantidade) for i in itens])
            return {"itens": [{"codigo": i.codigo, "preco_unitario": p[i.codigo], "origem": "sankhya_contextualizado"}
                              for i in itens]}
        return executa("precos.consultar", b.conversa_id, f)

    def com_idempotencia(chave: str | None, acao: str, conversa: str, fn: Callable[[], dict]) -> dict:
        if not chave:
            return erro("IDEMPOTENCY_KEY_OBRIGATORIA", "Cabeçalho Idempotency-Key obrigatório.")
        salvo = estado.resposta_salva(f"{acao}:{chave}")
        if salvo:
            return salvo
        r = executa(acao, conversa, fn)
        if r["ok"] or r["erro"]["codigo"] not in ("SANKHYA_LENTO", "SANKHYA_INDISPONIVEL", "DESLIGADO"):
            estado.salvar_resposta(f"{acao}:{chave}", r)
        return r

    @app.post("/v1/orcamentos")
    def orcamento(b: OrcamentoIn, idempotency_key: str | None = Header(default=None), _=agente):
        def f():
            itens = [Item(i.codigo, i.quantidade) for i in b.itens]
            validar_itens(itens, cfg.max_itens, cfg.quantidade_max_item)
            for i in itens:
                e = ops.estoque(i.codigo, cfg.empresa_padrao)
                validar_estoque(i.codigo, i.quantidade, e["disponivel"])
            p = ops.precos(b.codigo_cliente, [(i.codigo, i.quantidade) for i in itens])
            linhas = [{"codigo": i.codigo, "quantidade": i.quantidade, "preco_unitario": p[i.codigo],
                       "subtotal": round(p[i.codigo] * i.quantidade, 2)} for i in itens]
            total = round(sum(x["subtotal"] for x in linhas), 2)
            validar_total(total, cfg.valor_max_pedido)
            oid = estado.salvar_orcamento(b.conversa_id, b.codigo_cliente,
                                          {"itens": linhas, "total": total, "criado": agora()})
            return {"orcamento_id": oid, "validade_dias": VALIDADE_ORCAMENTO_DIAS, "total": total, "itens": linhas,
                    "registrado_no_sankhya": False,
                    "aviso": "Orçamento guardado só aqui. Registro na TOP do Sankhya ainda não habilitado."}
        return com_idempotencia(idempotency_key, "orcamentos", b.conversa_id, f)

    @app.post("/v1/pedidos")
    def pedido(b: PedidoIn, idempotency_key: str | None = Header(default=None), _=agente):
        def f():
            if not cfg.escrita_habilitada:
                raise ErroNegocio("MODO_SOMENTE_LEITURA", "Ainda não posso fechar pedidos por aqui. Vou registrar para a equipe.")
            if not b.confirmacao_cliente or len(b.texto_confirmacao.strip()) < 2:
                raise ErroNegocio("CONFIRMACAO_OBRIGATORIA", "Preciso da confirmação clara do cliente antes de fechar.")
            orc = estado.obter_orcamento(b.orcamento_id)
            if not orc or orc["conversa"] != b.conversa_id:
                raise ErroNegocio("ORCAMENTO_NAO_ENCONTRADO", "Orçamento não encontrado nesta conversa.")
            if agora() - orc["dados"]["criado"] > VALIDADE_ORCAMENTO_DIAS * 86400:
                raise ErroNegocio("ORCAMENTO_VENCIDO", "Esse orçamento venceu. Vou refazer com os preços de hoje.")
            itens = orc["dados"]["itens"]
            for i in itens:
                validar_estoque(i["codigo"], i["quantidade"], ops.estoque(i["codigo"], cfg.empresa_padrao)["disponivel"])
            atual = ops.precos(orc["cliente"], [(i["codigo"], i["quantidade"]) for i in itens])
            conferir_precos({i["codigo"]: i["preco_unitario"] for i in itens}, atual)
            validar_total(orc["dados"]["total"], cfg.valor_max_pedido)
            raise ErroNegocio("NAO_IMPLEMENTADO",
                              "Gravação do pedido no Sankhya ainda não validada no sandbox. Nada foi gravado.")
        return com_idempotencia(idempotency_key, "pedidos", b.conversa_id, f)

    @app.get("/v1/pedidos/{numero}")
    def status(numero: int, conversa_id: str, _=agente):
        return executa("pedidos.status", conversa_id, lambda: (_ for _ in ()).throw(
            ErroNegocio("NAO_IMPLEMENTADO", "Consulta de pedido ainda não habilitada.")))

    @app.post("/v1/pendencias")
    def pendencia(b: PendenciaIn, _=agente):
        # funciona mesmo desligado: registrar o que não foi resolvido nunca deve falhar
        pid = estado.salvar_pendencia(b.conversa_id, b.tipo, b.resumo)
        estado.registrar_chamada("pendencias.registrar", b.conversa_id, 0, True, "OK")
        return ok({"pendencia_id": pid})

    return app


def build() -> FastAPI:
    from .sankhya_client import SankhyaClient
    from .sankhya_ops import SankhyaOps

    cfg = Config.from_env()
    client = SankhyaClient.from_env(read_only=True)  # a API nunca grava no Sankhya nesta versão
    return create_app(cfg, SankhyaOps(client, cfg), Estado(cfg.db_path))

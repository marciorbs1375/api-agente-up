"""API do agente vendedor. Contrato: references/api-agente.md"""
import hmac
import threading
import time
from typing import Callable

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from .agente import Agente
from .config import Config
from .estado import Estado
from .orcamento import VALIDADE_ORCAMENTO_DIAS, calcular_orcamento, gravar_no_sankhya
from .regras import (ErroNegocio, Item, conferir_precos, validar_cliente, validar_estoque, validar_itens,
                     validar_total)


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


class MensagemIn(BaseModel):
    canal: str = Field(max_length=40)
    conversa_id: str = Field(min_length=1, max_length=120)
    mensagem_id: str | None = Field(default=None, max_length=120)
    telefone: str | None = Field(default=None, max_length=30)
    nome: str | None = Field(default=None, max_length=80)
    texto: str = Field(min_length=1, max_length=2000)
    novo_atendimento: bool = Field(default=False, description="true na 1ª mensagem depois que a conversa foi encerrada ou devolvida à IA (o atendimento humano anterior acabou).")


class DadosConversa(BaseModel):
    resposta: str = Field(description="Texto para enviar ao cliente no canal.")
    acao: str = Field(description="'responder' = envie a resposta; 'transferir_humano' = envie a resposta e passe a conversa para uma pessoa.")
    motivo: str | None = Field(default=None, description="Motivo da transferência (ex.: fechar_pedido, sem_preco, desconto, pediu_humano, erro_sistema).")
    orcamento_id: str | None = Field(default=None, description="Número do orçamento montado nesta resposta, se houver.")
    resumo: str | None = Field(default=None, description="Só na transferência: resumo para o vendedor (cliente, itens, orçamento e o que o cliente pediu). Mostrar à equipe, nunca ao cliente.")


class ErroApi(BaseModel):
    codigo: str
    mensagem: str


class RespostaConversa(BaseModel):
    ok: bool
    dados: DadosConversa | None = None
    erro: ErroApi | None = Field(default=None, description="Preenchido quando ok=false (ex.: AGENTE_NAO_CONFIGURADO). Nesse caso, passe a conversa para uma pessoa.")
    consultado_em: float


class PendenciaIn(BaseModel):
    conversa_id: str
    tipo: str = Field(max_length=40)
    resumo: str = Field(max_length=2000)


def _br(ts: float, fmt: str) -> str:
    """Data/hora no horário de Brasília (UTC-3), independente do fuso do servidor."""
    import datetime as _dt
    return _dt.datetime.fromtimestamp(ts, _dt.timezone(_dt.timedelta(hours=-3))).strftime(fmt)


def create_app(cfg: Config, ops, estado: Estado, agora: Callable[[], float] = time.time,
               transporte_modelo=None) -> FastAPI:
    app = FastAPI(title="Agente vendedor UP", version=cfg.versao)
    agente_conversa = Agente(cfg, ops, estado, transporte=transporte_modelo, agora=agora)

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

    @app.get("/admin/pendencias")
    def pendencias(limite: int = 30, _=admin):
        return {"ok": True, "pendencias": estado.pendencias_recentes(min(max(limite, 1), 100))}

    @app.post("/admin/conversa/{conversa_id}/reiniciar")
    def reiniciar_conversa(conversa_id: str, _=admin):
        """Zera a memória do agente nesta conversa (para testes). Não mexe no Sankhya nem no canal."""
        return {"ok": True, "mensagens_apagadas": estado.reiniciar_conversa(conversa_id)}

    @app.get("/admin/conversa/{conversa_id}")
    def ver_conversa(conversa_id: str, _=admin):
        return {"ok": True, "mensagens": estado.conversa_completa(conversa_id)}

    @app.get("/admin/opcoes")
    def opcoes(_=admin):
        return {"ok": True, "opcoes": ops.opcoes_config()}

    @app.get("/admin/top")
    def ver_top(top: int | None = None, _=admin):
        """Somente leitura: como a TOP de orçamento está configurada (tipo de movimento, estoque, financeiro)."""
        return {"ok": True, "top": ops.tipmov_da_top(top or cfg.top_orcamento)}

    def com_detalhe(r: dict) -> dict:
        """Só em rotas admin: anexa a mensagem do Sankhya quando a consulta falha (diagnóstico)."""
        if not r["ok"] and getattr(ops, "ultimo_erro", None):
            r["erro"]["detalhe_sankhya"] = str(ops.ultimo_erro)[:500]
        return r

    @app.get("/admin/nota/{nunota}")
    def ver_nota(nunota: int, _=admin):
        """Somente leitura: confere no Sankhya o que foi gravado (cabeçalho e itens)."""
        ops.ultimo_erro = None
        return com_detalhe(executa("admin.ver_nota", f"nota-{nunota}", lambda: ops.ver_nota(nunota)))

    @app.post("/admin/gravar-orcamento")
    def gravar_orcamento(orcamento_id: str, _=admin):
        """Grava UM orçamento já montado no Sankhya (TOP de orçamento). Exige ESCRITA_HABILITADA; uma vez só."""
        def f():
            r = gravar_no_sankhya(cfg, ops, estado, agora(), orcamento_id)
            if r["ja_gravado"]:
                raise ErroNegocio("DUPLICADO", f"Esse orçamento já foi gravado (nº {r['nunota']}).")
            return {"nunota": r["nunota"]}
        return executa("admin.gravar_orcamento", orcamento_id, f)

    @app.get("/admin/testar-negociacoes")
    def testar_neg(cliente: int, produto: int, top: int | None = None, _=admin):
        return {"ok": True, "resultado": ops.testa_negociacoes(cliente, produto, top)}

    @app.get("/admin/preco-bruto")
    def preco_bruto(cliente: int, produto: int, tipneg: int, top: int, data: str | None = None, _=admin):
        return {"ok": True, "resposta": ops.preco_bruto(cliente, produto, tipneg, top, data)}

    @app.get("/admin/preco-tabela")
    def preco_tabela(produto: int, tabela: int, _=admin):
        return {"ok": True, "resposta": ops.preco_tabela(produto, tabela)}

    locks: dict[str, threading.Lock] = {}
    locks_guarda = threading.Lock()

    @app.post("/v1/conversas/mensagem", response_model=RespostaConversa)
    def conversa(b: MensagemIn, _=agente):
        """Canal (WellChat, CloudCampaign...) manda a fala do cliente; recebe a resposta ou a decisão de transferir."""
        chave = f"msg:{b.canal}:{b.conversa_id}:{b.mensagem_id}" if b.mensagem_id else None
        if chave and (salvo := estado.resposta_salva(chave)):
            return salvo
        with locks_guarda:
            trava = locks.setdefault(f"{b.canal}:{b.conversa_id}", threading.Lock())
        with trava:  # uma mensagem por vez em cada conversa
            if estado.desligado():
                r = ok({"resposta": "O atendimento automático está pausado. Vou chamar alguém da equipe.",
                        "acao": "transferir_humano", "motivo": "desligado", "orcamento_id": None})
                estado.salvar_pendencia(b.conversa_id, "desligado", b.texto[:300])
                estado.registrar_chamada("conversas.mensagem", b.conversa_id, 0, True, "DESLIGADO")
                return r
            r = executa("conversas.mensagem", b.conversa_id,
                        lambda: agente_conversa.conversar(b.conversa_id, b.texto, b.telefone, b.nome,
                                                                  novo_atendimento=b.novo_atendimento))
            if chave and (r["ok"] or r["erro"]["codigo"] not in ("AGENTE_NAO_CONFIGURADO",)):
                estado.salvar_resposta(chave, r)
            return r

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
            return calcular_orcamento(cfg, ops, estado, agora(), b.conversa_id, b.codigo_cliente,
                                      [(i.codigo, i.quantidade) for i in b.itens])
        return com_idempotencia(idempotency_key, "orcamentos", b.conversa_id, f)

    @app.get("/v1/orcamentos/{orcamento_id}")
    def ver_orcamento(orcamento_id: str, conversa_id: str, _=agente):
        """Dados completos de um orçamento para o canal gerar o PDF. Só da própria conversa."""
        def f():
            o = estado.obter_orcamento(orcamento_id)
            if not o or o["conversa"] != conversa_id:
                raise ErroNegocio("ORCAMENTO_NAO_ENCONTRADO", "Orçamento não encontrado nesta conversa.")
            d = o["dados"]
            gravado = estado.ler_chave(f"gravado:{orcamento_id}") or ""
            criado = d["criado"]
            return {
                "orcamento_id": orcamento_id,
                "criado_em": _br(criado, "%d/%m/%Y %H:%M"),
                "valido_ate": _br(criado + VALIDADE_ORCAMENTO_DIAS * 86400, "%d/%m/%Y"),
                "vencido": agora() - criado > VALIDADE_ORCAMENTO_DIAS * 86400,
                "cliente": {"codigo": o["cliente"], "nome": d.get("cliente_nome"),
                            "documento": d.get("cliente_documento"),
                            "identificado_por": d.get("identificado_por")},
                "itens": [{k: x.get(k) for k in ("codigo", "descricao", "unidade", "quantidade",
                                                 "preco_unitario", "subtotal")} for x in d["itens"]],
                "total": d["total"],
                "desconto": 0,
                "numero_sankhya": int(gravado.split("=", 1)[1]) if gravado.startswith("NUNOTA=") else None,
                "observacao": "Orçamento sujeito à confirmação de um vendedor da UP. Preços da tabela do cliente, sem desconto.",
            }
        return executa("orcamentos.ver", conversa_id, f)

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
    # Leitura sempre. A ÚNICA escrita possível é incluir orçamento, e só com ESCRITA_HABILITADA=true.
    client = SankhyaClient.from_env(
        read_only=True,
        write_allowlist={"CACSP.incluirNota"} if cfg.escrita_habilitada else set(),
    )
    return create_app(cfg, SankhyaOps(client, cfg), Estado(cfg.db_path))

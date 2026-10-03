"""Agente conversacional: o modelo conversa e usa as consultas do Sankhya como ferramentas internas.

Única escrita: quando o cliente quer fechar e ESCRITA_HABILITADA=true, o último orçamento da conversa é
registrado na TOP de orçamento do Sankhya. O pedido é sempre finalizado por um vendedor humano.
"""
import datetime as dt
import json
import os
import re
import logging
import time
import urllib.error
import urllib.request
from typing import Callable

from .orcamento import calcular_orcamento, gravar_no_sankhya
from .prompt import montar_prompt
from .regras import ErroNegocio, Item, validar_itens

log = logging.getLogger("agente.conversa")

API_URL = "https://api.anthropic.com/v1/messages"
MAX_RODADAS = 6
JANELA_HISTORICO = 48 * 3600
_LOJAS_ARQ = os.path.join(os.path.dirname(__file__), "lojas.json")


def lojas_confirmadas() -> list[dict]:
    """Lê app/lojas.json e devolve só os campos preenchidos (confirmados)."""
    try:
        with open(_LOJAS_ARQ, encoding="utf-8") as f:
            dados = json.load(f)
    except (OSError, ValueError):
        return []
    saida = []
    for loja in dados.get("lojas", []):
        campos = {k: v for k, v in loja.items() if v not in (None, "")}
        if len(campos) > 1:  # além do nome, tem algo confirmado
            saida.append(campos)
    return saida


def _cpf_valido(d: str) -> bool:
    if len(d) != 11 or d == d[0] * 11:
        return False
    for n in (9, 10):
        soma = sum(int(d[i]) * (n + 1 - i) for i in range(n))
        if (soma * 10 % 11) % 10 != int(d[n]):
            return False
    return True


def _cnpj_valido(d: str) -> bool:
    if len(d) != 14 or d == d[0] * 14:
        return False
    for n in (12, 13):
        pesos = list(range(n - 7, 1, -1)) + list(range(9, 1, -1))
        soma = sum(int(d[i]) * pesos[i] for i in range(n))
        dv = 0 if soma % 11 < 2 else 11 - soma % 11
        if dv != int(d[n]):
            return False
    return True


def documento_no_texto(texto: str) -> str | None:
    """CPF/CNPJ válido (dígitos verificadores conferidos) escrito pelo cliente. Telefone não passa no teste."""
    for bruto in re.findall(r"\d[\d.\-/ ]{9,20}\d", texto or ""):
        d = re.sub(r"\D", "", bruto)
        if _cnpj_valido(d) or _cpf_valido(d):
            return d
    return None


MOTIVOS = ["fechar_pedido", "desconto", "pagamento", "entrega", "troca_devolucao", "reclamacao", "sem_preco",
           "cliente_nao_cadastrado", "pediu_humano", "fora_do_escopo", "erro_sistema"]

FERRAMENTAS = [
    {"name": "buscar_produtos",
     "description": "Busca produtos ativos pelo nome (ou pelo código numérico). Devolve até 10 opções, com indicação aproximada de estoque.",
     "input_schema": {"type": "object", "properties": {"texto": {"type": "string"}}, "required": ["texto"]}},
    {"name": "consultar_estoque",
     "description": "Consulta o estoque disponível agora de um produto.",
     "input_schema": {"type": "object", "properties": {"codigo_produto": {"type": "integer"}},
                      "required": ["codigo_produto"]}},
    {"name": "identificar_cliente",
     "description": "Identifica o cliente no cadastro. Sem argumentos, usa o telefone do WhatsApp. "
                    "Com 'documento', usa CPF ou CNPJ (só números).",
     "input_schema": {"type": "object", "properties": {"documento": {"type": "string"}}}},
    {"name": "consultar_preco",
     "description": "Consulta o preço unitário dos itens para o cliente já identificado.",
     "input_schema": {"type": "object", "properties": {"itens": {"type": "array", "items": {
         "type": "object", "properties": {"codigo": {"type": "integer"}, "quantidade": {"type": "number"}},
         "required": ["codigo", "quantidade"]}}}, "required": ["itens"]}},
    {"name": "montar_orcamento",
     "description": "Monta o orçamento (confere estoque, preço e total) para o cliente já identificado.",
     "input_schema": {"type": "object", "properties": {"itens": {"type": "array", "items": {
         "type": "object", "properties": {"codigo": {"type": "integer"}, "quantidade": {"type": "number"}},
         "required": ["codigo", "quantidade"]}}}, "required": ["itens"]}},
    {"name": "informacoes_lojas",
     "description": "Endereço, telefone, WhatsApp e horário das lojas da UP. Só traz o que foi confirmado; o que não vier, não informe.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "transferir_para_humano",
     "description": "Passa o atendimento para um vendedor humano, com um resumo para ele continuar.",
     "input_schema": {"type": "object", "properties": {
         "motivo": {"type": "string", "enum": MOTIVOS},
         "resumo": {"type": "string", "description": "Cliente, itens, quantidades, orçamento e o que ele pediu."}},
         "required": ["motivo", "resumo"]}},
]


def _http(url: str, headers: dict, corpo: bytes, timeout: float = 60.0) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=corpo, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


class Agente:
    def __init__(self, cfg, ops, estado, transporte: Callable | None = None, agora: Callable[[], float] = time.time):
        self.cfg, self.ops, self.estado = cfg, ops, estado
        self._http = transporte or _http
        self.agora = agora

    # ---------- modelo ----------
    def _chamar(self, system: str, mensagens: list[dict]) -> dict:
        corpo = json.dumps({"model": self.cfg.agente_modelo, "max_tokens": 800, "system": system,
                            "tools": FERRAMENTAS, "messages": mensagens}).encode()
        headers = {"x-api-key": self.cfg.anthropic_api_key, "anthropic-version": "2023-06-01",
                   "content-type": "application/json"}
        for tentativa in range(2):
            status, raw = self._http(API_URL, headers, corpo)
            if status == 200:
                return json.loads(raw)
            if status in (429, 500, 502, 503, 529) and tentativa == 0:
                time.sleep(1.5)
                continue
            log.error("modelo http=%s", status)  # nunca registrar conteúdo da conversa
            break
        raise RuntimeError("modelo indisponivel")

    # ---------- ferramentas ----------
    def _ferramenta(self, nome: str, entrada: dict, ctx: dict) -> dict:
        try:
            if nome == "buscar_produtos":
                itens = self.ops.buscar_produtos(str(entrada.get("texto", ""))[:80], 10)
                if not itens:
                    raise ErroNegocio("PRODUTO_NAO_ENCONTRADO", "Nenhum produto encontrado.")
                return {"produtos": itens}
            if nome == "consultar_estoque":
                cod = int(entrada["codigo_produto"])
                r = self.ops.estoque(cod, self.cfg.empresa_padrao)
                if r.get("sem_movimentacao") or not r.get("disponivel"):
                    # sem registro de estoque na empresa = sem saldo (não é falha da consulta)
                    return {"codigo": cod, "disponivel": 0, "situacao": "sem estoque no momento"}
                return {"codigo": cod, "disponivel": r.get("disponivel"), "situacao": "em estoque"}
            if nome == "identificar_cliente":
                # CPF/CNPJ informado pelo cliente sempre vale mais que o telefone do WhatsApp.
                doc = ctx.get("documento_informado") or documento_no_texto(str(entrada.get("documento") or ""))
                r = self.ops.identificar_cliente(None if doc else ctx["telefone"], doc)
                if not r.get("ativo"):
                    raise ErroNegocio("CLIENTE_INATIVO", "Cadastro inativo.")
                self.estado.gravar_chave(f"cli:{ctx['conversa']}", json.dumps({"codigo": r["codigo_cliente"], "nome": r["nome"]}))
                ctx["cliente"] = {"codigo": r["codigo_cliente"], "nome": r["nome"]}
                return {"identificado": True, "nome": r["nome"], "por": "documento" if doc else "telefone"}
            if nome in ("consultar_preco", "montar_orcamento"):
                if not ctx.get("cliente"):
                    raise ErroNegocio("CLIENTE_NAO_IDENTIFICADO", "Identifique o cliente antes.")
                entradas = [(int(i["codigo"]), float(i["quantidade"])) for i in entrada.get("itens", [])]
                if nome == "consultar_preco":
                    validar_itens([Item(c, q) for c, q in entradas], self.cfg.max_itens, self.cfg.quantidade_max_item)
                    p = self.ops.precos(ctx["cliente"]["codigo"], entradas)
                    return {"itens": [{"codigo": c, "preco_unitario": p[c]} for c, _ in entradas]}
                r = calcular_orcamento(self.cfg, self.ops, self.estado, self.agora(), ctx["conversa"],
                                       ctx["cliente"]["codigo"], entradas)
                ctx["orcamento_id"] = r["orcamento_id"]
                return {k: r[k] for k in ("orcamento_id", "validade_dias", "total", "itens")}
            if nome == "informacoes_lojas":
                return {"lojas": lojas_confirmadas(),
                        "aviso": "Informe só os campos presentes. Chame o número só de \"telefone\"; só diga que é WhatsApp se o campo whatsapp estiver presente. Em feriados o horário pode mudar: avise isso ao informar horário. Se o que o cliente pediu não estiver aqui, passe para um vendedor."}
            if nome == "transferir_para_humano":
                motivo = entrada.get("motivo") if entrada.get("motivo") in MOTIVOS else "fora_do_escopo"
                resumo = str(entrada.get("resumo", ""))[:1800]
                oid = ctx.get("orcamento_id") or self.estado.ultimo_orcamento(ctx["conversa"])
                saida = {"transferido": True}
                if oid:
                    resumo += f" [orçamento {oid}]"
                if motivo == "fechar_pedido" and oid and self.cfg.escrita_habilitada:
                    try:
                        g = gravar_no_sankhya(self.cfg, self.ops, self.estado, self.agora(), oid)
                        resumo += f" [REGISTRADO NO SANKHYA: orçamento nº {g['nunota']} (TOP {self.cfg.top_orcamento}), aguardando o vendedor finalizar]"
                        saida["orcamento_sankhya"] = g["nunota"]
                        ctx["nunota"] = g["nunota"]
                    except ErroNegocio as e:
                        resumo += f" [NÃO registrado no Sankhya ({e.codigo}): lançar manualmente]"
                        log.warning("gravacao orcamento %s falhou: %s", oid, e.codigo)
                ctx["orcamento_id"] = ctx.get("orcamento_id") or oid
                self.estado.salvar_pendencia(ctx["conversa"], motivo, resumo)
                ctx["acao"], ctx["motivo"], ctx["resumo"] = "transferir_humano", motivo, resumo
                return saida
            return {"erro": "FERRAMENTA_DESCONHECIDA"}
        except ErroNegocio as e:
            return {"erro": e.codigo, "mensagem": e.mensagem}
        except (KeyError, ValueError, TypeError):
            return {"erro": "ENTRADA_INVALIDA", "mensagem": "Argumentos inválidos."}

    # ---------- conversa ----------
    def _transferir(self, ctx: dict, motivo: str, resumo: str, resposta: str) -> dict:
        self.estado.salvar_pendencia(ctx["conversa"], motivo, resumo)
        return {"resposta": resposta, "acao": "transferir_humano", "motivo": motivo,
                "orcamento_id": ctx.get("orcamento_id"), "resumo": resumo[:1800]}

    def conversar(self, conversa: str, texto: str, telefone: str | None, nome: str | None) -> dict:
        if not self.cfg.anthropic_api_key:
            raise ErroNegocio("AGENTE_NAO_CONFIGURADO", "O agente de conversa ainda não foi configurado.")
        ctx = {"conversa": conversa, "telefone": telefone, "acao": "responder", "motivo": None, "orcamento_id": None,
               "cliente": None, "documento_informado": documento_no_texto(texto)}
        salvo = self.estado.ler_chave(f"cli:{conversa}")
        if salvo and not ctx["documento_informado"]:
            # cliente mandou CPF/CNPJ agora: identificação anterior (ex.: pelo telefone) não vale mais
            ctx["cliente"] = json.loads(salvo)
        dia = dt.date.fromtimestamp(self.agora()).isoformat()
        espera = "Um momento, vou chamar alguém da equipe para continuar seu atendimento."
        if self.estado.uso_do_dia(dia) >= self.cfg.agente_limite_diario:
            return self._transferir(ctx, "erro_sistema", f"Limite diário do agente atingido. Última msg: {texto[:300]}", espera)

        historico = self.estado.historico(conversa, self.agora() - JANELA_HISTORICO)
        novas = [{"role": "user", "content": texto}]
        system = montar_prompt(telefone, nome, (ctx["cliente"] or {}).get("nome"), dt.date.today().strftime("%d/%m/%Y"))
        resposta = None
        try:
            for _ in range(MAX_RODADAS):
                r = self._chamar(system, historico + novas)
                self.estado.somar_uso(dia)
                blocos = [b for b in r.get("content", []) if b.get("type") in ("text", "tool_use")]
                limpos = [({"type": "text", "text": b["text"]} if b["type"] == "text"
                           else {"type": "tool_use", "id": b["id"], "name": b["name"], "input": b.get("input", {})})
                          for b in blocos]
                novas.append({"role": "assistant", "content": limpos})
                usos = [b for b in blocos if b["type"] == "tool_use"]
                if r.get("stop_reason") == "tool_use" and usos:
                    resultados = [{"type": "tool_result", "tool_use_id": u["id"],
                                   "content": json.dumps(self._ferramenta(u["name"], u.get("input", {}), ctx),
                                                         ensure_ascii=False)} for u in usos]
                    novas.append({"role": "user", "content": resultados})
                    continue
                resposta = "\n".join(b["text"] for b in blocos if b["type"] == "text").strip()
                break
        except Exception:
            log.exception("falha no agente")
            return self._transferir(ctx, "erro_sistema", f"Falha técnica no agente. Última msg: {texto[:300]}", espera)
        if not resposta:
            return self._transferir(ctx, "erro_sistema", f"Agente sem resposta. Última msg: {texto[:300]}", espera)
        self.estado.adicionar_mensagens(conversa, novas)
        return {"resposta": resposta, "acao": ctx["acao"], "motivo": ctx["motivo"], "orcamento_id": ctx["orcamento_id"],
                "resumo": ctx.get("resumo") if ctx["acao"] == "transferir_humano" else None}

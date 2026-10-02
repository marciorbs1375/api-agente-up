"""Operações de leitura no Sankhya. Nomes marcados [A CONFIRMAR] ainda não foram vistos em produção."""
import datetime as dt
import logging
import re
import unicodedata

from .regras import ErroNegocio
from .sankhya_client import SankhyaClient, SankhyaError


log = logging.getLogger("agente.sankhya")


def _digitos(s: str) -> str:
    return re.sub(r"\D", "", s or "")


class SankhyaOps:
    def __init__(self, client: SankhyaClient, cfg):
        self.c = client
        self.cfg = cfg

    def _traduz(self, e: SankhyaError):
        corpo = str(e.body)[:300] if e.body is not None else ""
        self.ultimo_erro = corpo
        log.error("sankhya erro http=%s msg=%s corpo=%s", e.http_status, str(e)[:300], corpo)
        if e.http_status in (502, 503, 504) or "timed out" in str(e).lower():
            raise ErroNegocio("SANKHYA_LENTO", "O sistema está lento agora. Tente de novo em instantes.")
        raise ErroNegocio("SANKHYA_INDISPONIVEL", "Não consegui consultar o sistema agora.")

    def buscar_produtos(self, texto: str, limite: int) -> list[dict]:
        # Só letras/números (sem acento) entram no SQL: impossível injetar comandos.
        limpo = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode().upper()
        palavras = [p for p in re.split(r"[^A-Z0-9]+", limpo) if p][:4]
        if not palavras:
            return []
        limite = max(1, min(int(limite), 20))
        if len(palavras) == 1 and palavras[0].isdigit() and len(palavras[0]) <= 9:
            filtro = f"CODPROD = {int(palavras[0])}"
        else:
            filtro = "ATIVO = 'S' AND " + " AND ".join(f"UPPER(DESCRPROD) LIKE '%{p}%'" for p in palavras)
        sql = (
            "SELECT CODPROD, DESCRPROD, CODVOL FROM "
            f"(SELECT CODPROD, DESCRPROD, CODVOL FROM TGFPRO WHERE {filtro} ORDER BY DESCRPROD) "
            f"WHERE ROWNUM <= {limite}"
        )
        try:
            linhas = self.c.select_fixo(sql)
        except SankhyaError as e:
            self._traduz(e)
        return [{"codigo": int(r["CODPROD"]), "descricao": r["DESCRPROD"], "unidade": r.get("CODVOL")}
                for r in linhas]

    def estoque(self, codigo: int, empresa: int) -> dict:
        try:
            corpo = self.c.rest_get(f"/v1/estoque/produtos/{codigo}")  # confirmado em produção
        except SankhyaError as e:
            self._traduz(e)
        linhas = [x for x in (corpo.get("estoque") or []) if int(x.get("codigoEmpresa", -1)) == empresa]
        if not linhas:
            return {"codigo": codigo, "empresa": empresa, "disponivel": None, "sem_movimentacao": True, "locais": []}
        local = self.cfg.local_estoque_padrao
        no_local = [x for x in linhas if int(x.get("codigoLocal", -1)) == local]
        base = no_local or linhas
        return {
            "codigo": codigo, "empresa": empresa, "local": local if no_local else None,
            "disponivel": float(sum(float(x.get("estoque", 0)) for x in base)),
            "sem_movimentacao": False,
            "locais": [{"local": int(x["codigoLocal"]), "estoque": float(x.get("estoque", 0))} for x in linhas],
        }

    def identificar_cliente(self, telefone: str | None, documento: str | None) -> dict:
        # Só dígitos entram no SQL (montado em código): sem risco de injeção.
        base = "SELECT CODPARC, NOMEPARC, ATIVO, LIMCRED FROM TGFPAR WHERE CLIENTE = 'S' AND "
        try:
            if documento:
                d = _digitos(documento)
                if len(d) not in (11, 14):
                    raise ErroNegocio("DOCUMENTO_INVALIDO", "Documento inválido. Peça o CPF ou CNPJ de novo.")
                sql = base + f"CGC_CPF = '{d}' AND ROWNUM <= 3"
            elif telefone:
                t = _digitos(telefone)[-9:]
                if len(t) < 8:
                    raise ErroNegocio("TELEFONE_INVALIDO", "Telefone inválido.")
                sql = base + f"REGEXP_REPLACE(TELEFONE, '[^0-9]', '') LIKE '%{t}' AND ROWNUM <= 3"
            else:
                raise ErroNegocio("ENTRADA_INVALIDA", "Informe telefone ou documento.")
            linhas = self.c.select_fixo(sql)
        except SankhyaError as e:
            self._traduz(e)
        if not linhas:
            raise ErroNegocio("CLIENTE_NAO_CADASTRADO", "Não encontrei cadastro com esses dados.")
        if len(linhas) > 1:
            raise ErroNegocio("CLIENTE_AMBIGUO", "Mais de um cadastro encontrado. Peça o CPF ou CNPJ para confirmar.")
        r = linhas[0]
        return {
            "codigo_cliente": int(r["CODPARC"]), "nome": r["NOMEPARC"], "ativo": r.get("ATIVO") == "S",
            "limite_credito": float(r["LIMCRED"]) if r.get("LIMCRED") not in (None, "") else None,
            "credito_verificado": False,  # títulos vencidos ainda não consultados: venda a prazo fica desligada
        }

    def precos(self, codigo_cliente: int, itens: list[tuple[int, float]], tipneg: int | None = None,
               top: int | None = None) -> dict[int, float]:
        c = self.cfg
        tipneg = tipneg or c.tipneg_padrao
        if not c.codvend_agente or not tipneg:
            raise ErroNegocio("CONFIG_INCOMPLETA", "Preço indisponível: configuração do vendedor/negociação pendente.")
        codigos = sorted({int(cod) for cod, _ in itens})
        try:
            vols = self.c.select_fixo(
                "SELECT CODPROD, CODVOL FROM TGFPRO WHERE CODPROD IN (" + ",".join(str(x) for x in codigos) + ")")
        except SankhyaError as e:
            self._traduz(e)
        unidade = {int(v["CODPROD"]): v["CODVOL"] for v in vols}
        corpo = {
            "codigoEmpresa": c.empresa_padrao, "codigoCliente": codigo_cliente,
            "codigoVendedor": c.codvend_agente, "codigoTipoOperacao": top or c.top_pedido,
            "codigoTipoNegociacao": tipneg,
            "dataNegociacao": dt.date.today().strftime("%d/%m/%Y"),  # formato [A CONFIRMAR]
            "produtos": [{"codigoProduto": cod, "quantidade": q, "codigoLocalEstoque": c.local_estoque_padrao,
                          "controle": " ", "unidade": unidade.get(int(cod), "UN")}
                         for cod, q in itens],
        }
        try:
            resp = self.c.rest_post_leitura("/v1/precos/contextualizado", corpo)
        except SankhyaError as e:
            self._traduz(e)
        out = {int(p["codigoProduto"]): float(p["valor"]) for p in (resp.get("precos") or [])}
        faltando = [cod for cod, _ in itens if cod not in out]
        if faltando:
            raise ErroNegocio("PRECO_INDISPONIVEL", f"Sem preço para o produto {faltando[0]}.")
        return out


    def diagnostico_busca(self) -> dict:
        """Só leitura, consultas fixas. Mostra onde a busca de produto quebra. Uso: rota /admin/diagnostico."""
        passos: dict = {}

        def roda(nome, fn):
            try:
                passos[nome] = fn()
            except Exception as e:  # diagnóstico: devolve o erro em vez de esconder
                passos[nome] = {"erro": f"{type(e).__name__}: {str(e)[:300]}", "corpo": str(getattr(e, "body", ""))[:300]}

        roda("sql_contagem", lambda: self.c.query_select("SELECT COUNT(*) AS TOTAL FROM TGFPRO"))
        roda("sql_amostra", lambda: self.c.query_select("SELECT CODPROD, DESCRPROD, ATIVO, CODVOL FROM TGFPRO WHERE ROWNUM <= 3"))
        roda("crud_sem_filtro", lambda: self.c.load_records("Produto", ["CODPROD", "DESCRPROD"], None, None, max_pages=1)[:3])
        roda("crud_so_ativo", lambda: self.c.load_records(
            "Produto", ["CODPROD", "DESCRPROD"], "this.ATIVO = ?", [("S", "S")], max_pages=1)[:3])
        roda("crud_so_like", lambda: self.c.load_records(
            "Produto", ["CODPROD", "DESCRPROD"], "this.DESCRPROD LIKE ?", [("S", "%MAKITA%")], max_pages=1)[:3])
        roda("crud_upper_like", lambda: self.c.load_records(
            "Produto", ["CODPROD", "DESCRPROD"], "UPPER(this.DESCRPROD) LIKE ?", [("S", "%CALCULADORA%")], max_pages=1)[:3])
        roda("crud_busca_final", lambda: self.buscar_produtos("calculadora mesa", 3))
        return passos

    def opcoes_config(self) -> dict:
        """Só leitura, SELECTs fixos: ajuda a escolher CODVEND_AGENTE e TIPNEG_PADRAO. Rota /admin/opcoes."""
        out: dict = {}
        consultas = {
            "tipos_negociacao": "SELECT CODTIPVENDA, DESCRTIPVENDA FROM TGFTPV WHERE ATIVO = 'S' AND ROWNUM <= 40",
            "vendedores_candidatos": "SELECT CODVEND, APELIDO, ATIVO FROM TGFVEN WHERE ATIVO = 'S' AND "
                                     "(UPPER(APELIDO) LIKE '%WHATS%' OR UPPER(APELIDO) LIKE '%AGENTE%' "
                                     "OR UPPER(APELIDO) LIKE '%ONLINE%' OR UPPER(APELIDO) LIKE '%IA%' "
                                     "OR UPPER(APELIDO) LIKE '%SITE%') AND ROWNUM <= 20",
            "vendedores_amostra": "SELECT CODVEND, APELIDO FROM TGFVEN WHERE ATIVO = 'S' AND ROWNUM <= 25",
        }
        for nome, sql in consultas.items():
            try:
                out[nome] = self.c.select_fixo(sql)
            except Exception as e:
                out[nome] = {"erro": str(e)[:300]}
        return out

    def testa_negociacoes(self, codigo_cliente: int, codigo_produto: int, top: int | None = None) -> dict:
        """Só leitura: calcula o preço com cada forma de pagamento ativa e mostra quais a TOP aceita."""
        tipos = self.c.select_fixo(
            "SELECT DISTINCT CODTIPVENDA, DESCRTIPVENDA FROM TGFTPV WHERE ATIVO = 'S' AND ROWNUM <= 80")
        aceitas, motivos = [], {}
        for t in tipos:
            try:
                p = self.precos(codigo_cliente, [(codigo_produto, 1)], tipneg=int(t["CODTIPVENDA"]), top=top)
                aceitas.append({"tipneg": int(t["CODTIPVENDA"]), "descricao": t["DESCRTIPVENDA"],
                                "preco": p[codigo_produto]})
            except SankhyaError as e:
                motivos.setdefault(str(e.body)[-220:], []).append(int(t["CODTIPVENDA"]))
            except Exception as e:
                chave = (getattr(self, "ultimo_erro", "") or str(e))[-220:]
                motivos.setdefault(chave, []).append(int(t["CODTIPVENDA"]))
        return {"top": top or self.cfg.top_pedido, "aceitas": aceitas, "testadas": len(tipos),
                "motivos_recusa": {k: v[:12] for k, v in motivos.items()}}

    def preco_bruto(self, codigo_cliente: int, codigo_produto: int, tipneg: int, top: int) -> dict:
        """Só leitura: devolve a resposta crua do Sankhya para conferir o formato. Rota /admin/preco-bruto."""
        c = self.cfg
        vols = self.c.select_fixo(f"SELECT CODVOL FROM TGFPRO WHERE CODPROD = {int(codigo_produto)}")
        corpo = {
            "codigoEmpresa": c.empresa_padrao, "codigoCliente": int(codigo_cliente),
            "codigoVendedor": c.codvend_agente, "codigoTipoOperacao": int(top),
            "codigoTipoNegociacao": int(tipneg),
            "dataNegociacao": dt.date.today().strftime("%d/%m/%Y"),
            "produtos": [{"codigoProduto": int(codigo_produto), "quantidade": 1,
                          "codigoLocalEstoque": c.local_estoque_padrao, "controle": " ",
                          "unidade": (vols[0]["CODVOL"] if vols else "UN")}],
        }
        try:
            return self.c.rest_post_leitura("/v1/precos/contextualizado", corpo)
        except SankhyaError as e:
            return {"erro": str(e)[:300], "corpo": str(e.body)[:400]}

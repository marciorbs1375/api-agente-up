import json

import pytest
from fastapi.testclient import TestClient

from app.config import Config
from app.estado import Estado
from app.main import create_app
from tests.test_api import AD, AG, FakeOps


def resp(blocos, parada="end_turn"):
    return 200, json.dumps({"content": blocos, "stop_reason": parada}).encode()


def texto(t):
    return resp([{"type": "text", "text": t}])


def uso(id_, nome, entrada):
    return resp([{"type": "tool_use", "id": id_, "name": nome, "input": entrada}], "tool_use")


class Modelo:
    """Transporte falso do modelo: devolve respostas roteirizadas e guarda o que recebeu."""
    def __init__(self, *respostas):
        self.respostas = list(respostas)
        self.chamadas = []

    def __call__(self, url, headers, corpo, *a, **k):
        self.chamadas.append(json.loads(corpo))
        r = self.respostas.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def montar(tmp_path, modelo, chave="sk-teste", **kw):
    cfg = Config(api_key_agente="a" * 30, api_key_admin="b" * 30, db_path=str(tmp_path / "t.db"),
                 anthropic_api_key=chave, **kw)
    ops = FakeOps()
    app = create_app(cfg, ops, Estado(cfg.db_path), lambda: 1000.0, transporte_modelo=modelo)
    return TestClient(app), ops


def msg(c, texto_, conv="c1", **extra):
    return c.post("/v1/conversas/mensagem", headers=AG,
                  json={"canal": "wellchat", "conversa_id": conv, "texto": texto_, "telefone": "5547999990000", **extra}).json()


def test_resposta_simples(tmp_path):
    m = Modelo(texto("Olá! Como posso ajudar?"))
    c, _ = montar(tmp_path, m)
    r = msg(c, "oi")
    assert r["ok"] and r["dados"]["acao"] == "responder" and r["dados"]["resposta"].startswith("Olá")
    assert m.chamadas[0]["tools"] and "5547999990000" in m.chamadas[0]["system"]


def test_busca_e_orcamento_com_cliente_identificado(tmp_path):
    m = Modelo(
        uso("t1", "identificar_cliente", {}),
        uso("t2", "montar_orcamento", {"itens": [{"codigo": 1, "quantidade": 2}]}),
        texto("Orçamento: 2 un por R$ 20,00."),
    )
    c, _ = montar(tmp_path, m)
    r = msg(c, "quero 2 do produto um")["dados"]
    assert r["acao"] == "responder" and r["orcamento_id"].startswith("ORC-")
    resultado = json.loads(m.chamadas[2]["messages"][-1]["content"][0]["content"])
    assert resultado["total"] == 20.0


def test_preco_sem_cliente_identificado_e_bloqueado(tmp_path):
    m = Modelo(uso("t1", "consultar_preco", {"itens": [{"codigo": 1, "quantidade": 1}]}), texto("Preciso do seu CNPJ."))
    c, _ = montar(tmp_path, m)
    msg(c, "quanto custa?")
    resultado = json.loads(m.chamadas[1]["messages"][-1]["content"][0]["content"])
    assert resultado["erro"] == "CLIENTE_NAO_IDENTIFICADO"


def test_transferir_para_humano_registra_pendencia(tmp_path):
    m = Modelo(uso("t1", "transferir_para_humano", {"motivo": "fechar_pedido", "resumo": "Cliente quer 2 un"}),
               texto("Vou passar para um vendedor."))
    c, _ = montar(tmp_path, m)
    r = msg(c, "pode fechar")["dados"]
    assert r["acao"] == "transferir_humano" and r["motivo"] == "fechar_pedido"
    assert r["resumo"].startswith("Cliente quer 2 un")


def test_historico_continua_na_mesma_conversa(tmp_path):
    m = Modelo(texto("Qual produto?"), texto("Anotado."))
    c, _ = montar(tmp_path, m)
    msg(c, "oi")
    msg(c, "canetas")
    enviadas = m.chamadas[1]["messages"]
    assert [x["role"] for x in enviadas] == ["user", "assistant", "user"]


def test_falha_do_modelo_vira_transferencia(tmp_path):
    m = Modelo((500, b"{}"), (500, b"{}"))
    c, _ = montar(tmp_path, m)
    r = msg(c, "oi")["dados"]
    assert r["acao"] == "transferir_humano" and r["motivo"] == "erro_sistema"


def test_sem_chave_do_modelo(tmp_path):
    c, _ = montar(tmp_path, Modelo(), chave="")
    r = msg(c, "oi")
    assert not r["ok"] and r["erro"]["codigo"] == "AGENTE_NAO_CONFIGURADO"


def test_mensagem_repetida_nao_chama_o_modelo_de_novo(tmp_path):
    m = Modelo(texto("Oi!"))
    c, _ = montar(tmp_path, m)
    a = msg(c, "oi", mensagem_id="m1")
    b = msg(c, "oi", mensagem_id="m1")
    assert a == b and len(m.chamadas) == 1


def test_desligado_transfere_sem_chamar_modelo(tmp_path):
    m = Modelo()
    c, _ = montar(tmp_path, m)
    c.post("/admin/desligar", headers=AD)
    r = msg(c, "oi")["dados"]
    assert r["acao"] == "transferir_humano" and not m.chamadas


def test_limite_diario(tmp_path):
    m = Modelo()
    c, _ = montar(tmp_path, m, agente_limite_diario=0)
    assert msg(c, "oi")["dados"]["acao"] == "transferir_humano" and not m.chamadas


def test_exige_chave_do_agente(tmp_path):
    c, _ = montar(tmp_path, Modelo())
    r = c.post("/v1/conversas/mensagem", headers=AD, json={"canal": "x", "conversa_id": "c", "texto": "oi"})
    assert r.status_code == 401


def test_admin_ve_conversa_e_pendencias(tmp_path):
    m = Modelo(uso("t1", "transferir_para_humano", {"motivo": "desconto", "resumo": "Quer 20% de desconto"}),
               texto("Vou passar para um vendedor."))
    c, _ = montar(tmp_path, m)
    msg(c, "me dá 20% de desconto", conv="cX")
    conv = c.get("/admin/conversa/cX", headers=AD).json()["mensagens"]
    assert conv[0]["papel"] == "user" and conv[0]["conteudo"] == "me dá 20% de desconto"
    pend = c.get("/admin/pendencias", headers=AD).json()["pendencias"]
    assert pend[0]["tipo"] == "desconto"
    assert c.get("/admin/pendencias", headers=AG).status_code == 401


def test_lojas_so_campos_confirmados(tmp_path, monkeypatch):
    import app.agente as ag
    arq = tmp_path / "lojas.json"
    arq.write_text(json.dumps({"lojas": [
        {"nome": "Loja A", "endereco": "Rua X, 1", "telefone": None, "horario_semana": ""},
        {"nome": "Loja B", "endereco": None, "telefone": None}]}), encoding="utf-8")
    monkeypatch.setattr(ag, "_LOJAS_ARQ", str(arq))
    r = ag.lojas_confirmadas()
    assert r == [{"nome": "Loja A", "endereco": "Rua X, 1"}]


def test_lojas_arquivo_real_sem_dado_inventado():
    import app.agente as ag
    for loja in ag.lojas_confirmadas():
        assert "nome" in loja


def test_reiniciar_conversa_zera_memoria(tmp_path):
    from tests.test_api import AD as ADMIN, AG as AGENTE
    m = Modelo(texto("Oi!"))
    c, _ = montar(tmp_path, m)
    msg(c, "oi")
    assert c.get("/admin/conversa/c1", headers=ADMIN).json()["mensagens"]
    assert c.post("/admin/conversa/c1/reiniciar", headers=AGENTE).status_code == 401
    r = c.post("/admin/conversa/c1/reiniciar", headers=ADMIN).json()
    assert r["ok"] and r["mensagens_apagadas"] >= 2
    assert c.get("/admin/conversa/c1", headers=ADMIN).json()["mensagens"] == []


def _fluxo_fechar(tmp_path, escrita):
    m = Modelo(uso("t0", "identificar_cliente", {"documento": "10364152000127"}),
               uso("t1", "montar_orcamento", {"itens": [{"codigo": 1, "quantidade": 2}]}),
               texto("Segue o orçamento."),
               uso("t2", "transferir_para_humano", {"motivo": "fechar_pedido", "resumo": "Quer fechar 2 un"}),
               texto("Registrado, um vendedor vai finalizar."))
    c, _ = montar(tmp_path, m, escrita_habilitada=escrita)
    return c, m


def test_fechar_grava_orcamento_quando_escrita_ligada(tmp_path, monkeypatch):
    import tests.test_api as ta
    gravados = []
    monkeypatch.setattr(ta.FakeOps, "tipmov_da_top", lambda self, top: {"TIPMOV": "P"}, raising=False)
    monkeypatch.setattr(ta.FakeOps, "gravar_orcamento",
                        lambda self, oid, cli, itens, tipmov: gravados.append(oid) or {"nunota": 999}, raising=False)
    c, m = _fluxo_fechar(tmp_path, True)
    msg(c, "meu cnpj 10364152000127, quero 2 do produto 1")
    r = msg(c, "pode fechar")["dados"]
    assert r["acao"] == "transferir_humano" and r["motivo"] == "fechar_pedido"
    assert len(gravados) == 1 and "nº 999" in r["resumo"]
    resultado = json.loads(m.chamadas[-1]["messages"][-1]["content"][0]["content"])
    assert resultado["orcamento_sankhya"] == 999


def test_fechar_nao_grava_com_escrita_desligada(tmp_path, monkeypatch):
    import tests.test_api as ta
    gravados = []
    monkeypatch.setattr(ta.FakeOps, "tipmov_da_top", lambda self, top: {"TIPMOV": "P"}, raising=False)
    monkeypatch.setattr(ta.FakeOps, "gravar_orcamento",
                        lambda self, *a: gravados.append(a) or {"nunota": 1}, raising=False)
    c, m = _fluxo_fechar(tmp_path, False)
    msg(c, "meu cnpj 10364152000127, quero 2 do produto 1")
    r = msg(c, "pode fechar")["dados"]
    assert r["acao"] == "transferir_humano" and not gravados
    assert "REGISTRADO" not in r["resumo"]


def test_falha_na_gravacao_ainda_transfere(tmp_path, monkeypatch):
    import tests.test_api as ta
    from app.regras import ErroNegocio
    monkeypatch.setattr(ta.FakeOps, "tipmov_da_top", lambda self, top: {"TIPMOV": "P"}, raising=False)
    def quebra(self, *a):
        raise ErroNegocio("SANKHYA_INDISPONIVEL", "fora")
    monkeypatch.setattr(ta.FakeOps, "gravar_orcamento", quebra, raising=False)
    c, m = _fluxo_fechar(tmp_path, True)
    msg(c, "meu cnpj 10364152000127, quero 2 do produto 1")
    r = msg(c, "pode fechar")["dados"]
    assert r["acao"] == "transferir_humano" and "lançar manualmente" in r["resumo"]


def test_documento_no_texto_valida_digitos():
    from app.agente import documento_no_texto
    assert documento_no_texto("Meu CNPJ é 10364152000127. Quero 2") == "10364152000127"
    assert documento_no_texto("cnpj 10.364.152/0001-27") == "10364152000127"
    assert documento_no_texto("meu telefone 48999930968") is None
    assert documento_no_texto("pedido 12345678901234") is None


def test_cnpj_no_texto_vence_telefone(tmp_path, monkeypatch):
    import tests.test_api as ta
    chamadas = []
    def ident(self, tel, doc):
        chamadas.append((tel, doc))
        return {"codigo_cliente": 1417 if doc else 9999, "nome": "LINCE" if doc else "OUTRO", "ativo": True}
    monkeypatch.setattr(ta.FakeOps, "identificar_cliente", ident)
    m = Modelo(uso("t0", "identificar_cliente", {}), texto("Oi"),
               uso("t1", "identificar_cliente", {}), texto("Achei a LINCE"))
    c, _ = montar(tmp_path, m)
    msg(c, "oi")  # sem documento: usa telefone
    msg(c, "Meu CNPJ é 10364152000127. Quero orçamento")
    assert chamadas[0][1] is None and chamadas[0][0]
    assert chamadas[1] == (None, "10364152000127")


def test_cnpj_novo_exige_reidentificar_antes_do_preco(tmp_path):
    m = Modelo(uso("t0", "identificar_cliente", {}), texto("Oi"),
               uso("t1", "montar_orcamento", {"itens": [{"codigo": 1, "quantidade": 2}]}), texto("..."))
    c, _ = montar(tmp_path, m)
    msg(c, "oi")
    msg(c, "Meu CNPJ é 10364152000127. Quero 2 do produto 1")
    resultado = json.loads(m.chamadas[-1]["messages"][-1]["content"][0]["content"])
    assert resultado["erro"] == "CLIENTE_NAO_IDENTIFICADO"

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

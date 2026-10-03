import dataclasses
import pytest
from fastapi.testclient import TestClient

from app.config import Config
from app.estado import Estado
from app.main import create_app

AG = {"Authorization": "Bearer " + "a" * 30}
AD = {"Authorization": "Bearer " + "b" * 30}


class FakeOps:
    def __init__(self):
        self.estoque_disp = {1: 50.0, 2: 3.0}
        self.preco = {1: 10.0, 2: 100.0, 3: 5.0}

    def buscar_produtos(self, texto, limite):
        return [] if texto == "nada" else [{"codigo": 1, "descricao": "PRODUTO UM", "unidade": "UN"}]

    def estoque(self, codigo, empresa):
        return {"codigo_produto": codigo, "empresa": empresa, "disponivel": self.estoque_disp.get(codigo)}

    def identificar_cliente(self, tel, doc):
        return {"codigo_cliente": 7, "nome": "CLIENTE", "ativo": True}

    def precos(self, cliente, itens):
        return {c: self.preco[c] for c, _ in itens}


def montar(tmp_path, **kw):
    cfg = Config(api_key_agente="a" * 30, api_key_admin="b" * 30, db_path=str(tmp_path / "t.db"), **kw)
    ops = FakeOps()
    t = [1000.0]
    app = create_app(cfg, ops, Estado(cfg.db_path), lambda: t[0])
    return TestClient(app), ops, t


@pytest.fixture
def c(tmp_path):
    return montar(tmp_path)[0]


def orc(c, itens, key="k1"):
    return c.post("/v1/orcamentos", json={"conversa_id": "x", "codigo_cliente": 7, "itens": itens},
                  headers={**AG, "Idempotency-Key": key})


def test_saude(c):
    r = c.get("/saude").json()
    assert r["modo"] == "somente_leitura" and not r["desligado"]


def test_auth(c):
    assert c.post("/v1/produtos/buscar", json={"conversa_id": "x", "texto": "ab"}).status_code == 401
    assert c.post("/v1/produtos/buscar", json={"conversa_id": "x", "texto": "ab"},
                  headers=AD).status_code == 401
    assert c.post("/admin/desligar", headers=AG).status_code == 401


def test_buscar(c):
    r = c.post("/v1/produtos/buscar", json={"conversa_id": "x", "texto": "abc"}, headers=AG).json()
    assert r["ok"] and r["dados"]["produtos"][0]["codigo"] == 1
    r = c.post("/v1/produtos/buscar", json={"conversa_id": "x", "texto": "nada"}, headers=AG).json()
    assert r["erro"]["codigo"] == "PRODUTO_NAO_ENCONTRADO"


def test_kill_switch(c):
    assert c.post("/admin/desligar", headers=AD).json()["desligado"]
    r = c.post("/v1/produtos/buscar", json={"conversa_id": "x", "texto": "abc"}, headers=AG).json()
    assert r["erro"]["codigo"] == "DESLIGADO"
    p = c.post("/v1/pendencias", json={"conversa_id": "x", "tipo": "t", "resumo": "r"}, headers=AG).json()
    assert p["ok"]
    c.post("/admin/ligar", headers=AD)
    assert c.post("/v1/produtos/buscar", json={"conversa_id": "x", "texto": "abc"}, headers=AG).json()["ok"]


def test_orcamento_ok_e_idempotencia(c):
    a = orc(c, [{"codigo": 1, "quantidade": 2}]).json()
    assert a["ok"] and a["dados"]["total"] == 20.0 and not a["dados"]["registrado_no_sankhya"]
    b = orc(c, [{"codigo": 1, "quantidade": 2}]).json()
    assert b["dados"]["orcamento_id"] == a["dados"]["orcamento_id"]
    d = orc(c, [{"codigo": 1, "quantidade": 2}], key="k2").json()
    assert d["dados"]["orcamento_id"] != a["dados"]["orcamento_id"]


def test_orcamento_sem_chave(c):
    r = c.post("/v1/orcamentos", json={"conversa_id": "x", "codigo_cliente": 7,
                                       "itens": [{"codigo": 1, "quantidade": 1}]}, headers=AG).json()
    assert r["erro"]["codigo"] == "IDEMPOTENCY_KEY_OBRIGATORIA"


def test_orcamento_travas(c):
    assert orc(c, [{"codigo": 2, "quantidade": 5}], "a").json()["erro"]["codigo"] == "ESTOQUE_INSUFICIENTE"
    assert orc(c, [{"codigo": 9, "quantidade": 1}], "b").json()["erro"]["codigo"] == "ESTOQUE_INSUFICIENTE"
    assert orc(c, [{"codigo": 1, "quantidade": 0}], "c").json()["erro"]["codigo"] == "ITENS_INVALIDOS"
    assert orc(c, [{"codigo": 1, "quantidade": 1}] * 2, "d").json()["erro"]["codigo"] == "ITENS_INVALIDOS"
    assert orc(c, [], "e").json()["erro"]["codigo"] == "ITENS_INVALIDOS"


def test_orcamento_limite_valor(tmp_path):
    c, ops, _ = montar(tmp_path)
    ops.estoque_disp[1] = 500
    ops.preco[1] = 100.0
    assert orc(c, [{"codigo": 1, "quantidade": 21}]).json()["erro"]["codigo"] == "LIMITE_PEDIDO_EXCEDIDO"


def pedido(c, oid, key="p1", conf=True):
    return c.post("/v1/pedidos", json={"conversa_id": "x", "orcamento_id": oid, "confirmacao_cliente": conf,
                                       "texto_confirmacao": "sim, pode fechar"},
                  headers={**AG, "Idempotency-Key": key}).json()


def test_pedido_bloqueado_somente_leitura(c):
    oid = orc(c, [{"codigo": 1, "quantidade": 1}]).json()["dados"]["orcamento_id"]
    assert pedido(c, oid)["erro"]["codigo"] == "MODO_SOMENTE_LEITURA"


def test_pedido_escrita_habilitada(tmp_path):
    c, ops, t = montar(tmp_path, escrita_habilitada=True)
    oid = orc(c, [{"codigo": 1, "quantidade": 1}]).json()["dados"]["orcamento_id"]
    assert pedido(c, oid, "p0", conf=False)["erro"]["codigo"] == "CONFIRMACAO_OBRIGATORIA"
    assert pedido(c, "nao-existe", "p1")["erro"]["codigo"] == "ORCAMENTO_NAO_ENCONTRADO"
    ops.preco[1] = 11.0
    assert pedido(c, oid, "p2")["erro"]["codigo"] == "PRECO_DIVERGENTE"
    ops.preco[1] = 10.0
    assert pedido(c, oid, "p3")["erro"]["codigo"] == "NAO_IMPLEMENTADO"
    t[0] += 11 * 86400
    assert pedido(c, oid, "p4")["erro"]["codigo"] == "ORCAMENTO_VENCIDO"


def test_precos_e_estoque_e_cliente(c):
    r = c.post("/v1/precos/consultar", json={"conversa_id": "x", "codigo_cliente": 7,
                                             "itens": [{"codigo": 1, "quantidade": 1}]}, headers=AG).json()
    assert r["dados"]["itens"][0]["preco_unitario"] == 10.0
    r = c.post("/v1/estoque/consultar", json={"conversa_id": "x", "codigo_produto": 1}, headers=AG).json()
    assert r["dados"]["ao_vivo"] and r["dados"]["disponivel"] == 50.0
    r = c.post("/v1/clientes/identificar", json={"conversa_id": "x", "telefone": "11999999999"}, headers=AG).json()
    assert r["dados"]["codigo_cliente"] == 7


def test_config_exige_chaves(monkeypatch):
    monkeypatch.setenv("API_KEY_AGENTE", "curta")
    monkeypatch.setenv("API_KEY_ADMIN", "curta")
    with pytest.raises(RuntimeError):
        Config.from_env()


def test_diagnostico_exige_admin(tmp_path):
    c, ops, _ = montar(tmp_path)
    ops.diagnostico_busca = lambda: {"x": 1}
    assert c.get("/admin/diagnostico", headers=AG).status_code == 401
    assert c.get("/admin/diagnostico", headers=AD).json()["passos"] == {"x": 1}


def test_parametros_do_sankhya_usam_cifrao():
    from app.sankhya_client import SankhyaClient
    assert SankhyaClient._params([("S", "%A%"), ("I", 5)]) == [{"$": "%A%", "type": "S"}, {"$": "5", "type": "I"}]


def test_busca_sql_sanitizada():
    from app.sankhya_ops import SankhyaOps

    class FakeClient:
        sqls = []
        def select_fixo(self, sql):
            self.sqls.append(sql)
            return [{"CODPROD": "1", "DESCRPROD": "Calculadora", "CODVOL": "UN"}]

    c = FakeClient()
    r = SankhyaOps(c, None).buscar_produtos("Calculadora' OR 1=1 --; mesa", 3)
    sql = c.sqls[0]
    assert r[0]["codigo"] == 1
    from app.sankhya_ops import _COM_ACENTO, _SEM_ACENTO
    resto = sql.split("WHERE", 1)[1].replace(f"'{_COM_ACENTO}'", "").replace(f"'{_SEM_ACENTO}'", "")
    assert "'" not in resto.replace("'%", "").replace("%'", "").replace("'S'", "")
    assert ";" not in sql and "--" not in sql and "ROWNUM <= 3" in sql


# ---------- gravação do orçamento (rota admin, uma vez por orçamento) ----------
def _ops_grava(ops):
    ops.gravados = []
    ops.tipmov_da_top = lambda top: {"TIPMOV": "P", "CODTIPOPER": top}
    def grava(orc_id, cliente, itens, tipmov):
        ops.gravados.append((orc_id, cliente, tipmov, itens))
        return {"nunota": 12345}
    ops.gravar_orcamento = grava


def test_gravar_orcamento_bloqueado_em_leitura(tmp_path):
    c, ops, _ = montar(tmp_path)
    _ops_grava(ops)
    oid = orc(c, [{"codigo": 1, "quantidade": 2}]).json()["dados"]["orcamento_id"]
    r = c.post(f"/admin/gravar-orcamento?orcamento_id={oid}", headers=AD).json()
    assert r["erro"]["codigo"] == "MODO_SOMENTE_LEITURA" and not ops.gravados


def test_gravar_orcamento_exige_admin(tmp_path):
    c, ops, _ = montar(tmp_path, escrita_habilitada=True)
    _ops_grava(ops)
    assert c.post("/admin/gravar-orcamento?orcamento_id=X", headers=AG).status_code == 401


def test_gravar_orcamento_uma_vez_so(tmp_path):
    c, ops, _ = montar(tmp_path, escrita_habilitada=True)
    _ops_grava(ops)
    oid = orc(c, [{"codigo": 1, "quantidade": 2}]).json()["dados"]["orcamento_id"]
    r = c.post(f"/admin/gravar-orcamento?orcamento_id={oid}", headers=AD).json()
    assert r["ok"] and r["dados"]["nunota"] == 12345
    r2 = c.post(f"/admin/gravar-orcamento?orcamento_id={oid}", headers=AD).json()
    assert r2["erro"]["codigo"] == "DUPLICADO" and len(ops.gravados) == 1


def test_gravar_orcamento_falha_nao_repete(tmp_path):
    c, ops, _ = montar(tmp_path, escrita_habilitada=True)
    _ops_grava(ops)
    from app.regras import ErroNegocio
    def quebra(*a):
        raise ErroNegocio("SANKHYA_LENTO", "lento")
    ops.gravar_orcamento = quebra
    oid = orc(c, [{"codigo": 1, "quantidade": 2}]).json()["dados"]["orcamento_id"]
    c.post(f"/admin/gravar-orcamento?orcamento_id={oid}", headers=AD)
    r = c.post(f"/admin/gravar-orcamento?orcamento_id={oid}", headers=AD).json()
    assert r["erro"]["codigo"] == "DUPLICADO"


def test_orcamento_inexistente_e_vencido(tmp_path):
    c, ops, t = montar(tmp_path, escrita_habilitada=True)
    _ops_grava(ops)
    assert c.post("/admin/gravar-orcamento?orcamento_id=ORC-NADA", headers=AD).json()["erro"]["codigo"] == "ORCAMENTO_NAO_ENCONTRADO"
    oid = orc(c, [{"codigo": 1, "quantidade": 2}]).json()["dados"]["orcamento_id"]
    t[0] += 11 * 86400
    assert c.post(f"/admin/gravar-orcamento?orcamento_id={oid}", headers=AD).json()["erro"]["codigo"] == "ORCAMENTO_VENCIDO"


def test_ver_nota_somente_admin(tmp_path):
    c, ops, _ = montar(tmp_path)
    ops.ver_nota = lambda n: {"cabecalho": {"NUNOTA": n}, "itens": []}
    assert c.get("/admin/nota/5", headers=AG).status_code == 401
    r = c.get("/admin/nota/5", headers=AD).json()
    assert r["ok"] and r["dados"]["cabecalho"]["NUNOTA"] == 5


def test_ver_orcamento_para_pdf(tmp_path):
    c, ops, _ = montar(tmp_path)
    oid = orc(c, [{"codigo": 1, "quantidade": 2}]).json()["dados"]["orcamento_id"]
    assert c.get(f"/v1/orcamentos/{oid}?conversa_id=outra", headers=AG).json()["erro"]["codigo"] == "ORCAMENTO_NAO_ENCONTRADO"
    r = c.get(f"/v1/orcamentos/{oid}?conversa_id=x", headers=AG).json()
    assert r["ok"]
    d = r["dados"]
    assert d["total"] == 20.0 and d["itens"][0]["descricao"] == "PRODUTO UM" and d["numero_sankhya"] is None
    assert d["desconto"] == 0 and d["valido_ate"]

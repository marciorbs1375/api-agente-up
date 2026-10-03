from app.config import Config
from app.sankhya_ops import SankhyaOps


class FakeClient:
    def __init__(self, linhas=None):
        self.sqls = []
        self.linhas = linhas or []

    def select_fixo(self, sql):
        self.sqls.append(sql)
        return self.linhas


def test_busca_ignora_acento_e_palavras_vazias():
    c = FakeClient([{"CODPROD": 8001867, "DESCRPROD": "Pano de Chão MARTINS", "CODVOL": "UN"}])
    ops = SankhyaOps(c, Config(api_key_agente="a" * 30, api_key_admin="b" * 30))
    r = ops.buscar_produtos("pano de chão", 5)
    sql = c.sqls[0]
    assert "TRANSLATE(UPPER(DESCRPROD)" in sql
    assert "LIKE '%PANO%'" in sql and "LIKE '%CHAO%'" in sql
    assert "LIKE '%DE%'" not in sql
    assert r[0]["codigo"] == 8001867


def test_busca_texto_malicioso_vira_so_letras():
    c = FakeClient()
    ops = SankhyaOps(c, Config(api_key_agente="a" * 30, api_key_admin="b" * 30))
    ops.buscar_produtos("x' OR 1=1 --; DROP TABLE", 5)
    sql = c.sqls[0]
    assert "'%X%'" in sql and "--" not in sql and ";" not in sql and "1=1" not in sql

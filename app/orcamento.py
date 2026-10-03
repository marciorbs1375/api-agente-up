"""Cálculo do orçamento (único lugar). Usado pela rota /v1/orcamentos e pelo agente conversacional."""
from .regras import ErroNegocio, Item, validar_estoque, validar_itens, validar_total

VALIDADE_ORCAMENTO_DIAS = 10


def calcular_orcamento(cfg, ops, estado, agora: float, conversa: str, cliente: int,
                       entradas: list[tuple[int, float]]) -> dict:
    itens = [Item(c, q) for c, q in entradas]
    validar_itens(itens, cfg.max_itens, cfg.quantidade_max_item)
    for i in itens:
        e = ops.estoque(i.codigo, cfg.empresa_padrao)
        validar_estoque(i.codigo, i.quantidade, e["disponivel"])
    p = ops.precos(cliente, [(i.codigo, i.quantidade) for i in itens])
    linhas = [{"codigo": i.codigo, "quantidade": i.quantidade, "preco_unitario": p[i.codigo],
               "subtotal": round(p[i.codigo] * i.quantidade, 2)} for i in itens]
    total = round(sum(x["subtotal"] for x in linhas), 2)
    validar_total(total, cfg.valor_max_pedido)
    oid = estado.salvar_orcamento(conversa, cliente, {"itens": linhas, "total": total, "criado": agora})
    return {"orcamento_id": oid, "validade_dias": VALIDADE_ORCAMENTO_DIAS, "total": total, "itens": linhas,
            "registrado_no_sankhya": False,
            "aviso": "Orçamento guardado só aqui. Registro na TOP do Sankhya ainda não habilitado."}


def gravar_no_sankhya(cfg, ops, estado, agora: float, orcamento_id: str) -> dict:
    """Grava UM orçamento já montado na TOP de orçamento do Sankhya. Uma vez só por orçamento.

    Usado pelo comando admin e pelo agente (quando o cliente quer fechar). Nunca repete sozinho:
    em qualquer falha a chave fica marcada e o vendedor confere/lança manualmente.
    """
    if not cfg.escrita_habilitada:
        raise ErroNegocio("MODO_SOMENTE_LEITURA", "Gravação no Sankhya está desligada.")
    if estado.desligado():
        raise ErroNegocio("DESLIGADO", "O agente está desligado.")
    o = estado.obter_orcamento(orcamento_id)
    if not o:
        raise ErroNegocio("ORCAMENTO_NAO_ENCONTRADO", "Orçamento não encontrado.")
    if agora - o["dados"]["criado"] > VALIDADE_ORCAMENTO_DIAS * 86400:
        raise ErroNegocio("ORCAMENTO_VENCIDO", "Orçamento vencido.")
    chave = f"gravado:{orcamento_id}"
    anterior = estado.ler_chave(chave)
    if anterior:
        if anterior.startswith("NUNOTA="):
            return {"nunota": int(anterior.split("=", 1)[1]), "ja_gravado": True}
        raise ErroNegocio("DUPLICADO", "Esse orçamento já teve uma tentativa de gravação. Conferir no Sankhya.")
    top = ops.tipmov_da_top(cfg.top_orcamento)
    if not top.get("TIPMOV"):
        raise ErroNegocio("CONFIG_INCOMPLETA", "Não consegui ler a TOP de orçamento.")
    estado.gravar_chave(chave, "em_andamento")  # trava antes de gravar: em dúvida, nunca repete
    try:
        r = ops.gravar_orcamento(orcamento_id, o["cliente"], o["dados"]["itens"], top["TIPMOV"])
    except Exception:
        estado.gravar_chave(chave, "falhou_conferir_no_sankhya")
        raise
    estado.gravar_chave(chave, f"NUNOTA={r['nunota']}")
    return {"nunota": r["nunota"], "ja_gravado": False}

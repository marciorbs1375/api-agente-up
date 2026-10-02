"""Cálculo do orçamento (único lugar). Usado pela rota /v1/orcamentos e pelo agente conversacional."""
from .regras import Item, validar_estoque, validar_itens, validar_total

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
